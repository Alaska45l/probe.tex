"""
extractors/disk_reader.py
=========================
Extractor de hardware para el subsistema de almacenamiento de probe.tex.

CHANGELOG v1.1
--------------
* Importa StorageData en lugar de NVMeData.
* Detección de rotación promovida a Capa 1 (antes era Capa 6.5 tardía).
* Añadida Capa HDD-A: extracción de atributos mecánicos SMART
    (ID 3 Spin-Up Time, ID 5 Reallocated Sector Count, ID 188 Command Timeout).
* Añadida Capa HDD-B: medición de seek latency con fio randread 4K QD1.
  Usa clat_ns.mean del JSON de fio y lo convierte a ms.
* extract_disk_data() bifurca en HDD-path y SSD-path antes de lanzar
  cualquier test de estado sólido.
* Devuelve StorageData (NVMeData es alias en models.py).

Fuentes de datos SSD/NVMe (sin cambios respecto a v1.0)
--------------------------------------------------------
smartctl -a -j <device>
fio randread 4k QD1 runtime=15s (solo SSD)

Fuentes de datos HDD mecánico (nuevo v1.1)
------------------------------------------
smartctl -A -j <device>         → atributos mecánicos SMART
fio randread 4k QD1 runtime=10s → seek latency media (clat_ns.mean)

Degradación elegante
--------------------
Cada capa encapsula su lógica en try/except.
Guard exterior garantiza StorageData() vacío ante cualquier fallo.

stdlib únicamente: subprocess, json, pathlib.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Final, Optional

from core.models import StorageData
from tui import runtime_log


# ════════════════════════════════════════════════════════════════════════════
#  CONSTANTES
# ════════════════════════════════════════════════════════════════════════════

_BYTES_PER_SMART_UNIT: int   = 512_000
_BYTES_PER_TB:         float = 1e12

_NVME_CLI_TIMEOUT: Final[int] = 8
"""Timeout para nvme-cli. Comandos NVMe son sub-segundo; 8s es margen amplio."""

_TEMP_THRESHOLD:    float = 70.0
_LIFE_THRESHOLD:    int   = 20
_SPARE_THRESHOLD:   int   = 10
_BAD_BLK_THRESHOLD: int   = 50
_ECC_THRESHOLD:     int   = 100
_WAF_THRESHOLD:     float = 3.0
_WAF_SATA_NAND_WRITES_ID: Final[int] = 233
_WAF_SATA_HOST_WRITES_ID: Final[int] = 241

# Duraciones de test fio
_FIO_RUNTIME_SSD_S: int = 15   # SSD: histograma de latencia completo
_FIO_RUNTIME_HDD_S: int = 10   # HDD: seek latency media (no necesita más)

# Rangos de nuestros buckets expresados en nanosegundos (solo SSD)
_FIO_BUCKET_RANGES_NS: tuple[tuple[int, float], ...] = (
    (0,           1_000),
    (1_000,       2_000),
    (2_000,       4_000),
    (4_000,       8_000),
    (8_000,      16_000),
    (16_000,     32_000),
    (32_000,     64_000),
    (64_000,    128_000),
    (128_000,   256_000),
    (256_000,   float("inf")),
)

# IDs de atributos SMART mecánicos HDD
_SMART_ID_SPIN_UP_TIME:         Final[int] = 3
_SMART_ID_REALLOCATED_SECTORS:  Final[int] = 5
_SMART_ID_COMMAND_TIMEOUT:      Final[int] = 188


# ════════════════════════════════════════════════════════════════════════════
#  UTILIDADES DE BAJO NIVEL
# ════════════════════════════════════════════════════════════════════════════

def _run_smartctl(device: str, timeout: int = 8, is_nvme: bool = False) -> dict[str, Any]:
    """
    Ejecuta smartctl con manejo de return code correcto por tipo de dispositivo.

    FIX v1.1 — NVMe return code mask
    ----------------------------------
    ATA/SATA: rechaza si bit 0 o bit 1 están seteados (parse error o device open failure).
    NVMe:     rechaza SOLO si bit 1 está seteado (device open failure).
              Bit 0 en NVMe = "ATA commands unavailable" — esperado y normal.

    Referencia: smartmontools EXIT STATUS en `man smartctl`, sección ATA vs NVMe.
    """
    result = subprocess.run(
        ["sudo", "smartctl", "-a", "-j", device],
        capture_output=True, text=True, timeout=timeout,
    )
    fatal_mask = 0b10 if is_nvme else 0b11
    if result.returncode & fatal_mask:
        raise RuntimeError(
            f"smartctl rc={result.returncode} para {device!r}. "
            f"stderr: {result.stderr.strip()!r}"
        )
    if not result.stdout.strip():
        raise RuntimeError(f"smartctl sin salida para {device!r}.")
    return json.loads(result.stdout)


def _get(data: dict, *keys: str, default: Any = None) -> Any:
    node: Any = data
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k, default)
        if node is None:
            return default
    return node


def _uw_to_tb(units: int) -> float:
    return round(units * _BYTES_PER_SMART_UNIT / _BYTES_PER_TB, 2)


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 1 — DETECCIÓN DE TIPO DE DISCO (promovida desde Capa 6.5)
# ════════════════════════════════════════════════════════════════════════════

def _detect_rotational(device_path: str) -> bool:
    """
    Determina si el dispositivo es un disco mecánico.

    Estrategia 1 (canónica): sysfs /sys/block/<dev>/queue/rotational
      Valor "1" → HDD. Valor "0" → SSD/NVMe.

    Estrategia 2 (heurística de nombre): prefijos sd* sin información sysfs
      suelen ser SATA (potencialmente mecánico); nvme* son siempre SSD.

    Honestidad forense: si ninguna estrategia concluye, devolvemos False
    (asumir SSD). Preferiríamos omitir un test HDD que ejecutar un test
    NVMe destructivo sobre un disco mecánico lento.
    """
    try:
        dev_name = Path(device_path).name
        rot_path = Path(f"/sys/block/{dev_name}/queue/rotational")
        if rot_path.exists():
            return rot_path.read_text().strip() == "1"
    except Exception:
        pass

    # Heurística de nombre: /dev/sd* sin sysfs → probable HDD
    dev_name = Path(device_path).name
    if dev_name.startswith("sd") and not device_path.startswith("/dev/nvme"):
        return True

    return False


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 2 — IDENTIDAD DEL DISPOSITIVO (compartida SSD/HDD)
# ════════════════════════════════════════════════════════════════════════════

def _parse_identity(smart: dict, device: str) -> tuple[str, str, int, int, str]:
    nvme_model    = _get(smart, "model_name",       default="N/A")
    nvme_firmware = _get(smart, "firmware_version", default="N/A")
    cap_bytes     = _get(smart, "user_capacity", "bytes", default=0)
    nvme_capacity = int(cap_bytes / 1e9) if cap_bytes else 0
    nvme_hours    = (
        _get(smart, "power_on_time", "hours", default=0)
        or _get(smart, "nvme_smart_health_information_log", "power_on_hours", default=0)
    )
    return device, str(nvme_model), nvme_capacity, int(nvme_hours), str(nvme_firmware)


# ════════════════════════════════════════════════════════════════════════════
#  CAPA HDD-A — ATRIBUTOS SMART MECÁNICOS
# ════════════════════════════════════════════════════════════════════════════

def _parse_hdd_smart_attributes(
    smart: dict,
) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Extrae los tres atributos SMART relevantes para la cinemática mecánica.

    ID 3  — Spin-Up Time (ms):
        Tiempo que tarda el motor en alcanzar la velocidad nominal
        desde el reposo. Degradación típica: valores crecientes en
        el tiempo indican desgaste de los cojinetes del husillo.
        Se lee el campo ``raw.value`` directamente.

    ID 5  — Reallocated Sector Count:
        Sectores con errores irrecuperables que el firmware reasignó
        a la zona de reserva. Cualquier valor >0 implica daño físico
        confirmado en la superficie magnética del plato.

    ID 188 — Command Timeout:
        Número de comandos ATA que expiraron sin respuesta.
        Indicador de fallos del actuador, brazo de cabezal o interface
        SATA deteriorada.

    Returns
    -------
    (spin_up_ms, reallocated_sectors, command_timeouts)
        Cada campo es None si el atributo no está presente en la tabla
        SMART del disco (firmware propietario o disco IDE muy antiguo).
    """
    spin_up:      Optional[int] = None
    reallocated:  Optional[int] = None
    cmd_timeout:  Optional[int] = None

    for attr in _get(smart, "ata_smart_attributes", "table", default=[]):
        aid     = attr.get("id", -1)
        raw_val = attr.get("raw", {}).get("value", None)
        if not isinstance(raw_val, int):
            # Algunos firmwares reportan el raw como string "X (Y Y Y Y Y Y)"
            # Intentamos extraer el primer entero.
            raw_str = str(attr.get("raw", {}).get("string", "")).split()[0]
            try:
                raw_val = int(raw_str)
            except (ValueError, TypeError):
                continue

        if aid == _SMART_ID_SPIN_UP_TIME:
            spin_up = raw_val
        elif aid == _SMART_ID_REALLOCATED_SECTORS:
            reallocated = raw_val
        elif aid == _SMART_ID_COMMAND_TIMEOUT:
            cmd_timeout = raw_val

    return spin_up, reallocated, cmd_timeout


