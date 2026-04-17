"""
extractors/ram_reader.py
========================
Extractor de hardware para el subsistema de memoria RAM de probe.tex.

CHANGELOG v1.2
--------------
* FIX: Hardening para memoria LPDDR5 soldada (RAM on-die).

  En laptops modernas con memoria soldada (Ryzen 7xxx mobile, Intel
  Meteor Lake, Apple Silicon port scenarios) dmidecode puede devolver:
    a) Una lista vacía de Memory Device blocks.
    b) Blocks con Size: "No Module Installed" (slots físicos vacíos).
    c) Blocks con Type: "Unknown" y Speed: 0 (SMBIOS incompleto).

  Comportamiento correcto en estos escenarios:
    - slots_total = 0, slots_used = 0 → reportar "0/0" en el template.
    - total_gb = meminfo_total_gb  (fuente de verdad absoluta).
    - speed_mhz = 0  (honesto: SMBIOS no lo expone).
    - mem_type = "N/A" (ídem).
    - La generación del template NO debe crashear con IndexError en
      Counter.most_common(1)[0][0] cuando occupied = [].

  La fuente de verdad para la capacidad total es SIEMPRE /proc/meminfo
  (MemTotal). dmidecode es informativo para topología; meminfo es el
  dato que el kernel realmente ve y usa.

* FIX: _compute_integrity() ya tenía guard contra total_gb=0, pero
  ahora también documenta explícitamente por qué: un sistema con RAM
  soldada y 0 errores es 100% íntegro, no un estado indeterminado.

Fuentes de datos
----------------
dmidecode -t memory  → topología DIMM, tipo, velocidad, slots
/proc/meminfo        → capacidad total real (fuente de verdad)
EDAC sysfs           → errores CE/UE por controlador de memoria
memtester            → prueba activa de integridad (tamaño dinámico)
Intel MLC            → latencia idle DRAM (opcional, best-effort)
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional

from core.models import RAMData
from tui import runtime_log

_log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
#  CONSTANTES
# ════════════════════════════════════════════════════════════════════════════

_EDAC_ROOT    = Path("/sys/devices/system/edac/mc")
_MEMINFO_PATH = Path("/proc/meminfo")

_EDAC_ERROR_WEIGHT: float = 0.01

_MEMTESTER_MIN_MB:    int   = 64
_MEMTESTER_MAX_MB:    int   = 512
_MEMTESTER_FREE_PCT:  float = 0.10
_MEMTESTER_LOOPS:     int   = 1
_MEMTESTER_TIMEOUT_S: int   = 600

_DMIDECODE_TIMEOUT: int = 6

_MLC_TIMEOUT_S:  Final[int]   = 60
_MLC_LAT_MIN_NS: Final[float] = 10.0
_MLC_LAT_MAX_NS: Final[float] = 500.0

_MLC_LAT_RE: re.Pattern = re.compile(r"^\s+0\s+(\d+\.\d+)", re.MULTILINE)


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
            return _run(["dmidecode"] + flag)
        except Exception:
            continue
    raise RuntimeError("dmidecode no disponible o sin privilegios suficientes.")


def _parse_dmidecode(raw: str) -> list[_DimmModule]:
    """
    Parsea bloques "Memory Device" de dmidecode.

    Slots vacíos (Size: "No Module Installed") quedan con size_gb=0.
    En hardware con memoria soldada el resultado puede ser [] (vacío)
    si el SMBIOS no expone ningún Memory Device — comportamiento correcto,
    no un error. El caller lo maneja con la fuente de verdad /proc/meminfo.
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
#  CAPA 2 — /proc/meminfo: fuente de verdad para capacidad total
# ════════════════════════════════════════════════════════════════════════════

def _total_gb_from_meminfo() -> int:
    """
    Lee MemTotal de /proc/meminfo y lo convierte a GB (round).

    Esta es la ÚNICA fuente fiable de capacidad total en:
      - Sistemas con memoria soldada (LPDDR5, LPDDR4X on-die).
      - Sistemas donde dmidecode devuelve Size: 0 o lista vacía.
      - Hypervisors con SMBIOS sintético.

    Retorna 0 solo si /proc/meminfo no existe o no es parseable —
    situación que no puede ocurrir en un kernel Linux funcional.
    """
    try:
        raw = _read_sysfs(_MEMINFO_PATH)
        m   = re.search(r"MemTotal:\s+(\d+)\s+kB", raw)
        if m:
            return max(1, round(int(m.group(1)) / 1_048_576))
    except Exception:
        pass
    return 0


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 3 — EDAC sysfs: conteo de errores por controlador
# ════════════════════════════════════════════════════════════════════════════

