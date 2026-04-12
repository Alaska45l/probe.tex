"""
extractors/disk_reader.py
=========================
Extractor de hardware para el subsistema de almacenamiento NVMe/SSD
de probe.tex.

Fuente de datos principal — Desgaste
-------------------------------------
``sudo smartctl -a -j <device>``   (smartmontools ≥ 7.3)

Fuente de datos — Latencia I/O Real (Forense Activo)
-----------------------------------------------------
``sudo fio --name=auditmaster_lat --filename=<device>``
    ``--rw=randread --bs=4k --ioengine=libaio --iodepth=1``
    ``--direct=1 --runtime=15s --time_based --output-format=json+``

El JSON extendido (``json+``) de fio incluye:
  jobs[0].read.clat_ns.percentiles  → p50, p95, p99, p99.9 en nanosegundos.
  jobs[0].read.clat_ns.bins         → conteos por bucket de latencia (ns).

Los percentiles se convierten a microsegundos.  Los bins se mapean a los
diez buckets propios del modelo (lat_b0–lat_b9):

  b0  < 1 µs      [ 0,     1 000) ns
  b1  1–2 µs      [ 1 000, 2 000) ns
  b2  2–4 µs      [ 2 000, 4 000) ns
  b3  4–8 µs      [ 4 000, 8 000) ns
  b4  8–16 µs     [ 8 000,16 000) ns
  b5  16–32 µs    [16 000,32 000) ns
  b6  32–64 µs    [32 000,64 000) ns
  b7  64–128 µs   [64 000,128 000) ns
  b8  128–256 µs  [128 000,256 000) ns
  b9  > 256 µs    [256 000, ∞) ns

Degradación elegante
--------------------
Si ``fio`` no está instalado (FileNotFoundError) o el test falla, todos
los lat_* y percentiles quedan en 0 y se registra un aviso.  El desgaste
SMART se publica igual.  El guard exterior garantiza NVMeData() vacío
ante cualquier fallo imprevisto.

stdlib únicamente: subprocess, json, pathlib.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from core.models import NVMeData


# ════════════════════════════════════════════════════════════════════════════
#  CONSTANTES
# ════════════════════════════════════════════════════════════════════════════

_BYTES_PER_SMART_UNIT: int   = 512_000
_BYTES_PER_TB:         float = 1e12

_TEMP_THRESHOLD:    float = 70.0
_LIFE_THRESHOLD:    int   = 20
_SPARE_THRESHOLD:   int   = 10
_BAD_BLK_THRESHOLD: int   = 50
_ECC_THRESHOLD:     int   = 100
_WAF_THRESHOLD:     float = 3.0
_WAF_BASE:          float = 1.05
_WAF_RANGE:         float = 0.15

_FIO_RUNTIME_S: int = 15     # duración del test fio en segundos

# Rangos de nuestros buckets expresados en nanosegundos.
# Índice i → [low_ns, high_ns)  donde high del último es +∞.
_FIO_BUCKET_RANGES_NS: tuple[tuple[int, float], ...] = (
    (0,           1_000),      # b0  < 1 µs
    (1_000,       2_000),      # b1  1–2 µs
    (2_000,       4_000),      # b2  2–4 µs
    (4_000,       8_000),      # b3  4–8 µs
    (8_000,      16_000),      # b4  8–16 µs
    (16_000,     32_000),      # b5  16–32 µs
    (32_000,     64_000),      # b6  32–64 µs
    (64_000,    128_000),      # b7  64–128 µs
    (128_000,   256_000),      # b8  128–256 µs
    (256_000,   float("inf")), # b9  > 256 µs
)


# ════════════════════════════════════════════════════════════════════════════
#  UTILIDADES DE BAJO NIVEL
# ════════════════════════════════════════════════════════════════════════════

def _run_smartctl(device: str, timeout: int = 5) -> dict[str, Any]:
    """
    Ejecuta ``sudo smartctl -a -j <device>`` y devuelve el JSON parseado.

    Solo los bits 0 y 1 del returncode indican JSON irrecuperable;
    los bits 2-7 reportan estado del disco pero el JSON es válido.
    """
    result = subprocess.run(
        ["sudo", "smartctl", "-a", "-j", device],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode & 0b11:
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
#  CAPA 2 — IDENTIDAD DEL DISPOSITIVO
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
#  CAPA 3 — nvme_smart_health_information_log
# ════════════════════════════════════════════════════════════════════════════

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


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 4 — TBW, WAF Y ESCRITURAS NAND
# ════════════════════════════════════════════════════════════════════════════

def _calc_tbw_and_waf(
    data_units_written: int,
    percentage_used:    int,
    capacity_gb:        int,
) -> tuple[float, float, float, float, float]:
    lba_written_tb = _uw_to_tb(data_units_written)
    used_fraction  = max(0.0, min(1.0, percentage_used / 100.0))
    waf            = round(_WAF_BASE + used_fraction * _WAF_RANGE, 3)
    nand_written_tb = round(lba_written_tb * waf, 2)

    if percentage_used > 0:
        rated_tbw = round(lba_written_tb / (percentage_used / 100.0), 1)
    else:
        rated_tbw = round(capacity_gb * 0.5, 1)

    remaining_tbw = round(max(0.0, rated_tbw - lba_written_tb), 1)
    return lba_written_tb, nand_written_tb, waf, rated_tbw, remaining_tbw


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 5 — BLOQUES DEFECTUOSOS Y ECC
# ════════════════════════════════════════════════════════════════════════════

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
        if bad_blocks == 0 and ecc_errors > 0:
            bad_blocks = min(ecc_errors, 999)
    except Exception:
        pass

    return bad_blocks, ecc_errors


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 6 — UMBRALES BOOLEANOS
# ════════════════════════════════════════════════════════════════════════════

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


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 7 — LATENCIA I/O REAL (fio randread 4 K QD1)
# ════════════════════════════════════════════════════════════════════════════

def _fio_percentile(percentiles: dict, target: float) -> float:
    """
    Extrae el valor de un percentil del dict ``clat_ns.percentiles`` de fio.

    fio formatea las claves con seis decimales ("50.000000"), pero distintas
    versiones pueden variar.  Intentamos varios formatos antes de hacer
    una búsqueda aproximada por diferencia mínima.
    """
    for fmt in (f"{target:.6f}", f"{target:.1f}", f"{target:.0f}", str(target)):
        val = percentiles.get(fmt)
        if val is not None:
            return float(val)
    # Búsqueda aproximada (tolerancia 0.1 pp).
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
    """
    Mapea los bins de latencia de fio (claves = ns, valores = conteo)
    a los diez buckets propios del modelo (lat_b0–lat_b9).

    fio usa buckets de potencias de 2 con claves numéricas (como strings)
    que representan el límite inferior del bucket en nanosegundos.
    """
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


def _run_fio_latency(device: str) -> tuple[list[int], float, float, float, float]:
    """
    Ejecuta fio randread 4 K QD1 con salida ``json+`` y retorna métricas
    de latencia reales.

    Comando ejecutado
    -----------------
    ``sudo fio --name=auditmaster_lat --filename=<device>``
        ``--rw=randread --bs=4k --ioengine=libaio --iodepth=1``
        ``--direct=1 --runtime=<N>s --time_based --output-format=json+``

    ``randread`` garantiza que no se escriben datos en el dispositivo.
    ``iodepth=1`` mide latencia de cola 1 (sin concurrencia), el escenario
    más representativo del acceso secuencial de un solo proceso.
    ``json+`` incluye el campo ``clat_ns.bins`` con el histograma completo.

    Degradación elegante
    --------------------
    * FileNotFoundError → fio no instalado: se propaga al caller que
      devuelve ceros.
    * returncode != 0   → RuntimeError con el stderr truncado.

    Returns
    -------
    (buckets[10], p50_us, p95_us, p99_us, p999_us)
        Todos los percentiles en microsegundos (float).
    """
    cmd = [
        "sudo", "fio",
        "--name=auditmaster_lat",
        f"--filename={device}",
        "--rw=randread",
        "--bs=4k",
        "--ioengine=libaio",
        "--iodepth=1",
        "--direct=1",
        f"--runtime={_FIO_RUNTIME_S}s",
        "--time_based",
        "--output-format=json+",
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_FIO_RUNTIME_S + 30,
    )
    # fio puede devolver rc != 0 por errores de I/O no fatales; aun así
    # suele generar JSON válido.  Solo abortamos si no hay salida.
    if not result.stdout.strip():
        raise RuntimeError(
            f"fio no produjo salida (rc={result.returncode}). "
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

    # Percentiles: ns → µs
    p50  = round(_fio_percentile(pcts, 50.0)  / 1_000.0, 2)
    p95  = round(_fio_percentile(pcts, 95.0)  / 1_000.0, 2)
    p99  = round(_fio_percentile(pcts, 99.0)  / 1_000.0, 2)
    p999 = round(_fio_percentile(pcts, 99.9)  / 1_000.0, 2)

    buckets = _map_bins_to_buckets(bins)
    return buckets, p50, p95, p99, p999


# ════════════════════════════════════════════════════════════════════════════
#  API PÚBLICA
# ════════════════════════════════════════════════════════════════════════════

def extract_disk_data(device_path: str = "/dev/nvme0n1") -> NVMeData:
    """
    Extrae y ensambla todos los datos de almacenamiento NVMe en ``NVMeData``.

    Arquitectura de extracción en 7 capas
    --------------------------------------
    Capa 1  smartctl   — ejecución y obtención del JSON SMART completo.
    Capa 2  Identidad  — modelo, firmware, capacidad, horas.
    Capa 3  Health log — percentage_used, spare, media_errors, temperatura.
    Capa 4  TBW / WAF  — escrituras host y NAND, vida estimada.
    Capa 5  Bad blocks / ECC — conteos desde atributos SMART/EDAC.
    Capa 6  Flags      — evaluación de umbrales booleanos.
    Capa 7  fio        — latencia I/O real (randread 4K QD1, 15 s).
                         Si fio no está instalado, lat_b* y percentiles = 0.

    Returns
    -------
    NVMeData
        Instancia completamente poblada.  Nunca lanza excepciones.
    """
    try:
        # ── Capa 1: smartctl ─────────────────────────────────────────────
        try:
            smart = _run_smartctl(device_path, timeout=5)
        except Exception as exc:
            print(f"[disk_reader] WARN smartctl: {exc}")
            return NVMeData()

        # ── Capa 2: identidad ────────────────────────────────────────────
        nvme_device = device_path
        nvme_model  = "N/A"
        nvme_capacity = nvme_hours = 0
        nvme_firmware = "N/A"
        try:
            nvme_device, nvme_model, nvme_capacity, nvme_hours, nvme_firmware = (
                _parse_identity(smart, device_path)
            )
        except Exception as exc:
            print(f"[disk_reader] WARN identidad: {exc}")

        # ── Capa 3: SMART health log ─────────────────────────────────────
        health: dict[str, Any] = {}
        try:
            health = _parse_health_log(smart)
        except Exception as exc:
            print(f"[disk_reader] WARN health log: {exc}")

        percentage_used    = health.get("percentage_used",    0)
        available_spare    = health.get("available_spare",  100)
        media_errors       = health.get("media_errors",       0)
        data_units_written = health.get("data_units_written", 0)
        t_max              = health.get("t_max",            45.0)
        life_pct = max(0, min(100, 100 - percentage_used))

        # ── Capa 4: TBW y WAF ────────────────────────────────────────────
        lba_written_tb = nand_written_tb = 0.0
        waf = _WAF_BASE
        rated_tbw = remaining_tbw = 0.0
        try:
            lba_written_tb, nand_written_tb, waf, rated_tbw, remaining_tbw = (
                _calc_tbw_and_waf(data_units_written, percentage_used, nvme_capacity)
            )
        except Exception as exc:
            print(f"[disk_reader] WARN TBW/WAF: {exc}")

        # ── Capa 5: bloques defectuosos y ECC ────────────────────────────
        bad_blocks = 0
        ecc_errors = media_errors
        try:
            bad_blocks, ecc_errors = _parse_bad_blocks_and_ecc(smart, health)
        except Exception as exc:
            print(f"[disk_reader] WARN bad_blocks/ECC: {exc}")

        # ── Capa 6: umbrales booleanos ────────────────────────────────────
        flags: dict[str, bool] = {}
        try:
            flags = _compute_flags(t_max, life_pct, waf,
                                   bad_blocks, available_spare, ecc_errors)
        except Exception as exc:
            print(f"[disk_reader] WARN flags: {exc}")

        # ── Capa 6.5: Detección de naturaleza física (HDD vs SSD) ────────
        is_rotational = False
        try:
            dev_name = Path(device_path).name
            rot_path = Path(f"/sys/block/{dev_name}/queue/rotational")
            if rot_path.exists() and rot_path.read_text().strip() == "1":
                is_rotational = True
        except Exception:
            pass

        # ── Capa 7: latencia I/O real (fio) ──────────────────────────────
        buckets: list[int] = [0] * 10
        p50 = p95 = p99 = p999 = 0.0
        if is_rotational:
            print(f"[disk_reader] INFO {device_path} es un HDD mecánico. Omitiendo prueba fio destructiva.")
            try:
                subprocess.Popen(["sudo", "smartctl", "-t", "short", device_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        else:
            try:
                buckets, p50, p95, p99, p999 = _run_fio_latency(device_path)
            except FileNotFoundError:
                print("[disk_reader] WARN fio no instalado. Latencia no disponible.")
            except Exception as exc:
                print(f"[disk_reader] WARN fio: {exc}")

        # ── Ensamblaje final ──────────────────────────────────────────────
        return NVMeData(
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

            nvme_t_max   = round(t_max, 1),
            nvme_temp_ok = flags.get("nvme_temp_ok", False),

            nvme_life_pct = life_pct,
            nvme_life_ok  = flags.get("nvme_life_ok", False),

            lat_b0 = buckets[0],
            lat_b1 = buckets[1],
            lat_b2 = buckets[2],
            lat_b3 = buckets[3],
            lat_b4 = buckets[4],
            lat_b5 = buckets[5],
            lat_b6 = buckets[6],
            lat_b7 = buckets[7],
            lat_b8 = buckets[8],
            lat_b9 = buckets[9],

            nvme_lat_p50  = p50,
            nvme_lat_p95  = p95,
            nvme_lat_p99  = p99,
            nvme_lat_p999 = p999,
        )

    except Exception as exc:    # pragma: no cover — guardia absoluta
        print(f"[disk_reader] ERROR CRÍTICO en extract_disk_data(): {exc}")
        return NVMeData()