# ════════════════════════════════════════════════════════════════════════════
#  CAPA HDD-B — SEEK LATENCY VÍA fio
# ════════════════════════════════════════════════════════════════════════════

def _run_fio_seek_latency_hdd(device: str) -> Optional[float]:
    """
    Mide la latencia media de búsqueda aleatoria del HDD usando fio.

    Diseño del test
    ---------------
    ``fio --rw=randread --bs=4k --iodepth=1 --direct=1``
    ``    --time_based --runtime=10s --output-format=json``

    * randread 4K QD1: fuerza al cabezal a moverse a una nueva posición
      aleatoria en cada operación (seek real), sin cola de profundidad
      que enmascare la latencia mecánica.
    * direct=1: bypass del page cache para medir el hardware real.
    * runtime=10s: suficiente para promediar 200-400 seeks con un HDD
      de 5400-7200 RPM típico, sin desgaste adicional relevante.
    * output-format=json (no json+): sin histograma de bins; solo
      necesitamos clat_ns.mean.

    Conversión
    ----------
    clat_ns.mean (nanosegundos) → ms: dividir entre 1_000_000.

    Latencias de referencia orientativas
    -------------------------------------
    HDD 5400 RPM:  ~12–18 ms (laptop económico)
    HDD 7200 RPM:  ~8–12 ms  (desktop o servidor)
    HDD 10000 RPM: ~4–6 ms   (SCSI/SAS antiguo)
    Umbral degradación (entropy.py): >25 ms → ΔA += 20

    Degradación elegante
    --------------------
    FileNotFoundError → fio no instalado: retorna None.
    Timeout o estructura inesperada: retorna None.

    Returns
    -------
    Optional[float]
        Latencia media de seek en milisegundos, redondeada a 2 decimales.
        None si la medición no pudo completarse.
    """
    cmd = [
        "sudo", "fio",
        "--name=probe_tex_hdd_seek",
        f"--filename={device}",
        "--rw=randread",
        "--bs=4k",
        "--ioengine=libaio",
        "--iodepth=1",
        "--direct=1",
        f"--runtime={_FIO_RUNTIME_HDD_S}s",
        "--time_based",
        "--output-format=json",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_FIO_RUNTIME_HDD_S + 20,
        )
        if not result.stdout.strip():
            raise RuntimeError(
                f"fio HDD sin salida (rc={result.returncode}). "
                f"stderr: {result.stderr[:200]!r}"
            )
        data = json.loads(result.stdout)
        clat_ns_mean: float = data["jobs"][0]["read"]["clat_ns"]["mean"]
        seek_ms = round(clat_ns_mean / 1_000_000.0, 2)
        return seek_ms

    except FileNotFoundError:
        print("[disk_reader] WARN fio no instalado. Seek latency HDD no disponible.")
        return None
    except (KeyError, IndexError) as exc:
        print(f"[disk_reader] WARN fio HDD: estructura JSON inesperada: {exc}")
        return None
    except subprocess.TimeoutExpired:
        print(f"[disk_reader] WARN fio HDD superó timeout de {_FIO_RUNTIME_HDD_S + 20} s.")
        return None
    except Exception as exc:
        print(f"[disk_reader] WARN fio HDD seek: {exc}")
        return None


