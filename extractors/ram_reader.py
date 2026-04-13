"""
extractors/ram_reader.py
========================
Extractor de hardware para el subsistema de memoria RAM de probe.tex.

Fuentes de datos (en orden de preferencia / fallback)
------------------------------------------------------
Identificación y topología
  1. ``sudo dmidecode -t memory``  → módulos DIMM, tipo, velocidad, slots.
  2. ``sudo dmidecode -t 17``      → alias, como respaldo.
  3. /proc/meminfo                 → capacidad total real (validación).

Errores EDAC (hardware ECC)
  4. /sys/devices/system/edac/mc/ → conteo de errores por controlador.
     Si el directorio no existe (hardware consumer, sin ECC), devuelve 0.

Prueba Activa de Integridad (Forense Activo)
  5. ``sudo memtester 1G 1``
     Ejecuta una pasada completa de pruebas de integridad sobre 1 GB de
     memoria.  Si todas las pruebas pasan ("ok"), no se añaden errores.
     Si alguna muestra "FAILURE", se cuentan y se suman a
     ``ram_total_errors``.  Los errores de memtester y de EDAC son
     independientes y se acumulan.

     Si memtester no está instalado (FileNotFoundError) o supera el
     timeout (_MEMTESTER_TIMEOUT_S), se registra un aviso y la prueba
     se omite con 0 errores adicionales.

Degradación elegante
---------------------
Cada sub-rutina encapsula su lógica en try/except.  El guard externo
de ``extract_ram_data()`` garantiza RAMData() vacío ante cualquier
fallo no anticipado.

stdlib únicamente: subprocess, re, pathlib, collections.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional

from core.models import RAMData
from tui import runtime_log


# ════════════════════════════════════════════════════════════════════════════
#  CONSTANTES
# ════════════════════════════════════════════════════════════════════════════

_EDAC_ROOT    = Path("/sys/devices/system/edac/mc")
_MEMINFO_PATH = Path("/proc/meminfo")

_EDAC_ERROR_WEIGHT: float = 0.01   # % de integridad por error (ajustado por GB)

# ── Memtester — tamaño dinámico (Live OS Safety) ─────────────────────────────
# La constante fija _MEMTESTER_SIZE = "1G" se elimina. El tamaño se calcula
# en tiempo de ejecución mediante _compute_memtester_size().
_MEMTESTER_MIN_MB:    int   = 64     # piso absoluto
_MEMTESTER_MAX_MB:    int   = 512    # techo para Live OS (evita OOM en 4 GB)
_MEMTESTER_FREE_PCT:  float = 0.10   # fracción de MemAvailable a usar
_MEMTESTER_LOOPS:     int   = 1
_MEMTESTER_TIMEOUT_S: int   = 600

_DMIDECODE_TIMEOUT: int = 6

_MLC_TIMEOUT_S:  Final[int]   = 60
_MLC_LAT_MIN_NS: Final[float] = 10.0
"""Mínimo físicamente plausible para DRAM en ns (LPDDR5X ~10–20 ns)."""
_MLC_LAT_MAX_NS: Final[float] = 500.0
"""Máximo plausible. >500 ns indica unidad incorrecta o artefacto de parseo."""

# Captura la fila "  0    75.3" del output de mlc --idle_latency:
# la primera columna es el nodo NUMA origen (0), la siguiente es la latencia
# al nodo 0 (local, sin cruce de interconexión). Se requiere al menos un
# dígito antes y después del punto decimal para evitar colisiones con
# versiones del binario que imprimen rangos como "73-78".
_MLC_LAT_RE: re.Pattern = re.compile(
    r"^\s+0\s+(\d+\.\d+)",
    re.MULTILINE,
)


# ════════════════════════════════════════════════════════════════════════════
#  UTILIDADES INTERNAS
# ════════════════════════════════════════════════════════════════════════════

def _run(cmd: list[str], timeout: int = _DMIDECODE_TIMEOUT) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"{cmd!r} rc={result.returncode} stderr={result.stderr.strip()!r}"
        )
    return result.stdout


def _read_sysfs(path: Path | str) -> str:
    return Path(path).read_text().strip()


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (ValueError, TypeError):
        return default


# ════════════════════════════════════════════════════════════════════════════
#  ESTRUCTURA INTERNA DE UN MÓDULO DIMM
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class _DimmModule:
    locator:   str = "?"
    size_gb:   int = 0
    speed_mhz: int = 0
    mem_type:  str = "Unknown"
    rank:      int = 1
    ce_errors: int = 0
    ue_errors: int = 0

    @property
    def total_errors(self) -> int:
        return self.ce_errors + self.ue_errors

    @property
    def occupied(self) -> bool:
        return self.size_gb > 0


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 1 — dmidecode: topología de memoria
# ════════════════════════════════════════════════════════════════════════════

def _run_dmidecode() -> str:
    for flag in (["-t", "memory"], ["-t", "17"]):
        try:
            return _run(["sudo", "dmidecode"] + flag)
        except Exception:
            continue
    raise RuntimeError("dmidecode no disponible o sin privilegios suficientes.")


def _parse_dmidecode(raw: str) -> list[_DimmModule]:
    """
    Parsea la salida de ``dmidecode -t memory`` en una lista de _DimmModule.
    Cada bloque "Memory Device" se convierte en un objeto.
    Los slots vacíos quedan con size_gb=0.
    """
    modules: list[_DimmModule] = []
    blocks = re.split(r"(?=^Memory Device$)", raw, flags=re.MULTILINE)

    for block in blocks:
        if "Memory Device" not in block:
            continue
        mod = _DimmModule()

        m = re.search(r"^\s+Locator:\s+(.+)$", block, re.MULTILINE)
        if m:
            mod.locator = m.group(1).strip()

        m = re.search(r"^\s+Size:\s+(.+)$", block, re.MULTILINE)
        if m:
            size_str = m.group(1).strip()
            if re.search(r"No Module|Unknown|Not Installed", size_str, re.IGNORECASE):
                mod.size_gb = 0
            else:
                nm = re.search(r"(\d+)\s*(GB|MB|MiB|GiB)", size_str, re.IGNORECASE)
                if nm:
                    val  = int(nm.group(1))
                    unit = nm.group(2).upper()
                    mod.size_gb = val if unit in ("GB", "GIB") else val // 1024

        m = re.search(r"^\s+Type:\s+(.+)$", block, re.MULTILINE)
        if m:
            raw_type = m.group(1).strip()
            if raw_type.lower() not in ("unknown", "other", ""):
                mod.mem_type = raw_type

        for speed_key in ("Configured Memory Speed", "Speed"):
            m = re.search(
                rf"^\s+{re.escape(speed_key)}:\s+(\d+)\s*(MT/s|MHz)",
                block, re.MULTILINE,
            )
            if m:
                mod.speed_mhz = int(m.group(1))
                break

        m = re.search(r"^\s+Rank:\s+(\d+)", block, re.MULTILINE)
        if m:
            mod.rank = _safe_int(m.group(1), default=1)

        modules.append(mod)

    return modules


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 2 — /proc/meminfo: validación de capacidad total
# ════════════════════════════════════════════════════════════════════════════

def _total_gb_from_meminfo() -> int:
    raw = _read_sysfs(_MEMINFO_PATH)
    m   = re.search(r"MemTotal:\s+(\d+)\s+kB", raw)
    if m:
        return max(1, round(int(m.group(1)) / 1_048_576))
    return 0


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 3 — EDAC sysfs: conteo de errores por controlador
# ════════════════════════════════════════════════════════════════════════════

def _read_edac_errors() -> dict[str, tuple[int, int]]:
    """
    Lee CE/UE por csrow desde sysfs EDAC.
    Devuelve vacío silenciosamente si EDAC no está disponible (no-ECC).
    """
    result: dict[str, tuple[int, int]] = {}
    if not _EDAC_ROOT.exists():
        return result
    try:
        for mc_dir in sorted(_EDAC_ROOT.iterdir()):
            if not mc_dir.is_dir() or not mc_dir.name.startswith("mc"):
                continue
            for csrow_dir in sorted(mc_dir.glob("csrow*")):
                try:
                    ce = _safe_int(_read_sysfs(csrow_dir / "ce_count"))
                    ue = _safe_int(_read_sysfs(csrow_dir / "ue_count"))
                    result[str(csrow_dir)] = (ce, ue)
                except Exception:
                    result[str(csrow_dir)] = (0, 0)
    except Exception:
        pass
    return result


def _assign_edac_to_dimms(
    modules:  list[_DimmModule],
    edac:     dict[str, tuple[int, int]],
) -> None:
    csrow_entries = sorted(edac.items())
    occupied      = [m for m in modules if m.occupied]
    for idx, (_, (ce, ue)) in enumerate(csrow_entries):
        if idx < len(occupied):
            occupied[idx].ce_errors += ce
            occupied[idx].ue_errors += ue


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 4 — Inferencia de Dual Channel
# ════════════════════════════════════════════════════════════════════════════

def _infer_dual_channel(modules: list[_DimmModule]) -> bool:
    """
    Infiere dual-channel desde los locator names de DMI.

    Patrones comunes:
      - Intel: DIMM_A1, DIMM_B1 → canales A y B poblados → dual
      - AMD:   DIMM_P0, DIMM_P1 → ídem
      - Fallback numérico: pares de slots con IDs distintos

    Si los locators son genéricos ("DIMM 0", "DIMM 1") sin letra de canal,
    usa el conteo como heurística pero lo marca como incierto.
    """
    occupied = [m for m in modules if m.occupied]
    if len(occupied) < 2:
        return False

    channel_pattern = re.compile(r'(?:DIMM[_\s]?|Channel\s+)([A-Z])', re.IGNORECASE)
    channels: set[str] = set()
    for mod in occupied:
        m = channel_pattern.search(mod.locator)
        if m:
            channels.add(m.group(1).upper())

    if channels:
        # Solo dual-channel si hay al menos 2 canales distintos poblados
        return len(channels) >= 2

    # Fallback: sin letras de canal en los locators → heurística por conteo
    # Es una estimación, no un hecho verificado.
    return len(occupied) in (2, 4)


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 5 — Prueba Activa: memtester
# ════════════════════════════════════════════════════════════════════════════

def _compute_memtester_size() -> str:
    """
    Calcula el tamaño seguro para memtester en función de la RAM disponible
    en tiempo de ejecución.

    Live OS Safety
    --------------
    Un Live OS arrancado desde squashfs+tmpfs puede dejar ≤ 600 MB libres
    en un equipo de 4 GB.  La constante fija "1G" dispararía el OOM Killer
    del kernel, matando probe.tex antes de poder emitir el reporte.

    Estrategia
    ----------
    1. Lee ``MemAvailable`` de /proc/meminfo (incluye page cache recuperable,
       más preciso que MemFree en sistemas con tmpfs activo).
    2. Aplica _MEMTESTER_FREE_PCT (10 %).
    3. Clampea entre _MEMTESTER_MIN_MB (64) y _MEMTESTER_MAX_MB (512).
    4. Ante cualquier fallo → fallback conservador de 64 MB.

    Returns
    -------
    str
        Cadena lista para pasar a memtester, ej. ``"128M"``.
    """
    try:
        content = _MEMINFO_PATH.read_text()
        # MemAvailable preferido; MemFree como segundo recurso
        m = re.search(r"^MemAvailable:\s+(\d+)\s+kB", content, re.MULTILINE)
        if not m:
            m = re.search(r"^MemFree:\s+(\d+)\s+kB",      content, re.MULTILINE)
        if m:
            free_kb   = int(m.group(1))
            target_mb = int(free_kb / 1024 * _MEMTESTER_FREE_PCT)
            clamped   = max(_MEMTESTER_MIN_MB, min(_MEMTESTER_MAX_MB, target_mb))
            print(
                f"[ram_reader] INFO memtester: {clamped} MB asignados "
                f"(10 % de {free_kb // 1024} MB disponibles)"
            )
            return f"{clamped}M"
    except Exception as exc:
        print(f"[ram_reader] WARN _compute_memtester_size: {exc} → fallback 64M")
    return f"{_MEMTESTER_MIN_MB}M"


def _run_memtester() -> int:
    """
    Ejecuta ``sudo memtester <TAMAÑO_DINÁMICO> 1`` y devuelve el número
    de líneas "FAILURE" encontradas.

    TAMAÑO_DINÁMICO = 10 % de MemAvailable, clampado en [64 MB, 512 MB].
    Garantiza que el proceso nunca consuma más RAM de la disponible y
    no active el OOM Killer en entornos Live OS con memoria limitada.
    """
    size = _compute_memtester_size()
    try:
        runtime_log(f"memtester: Auditando {size}B de RAM (Live OS safe mode)...")
        result = subprocess.run(
            ["sudo", "memtester", size, str(_MEMTESTER_LOOPS)],
            capture_output=True,
            text=True,
            timeout=_MEMTESTER_TIMEOUT_S,
        )
        output   = result.stdout + result.stderr
        failures = len(re.findall(r"\bFAILURE\b", output, re.IGNORECASE))
        if failures:
            print(f"[ram_reader] memtester detectó {failures} fallo(s) de memoria.")
        return failures

    except FileNotFoundError:
        print("[ram_reader] WARN memtester no encontrado. Prueba activa omitida.")
        return 0
    except subprocess.TimeoutExpired:
        print(
            f"[ram_reader] WARN memtester superó {_MEMTESTER_TIMEOUT_S} s. "
            "Prueba considerada incompleta."
        )
        return 0
    except Exception as exc:
        print(f"[ram_reader] WARN memtester: {exc}")
        return 0


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 5b — Intel MLC: latencia de acceso idle a DRAM
# ════════════════════════════════════════════════════════════════════════════

def _measure_mlc_latency() -> Optional[float]:
    """
    Mide la latencia de acceso idle a DRAM usando Intel Memory Latency Checker.

    Procedimiento
    ─────────────
    1. Carga el módulo MSR del kernel: MLC lo necesita para acceder a los
       contadores de rendimiento (PMU). El fallo de modprobe es silencioso:
       el módulo puede estar compilado estáticamente o ser irrelevante
       para la arquitectura objetivo.
    2. Ejecuta ``sudo mlc --idle_latency`` y parsea la latencia local del
       nodo NUMA 0 → 0 (DRAM local, sin cruce de interconexión QPI/IF).
    3. Valida que el resultado esté en el rango físico plausible de DRAM.

    Latencias de referencia orientativas
    ──────────────────────────────────────
    LPDDR5X-8533:  ~14–18 ns   (portátiles ultrafinos, Intel 13+/AMD 7000)
    DDR5-6000:     ~42–52 ns   (desktop high-end)
    DDR4-3200:     ~62–80 ns   (desktop mainstream)
    DDR4-2133:     ~75–95 ns   (portátil convencional)

    Compatibilidad
    ──────────────
    - Intel MLC ≥ v3.x (binario propietario de uso libre, sin código fuente).
      Descarga: https://www.intel.com/content/www/us/en/download/736633/
    - Funciona en CPU Intel x86-64. Soporte AMD variable (sin garantía Intel).
    - En sistemas NUMA con >1 nodo: se extrae solo la latencia local (0→0).
    - Requiere: Linux ≥ 4.0, ejecución como root o CAP_SYS_RAWIO.

    Returns
    -------
    float
        Latencia idle en nanosegundos, redondeada a 1 decimal.
        Nunca retorna 0.0 (latencia cero es físicamente imposible para DRAM).
    None
        mlc no instalado, error de ejecución, timeout o resultado no plausible.
    """
    # Paso 1: cargar módulo MSR (fallo absolutamente silencioso)
    try:
        subprocess.run(
            ["sudo", "modprobe", "msr"],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass   # No bloqueante bajo ninguna circunstancia

    # Paso 2: ejecutar mlc --idle_latency
    try:
        result = subprocess.run(
            ["sudo", "mlc", "--idle_latency"],
            capture_output=True, text=True,
            timeout=_MLC_TIMEOUT_S,
        )
    except FileNotFoundError:
        print(
            "[ram_reader] INFO mlc no encontrado → ram_cas_ns = None.\n"
            "             Instalar desde: https://www.intel.com/content/www/us/en/"
            "download/736633/intel-memory-latency-checker-intel-mlc.html"
        )
        return None
    except subprocess.TimeoutExpired:
        print(f"[ram_reader] WARN mlc superó {_MLC_TIMEOUT_S} s → ram_cas_ns = None.")
        return None
    except Exception as exc:
        print(f"[ram_reader] WARN mlc invocación: {exc}")
        return None

    # Paso 3: parsear latencia nodo local (nodo 0 → nodo 0)
    output = result.stdout + result.stderr

    # rc=1 ocurre en sistemas sin PMU hardware pero con salida parseable.
    # Solo abortamos si no hay salida alguna.
    if not output.strip():
        print(f"[ram_reader] WARN mlc rc={result.returncode}, sin salida → None.")
        return None

    match = _MLC_LAT_RE.search(output)
    if not match:
        # Imprimir el primer segmento de la salida para diagnóstico,
        # sin saturar el log con dumps completos.
        snippet = output[:200].replace("\n", "  ").strip()
        print(f"[ram_reader] WARN mlc salida no reconocida: {snippet!r}")
        return None

    # Paso 4: convertir y validar rango físico
    try:
        ns = round(float(match.group(1)), 1)
    except ValueError:
        print("[ram_reader] WARN mlc: conversión a float falló.")
        return None

    if not (_MLC_LAT_MIN_NS <= ns <= _MLC_LAT_MAX_NS):
        print(
            f"[ram_reader] WARN mlc retornó {ns} ns — fuera del rango plausible "
            f"[{_MLC_LAT_MIN_NS:.0f}, {_MLC_LAT_MAX_NS:.0f}] ns. Valor descartado."
        )
        return None

    print(f"[ram_reader] INFO latencia CAS real (mlc): {ns} ns")
    return ns


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 6 — Generación del string LaTeX ram_edac_filas
# ════════════════════════════════════════════════════════════════════════════

def _build_edac_latex_rows(modules: list[_DimmModule]) -> str:
    rows: list[str] = []
    for idx, mod in enumerate(modules):
        alt = r"    \rowalt" + "\n" if idx % 2 == 1 else ""
        if not mod.occupied:
            size_str, errors, badge = "---", "---", r"\badgeinfo"
        else:
            size_str = f"{mod.size_gb} GB"
            errors   = str(mod.total_errors)
            if mod.ue_errors > 0:
                badge = r"\badgefail"
            elif mod.ce_errors > 0:
                badge = r"\badgewarn"
            else:
                badge = r"\badgeok"
        locator_tex = mod.locator.replace("_", r"\_").replace("#", r"\#")
        rows.append(f"{alt}    {locator_tex} & {size_str} & {errors} & {badge} \\\\")
    return "\n".join(rows)


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 7 — Métricas del donut de integridad
# ════════════════════════════════════════════════════════════════════════════

def _compute_integrity(
    total_errors: int,
    total_gb:     int,
) -> tuple[float, float, float]:
    if total_errors == 0 or total_gb == 0:
        return 360.0, 100.0, 0.0
    weight_per_error = _EDAC_ERROR_WEIGHT * (4.0 / max(total_gb, 1))
    fail_pct  = min(100.0, round(total_errors * weight_per_error, 2))
    integ_pct = round(100.0 - fail_pct, 2)
    angle     = round((integ_pct / 100.0) * 360.0, 2)
    return angle, integ_pct, fail_pct


# ════════════════════════════════════════════════════════════════════════════
#  API PÚBLICA
# ════════════════════════════════════════════════════════════════════════════

def extract_ram_data() -> RAMData:
    """
    Extrae y ensambla todos los datos de RAM en una instancia ``RAMData``.

    Arquitectura de extracción en 7 capas
    --------------------------------------
    Capa 1  dmidecode     — topología de módulos DIMM.
    Capa 2  /proc/meminfo — validación de capacidad total.
    Capa 3  EDAC sysfs    — errores CE/UE por hardware ECC.
    Capa 4  Dual channel  — inferencia por topología de slots.
    Capa 5  memtester     — prueba activa de integridad (1 GB, 1 pasada).
                            Los fallos se acumulan a ram_total_errors.
                            ram_speed_effective viene de dmidecode (real).
                            ram_cas_ns = 0.0 (sin benchmark dedicado).
    Capa 6  LaTeX rows    — tabla EDAC para el reporte.
    Capa 7  Donut         — ángulo e integridad para el gráfico circular.

    Returns
    -------
    RAMData
        Instancia completamente poblada.  Nunca lanza excepciones.
    """
    try:
        # ── Capa 1: dmidecode ────────────────────────────────────────────
        modules: list[_DimmModule] = []
        try:
            raw     = _run_dmidecode()
            modules = _parse_dmidecode(raw)
        except Exception as exc:
            print(f"[ram_reader] WARN dmidecode: {exc}")

        # ── Capa 2: /proc/meminfo ─────────────────────────────────────────
        meminfo_total_gb = 0
        try:
            meminfo_total_gb = _total_gb_from_meminfo()
        except Exception:
            pass

        # ── Capa 3: EDAC sysfs ───────────────────────────────────────────
        try:
            edac_errors = _read_edac_errors()
            _assign_edac_to_dimms(modules, edac_errors)
        except Exception as exc:
            print(f"[ram_reader] WARN EDAC: {exc}")

        # ── Métricas agregadas de topología ──────────────────────────────
        occupied    = [m for m in modules if m.occupied]
        slots_total = len(modules)
        slots_used  = len(occupied)

        total_gb = sum(m.size_gb for m in occupied) or meminfo_total_gb

        mem_type  = "N/A"
        speed_mhz = 0
        if occupied:
            mem_type  = Counter(m.mem_type  for m in occupied).most_common(1)[0][0]
            speed_mhz = Counter(m.speed_mhz for m in occupied).most_common(1)[0][0]

        # ── Capa 4: Dual Channel ─────────────────────────────────────────
        dual_channel = False
        try:
            dual_channel = _infer_dual_channel(modules)
        except Exception:
            pass

        # ── Capa 5: memtester + MLC ──────────────────────────────────────
        edac_total       = sum(m.total_errors for m in occupied)
        memtester_errors = 0
        try:
            memtester_errors = _run_memtester()
        except Exception as exc:
            print(f"[ram_reader] WARN memtester (inesperado): {exc}")

        total_errors    = edac_total + memtester_errors
        speed_effective = speed_mhz   # dato real de dmidecode (XMP/EXPO ya negociado)

        # Latencia CAS: medición real vía mlc, o None si no disponible.
        # NUNCA se usa 0.0: cero nanosegundos es una medición físicamente imposible.
        cas_ns: Optional[float] = None
        try:
            cas_ns = _measure_mlc_latency()
        except Exception as exc:
            print(f"[ram_reader] WARN _measure_mlc_latency: {exc}")
            cas_ns = None

        # ── Capa 6: LaTeX EDAC rows ──────────────────────────────────────
        edac_latex = ""
        try:
            edac_latex = _build_edac_latex_rows(modules)
        except Exception as exc:
            print(f"[ram_reader] WARN LaTeX rows: {exc}")

        # ── Capa 7: Donut de integridad ───────────────────────────────────
        pie_angle = 360.0
        integ_pct = 100.0
        fail_pct  = 0.0
        try:
            pie_angle, integ_pct, fail_pct = _compute_integrity(total_errors, total_gb)
        except Exception:
            pass

        test_method = (
            "edac-utils + memtester" if _EDAC_ROOT.exists()
            else "memtester (sin ECC HW)"
        )

        return RAMData(
            ram_total_gb        = total_gb,
            ram_type            = mem_type,
            ram_speed           = speed_mhz,
            ram_slots_used      = slots_used,
            ram_slots_total     = slots_total,
            ram_dual_channel    = dual_channel,
            ram_edac_filas      = edac_latex,
            ram_total_errors    = total_errors,
            ram_speed_effective = speed_effective,
            ram_cas_ns          = cas_ns,
            ram_test_method     = test_method,
            ram_pie_angle       = pie_angle,
            ram_integrity_pct   = integ_pct,
            ram_fail_pct        = fail_pct,
        )

    except Exception as exc:    # pragma: no cover — guardia absoluta
        print(f"[ram_reader] ERROR CRÍTICO en extract_ram_data(): {exc}")
        return RAMData()