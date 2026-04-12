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
from typing import Optional

from core.models import RAMData


# ════════════════════════════════════════════════════════════════════════════
#  CONSTANTES
# ════════════════════════════════════════════════════════════════════════════

_EDAC_ROOT    = Path("/sys/devices/system/edac/mc")
_MEMINFO_PATH = Path("/proc/meminfo")

_EDAC_ERROR_WEIGHT: float = 0.01   # % de integridad por error (ajustado por GB)

_MEMTESTER_SIZE:      str = "1G"   # tamaño de la prueba activa
_MEMTESTER_LOOPS:     int = 1      # número de iteraciones
_MEMTESTER_TIMEOUT_S: int = 600    # 10 minutos; memtester en 1 GB puede tardar

_DMIDECODE_TIMEOUT: int = 6


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
    occupied = [m for m in modules if m.occupied]
    count    = len(occupied)
    if count in (2, 4):
        return True
    if count <= 1:
        return False
    return count > 1


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 5 — Prueba Activa: memtester
# ════════════════════════════════════════════════════════════════════════════

def _run_memtester() -> int:
    """
    Ejecuta ``sudo memtester <_MEMTESTER_SIZE> <_MEMTESTER_LOOPS>`` y
    devuelve el número de líneas "FAILURE" encontradas en la salida.

    memtester escribe su progreso en stdout.  Cada sub-prueba termina
    con "ok" si pasa o con "FAILURE: 0x... != 0x..." si falla.

    Degradación elegante
    --------------------
    * FileNotFoundError  → memtester no instalado: retorna 0 y loguea aviso.
    * TimeoutExpired     → retorna 0 y loguea aviso (prueba incompleta).
    * Cualquier otro exc → retorna 0 y loguea aviso.

    Returns
    -------
    int
        Número de fallos detectados (0 = sin errores o prueba no disponible).
    """
    try:
        result = subprocess.run(
            ["sudo", "memtester", _MEMTESTER_SIZE, str(_MEMTESTER_LOOPS)],
            capture_output=True,
            text=True,
            timeout=_MEMTESTER_TIMEOUT_S,
        )
        # memtester puede devolver rc != 0 si detecta errores de memoria;
        # el contador de fallos lo derivamos del texto, no del rc.
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
            f"[ram_reader] WARN memtester superó {_MEMTESTER_TIMEOUT_S} s de timeout. "
            "Prueba considerada incompleta."
        )
        return 0
    except Exception as exc:
        print(f"[ram_reader] WARN memtester: {exc}")
        return 0


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

        # ── Capa 5: Prueba activa — memtester ────────────────────────────
        # Errores EDAC ya asignados a módulos; memtester puede sumar más.
        edac_total = sum(m.total_errors for m in occupied)
        memtester_errors = 0
        try:
            memtester_errors = _run_memtester()
        except Exception as exc:
            print(f"[ram_reader] WARN memtester (inesperado): {exc}")

        total_errors = edac_total + memtester_errors

        # Velocidad efectiva: dato real de dmidecode (ya negociado por XMP/EXPO).
        # CAS en ns: no disponible sin benchmark dedicado de latencia de acceso.
        speed_effective = speed_mhz
        cas_ns          = 0.0

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