# ════════════════════════════════════════════════════════════════════════════
#  RUTA HDD — ENSAMBLAJE COMPLETO
# ════════════════════════════════════════════════════════════════════════════

def _extract_hdd_data(device_path: str, smart: dict) -> StorageData:
    """
    Ensambla StorageData para un disco mecánico.

    Capas ejecutadas
    ----------------
    Capa 2     Identidad (modelo, firmware, capacidad, horas) — compartida.
    Capa HDD-A Atributos SMART mecánicos (ID 3, 5, 188).
    Capa HDD-B fio seek latency (clat_ns.mean → ms).

    Los campos nvme_* de estado sólido se dejan en sus valores por defecto
    (cero / False) para garantizar que el template Jinja2 no renderice
    datos espurios si accidentalmente entra al bloque SSD.

    Returns
    -------
    StorageData
        Con is_hdd=True y campos hdd_* populados según disponibilidad.
    """
    # ── Identidad ─────────────────────────────────────────────────────────
    nvme_device = device_path
    nvme_model  = "N/A"
    nvme_capacity = nvme_hours = 0
    nvme_firmware = "N/A"
    try:
        nvme_device, nvme_model, nvme_capacity, nvme_hours, nvme_firmware = (
            _parse_identity(smart, device_path)
        )
    except Exception as exc:
        print(f"[disk_reader] WARN HDD identidad: {exc}")

    # Horas de encendido (SMART ATA, campo diferente al NVMe)
    if nvme_hours == 0:
        try:
            nvme_hours = int(
                _get(smart, "power_on_time", "hours", default=0)
                or _get(smart, "ata_smart_attributes", default={})
            )
        except Exception:
            pass

    # ── SMART mecánico ─────────────────────────────────────────────────────
    spin_up:     Optional[int] = None
    reallocated: Optional[int] = None
    cmd_timeout: Optional[int] = None
    try:
        spin_up, reallocated, cmd_timeout = _parse_hdd_smart_attributes(smart)
    except Exception as exc:
        print(f"[disk_reader] WARN HDD SMART mecánico: {exc}")

    # ── fio Seek Latency ─────────────────────────────────────────────────
    seek_ms: Optional[float] = None
    try:
        print(f"[disk_reader] INFO HDD detectado en {device_path}. "
              "Ejecutando test de seek latency (fio 10s)...")
        runtime_log(f"fio: Measuring physical seek latency on {device_path}...")
        seek_ms = _run_fio_seek_latency_hdd(device_path)
    except Exception as exc:
        print(f"[disk_reader] WARN HDD fio seek: {exc}")

    return StorageData(
        is_hdd            = True,
        nvme_device       = nvme_device,
        nvme_model        = nvme_model,
        nvme_capacity     = nvme_capacity,
        nvme_firmware     = nvme_firmware,
        nvme_hours        = nvme_hours,
        # Campos mecánicos
        hdd_spin_up_time         = spin_up,
        hdd_seek_latency_ms      = seek_ms,
        hdd_reallocated_sectors  = reallocated,
        hdd_command_timeouts     = cmd_timeout,
    )