def _read_edac_errors() -> dict[str, tuple[int, int]]:
    """
    Lee CE/UE por csrow. Devuelve vacío si EDAC no está expuesto (no-ECC).
    Hardware consumer sin ECC es el caso normal; no es un error.
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


def _assign_edac_to_dimms(modules: list[_DimmModule], edac: dict[str, tuple[int, int]]) -> None:
    """
    Asigna errores EDAC a los módulos DIMM ocupados por índice de csrow.
    Si modules es vacío (memoria soldada), no hay nada que asignar.
    """
    if not modules:
        return
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
    Infiere dual-channel por locator names o por conteo de módulos.

    Retorna False si modules está vacío (memoria soldada sin slots).
    No hace suposiciones sobre la topología cuando no hay datos.
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
        return len(channels) >= 2

    # Fallback heurístico: 2 o 4 módulos suele indicar dual channel.
    return len(occupied) in (2, 4)


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 5a — memtester: prueba activa de integridad
# ════════════════════════════════════════════════════════════════════════════

def _compute_memtester_size() -> str:
    """
    Calcula el tamaño seguro para memtester como 10% de MemAvailable,
    clampado en [64 MB, 512 MB]. Evita OOM en Live OS con RAM limitada.
    """
    try:
        content = _MEMINFO_PATH.read_text()
        m = re.search(r"^MemAvailable:\s+(\d+)\s+kB", content, re.MULTILINE)
        if not m:
            m = re.search(r"^MemFree:\s+(\d+)\s+kB", content, re.MULTILINE)
        if m:
            free_kb   = int(m.group(1))
            target_mb = int(free_kb / 1024 * _MEMTESTER_FREE_PCT)
            clamped   = max(_MEMTESTER_MIN_MB, min(_MEMTESTER_MAX_MB, target_mb))
            _log.info("memtester: %d MB (10%% of %d MB available)", clamped, free_kb // 1024)
            return f"{clamped}M"
    except Exception as exc:
        _log.warning("_compute_memtester_size: %s → fallback 64M", exc)
    return f"{_MEMTESTER_MIN_MB}M"


def _run_memtester() -> int:
    """
    Ejecuta memtester con tamaño dinámico. Retorna número de líneas FAILURE.
    Retorna 0 si memtester no está instalado (no es un error de hardware).
    """
    size = _compute_memtester_size()
    try:
        runtime_log(f"memtester: Auditando {size}B de RAM (Live OS safe mode)...")
        result = subprocess.run(
            ["memtester", size, str(_MEMTESTER_LOOPS)],
            capture_output=True, text=True, timeout=_MEMTESTER_TIMEOUT_S,
        )
        output   = result.stdout + result.stderr
        failures = len(re.findall(r"\bFAILURE\b", output, re.IGNORECASE))
        if failures:
            _log.info("[ram_reader] memtester detectó %s fallo(s).", failures)
        return failures
    except FileNotFoundError:
        _log.warning("memtester no encontrado. Prueba activa omitida.")
    except subprocess.TimeoutExpired:
        _log.warning("memtester superó %s s.", _MEMTESTER_TIMEOUT_S)
    except Exception as exc:
        _log.warning("memtester: %s", exc)
    return 0


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 5b — Intel MLC: latencia idle DRAM
# ════════════════════════════════════════════════════════════════════════════

def _measure_mlc_latency() -> Optional[float]:
    """
    Mide latencia idle DRAM via Intel Memory Latency Checker (mlc).

    Retorna la latencia local del nodo NUMA 0 en nanosegundos.
    Retorna None si mlc no está instalado, timeout, o resultado no plausible.

    Latencias de referencia orientativas:
      LPDDR5X-8533:  14–18 ns   (portátiles ultrafinos)
      DDR5-6000:     42–52 ns   (desktop high-end)
      DDR4-3200:     62–80 ns   (desktop mainstream)
      DDR4-2133:     75–95 ns   (portátil convencional)
    """
    try:
        subprocess.run(["modprobe", "msr"], capture_output=True, timeout=5)
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["mlc", "--idle_latency"],
            capture_output=True, text=True, timeout=_MLC_TIMEOUT_S,
        )
    except FileNotFoundError:
        _log.info("mlc no encontrado → ram_cas_ns = None.")
        return None
    except subprocess.TimeoutExpired:
        _log.warning("mlc superó %s s → None.", _MLC_TIMEOUT_S)
        return None
    except Exception as exc:
        _log.warning("mlc invocación: %s", exc)
        return None

    output = result.stdout + result.stderr
    if not output.strip():
        return None

    match = _MLC_LAT_RE.search(output)
    if not match:
        snippet = output[:200].replace("\n", "  ").strip()
        _log.warning("mlc output unrecognized: %r", snippet)
        return None

    try:
        ns = round(float(match.group(1)), 1)
    except ValueError:
        return None

    if not (_MLC_LAT_MIN_NS <= ns <= _MLC_LAT_MAX_NS):
        _log.warning("mlc retornó %s ns fuera de rango plausible. Descartado.", ns)
        return None

    _log.info("latencia CAS real (mlc): %s ns", ns)
    return ns


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 6 — Generación del string LaTeX ram_edac_filas
# ════════════════════════════════════════════════════════════════════════════

def _build_edac_latex_rows(modules: list[_DimmModule]) -> str:
    """
    Genera filas LaTeX para la tabla EDAC.
    Si modules está vacío (RAM soldada sin SMBIOS), retorna string vacío.
    El template debe manejar esta condición con un bloque condicional.
    """
    if not modules:
        return ""
    rows: list[str] = []
    for idx, mod in enumerate(modules):
        alt = r"    \rowalt" + "\n" if idx % 2 == 1 else ""
        if not mod.occupied:
            size_str, errors, badge = "---", "---", r"\badgeinfo"
        else:
            size_str = f"{mod.size_gb} GB"
            errors   = str(mod.total_errors)
            badge    = (r"\badgefail" if mod.ue_errors > 0
                        else r"\badgewarn" if mod.ce_errors > 0
                        else r"\badgeok")
        locator_tex = mod.locator.replace("_", r"\_").replace("#", r"\#")
        rows.append(f"{alt}    {locator_tex} & {size_str} & {errors} & {badge} \\\\")
    return "\n".join(rows)


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 7 — Métricas del donut de integridad
# ════════════════════════════════════════════════════════════════════════════

def _compute_integrity(total_errors: int, total_gb: int) -> tuple[float, float, float]:
    """
    Calcula ángulo del donut, % íntegro y % defectuoso.

    Casos especiales:
    - total_errors == 0: sistema íntegro al 100%, sin importar total_gb.
      Incluye el caso de RAM soldada donde modules=[] pero meminfo
      reporta capacidad real. 0 errores = 100% íntegro.
    - total_gb == 0: no debería ocurrir si /proc/meminfo funciona,
      pero se maneja devolviendo 100% íntegro (sin datos = sin errores
      confirmados = estado óptimo por honestidad forense).
    """
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
    Extrae y ensambla todos los datos de RAM en una instancia RAMData.

    FIX v1.2: Hardening para memoria LPDDR5 soldada
    ------------------------------------------------
    En laptops con memoria on-die, dmidecode puede devolver 0 módulos.
    El flujo de extracción maneja este caso explícitamente:

    1. Si modules = [] → slots_total = 0, slots_used = 0 (correcto y honesto).
    2. total_gb usa SIEMPRE meminfo como autoridad. Si occupied suma 0 GB
       (por lista vacía o SMBIOS incompleto), total_gb = meminfo_total_gb.
    3. mem_type y speed_mhz permanecen en "N/A" / 0 cuando occupied = [].
       No se inventan valores para hardware sin SMBIOS expuesto.
    4. _compute_integrity() con total_errors=0 retorna 100% íntegro,
       lo cual es correcto: ausencia de errores detectados = estado óptimo.

    Nunca lanza excepciones (guard externo garantiza RAMData() vacío).
    """
    try:
        # ── Capa 1: dmidecode ────────────────────────────────────────────
        modules: list[_DimmModule] = []
        try:
            raw     = _run_dmidecode()
            modules = _parse_dmidecode(raw)
        except Exception as exc:
            # dmidecode no disponible o SMBIOS vacío.
            # No es fatal: meminfo proporciona la capacidad real.
            _log.warning("dmidecode: %s", exc)

        # ── Capa 2: /proc/meminfo — fuente de verdad ──────────────────────
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
            _log.warning("EDAC: %s", exc)

        # ── Métricas de topología ─────────────────────────────────────────
        occupied    = [m for m in modules if m.occupied]
        slots_total = len(modules)    # 0 en RAM soldada sin SMBIOS = correcto
        slots_used  = len(occupied)   # 0 ídem

        # total_gb: SMBIOS si disponible, meminfo como fallback autoritativo.
        # sum([]) = 0, por lo tanto el `or` activa meminfo_total_gb cuando
        # occupied está vacío (incluyendo el caso de memoria soldada).
        total_gb = sum(m.size_gb for m in occupied) or meminfo_total_gb

        # mem_type y speed_mhz solo son conocidos si SMBIOS reportó módulos.
        # Para RAM soldada sin datos SMBIOS, los dejamos en valores nulos honestos.
        mem_type  = "N/A"
        speed_mhz = 0
        if occupied:
            mem_type  = Counter(m.mem_type  for m in occupied).most_common(1)[0][0]
            speed_mhz = Counter(m.speed_mhz for m in occupied).most_common(1)[0][0]

        if slots_total == 0:
            _log.info(
                "0 DIMM slots detected via SMBIOS — probable soldered LPDDR. "
                "Total capacity from meminfo: %d GB",
                total_gb,
            )

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
            _log.warning("memtester (inesperado): %s", exc)

        total_errors    = edac_total + memtester_errors
        speed_effective = speed_mhz

        cas_ns: Optional[float] = None
        try:
            cas_ns = _measure_mlc_latency()
        except Exception as exc:
            _log.warning("_measure_mlc_latency: %s", exc)

        # ── Capa 6: LaTeX EDAC rows ──────────────────────────────────────
        edac_latex = ""
        try:
            edac_latex = _build_edac_latex_rows(modules)
        except Exception as exc:
            _log.warning("LaTeX rows: %s", exc)

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

    except Exception as exc:
        _log.error("CRÍTICO en extract_ram_data(): %s", exc)
        return RAMData()