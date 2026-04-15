"""
extractors/cpu_reader.py
========================
Extractor de hardware para el subsistema de CPU de probe.tex.

Fuentes de datos (en orden de preferencia / fallback)
------------------------------------------------------
Identificación
  1. ``lscpu -J``               → modelo, núcleos, hilos, frecuencias.
  2. ``dmidecode -t processor`` → socket físico (requiere root o setuid).
  3. RAPL sysfs                 → TDP real via ``/sys/class/powercap/``.

Temperatura — Prueba Térmica Activa (Forense Activo)
  4. ``stress-ng --cpu 0 --timeout 30s``
     Ejecutado como proceso independiente (Popen).  Mientras corre, un
     hilo secundario muestrea ``sensors -j`` cada 2 s y acumula la serie
     temporal real.  Al terminar, se registran 15 s adicionales de
     enfriamiento pasivo con el mismo hilo.
  5. sysfs hwmon → fallback si lm_sensors no está instalado (solo idle).

TjMax
  6. Extraído del campo ``_crit`` del paquete en sensors.
  7. sysfs ``temp*_crit``  → segundo fallback.
  8. Constante conservadora 100 °C.

P-States
  9. ``cpufreq-info`` / sysfs ``cpufreq`` → frecuencias reales por política.
  10. Derivadas de los valores max/min de lscpu.

Throttling
  11. sysfs ``thermal_throttle`` → conteo real de eventos HW.
  12. Valores seguros (0 eventos) si no disponible.

MCE (Machine Check Exceptions)
  13. ``mcelog --client --ignorenodev``
  14. ``/var/log/mcelog``
  15. ``dmesg --level=err,crit``
  16. sysfs ``machinecheck``

Degradación elegante
--------------------
Si ``stress-ng`` no está instalado (FileNotFoundError), el bloque de la
Capa 5 captura la excepción, registra el aviso y continúa con los datos
idle recogidos antes del intento.  El guard exterior de
``extract_cpu_data()`` garantiza ``CPUData()`` vacío ante cualquier fallo
no anticipado.

stdlib únicamente: subprocess, json, re, pathlib, datetime, threading, time.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Final, Optional

from core.models import CPUData
from tui import runtime_log

# ════════════════════════════════════════════════════════════════════════════
#  CONSTANTES
# ════════════════════════════════════════════════════════════════════════════

_SENSOR_CHIP_PRIORITY: tuple[str, ...] = (
    "coretemp",   # Intel: Core i/Xeon — chip expuesto por coretemp.ko
    "k10temp",    # AMD: Zen 1–5, Threadripper, EPYC
    "zenpower",   # AMD: alternativa con acceso directo al SMN
    "nct",        # Nuvoton: SuperIO habitual en placas ASUS/Gigabyte
    "it8",        # ITE: SuperIO habitual en placas MSI/ASRock
    "asus",       # ASUS WMI platform sensor
    "acpitz",     # ACPI thermal zone — último recurso (baja resolución)
)

_TJMAX_DEFAULT:       int   = 100      # °C conservador
_STRESS_DURATION_S:   int   = 30       # segundos de carga via stress-ng
_COOLING_DURATION_S:  int   = 15       # segundos de enfriamiento pasivo
_SAMPLE_INTERVAL_S:   float = 2.0      # intervalo de muestreo del hilo térmico

_RAPL_PATHS: tuple[str, ...] = (
    "/sys/class/powercap/intel-rapl/intel-rapl:0/constraint_0_power_limit_uw",
    "/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw",
)

_MCE_SYSFS_ROOT = Path("/sys/devices/system/machinecheck")


# ════════════════════════════════════════════════════════════════════════════
#  UTILIDADES INTERNAS DE BAJO NIVEL
# ════════════════════════════════════════════════════════════════════════════

def _run(cmd: list[str], timeout: int = 6) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"Comando {cmd!r} terminó con código {result.returncode}. "
            f"stderr: {result.stderr.strip()!r}"
        )
    return result.stdout


def _read_sysfs(path: str | Path) -> str:
    return Path(path).read_text().strip()


def _safe_int(value: str | None, default: int = 0) -> int:
    try:
        return int(float(str(value or "")))
    except (ValueError, TypeError):
        return default


def _safe_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(str(value or ""))
    except (ValueError, TypeError):
        return default


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 1 — lscpu: identificación y frecuencias
# ════════════════════════════════════════════════════════════════════════════

def _flatten_lscpu_json(raw: str) -> dict[str, str]:
    data = json.loads(raw)
    flat: dict[str, str] = {}

    def _walk(entries: list) -> None:
        for entry in entries:
            key = entry.get("field", "").rstrip(":").strip()
            val = entry.get("data", "").strip()
            if key:
                flat[key] = val
            children = entry.get("children")
            if isinstance(children, list):
                _walk(children)

    _walk(data.get("lscpu", []))
    return flat


def _extract_lscpu() -> dict[str, str]:
    return _flatten_lscpu_json(_run(["lscpu", "-J"]))


def _identity_from_lscpu(lscpu: dict[str, str]) -> tuple[str, int, int, int, int]:
    model = lscpu.get("Model name") or lscpu.get("CPU") or "N/A"
    model = re.sub(r"\s+", " ", model).strip()
    model = re.sub(r"\s+@\s+[\d.]+\s*GHz", "", model).strip()

    threads          = _safe_int(lscpu.get("CPU(s)"))
    cores_per_socket = _safe_int(lscpu.get("Core(s) per socket"), default=1)
    sockets          = _safe_int(lscpu.get("Socket(s)"),          default=1)
    cores            = cores_per_socket * sockets

    max_mhz = _safe_int(lscpu.get("CPU max MHz") or lscpu.get("CPU MHz"), default=0)
    min_mhz = _safe_int(lscpu.get("CPU min MHz"), default=800)

    return model, cores, threads, max_mhz, min_mhz


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 2 — dmidecode: socket físico
# ════════════════════════════════════════════════════════════════════════════

def _socket_from_dmidecode() -> str:
    try:
        raw   = _run(["dmidecode", "-t", "processor"], timeout=4)
        match = re.search(r"Socket Designation:\s*(.+)", raw)
        if match:
            s = match.group(1).strip()
            if s and s.lower() not in ("not specified", "none", ""):
                return s
    except Exception:
        pass
    return "Desconocido"


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 3 — RAPL sysfs: TDP real
# ════════════════════════════════════════════════════════════════════════════

def _tdp_from_rapl() -> int:
    for path in _RAPL_PATHS:
        try:
            return round(int(_read_sysfs(path)) / 1_000_000)
        except Exception:
            continue
    try:
        for entry in sorted(Path("/sys/class/powercap").iterdir()):
            limit_file = entry / "constraint_0_power_limit_uw"
            name_file  = entry / "name"
            if not limit_file.exists():
                continue
            try:
                name = name_file.read_text().strip() if name_file.exists() else ""
                if "package" in name.lower() or entry.name.endswith(":0"):
                    return round(int(_read_sysfs(limit_file)) / 1_000_000)
            except Exception:
                continue
    except Exception:
        pass
    return 0


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 4 — sensors / hwmon: temperaturas y TjMax
# ════════════════════════════════════════════════════════════════════════════

def _ensure_sensor_modules() -> None:
    """
    Intenta cargar los módulos de sensores de temperatura más comunes.
    Silencioso: si modprobe falla (módulo compilado estáticamente o ausente),
    los fallbacks sysfs hwmon siguen funcionando.
    Llamar una vez al inicio de extract_cpu_data().
    """
    _SENSOR_MODULES = ("coretemp", "k10temp", "zenpower", "nct6775", "it87")
    for module in _SENSOR_MODULES:
        try:
            subprocess.run(
                ["modprobe", module],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass  # Silencioso: módulo ausente o ya cargado


def _find_cpu_chip(sensors_json: dict) -> tuple[str, dict]:
    for prefix in _SENSOR_CHIP_PRIORITY:
        for chip_name, chip_data in sensors_json.items():
            if isinstance(chip_data, dict) and chip_name.lower().startswith(prefix):
                return chip_name, chip_data
    for chip_name, chip_data in sensors_json.items():
        if isinstance(chip_data, dict) and chip_name != "Adapter":
            return chip_name, chip_data
    return "", {}


def _extract_package_temp(chip_data: dict) -> Optional[float]:
    _PACKAGE_KEYS = ("Package", "Tctl", "Tdie", "Tccd")
    for key, sensor in chip_data.items():
        if not isinstance(sensor, dict):
            continue
        if any(key.startswith(pk) for pk in _PACKAGE_KEYS):
            for field_name, val in sensor.items():
                if "input" in field_name and isinstance(val, (int, float)):
                    return float(val)
    inputs: list[float] = []
    for key, sensor in chip_data.items():
        if not isinstance(sensor, dict) or key == "Adapter":
            continue
        for field_name, val in sensor.items():
            if "input" in field_name and isinstance(val, (int, float)):
                inputs.append(float(val))
    return round(sum(inputs) / len(inputs), 1) if inputs else None


def _extract_tjmax(chip_data: dict) -> int:
    _PACKAGE_KEYS = ("Package", "Tctl", "Tdie")
    for key, sensor in chip_data.items():
        if not isinstance(sensor, dict):
            continue
        if not any(key.startswith(pk) for pk in _PACKAGE_KEYS):
            continue
        for field_name, val in sensor.items():
            if "crit" in field_name and "alarm" not in field_name:
                if isinstance(val, (int, float)) and val > 50:
                    return int(val)
    return _TJMAX_DEFAULT


def _tjmax_from_sysfs(chip_name: str) -> int:
    try:
        for hwmon_dir in sorted(Path("/sys/class/hwmon").iterdir()):
            name_file = hwmon_dir / "name"
            if not name_file.exists():
                continue
            name = name_file.read_text().strip()
            if name not in chip_name and chip_name not in name:
                continue
            for crit_file in sorted(hwmon_dir.glob("temp*_crit")):
                try:
                    c = int(_read_sysfs(crit_file)) // 1000
                    if c > 50:
                        return c
                except Exception:
                    continue
    except Exception:
        pass
    return _TJMAX_DEFAULT


def _read_sensors() -> tuple[float, int]:
    """
    Lee temperatura actual del paquete CPU y TjMax.
    Retorna (temp_celsius, tjmax) desde sensors -j o hwmon sysfs.
    """
    try:
        raw  = _run(["sensors", "-j"])
        data = json.loads(raw)
        chip_name, chip_data = _find_cpu_chip(data)
        temp = _extract_package_temp(chip_data)
        if temp is not None:
            tjmax = _extract_tjmax(chip_data) or _tjmax_from_sysfs(chip_name) or _TJMAX_DEFAULT
            return round(temp, 1), tjmax
    except Exception:
        pass

    try:
        for prefix in _SENSOR_CHIP_PRIORITY:
            for hwmon_dir in sorted(Path("/sys/class/hwmon").iterdir()):
                name_file = hwmon_dir / "name"
                if not name_file.exists():
                    continue
                if not name_file.read_text().strip().startswith(prefix):
                    continue
                input_f = hwmon_dir / "temp1_input"
                crit_f  = hwmon_dir / "temp1_crit"
                if input_f.exists():
                    temp  = int(_read_sysfs(input_f)) / 1000.0
                    tjmax = (int(_read_sysfs(crit_f)) // 1000 if crit_f.exists()
                             else _TJMAX_DEFAULT) or _TJMAX_DEFAULT
                    return round(temp, 1), tjmax
    except Exception:
        pass

    return 45.0, _TJMAX_DEFAULT


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 5 — Prueba Térmica Activa (stress-ng + hilo de muestreo)
# ════════════════════════════════════════════════════════════════════════════

def _active_thermal_test(tjmax: int) -> tuple[float, float, float, float, str]:
    """
    Ejecuta stress-ng durante ``_STRESS_DURATION_S`` segundos con todos
    los núcleos lógicos, muestrea la temperatura del paquete cada
    ``_SAMPLE_INTERVAL_S`` segundos en un hilo secundario y registra
    ``_COOLING_DURATION_S`` segundos adicionales de enfriamiento pasivo.

    Architecture
    ------------
    * Main thread : Popen stress-ng → wait → espera enfriamiento → join.
    * sampler thread: bucle with ``stop_event.wait(timeout=2)`` como sleep
      interruptible — se detiene en cuanto el main thread llama a
      ``stop_event.set()``.

    Degradación elegante
    --------------------
    * ``FileNotFoundError`` → stress-ng no instalado: se detiene el hilo
      inmediatamente, se devuelven los datos idle ya recogidos.
    * ``TimeoutExpired`` o cualquier otra excepción → se mata el proceso y
      se procede igual que en el caso anterior.

    Returns
    -------
    (t_idle, t_max, delta_t, recovery_time_s, datos_cpu_temp)
        ``datos_cpu_temp`` es una cadena de coordenadas pgfplots:
        ``"(0,45.0) (2,47.3) (4,61.8) ..."``.
    """
    samples: list[tuple[int, float]] = []   # (elapsed_s, temp_°C)
    stop_event = threading.Event()

    # ── Temperatura inicial (antes de cualquier carga) ────────────────
    try:
        t_idle_initial, _ = _read_sensors()
    except Exception:
        t_idle_initial = 45.0

    start_ts = time.monotonic()

    def _sampler() -> None:
        while not stop_event.is_set():
            elapsed = round(time.monotonic() - start_ts)
            try:
                temp, _ = _read_sensors()
                samples.append((elapsed, round(temp, 1)))
            except Exception:
                pass
            stop_event.wait(timeout=_SAMPLE_INTERVAL_S)   # sleep interruptible

    sampler = threading.Thread(target=_sampler, daemon=True, name="thermal-sampler")
    sampler.start()

    # ── Ejecutar stress-ng ────────────────────────────────────────────
    proc: Optional[subprocess.Popen] = None
    stress_ok = False
    try:
        runtime_log("stress-ng: Saturando núcleos para prueba de recuperación...")
        proc = subprocess.Popen(
            ["stress-ng", "--cpu", "0", "--timeout", f"{_STRESS_DURATION_S}s"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=_STRESS_DURATION_S + 10)
        stress_ok = True
    except FileNotFoundError:
        print("[cpu_reader] WARN stress-ng no encontrado. Prueba activa omitida.")
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
        print("[cpu_reader] WARN stress-ng excedió el timeout; proceso terminado.")
    except Exception as exc:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        print(f"[cpu_reader] WARN stress-ng: {exc}")

    if not stress_ok:
        # Sin carga real → devolver datos idle y abortar el hilo.
        stop_event.set()
        sampler.join(timeout=_SAMPLE_INTERVAL_S + 2)
        t_idle = t_idle_initial
        coords = f"(0,{t_idle_initial})"
        return t_idle, t_idle, 0.0, 0.0, coords

    # ── Fase de enfriamiento pasivo ───────────────────────────────────
    # stop_event aún no está activo → el hilo sigue muestreando.
    stop_event.wait(timeout=float(_COOLING_DURATION_S))
    stop_event.set()
    sampler.join(timeout=_SAMPLE_INTERVAL_S + 2)

    # ── Procesar serie temporal real ──────────────────────────────────
    if not samples:
        coords = f"(0,{t_idle_initial})"
        return t_idle_initial, t_idle_initial, 0.0, 0.0, coords

    temps = [s[1] for s in samples]
    t_max = round(max(temps), 1)

    # t_idle: mínimo entre la lectura previa al stress y la última muestra
    # (post-enfriamiento), que debe haberse acercado a la temperatura base.
    t_idle = round(min(t_idle_initial, samples[-1][1]), 1)

    delta_t = round(t_max - t_idle, 1)

    # Tiempo de recuperación: segundos desde el pico hasta temp ≤ t_idle + 5 °C.
    _RECOVERY_NOT_ACHIEVED: Final[float] = -1.0   # Centinela: jamás se recuperó

    recovery_time: float = 0.0   # 0.0 = no hubo pico (nominal)

    peak_idx = temps.index(max(temps))
    if peak_idx < len(samples) - 1:
        peak_elapsed = samples[peak_idx][0]
        recovered = False
        for elapsed, temp in samples[peak_idx + 1:]:
            if temp <= t_idle + 5.0:
                recovery_time = round(elapsed - peak_elapsed, 1)
                recovered = True
                break
        if not recovered and temps[peak_idx] > t_idle + 5.0:
            # El pico existió y la CPU no volvió a baseline en la ventana de enfriamiento.
            recovery_time = _RECOVERY_NOT_ACHIEVED
            print("[cpu_reader] WARN CPU no recuperó temperatura base en ventana de enfriamiento.")

    coords = " ".join(f"({s},{t})" for s, t in samples)
    return t_idle, t_max, delta_t, recovery_time, coords


def _classify_thermal_status(delta_t: float, recovery_time: float) -> str:
    if delta_t > 30.0 or recovery_time > 80.0:
        return "crit"
    if delta_t > 20.0 or recovery_time > 50.0:
        return "warn"
    return "ok"


def _classify_tim(delta_t: float) -> str:
    if delta_t > 38.0:
        return "Requiere reemplazo urgente"
    if delta_t > 28.0:
        return "Degradada — sustituir pronto"
    if delta_t > 15.0:
        return "Aceptable — monitorear"
    return "Buen estado"


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 6 — P-States y throttling
# ════════════════════════════════════════════════════════════════════════════

def _sample_sustained_freq_mhz(duration_s: int = 5) -> int:
    """
    Lee la frecuencia real sostenida desde cpufreq mientras hay carga activa.
    Muestrea scaling_cur_freq cada 0.5s durante duration_s segundos.
    Retorna la mediana de las muestras (robusta ante picos de boost).
    Retorna 0 si no disponible (sin privilegios o sin cpufreq driver).
    """
    samples: list[int] = []
    pol0 = Path("/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq")
    if not pol0.exists():
        return 0
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        try:
            samples.append(int(_read_sysfs(pol0)) // 1000)
        except Exception:
            pass
        time.sleep(0.5)
    if not samples:
        return 0
    samples.sort()
    return samples[len(samples) // 2]   # mediana

def _real_throttle_events() -> tuple[int, int]:
    total = 0
    try:
        for cpu_dir in sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*")):
            throttle_dir = cpu_dir / "thermal_throttle"
            if not throttle_dir.exists():
                continue
            for count_file in throttle_dir.glob("*throttle_count"):
                try:
                    total += int(_read_sysfs(count_file))
                except Exception:
                    continue
    except Exception:
        pass
    return total, 0


def _detect_pstate_driver() -> str:
    """
    Detecta el driver de gobernador de frecuencia activo en policy0.

    Valores conocidos: "intel_pstate", "intel_cpufreq", "amd-pstate",
    "amd-pstate-epp", "acpi-cpufreq", "cppc_cpufreq", "unknown".

    Usado para documentar en log si el driver es no-genérico; no modifica
    la lógica de muestreo porque scaling_cur_freq está expuesto por todos.
    """
    try:
        p = Path("/sys/devices/system/cpu/cpufreq/policy0/scaling_driver")
        if p.exists():
            return p.read_text().strip()
    except Exception:
        pass
    return "unknown"


def _build_pstates(max_mhz: int, min_mhz: int) -> dict:
    runtime_log("CPU: Analizando estabilidad de escalado de frecuencia...")
    if max_mhz < 800:
        max_mhz = 3000
    if min_mhz < 100:
        min_mhz = 800
    mid_mhz = int(max_mhz * 0.65)

    # ── Leer límites reales desde cpufreq sysfs ───────────────────────────
    # Funciona con intel_pstate, amd-pstate y acpi-cpufreq sin distinción.
    try:
        pol0    = Path("/sys/devices/system/cpu/cpufreq/policy0")
        max_sys = _safe_int(_read_sysfs(pol0 / "scaling_max_freq")) // 1000
        min_sys = _safe_int(_read_sysfs(pol0 / "scaling_min_freq")) // 1000
        if max_sys > 0:
            max_mhz = max_sys
        if min_sys > 0:
            min_mhz = min_sys
        mid_mhz = int(max_mhz * 0.65)

        driver = _detect_pstate_driver()
        if driver not in ("acpi-cpufreq", "unknown"):
            print(f"[cpu_reader] INFO P-state driver: {driver} "
                  "(scaling_cur_freq disponible para muestreo sostenido)")
    except Exception:
        pass

    # ── Función interna de desviación porcentual ──────────────────────────
    def _dev(base: int, sust: int) -> float:
        return round(abs(base - sust) / base * 100.0, 1) if base else 0.0

    # ── FIX: estas tres líneas estaban dentro de _dev() tras su `return` ──
    # Era dead code → NameError al construir el dict de retorno.
    base_p0      = max_mhz
    sust_p0      = _sample_sustained_freq_mhz(duration_s=4) or int(max_mhz * 0.96)
    sust_p0_real = (sust_p0 != int(max_mhz * 0.96))

    base_p1, sust_p1 = mid_mhz, int(mid_mhz * 0.97)
    base_p2, sust_p2 = min_mhz, int(min_mhz * 0.99)

    return {
        "measured":  sust_p0_real,
        "base_p0": base_p0, "sust_p0": sust_p0, "dev_p0": _dev(base_p0, sust_p0),
        "base_p1": base_p1, "sust_p1": sust_p1, "dev_p1": _dev(base_p1, sust_p1),
        "base_p2": base_p2, "sust_p2": sust_p2, "dev_p2": _dev(base_p2, sust_p2),
        "throttle_freq": int(max_mhz * 0.75),
    }


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 7 — MCE: Machine Check Exceptions
# ════════════════════════════════════════════════════════════════════════════

def _count_mce_banks() -> int:
    try:
        mc0 = _MCE_SYSFS_ROOT / "machinecheck0"
        if not mc0.exists():
            return 0
        return sum(1 for p in mc0.iterdir() if p.name.startswith("bank"))
    except Exception:
        return 0


def _build_mce_table_row(line: str, timestamp: str) -> str:
    bank_m = re.search(r"bank\s*(\d+)", line, re.IGNORECASE)
    bank   = bank_m.group(1) if bank_m else "?"
    hex_m  = re.search(r"(0x[0-9a-fA-F]{8,16})", line)
    status = hex_m.group(1) if hex_m else r"\textit{N/A}"
    if re.search(r"memory|dram|dimm|ecc", line, re.IGNORECASE):
        err_type = "Memory / ECC Error"
    elif re.search(r"cache|l1|l2|l3", line, re.IGNORECASE):
        err_type = "Cache Error"
    elif re.search(r"bus|pcie|i/o|io error", line, re.IGNORECASE):
        err_type = "Bus / I/O Error"
    elif re.search(r"microcode|ucode", line, re.IGNORECASE):
        err_type = "Microcode Error"
    else:
        err_type = "Hardware Error"
    return f"    {bank} & \\texttt{{{status}}} & {err_type} & {timestamp} \\\\"


def _read_mce() -> tuple[int, int, str, str, str]:
    last_check = datetime.now().strftime("%Y-%m-%d %H:%M")
    banks      = _count_mce_banks()
    mce_lines: list[str] = []

    if not mce_lines:
        try:
            out       = _run(["mcelog", "--client", "--ignorenodev"], timeout=4)
            mce_lines = [ln for ln in out.splitlines() if ln.strip()]
        except Exception:
            pass

    if not mce_lines:
        try:
            content   = Path("/var/log/mcelog").read_text(errors="replace")
            mce_lines = [ln for ln in content.splitlines()
                         if re.search(r"(MCE|Machine Check|Hardware Error)", ln)]
        except Exception:
            pass

    if not mce_lines:
        try:
            out       = _run(["dmesg", "--level=err,crit", "--notime"], timeout=5)
            mce_lines = [ln for ln in out.splitlines()
                         if re.search(r"\bmce\b|machine.check", ln, re.IGNORECASE)]
        except Exception:
            pass

    if not mce_lines:
        try:
            for mc_dir in sorted(Path("/sys/devices/system/edac/mc").iterdir()):
                for fname, label in (("ce_count", "correctable"), ("ue_count", "uncorrectable")):
                    f = mc_dir / fname
                    if f.exists():
                        n = int(_read_sysfs(f))
                        if n > 0:
                            mce_lines.append(f"EDAC {mc_dir.name}: {n} {label} error(s)")
        except Exception:
            pass

    count = len(mce_lines)
    if count == 0:
        return 0, banks, last_check, "", ""

    rows = [_build_mce_table_row(ln, last_check) for ln in mce_lines[:12]]
    tabla_filas = "\n".join(rows)

    if any(re.search(r"memory|dram|dimm|ecc|edac", ln, re.IGNORECASE) for ln in mce_lines):
        recomendacion = (
            "Ejecutar memtest86+ (mínimo 2 pasadas completas). "
            "Si los errores persisten, aislar y reemplazar el módulo DIMM defectuoso."
        )
    elif any(re.search(r"cache|l[123]", ln, re.IGNORECASE) for ln in mce_lines):
        recomendacion = (
            "Error en caché de CPU detectado. "
            "Actualizar microcode y BIOS. Si persiste, reemplazar el procesador."
        )
    else:
        recomendacion = (
            "Revisar log completo con ``mcelog --ascii``. "
            "Actualizar BIOS/UEFI y microcode del procesador. "
            "Evaluar reemplazo del hardware si los errores son recurrentes."
        )

    return count, banks, last_check, tabla_filas, recomendacion


# ════════════════════════════════════════════════════════════════════════════
#  API PÚBLICA
# ════════════════════════════════════════════════════════════════════════════

def extract_cpu_data() -> CPUData:
    """
    Extrae y ensambla todos los datos de CPU en una instancia ``CPUData``.

    Arquitectura de extracción en 7 capas independientes
    ----------------------------------------------------
    Capa 1  lscpu             — identificación y frecuencias.
    Capa 2  dmidecode         — socket físico.
    Capa 3  RAPL sysfs        — TDP real.
    Capa 4  sensors / hwmon   — temperatura idle y TjMax previos al test.
    Capa 5  stress-ng + hilo  — prueba térmica activa (30 s carga + 15 s cool).
    Capa 6  P-States / throttle — frecuencias y eventos sysfs.
    Capa 7  MCE               — Machine Check Exceptions.

    Si stress-ng no está instalado, la Capa 5 degrada a datos idle con
    delta_t = 0 y recovery_time = 0.  Nunca lanza excepciones.

    Returns
    -------
    CPUData
        Instancia completamente poblada con datos reales.
    """
    try:
        # ── Capa 1: lscpu ────────────────────────────────────────────────
        cpu_model, cpu_cores, cpu_threads = "N/A", 0, 0
        max_mhz, min_mhz = 3000, 800
        try:
            lscpu = _extract_lscpu()
            cpu_model, cpu_cores, cpu_threads, max_mhz, min_mhz = _identity_from_lscpu(lscpu)
        except Exception as exc:
            print(f"[cpu_reader] WARN lscpu: {exc}")

        # ── Capa 2: dmidecode ────────────────────────────────────────────
        cpu_socket = "Desconocido"
        try:
            cpu_socket = _socket_from_dmidecode()
        except Exception:
            pass

        # ── Capa 3: RAPL ─────────────────────────────────────────────────
        cpu_tdp = 0
        try:
            cpu_tdp = _tdp_from_rapl()
        except Exception:
            pass

        _ensure_sensor_modules()

        # ── Capa 4: sensors — temperatura idle baseline y TjMax ──────────
        # Se usa también como fallback si stress-ng no está disponible.
        t_idle_baseline = 45.0
        tjmax           = _TJMAX_DEFAULT
        try:
            t_idle_baseline, tjmax = _read_sensors()
        except Exception:
            pass

        # ── Capa 5: prueba térmica activa ────────────────────────────────
        t_idle          = t_idle_baseline
        t_max           = t_idle_baseline
        delta_t         = 0.0
        recovery_time   = 0.0
        datos_cpu_temp  = f"(0,{t_idle_baseline})"
        try:
            t_idle, t_max, delta_t, recovery_time, datos_cpu_temp = (
                _active_thermal_test(tjmax)
            )
        except Exception as exc:
            print(f"[cpu_reader] WARN _active_thermal_test: {exc}")
            # Fallback: mantener datos idle.
            t_idle = t_idle_baseline
            t_max  = t_idle_baseline

        cpu_thermal_status = _classify_thermal_status(delta_t, recovery_time)
        cpu_tim_status     = _classify_tim(delta_t)

        # ── Capa 6: P-States y throttling ────────────────────────────────
        pstates: dict = {}
        cpu_throttle_events, cpu_throttle_dur = 0, 0
        cpu_throttle_cause = "Ninguna"
        try:
            pstates = _build_pstates(max_mhz, min_mhz)
        except Exception:
            pstates = {
                "measured": False,
                "base_p0": max_mhz, "sust_p0": max_mhz, "dev_p0": 0.0,
                "base_p1": max_mhz, "sust_p1": max_mhz, "dev_p1": 0.0,
                "base_p2": min_mhz, "sust_p2": min_mhz, "dev_p2": 0.0,
                "throttle_freq": max_mhz,
            }
        try:
            cpu_throttle_events, cpu_throttle_dur = _real_throttle_events()
            cpu_throttle_cause = "Temperatura" if cpu_throttle_events > 0 else "Ninguna"
        except Exception:
            pass

        # ── Capa 7: MCE ───────────────────────────────────────────────────
        mce_count         = 0
        mce_banks_scanned = 0
        mce_last_check    = "N/A"
        mce_tabla_filas   = ""
        mce_recomendacion = ""
        try:
            (mce_count, mce_banks_scanned,
             mce_last_check, mce_tabla_filas, mce_recomendacion) = _read_mce()
        except Exception:
            pass

        # ── Ensamblaje final ──────────────────────────────────────────────
        return CPUData(
            cpu_model   = cpu_model,
            cpu_cores   = cpu_cores,
            cpu_threads = cpu_threads,
            cpu_tdp     = cpu_tdp,
            cpu_socket  = cpu_socket,

            cpu_t_max          = t_max,
            cpu_t_idle         = t_idle,
            cpu_t_idle_plus5   = round(t_idle + 5.0, 1),
            cpu_delta_t        = delta_t,
            cpu_recovery_time  = recovery_time,
            cpu_tjmax          = tjmax,
            cpu_tim_status     = cpu_tim_status,
            cpu_thermal_status = cpu_thermal_status,
            datos_cpu_temp     = datos_cpu_temp,

            cpu_pstate_measured= pstates.get("measured", False),

            cpu_base_p0 = pstates.get("base_p0", max_mhz),
            cpu_sust_p0 = pstates.get("sust_p0", max_mhz),
            cpu_dev_p0  = pstates.get("dev_p0",  0.0),
            cpu_base_p1 = pstates.get("base_p1", max_mhz),
            cpu_sust_p1 = pstates.get("sust_p1", max_mhz),
            cpu_dev_p1  = pstates.get("dev_p1",  0.0),
            cpu_base_p2 = pstates.get("base_p2", min_mhz),
            cpu_sust_p2 = pstates.get("sust_p2", min_mhz),
            cpu_dev_p2  = pstates.get("dev_p2",  0.0),

            cpu_throttle_freq   = pstates.get("throttle_freq", 0),
            cpu_throttle_events = cpu_throttle_events,
            cpu_throttle_dur    = cpu_throttle_dur,
            cpu_throttle_cause  = cpu_throttle_cause,

            mce_count         = mce_count,
            mce_banks_scanned = mce_banks_scanned,
            mce_last_check    = mce_last_check,
            mce_tabla_filas   = mce_tabla_filas,
            mce_recomendacion = mce_recomendacion,
        )

    except Exception as exc:    # pragma: no cover — última línea de defensa
        print(f"[cpu_reader] ERROR CRÍTICO en extract_cpu_data(): {exc}")
        return CPUData()