# ════════════════════════════════════════════════════════════════════════════
#  RUTA SSD/NVMe — CAPAS ORIGINALES (sin cambios respecto a v1.0)
# ════════════════════════════════════════════════════════════════════════════

def _nvme_ctrl_from_ns(namespace_path: str) -> str:
    """
    Deriva la ruta del controlador NVMe desde el namespace.

    nvme-cli opera sobre el CONTROLADOR (/dev/nvme0), no el NAMESPACE
    (/dev/nvme0n1). smartctl usa el namespace; nvme-cli usa el controlador.

    /dev/nvme0n1  →  /dev/nvme0
    /dev/nvme1n2  →  /dev/nvme1
    /dev/nvme0    →  /dev/nvme0   (ya es controlador, pass-through)
    """
    m = re.match(r"(/dev/nvme\d+)(?:n\d+)?$", namespace_path)
    return m.group(1) if m else namespace_path


def _collect_nvme_via_nvme_cli(namespace_path: str) -> dict[str, Any] | None:
    """
    Recopila datos SMART NVMe usando nvme-cli como fuente primaria.

    Combina `nvme smart-log` (métricas de salud) y `nvme id-ctrl` (identidad)
    en un dict normalizado al esquema de smartctl -a -j, para que
    _parse_health_log() e _parse_identity() lo consuman sin cambios.

    Retorna None si nvme-cli no está instalado (fallback a smartctl).

    Nota sobre temperatura
    ----------------------
    nvme-cli < 2.0 reporta temperatura en Kelvin compuesto (ej. 308 = 35°C).
    nvme-cli >= 2.0 reporta en Celsius directo.
    Heurística: si valor > 200 → Kelvin, restar 273.
    """
    ctrl = _nvme_ctrl_from_ns(namespace_path)

    # --- nvme smart-log ---
    try:
        r_smart = subprocess.run(
            ["sudo", "nvme", "smart-log", ctrl, "-o", "json"],
            capture_output=True, text=True, timeout=_NVME_CLI_TIMEOUT,
        )
        if r_smart.returncode != 0 or not r_smart.stdout.strip():
            raise RuntimeError(
                f"nvme smart-log rc={r_smart.returncode} "
                f"stderr={r_smart.stderr.strip()!r}"
            )
        smart_log: dict = json.loads(r_smart.stdout)
    except FileNotFoundError:
        return None  # nvme-cli no instalado → señal de fallback a smartctl
    except Exception as exc:
        print(f"[disk_reader] WARN nvme smart-log: {exc}")
        return None

    # --- nvme id-ctrl ---
    try:
        r_id = subprocess.run(
            ["sudo", "nvme", "id-ctrl", ctrl, "-o", "json"],
            capture_output=True, text=True, timeout=_NVME_CLI_TIMEOUT,
        )
        id_ctrl: dict = json.loads(r_id.stdout) if r_id.returncode == 0 else {}
    except Exception as exc:
        print(f"[disk_reader] WARN nvme id-ctrl: {exc}")
        id_ctrl = {}

    # --- Normalización de temperatura ---
    raw_temp: int = smart_log.get("temperature", 0)
    temp_c: float = float(raw_temp - 273 if raw_temp > 200 else raw_temp)

    # --- Capacidad: tnvmcap (bytes) es la fuente canónica en id-ctrl ---
    cap_bytes: int = (
        id_ctrl.get("tnvmcap", 0)
        or id_ctrl.get("nsze", 0) * 512   # nsze en bloques de 512B como fallback
    )

    # --- Dict normalizado al esquema de smartctl -a -j ---
    return {
        "model_name":       id_ctrl.get("mn", "N/A").strip(),
        "firmware_version": id_ctrl.get("fr", "N/A").strip(),
        "user_capacity":    {"bytes": cap_bytes},
        "power_on_time":    {"hours": smart_log.get("power_on_hours", 0)},
        "nvme_smart_health_information_log": {
            "percentage_used":    smart_log.get("percent_used",       0),
            "available_spare":    smart_log.get("avail_spare",      100),
            "media_errors":       smart_log.get("media_errors",       0),
            "data_units_written": smart_log.get("data_units_written", 0),
            "data_units_read":    smart_log.get("data_units_read",    0),
            "temperature":        int(temp_c),
            "power_on_hours":     smart_log.get("power_on_hours",     0),
        },
        "temperature": {"current": int(temp_c)},
    }

