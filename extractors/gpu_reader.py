"""
extractors/gpu_reader.py
========================
Extractor de hardware para el subsistema de GPU de probe.tex.

CHANGELOG v1.2
--------------
* FIX: _parse_lspci_mm() usaba line.split("\t") que no coincidía con el
  formato real de lspci -mm (campos separados por espacios, entrecomillados).
  Reemplazado por shlex.split(line) que parsea correctamente cualquier
  output de lspci sin importar el separador o la presencia de comas en
  los nombres de vendor/device.

* CLEANUP: Eliminada la constante _GUI_PROCESS_PATTERN (pgrep nunca se
  usó; _is_gui_active() escanea /proc directamente desde v1.1).

* FIX: En _run_vram_stress_test(), la rama "amdgpu" discreta (con
  gpu_memtest) ahora colapsa explícitamente hacia memtester si
  gpu_memtest no está disponible, en lugar de depender del fallthrough
  implícito al bloque "i915/xe/amdgpu". El comportamiento era correcto
  pero el control de flujo resultaba difícil de auditar.

Fuentes de datos
----------------
lspci -mm / -v / -k  →  identificación, driver, PCIe LnkCap/LnkSta
/sys/class/drm/      →  nodo DRM activo, VRAM (amdgpu), hwmon
nvidia-smi           →  VRAM y temperatura (driver propietario NVIDIA)
stress-ng --matrix   →  prueba térmica activa (20 s + 10 s cooling)
gpu_memtest          →  integridad VRAM AMD discreto (ROCm)
cuda-memtest         →  integridad VRAM NVIDIA
memtester            →  proxy para iGPU Intel / AMD APU (DRAM compartida)

stdlib + shlex únicamente (sin dependencias externas nuevas).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Final, Optional

from core.models import GPUData
from tui import runtime_log


# ════════════════════════════════════════════════════════════════════════════
#  CONSTANTES
# ════════════════════════════════════════════════════════════════════════════

_LSPCI_TIMEOUT:   int = 5
_MODINFO_TIMEOUT: int = 4
_NSMI_TIMEOUT:    int = 5

_VRAM_TEST_TIMEOUT_S: Final[int] = 300

_GPU_STRESS_DURATION_S:  int   = 20
_GPU_COOLING_DURATION_S: int   = 10
_GPU_SAMPLE_INTERVAL_S:  float = 2.0

_DELTA_WARN: float = 20.0
_DELTA_CRIT: float = 35.0

_TEMP_MIN: float =  10.0
_TEMP_MAX: float = 110.0

_DRM_ROOT = Path("/sys/class/drm")

_PCIE_SPEED_TO_GEN: dict[str, int] = {
    "2.5 GT/s": 1, "5.0 GT/s": 2, "8.0 GT/s": 3,
    "16.0 GT/s": 4, "32.0 GT/s": 5, "64.0 GT/s": 6,
}

_PCIE_BW_TABLE: dict[tuple[int, int], str] = {
    (1, 16): "4 GB/s",  (1, 8): "2 GB/s",  (1, 4): "1 GB/s",
    (2, 16): "8 GB/s",  (2, 8): "4 GB/s",  (2, 4): "2 GB/s",
    (3, 16): "16 GB/s", (3, 8): "8 GB/s",  (3, 4): "4 GB/s",
    (4, 16): "32 GB/s", (4, 8): "16 GB/s", (4, 4): "8 GB/s",
    (5, 16): "64 GB/s", (5, 8): "32 GB/s", (5, 4): "16 GB/s",
    (6, 16): "128 GB/s",(6, 8): "64 GB/s", (6, 4): "32 GB/s",
}


# ════════════════════════════════════════════════════════════════════════════
#  UTILIDADES INTERNAS
# ════════════════════════════════════════════════════════════════════════════

def _run(cmd: list[str], timeout: int = _LSPCI_TIMEOUT) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd!r} rc={r.returncode} stderr={r.stderr.strip()!r}")
    return r.stdout


def _sysfs(path: Path | str) -> str:
    return Path(path).read_text().strip()


def _safe_int(v: object, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return default


def _safe_float(v: object, default: float = 0.0) -> float:
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return default


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 1 — lspci: identificación de GPU y driver
# ════════════════════════════════════════════════════════════════════════════

def _parse_lspci_mm() -> list[dict[str, str]]:
    """
    Parsea ``lspci -mm`` para extraer GPUs.

    Formato real de lspci -mm
    --------------------------
    Los campos están separados por espacios y encerrados en comillas:

        03:00.0 "VGA compatible controller" "Advanced Micro Devices, Inc. [AMD/ATI]" "Navi 23 [RX 6600]" ...

    FIX v1.2: la versión anterior usaba line.split("\\t") asumiendo
    separación por tabulaciones, lo que no coincide con la salida real de
    lspci en ninguna distribución Linux conocida. Esto causaba que
    len(parts) < 4 siempre fuera True y que ninguna GPU se detectara.

    shlex.split() maneja correctamente campos con espacios dentro de
    comillas, comas en nombres de fabricante, y cualquier variante
    regional del output de lspci.
    """
    raw  = _run(["lspci", "-mm"])
    gpus: list[dict[str, str]] = []

    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            # Línea con comillas desbalanceadas (raro, pero posible en
            # nombres de dispositivo con caracteres especiales).
            continue

        if len(parts) < 4:
            continue

        bdf, cls, vendor, device = parts[0], parts[1], parts[2], parts[3]
        cls_lower = cls.lower()
        is_gpu = (
            "vga"     in cls_lower
            or "display" in cls_lower
            or "3d"      in cls_lower
            or any(c in cls_lower for c in ("0300", "0301", "0302", "0380"))
        )
        if is_gpu:
            gpus.append({"bdf": bdf, "class": cls, "vendor": vendor, "device": device})

    return gpus


def _parse_lspci_verbose(bdf: str) -> dict[str, str]:
    """Extrae driver, LnkCap y LnkSta desde ``lspci -v -s <bdf>``."""
    result: dict[str, str] = {}
    try:
        raw = _run(["lspci", "-v", "-s", bdf])
        m = re.search(r"Kernel driver in use:\s+(\S+)", raw)
        if m:
            result["driver"] = m.group(1)
        m = re.search(r"LnkCap:.*?Speed\s+([\d.]+\s*GT/s).*?Width\s+x(\d+)", raw)
        if m:
            result["lnkcap_speed"] = m.group(1).strip()
            result["lnkcap_width"] = m.group(2)
        m = re.search(r"LnkSta:.*?Speed\s+([\d.]+\s*GT/s).*?Width\s+x(\d+)", raw)
        if m:
            result["lnksta_speed"] = m.group(1).strip()
            result["lnksta_width"] = m.group(2)
    except Exception:
        pass
    return result


def _driver_version(driver_name: str) -> str:
    try:
        raw = _run(["modinfo", driver_name], timeout=_MODINFO_TIMEOUT)
        m   = re.search(r"^version:\s+(\S+)", raw, re.MULTILINE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "N/A"


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 2 — Localización del nodo DRM activo
# ════════════════════════════════════════════════════════════════════════════

def _find_drm_card(bdf: str) -> Optional[Path]:
    bdf_short = bdf.split(":")[-2] + ":" + bdf.split(":")[-1] if ":" in bdf else bdf
    try:
        cards = sorted(
            d for d in _DRM_ROOT.iterdir()
            if d.name.startswith("card") and "-" not in d.name
        )
    except Exception:
        return None

    for card in cards:
        try:
            if bdf_short in str((card / "device").resolve()) or bdf in str((card / "device").resolve()):
                return card
        except Exception:
            continue

    return cards[0] if cards else None


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 3 — VRAM
# ════════════════════════════════════════════════════════════════════════════

def _read_vram_amdgpu(card: Path) -> tuple[int, str]:
    vram_gb   = 0
    vram_type = "N/A"
    try:
        raw_bytes = int(_sysfs(card / "device" / "mem_info_vram_total"))
        vram_gb   = max(0, round(raw_bytes / 1_000_000_000))
    except Exception:
        pass
    try:
        t = _sysfs(card / "device" / "mem_info_vram_type").strip()
        if t and t.lower() not in ("unknown", "none", "0"):
            vram_type = t
    except Exception:
        pass
    return vram_gb, vram_type


def _read_vram_nvidia() -> tuple[int, str]:
    try:
        raw = _run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            timeout=_NSMI_TIMEOUT,
        )
        mib = _safe_int(raw.strip().split("\n")[0].strip())
        if mib > 0:
            return max(1, round(mib / 1024)), "GDDR"
    except Exception:
        pass
    return 0, "N/A"


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 4 — Temperatura: hwmon del dispositivo DRM
# ════════════════════════════════════════════════════════════════════════════

def _find_gpu_hwmon(card: Path) -> Optional[Path]:
    try:
        dirs = sorted((card / "device" / "hwmon").iterdir())
        if dirs:
            return dirs[0]
    except Exception:
        pass
    # Fallback APU: k10temp / coretemp (silicio compartido)
    try:
        for hwmon_dir in sorted(Path("/sys/class/hwmon").iterdir()):
            name_f = hwmon_dir / "name"
            if name_f.exists() and name_f.read_text().strip().lower() in ("k10temp", "coretemp", "zenpower"):
                return hwmon_dir
    except Exception:
        pass
    return None


def _read_temp_millic(hwmon: Path, filename: str) -> Optional[float]:
    try:
        raw = int(_sysfs(hwmon / filename))
        c   = raw / 1000.0
        if _TEMP_MIN <= c <= _TEMP_MAX:
            return round(c, 1)
    except Exception:
        pass
    return None


def _read_temp_limit(hwmon: Path) -> int:
    for fname in ("temp2_crit", "temp1_crit", "temp1_emergency"):
        try:
            c = int(_sysfs(hwmon / fname)) // 1000
            if 70 <= c <= 120:
                return c
        except Exception:
            continue
    return 110


def _classify_hotspot(delta: float, t_edge: float, t_hotspot: float) -> str:
    if t_edge == 0.0 and t_hotspot == 0.0:
        return "info"
    if delta >= _DELTA_CRIT:
        return "crit"
    if delta >= _DELTA_WARN:
        return "warn"
    return "ok"


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 4b — Detección de entorno gráfico
# ════════════════════════════════════════════════════════════════════════════

def _is_gui_active() -> bool:
    """
    Detecta compositor Wayland/X11 activo sin depender de pgrep.

    Capas (en orden de coste):
    1. Variables de entorno del display server (O(1)).
    2. XDG_SESSION_TYPE.
    3. Escaneo de /proc/<pid>/cmdline contra lista de compositores conocidos.

    Retorna False por defecto (TTY bare-metal), True solo con confirmación
    positiva. KMS/DRM activo no implica GUI.
    """
    if os.environ.get("WAYLAND_DISPLAY", "").strip():
        return True
    if os.environ.get("DISPLAY", "").strip():
        return True
    if os.environ.get("XDG_SESSION_TYPE", "").lower().strip() in ("x11", "wayland", "mir"):
        return True

    _COMPOSITOR_BASENAMES: frozenset[bytes] = frozenset({
        b"Xorg", b"Xwayland", b"Xvfb",
        b"sway", b"kwin_wayland", b"kwin_x11",
        b"mutter", b"gnome-shell", b"plasmashell",
        b"weston", b"hyprland", b"niri", b"river",
        b"openbox", b"labwc", b"wayfire",
    })
    try:
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                raw      = (pid_dir / "cmdline").read_bytes()
                basename = raw.split(b"\x00", 1)[0].rsplit(b"/", 1)[-1]
                if basename in _COMPOSITOR_BASENAMES:
                    return True
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
    except (PermissionError, FileNotFoundError):
        pass
    return False


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 4c — Test de integridad VRAM
# ════════════════════════════════════════════════════════════════════════════

def _parse_gpu_memtest_errors(output: str) -> int:
    total = sum(int(m.group(1)) for m in re.finditer(r"ERROR:\s+(\d+)\s+bit\s+error", output, re.IGNORECASE))
    if total == 0:
        total = len(re.findall(r"\bFAILED\b", output, re.IGNORECASE))
    return total


def _parse_cuda_memtest_errors(output: str) -> int:
    m = re.search(r"(\d+)\s+error", output, re.IGNORECASE)
    return int(m.group(1)) if m else len(re.findall(r"\bError\b", output))


def _run_vram_stress_test(
    vram_total_gb: int,
    driver_name:   str,
) -> tuple[int, int, int, int, bool]:
    """
    Ejecuta test de integridad de VRAM en TTY pura confirmada.

    Cadena de herramientas por driver
    ----------------------------------
    amdgpu discreto → gpu_memtest (ROCm).
      Si gpu_memtest no está instalado o falla, el flujo cae
      explícitamente hacia memtester (mismo path que APU).

    nvidia           → cuda-memtest.
      Si no está disponible, retorna tested=False (sin fallback:
      memtester no accede a VRAM NVIDIA dedicada).

    i915 / xe / amdgpu APU → memtester sobre DRAM compartida.
      Para las APUs Ryzen (amdgpu sin VRAM dedicada, vram_total_gb == 0)
      este es el único path de integridad disponible. La cobertura es
      sobre el pool de DRAM que el driver asigna vía GTT, no sobre
      VRAM dedicada (que no existe).

    FIX v1.2: el fallback amdgpu → memtester ahora es explícito en
    lugar de depender del orden de evaluación de bloques if/elif.
    """
    d = driver_name.lower()

    # ── AMD discreto con ROCm ──────────────────────────────────────────────
    if "amdgpu" in d and vram_total_gb > 0:
        try:
            r = subprocess.run(["gpu_memtest"], capture_output=True, text=True,
                               timeout=_VRAM_TEST_TIMEOUT_S)
            errors = _parse_gpu_memtest_errors(r.stdout + r.stderr)
            return 0, 0, errors, max(vram_total_gb, 1), True
        except FileNotFoundError:
            print("[gpu_reader] INFO gpu_memtest no disponible → fallback a memtester.")
        except subprocess.TimeoutExpired:
            print(f"[gpu_reader] WARN gpu_memtest superó {_VRAM_TEST_TIMEOUT_S} s → fallback.")
        except Exception as exc:
            print(f"[gpu_reader] WARN gpu_memtest: {exc} → fallback.")
        # Fallback explícito: caer al bloque memtester de abajo.

    # ── NVIDIA cuda-memtest ───────────────────────────────────────────────
    if "nvidia" in d:
        runtime_log("CUDA: Lanzando test de integridad de VRAM...")
        try:
            r = subprocess.run(["cuda-memtest"], capture_output=True, text=True,
                               timeout=_VRAM_TEST_TIMEOUT_S)
            errors = _parse_cuda_memtest_errors(r.stdout + r.stderr)
            return 0, 0, errors, max(vram_total_gb, 1), True
        except FileNotFoundError:
            print("[gpu_reader] INFO cuda-memtest no disponible.")
        except subprocess.TimeoutExpired:
            print(f"[gpu_reader] WARN cuda-memtest superó {_VRAM_TEST_TIMEOUT_S} s.")
        except Exception as exc:
            print(f"[gpu_reader] WARN cuda-memtest: {exc}")
        return 0, 0, 0, 0, False  # Sin fallback para NVIDIA

    # ── Intel iGPU / Xe / AMD APU → memtester sobre DRAM compartida ───────
    # Alcanzado por:
    #   - i915 / xe (siempre)
    #   - amdgpu con vram_total_gb == 0 (APU, sin VRAM dedicada)
    #   - amdgpu discreto cuando gpu_memtest no estaba disponible
    if "i915" in d or "xe" in d or "amdgpu" in d:
        test_mb = min(max(vram_total_gb * 1024, 256), 1024)
        try:
            r = subprocess.run(
                ["memtester", f"{test_mb}M", "1"],
                capture_output=True, text=True, timeout=_VRAM_TEST_TIMEOUT_S,
            )
            failures  = len(re.findall(r"\bFAILURE\b", r.stdout + r.stderr, re.IGNORECASE))
            tested_gb = max(test_mb // 1024, 1)
            return failures, 0, 0, tested_gb, True
        except FileNotFoundError:
            print("[gpu_reader] INFO memtester no disponible para proxy iGPU/APU.")
        except subprocess.TimeoutExpired:
            print(f"[gpu_reader] WARN memtester iGPU/APU superó {_VRAM_TEST_TIMEOUT_S} s.")
        except Exception as exc:
            print(f"[gpu_reader] WARN memtester iGPU/APU: {exc}")

    return 0, 0, 0, 0, False


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 5 — Prueba Térmica Activa (stress-ng + hilo de muestreo)
# ════════════════════════════════════════════════════════════════════════════

def _read_temps_nvidia() -> tuple[float, float]:
    try:
        raw = _run(
            ["nvidia-smi", "--query-gpu=temperature.gpu,temperature.memory",
             "--format=csv,noheader,nounits"],
            timeout=_NSMI_TIMEOUT,
        )
        parts     = [p.strip() for p in raw.strip().split(",")]
        t_edge    = _safe_float(parts[0]) if parts else 0.0
        mem_raw   = parts[1].upper() if len(parts) > 1 else ""
        t_hotspot = (_safe_float(parts[1]) if mem_raw and "N/A" not in mem_raw else t_edge)
        if _TEMP_MIN <= t_edge <= _TEMP_MAX:
            return round(t_edge, 1), round(t_hotspot, 1)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[gpu_reader] WARN _read_temps_nvidia: {exc}")
    return 0.0, 0.0


def _active_thermal_test_gpu(hwmon: Path) -> tuple[float, float]:
    """
    Genera carga matricial con stress-ng y registra picos de T_edge / T_hotspot.
    Retorna temperaturas idle si stress-ng no está disponible.
    """
    t_edge_idle    = _read_temp_millic(hwmon, "temp1_input") or 0.0
    t_hotspot_idle = _read_temp_millic(hwmon, "temp2_input") or 0.0

    samples_edge:    list[float] = [t_edge_idle]    if t_edge_idle    > 0.0 else []
    samples_hotspot: list[float] = [t_hotspot_idle] if t_hotspot_idle > 0.0 else []

    stop_event = threading.Event()

    def _sampler() -> None:
        while not stop_event.is_set():
            te = _read_temp_millic(hwmon, "temp1_input")
            th = _read_temp_millic(hwmon, "temp2_input")
            if te is not None:
                samples_edge.append(te)
            if th is not None:
                samples_hotspot.append(th)
            stop_event.wait(timeout=_GPU_SAMPLE_INTERVAL_S)

    sampler = threading.Thread(target=_sampler, daemon=True, name="gpu-thermal-sampler")
    sampler.start()

    proc: Optional[subprocess.Popen] = None
    stress_ok = False
    try:
        runtime_log("GPU: Iniciando carga matricial para medición de Hotspot...")
        proc = subprocess.Popen(
            ["stress-ng", "--matrix", "0", "--timeout", f"{_GPU_STRESS_DURATION_S}s"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=_GPU_STRESS_DURATION_S + 15)
        stress_ok = True
    except FileNotFoundError:
        print("[gpu_reader] WARN stress-ng no encontrado. Usando lectura idle.")
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
    except Exception as exc:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        print(f"[gpu_reader] WARN stress-ng GPU: {exc}")

    if not stress_ok:
        stop_event.set()
        sampler.join(timeout=_GPU_SAMPLE_INTERVAL_S + 2)
        return t_edge_idle, t_hotspot_idle

    stop_event.wait(timeout=float(_GPU_COOLING_DURATION_S))
    stop_event.set()
    sampler.join(timeout=_GPU_SAMPLE_INTERVAL_S + 2)

    t_edge_peak    = round(max(samples_edge),    1) if samples_edge    else 0.0
    t_hotspot_peak = round(max(samples_hotspot), 1) if samples_hotspot else 0.0
    return t_edge_peak, t_hotspot_peak


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 6 — PCIe desde sysfs
# ════════════════════════════════════════════════════════════════════════════

def _pcie_speed_str_to_gen(speed_str: str) -> int:
    m = re.search(r"([\d.]+)\s*GT/s", speed_str, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        if v <= 2.5:  return 1
        if v <= 5.0:  return 2
        if v <= 8.0:  return 3
        if v <= 16.0: return 4
        if v <= 32.0: return 5
        return 6
    return 0


def _read_pcie_sysfs(card: Path) -> dict[str, object]:
    result: dict[str, object] = {
        "gen_active": 0, "lanes_active": 0,
        "gen_max":    0, "lanes_max":    0,
        "bw_active":  "N/A", "bw_max":  "N/A",
    }
    dev = card / "device"
    for key, sysfs_file, converter in (
        ("gen_active",   "current_link_speed", _pcie_speed_str_to_gen),
        ("lanes_active", "current_link_width", _safe_int),
        ("gen_max",      "max_link_speed",     _pcie_speed_str_to_gen),
        ("lanes_max",    "max_link_width",     _safe_int),
    ):
        try:
            result[key] = converter(_sysfs(dev / sysfs_file))
        except Exception:
            pass

    for prefix, gen_k, lane_k, bw_k in (
        ("active", "gen_active",  "lanes_active", "bw_active"),
        ("max",    "gen_max",     "lanes_max",    "bw_max"),
    ):
        g, l = int(result[gen_k]), int(result[lane_k])
        if g > 0 and l > 0:
            result[bw_k] = _PCIE_BW_TABLE.get((g, l), f"Gen{g} x{l}")

    return result


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 7 — AER (Advanced Error Reporting)
# ════════════════════════════════════════════════════════════════════════════

def _read_aer(bdf: str) -> tuple[int, int]:
    bdf_norm = bdf if bdf.count(":") == 2 else f"0000:{bdf}"
    dev_path = Path(f"/sys/bus/pci/devices/{bdf_norm}")
    correctable = fatal = 0
    for fname, counter in (("aer_dev_correctable", "correctable"), ("aer_dev_fatal", "fatal")):
        try:
            raw = _sysfs(dev_path / fname)
            total = sum(_safe_int(ln.split()[1]) for ln in raw.splitlines() if len(ln.split()) == 2)
            if counter == "correctable":
                correctable = total
            else:
                fatal = total
        except Exception:
            pass
    return correctable, fatal


# ════════════════════════════════════════════════════════════════════════════
#  API PÚBLICA
# ════════════════════════════════════════════════════════════════════════════

def extract_gpu_data() -> GPUData:
    """
    Extrae y ensambla todos los datos de GPU en una instancia GPUData.

    FIX v1.2: _parse_lspci_mm() usa shlex.split() en lugar de split("\\t"),
    resolviendo la no-detección de GPU en hardware real.

    Nunca lanza excepciones (guard externo garantiza GPUData() vacío).
    """
    try:
        # ── Capa 1: identificación ────────────────────────────────────────
        gpu_model = "N/A"
        bdf       = ""
        driver_name        = ""
        driver_version_str = "N/A"
        gpu_pcie_gen_info  = 0
        gpu_pcie_width_info = 0

        try:
            gpus = _parse_lspci_mm()
            if gpus:
                g          = gpus[0]
                bdf        = g["bdf"]
                gpu_model  = f"{g['vendor']} {g['device']}".strip()
                verbose    = _parse_lspci_verbose(bdf)
                driver_name         = verbose.get("driver", "")
                cap_speed           = verbose.get("lnkcap_speed", "")
                if cap_speed:
                    gpu_pcie_gen_info   = _pcie_speed_str_to_gen(cap_speed)
                    gpu_pcie_width_info = _safe_int(verbose.get("lnkcap_width", "0"))
        except Exception as exc:
            print(f"[gpu_reader] WARN lspci: {exc}")

        if driver_name:
            try:
                driver_version_str = _driver_version(driver_name)
            except Exception:
                pass

        # ── Capa 2: nodo DRM ─────────────────────────────────────────────
        card: Optional[Path] = None
        try:
            card = _find_drm_card(bdf)
        except Exception:
            pass

        # ── Capa 3: VRAM ─────────────────────────────────────────────────
        vram_gb = vram_type_str = 0, "N/A"
        try:
            if card is not None:
                vram_gb, vram_type_str = _read_vram_amdgpu(card)
        except Exception:
            pass

        if vram_gb == 0 and "nvidia" in driver_name.lower():
            try:
                vram_gb, vram_type_str = _read_vram_nvidia()
            except Exception:
                pass

        # ── Capa 4: integridad VRAM ───────────────────────────────────────
        vram_tested = False
        seq_errors = rand_errors = stress_errors = stress_gb = 0

        if _is_gui_active():
            print("[gpu_reader] INFO GUI activa. Test destructivo de VRAM omitido.")
        else:
            print("[gpu_reader] INFO TTY puro. Ejecutando test de integridad de VRAM...")
            try:
                (seq_errors, rand_errors, stress_errors,
                 stress_gb, vram_tested) = _run_vram_stress_test(vram_gb, driver_name)
            except Exception as exc:
                print(f"[gpu_reader] WARN _run_vram_stress_test: {exc}")

        # ── Capa 5: temperatura activa ────────────────────────────────────
        gpu_t_edge = gpu_t_hotspot = 0.0
        gpu_delta_hotspot = 0.0
        temp_limit     = 110
        hotspot_status = "info"

        try:
            if card is not None:
                hwmon = _find_gpu_hwmon(card)
                if hwmon is not None:
                    temp_limit                     = _read_temp_limit(hwmon)
                    gpu_t_edge, gpu_t_hotspot      = _active_thermal_test_gpu(hwmon)
                    gpu_delta_hotspot              = round(gpu_t_hotspot - gpu_t_edge, 1)
                    hotspot_status                 = _classify_hotspot(gpu_delta_hotspot, gpu_t_edge, gpu_t_hotspot)

            if gpu_t_edge == 0.0 and "nvidia" in driver_name.lower():
                nv_edge, nv_hotspot = _read_temps_nvidia()
                if nv_edge > 0.0:
                    gpu_t_edge        = nv_edge
                    gpu_t_hotspot     = nv_hotspot
                    gpu_delta_hotspot = round(nv_hotspot - nv_edge, 1)
                    hotspot_status    = _classify_hotspot(gpu_delta_hotspot, nv_edge, nv_hotspot)
                    print(f"[gpu_reader] INFO NVIDIA hwmon ausente; temps via nvidia-smi: "
                          f"edge={nv_edge}°C hotspot={nv_hotspot}°C")
        except Exception as exc:
            print(f"[gpu_reader] WARN prueba térmica activa: {exc}")

        # ── Capa 6: PCIe ─────────────────────────────────────────────────
        pcie: dict[str, object] = {
            "gen_active": 0, "lanes_active": 0,
            "gen_max":    0, "lanes_max":    0,
            "bw_active":  "N/A", "bw_max":  "N/A",
        }
        try:
            if card is not None:
                pcie = _read_pcie_sysfs(card)
        except Exception as exc:
            print(f"[gpu_reader] WARN PCIe: {exc}")

        if gpu_pcie_gen_info   == 0: gpu_pcie_gen_info   = int(pcie["gen_max"])
        if gpu_pcie_width_info == 0: gpu_pcie_width_info = int(pcie["lanes_max"])

        # ── Capa 7: AER ──────────────────────────────────────────────────
        aer_corr = aer_fatal = 0
        try:
            if bdf:
                aer_corr, aer_fatal = _read_aer(bdf)
        except Exception:
            pass

        return GPUData(
            gpu_model          = gpu_model,
            gpu_vram_total     = vram_gb,
            gpu_vram_type      = vram_type_str,
            gpu_driver_version = driver_version_str,
            gpu_pcie_gen       = gpu_pcie_gen_info,
            gpu_pcie_width     = gpu_pcie_width_info,
            gpu_t_edge           = gpu_t_edge,
            gpu_t_hotspot        = gpu_t_hotspot,
            gpu_delta_t_hotspot  = gpu_delta_hotspot,
            gpu_temp_limit       = temp_limit,
            gpu_hotspot_status   = hotspot_status,
            gpu_vram_tested       = vram_tested,
            gpu_vram_seq_errors   = seq_errors,
            gpu_vram_rand_errors  = rand_errors,
            gpu_vram_stress_gb    = stress_gb,
            gpu_vram_stress_errors= stress_errors,
            gpu_ecc_correctable   = 0,
            gpu_pcie_gen_max      = int(pcie["gen_max"]),
            gpu_pcie_gen_active   = int(pcie["gen_active"]),
            gpu_pcie_lanes_max    = int(pcie["lanes_max"]),
            gpu_pcie_lanes_active = int(pcie["lanes_active"]),
            gpu_pcie_bw_max       = str(pcie["bw_max"]),
            gpu_pcie_bw_active    = str(pcie["bw_active"]),
            gpu_aer_correctable   = aer_corr,
            gpu_aer_fatal         = aer_fatal,
        )

    except Exception as exc:
        print(f"[gpu_reader] ERROR CRÍTICO en extract_gpu_data(): {exc}")
        return GPUData()