def _parse_health_log(smart: dict) -> dict[str, Any]:
    log = _get(smart, "nvme_smart_health_information_log", default={})
    temp_current = float(
        _get(smart, "temperature", "current", default=0)
        or _get(log, "temperature", default=0)
    )
    temp_log  = _get(smart, "temperature_log", default=[])
    t_max_log = 0.0
    if isinstance(temp_log, list):
        for entry in temp_log:
            if isinstance(entry, dict):
                v = entry.get("max") or entry.get("current") or 0
                if isinstance(v, (int, float)):
                    t_max_log = max(t_max_log, float(v))
    t_max = max(temp_current, t_max_log) if t_max_log else temp_current
    return {
        "percentage_used":    int(_get(log, "percentage_used",    default=0)),
        "available_spare":    int(_get(log, "available_spare",    default=100)),
        "media_errors":       int(_get(log, "media_errors",       default=0)),
        "data_units_written": int(_get(log, "data_units_written", default=0)),
        "data_units_read":    int(_get(log, "data_units_read",    default=0)),
        "temperature_raw":    temp_current,
        "t_max":              t_max,
    }


def _calc_tbw(
    data_units_written: int,
    percentage_used:    int,
    capacity_gb:        int,
) -> tuple[float, float, float]:
    lba_written_tb = _uw_to_tb(data_units_written)
    if percentage_used > 0:
        rated_tbw = round(lba_written_tb / (percentage_used / 100.0), 1)
    else:
        rated_tbw = round(capacity_gb * 0.5, 1)
    remaining_tbw = round(max(0.0, rated_tbw - lba_written_tb), 1)
    return lba_written_tb, rated_tbw, remaining_tbw


def _extract_real_waf(smart: dict, lba_written_tb: float) -> tuple[float, float]:
    nand_raw = host_raw = 0
    for attr in _get(smart, "ata_smart_attributes", "table", default=[]):
        aid     = attr.get("id", 0)
        raw_val = attr.get("raw", {}).get("value", 0)
        if aid == 233 and isinstance(raw_val, int):
            nand_raw = raw_val
        elif aid == 241 and isinstance(raw_val, int):
            host_raw = raw_val
    if nand_raw > 0 and host_raw > 0:
        waf    = round(nand_raw / host_raw, 3)
        nand_tb = _uw_to_tb(nand_raw * 32)
        return waf, nand_tb
    return 0.0, 0.0


def _parse_bad_blocks_and_ecc(smart: dict, health: dict) -> tuple[int, int]:
    ecc_errors: int = health.get("media_errors", 0)
    try:
        for attr in _get(smart, "ata_smart_attributes", "table", default=[]):
            aid     = attr.get("id", 0)
            raw_val = attr.get("raw", {}).get("value", 0)
            if aid == 187 and isinstance(raw_val, int):
                ecc_errors = max(ecc_errors, raw_val)
            if aid == 196 and isinstance(raw_val, int):
                ecc_errors += raw_val
    except Exception:
        pass
    bad_blocks: int = 0
    try:
        for attr in _get(smart, "ata_smart_attributes", "table", default=[]):
            if attr.get("id") == 5:
                raw_val = attr.get("raw", {}).get("value", 0)
                if isinstance(raw_val, int):
                    bad_blocks = raw_val
                    break
    except Exception:
        pass
    return bad_blocks, ecc_errors


def _compute_flags(
    t_max: float, life_pct: int, waf: float,
    bad_blocks: int, spare_pct: int, ecc_errors: int,
) -> dict[str, bool]:
    return {
        "nvme_temp_ok":       t_max < _TEMP_THRESHOLD,
        "nvme_life_ok":       life_pct >= _LIFE_THRESHOLD,
        "nvme_waf_ok":        waf <= _WAF_THRESHOLD,
        "nvme_bad_blocks_ok": bad_blocks <= _BAD_BLK_THRESHOLD,
        "nvme_spare_ok":      spare_pct >= _SPARE_THRESHOLD,
        "nvme_ecc_ok":        ecc_errors < _ECC_THRESHOLD,
    }


def _fio_percentile(percentiles: dict, target: float) -> float:
    for fmt in (f"{target:.6f}", f"{target:.1f}", f"{target:.0f}", str(target)):
        val = percentiles.get(fmt)
        if val is not None:
            return float(val)
    best: float | None = None
    best_diff = float("inf")
    for k, v in percentiles.items():
        try:
            diff = abs(float(k) - target)
            if diff < best_diff:
                best_diff = diff
                best = float(v)
        except (ValueError, TypeError):
            continue
    return best if (best is not None and best_diff < 0.1) else 0.0


def _map_bins_to_buckets(bins: dict) -> list[int]:
    buckets = [0] * 10
    for ns_str, count in bins.items():
        try:
            ns  = int(ns_str)
            cnt = int(count)
        except (ValueError, TypeError):
            continue
        for i, (lo, hi) in enumerate(_FIO_BUCKET_RANGES_NS):
            if lo <= ns < hi:
                buckets[i] += cnt
                break
    return buckets


def _run_fio_latency_ssd(device: str) -> tuple[list[int], float, float, float, float]:
    """fio randread 4K QD1 json+ para histograma completo (solo SSD/NVMe)."""
    cmd = [
        "sudo", "fio",
        "--name=probe_tex_ssd_lat",
        f"--filename={device}",
        "--rw=randread",
        "--bs=4k",
        "--ioengine=libaio",
        "--iodepth=1",
        "--direct=1",
        f"--runtime={_FIO_RUNTIME_SSD_S}s",
        "--time_based",
        "--output-format=json+",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=_FIO_RUNTIME_SSD_S + 30,
    )
    if not result.stdout.strip():
        raise RuntimeError(
            f"fio sin salida (rc={result.returncode}). "
            f"stderr: {result.stderr[:200]!r}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"JSON de fio inválido: {exc}") from exc
    try:
        job_read = data["jobs"][0]["read"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Estructura JSON de fio inesperada: {exc}") from exc

    clat = job_read.get("clat_ns", {})
    pcts = clat.get("percentiles", {})
    bins = clat.get("bins", {})

    p50  = round(_fio_percentile(pcts, 50.0) / 1_000.0, 2)
    p95  = round(_fio_percentile(pcts, 95.0) / 1_000.0, 2)
    p99  = round(_fio_percentile(pcts, 99.0) / 1_000.0, 2)
    p999 = round(_fio_percentile(pcts, 99.9) / 1_000.0, 2)

    buckets = _map_bins_to_buckets(bins)
    return buckets, p50, p95, p99, p999


def _extract_ssd_data(device_path: str, smart: dict) -> StorageData:
    """
    Ensambla StorageData para un disco SSD/NVMe.
    Ruta original completa, sin modificaciones respecto a v1.0.
    """
    # ── Identidad ─────────────────────────────────────────────────────────
    nvme_device = device_path
    nvme_model  = "N/A"
    nvme_capacity = nvme_hours = 0
    nvme_firmware = "N/A"
    try:
        nvme_device, nvme_model, nvme_capacity, nvme_hours, nvme_firmware = (
            _parse_identity(smart, device_path)
        )
    except Exception as exc:
        print(f"[disk_reader] WARN SSD identidad: {exc}")

    # ── SMART health log ───────────────────────────────────────────────────
    health: dict[str, Any] = {}
    try:
        health = _parse_health_log(smart)
    except Exception as exc:
        print(f"[disk_reader] WARN SSD health log: {exc}")

    percentage_used    = health.get("percentage_used",    0)
    available_spare    = health.get("available_spare",  100)
    media_errors       = health.get("media_errors",       0)
    data_units_written = health.get("data_units_written", 0)
    t_max              = health.get("t_max",            45.0)
    life_pct = max(0, min(100, 100 - percentage_used))

    # ── TBW y WAF ────────────────────────────────────────────────────────
    lba_written_tb = rated_tbw = remaining_tbw = 0.0
    waf = nand_written_tb = 0.0
    try:
        lba_written_tb, rated_tbw, remaining_tbw = _calc_tbw(
            data_units_written, percentage_used, nvme_capacity
        )
        waf, nand_written_tb = _extract_real_waf(smart, lba_written_tb)
    except Exception as exc:
        print(f"[disk_reader] WARN SSD TBW/WAF: {exc}")

    # ── Bloques y ECC ────────────────────────────────────────────────────
    bad_blocks = 0
    ecc_errors = media_errors
    try:
        bad_blocks, ecc_errors = _parse_bad_blocks_and_ecc(smart, health)
    except Exception as exc:
        print(f"[disk_reader] WARN SSD bad_blocks/ECC: {exc}")

    # ── Flags booleanos ───────────────────────────────────────────────────
    flags: dict[str, bool] = {}
    try:
        flags = _compute_flags(t_max, life_pct, waf,
                               bad_blocks, available_spare, ecc_errors)
    except Exception as exc:
        print(f"[disk_reader] WARN SSD flags: {exc}")

    # ── fio histograma de latencia ────────────────────────────────────────
    buckets: list[int] = [0] * 10
    p50 = p95 = p99 = p999 = 0.0
    try:
        runtime_log(f"fio: Sweeping NVMe/SSD latency on {device_path}...")
        buckets, p50, p95, p99, p999 = _run_fio_latency_ssd(device_path)
    except FileNotFoundError:
        print("[disk_reader] WARN fio no instalado. Latencia SSD no disponible.")
    except Exception as exc:
        print(f"[disk_reader] WARN fio SSD: {exc}")

    return StorageData(
        is_hdd         = False,
        nvme_device    = nvme_device,
        nvme_model     = nvme_model,
        nvme_capacity  = nvme_capacity,
        nvme_firmware  = nvme_firmware,
        nvme_hours     = nvme_hours,

        nvme_tbw_remaining = remaining_tbw,
        nvme_tbw_rated     = rated_tbw,
        nvme_lba_written   = lba_written_tb,
        nvme_nand_written  = nand_written_tb,

        nvme_waf            = round(waf, 3),
        nvme_waf_ok         = flags.get("nvme_waf_ok",         False),
        nvme_bad_blocks     = bad_blocks,
        nvme_bad_blocks_ok  = flags.get("nvme_bad_blocks_ok",  False),
        nvme_spare_blocks   = available_spare,
        nvme_spare_ok       = flags.get("nvme_spare_ok",       False),
        nvme_ecc_errors     = ecc_errors,
        nvme_ecc_ok         = flags.get("nvme_ecc_ok",         False),
        nvme_t_max          = round(t_max, 1),
        nvme_temp_ok        = flags.get("nvme_temp_ok",        False),
        nvme_life_pct       = life_pct,
        nvme_life_ok        = flags.get("nvme_life_ok",        False),

        lat_b0 = buckets[0],  lat_b1 = buckets[1],  lat_b2 = buckets[2],
        lat_b3 = buckets[3],  lat_b4 = buckets[4],  lat_b5 = buckets[5],
        lat_b6 = buckets[6],  lat_b7 = buckets[7],  lat_b8 = buckets[8],
        lat_b9 = buckets[9],

        nvme_lat_p50  = p50,
        nvme_lat_p95  = p95,
        nvme_lat_p99  = p99,
        nvme_lat_p999 = p999,
    )


# ════════════════════════════════════════════════════════════════════════════
#  API PÚBLICA
# ════════════════════════════════════════════════════════════════════════════

def extract_disk_data(device_path: str = "/dev/nvme0n1") -> StorageData:
    """
    Extrae datos de almacenamiento bifurcando en HDD o SSD/NVMe.

    Flujo v1.2 — NVMe-aware
    -----------------------
    1. Detectar rotación (sysfs rotational).
    2. Si NVMe:
       2a. Intentar nvme-cli (fuente primaria, sin problemas de RC).
       2b. Fallback a smartctl con mask NVMe-correcta (is_nvme=True).
    3. Si HDD → _extract_hdd_data() (sin cambios).
    4. Si SSD SATA → _extract_ssd_data() con smartctl estándar.
    """
    try:
        is_rotational = _detect_rotational(device_path)
        is_nvme       = Path(device_path).name.startswith("nvme")

        if is_rotational:
            print(f"[disk_reader] INFO {device_path}: HDD mecánico → rama cinemática.")
            try:
                smart = _run_smartctl(device_path, timeout=8, is_nvme=False)
            except Exception as exc:
                print(f"[disk_reader] WARN smartctl HDD: {exc}")
                return StorageData()
            return _extract_hdd_data(device_path, smart)

        if is_nvme:
            print(f"[disk_reader] INFO {device_path}: NVMe → nvme-cli primario.")
            # Intento 1: nvme-cli (fuente canónica para NVMe)
            smart = _collect_nvme_via_nvme_cli(device_path)
            if smart is None:
                # Intento 2: smartctl con RC mask correcta para NVMe
                print(f"[disk_reader] INFO nvme-cli no disponible → smartctl NVMe fallback.")
                try:
                    smart = _run_smartctl(device_path, timeout=10, is_nvme=True)
                except Exception as exc:
                    print(f"[disk_reader] WARN smartctl NVMe fallback: {exc}")
                    return StorageData()
        else:
            print(f"[disk_reader] INFO {device_path}: SSD SATA → smartctl estándar.")
            try:
                smart = _run_smartctl(device_path, timeout=8, is_nvme=False)
            except Exception as exc:
                print(f"[disk_reader] WARN smartctl SATA: {exc}")
                return StorageData()

        return _extract_ssd_data(device_path, smart)

    except Exception as exc:    # pragma: no cover — guardia absoluta
        print(f"[disk_reader] ERROR CRÍTICO en extract_disk_data(): {exc}")
        return StorageData()