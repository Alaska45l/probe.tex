"""
extractors/gpu_reader.py
========================
Extractor de hardware para el subsistema de GPU de probe.tex.

Fuentes de datos (en orden de preferencia / fallback honesto)
-------------------------------------------------------------
Identificación
  1. ``lspci -mm``          → modelo completo de la GPU (VGA/Display/3D).
  2. ``lspci -k``           → driver de kernel en uso (amdgpu, nvidia, i915…).
  3. ``modinfo <driver>``   → versión del módulo de kernel del driver.
  4. /sys/class/drm/card*/  → glob para encontrar la tarjeta activa correcta.

VRAM
  5. sysfs drm ``mem_info_vram_total``    → AMDGPU (bytes → GB).
  6. sysfs drm ``mem_info_vram_type``     → tipo de VRAM (GDDR6, HBM2…).
  7. ``nvidia-smi --query-gpu``           → NVIDIA (fallback si amdgpu falla).
  8. Si ninguno funciona → ``gpu_vram_total = 0``, ``gpu_vram_type = "N/A"``.

Temperatura — Prueba Térmica Activa (Forense Activo)
  9. ``stress-ng --matrix 0 --timeout 20s``
     Genera carga matricial en CPU/FPU/iGPU durante _GPU_STRESS_DURATION_S
     segundos.  Un hilo secundario muestrea ``temp1_input`` (Edge) y
     ``temp2_input`` (Hotspot) desde el hwmon del dispositivo DRM cada
     _GPU_SAMPLE_INTERVAL_S segundos.  Tras la carga, _GPU_COOLING_DURATION_S
     segundos de muestreo adicional (enfriamiento pasivo).
     El resultado es el delta hotspot REAL bajo condición de estrés, no una
     lectura estática en reposo.
 10. sysfs hwmon → fallback si stress-ng no está instalado (lectura idle).
     En ese caso, delta_t_hotspot refleja la condición de reposo actual.

Límite de temperatura (TjMax GPU)
 11. hwmon ``temp1_crit`` / ``temp2_crit`` → límite real.
 12. Fallback conservador 110 °C como último recurso.

PCIe
 13. sysfs ``current_link_speed`` / ``current_link_width``  → activo.
 14. sysfs ``max_link_speed``     / ``max_link_width``      → máximo.
 15. Si no se lee → "N/A" / 0.

AER (Advanced Error Reporting)
 16. /sys/bus/pci/devices/<bdf>/aer_dev_correctable  → errores corregibles.
 17. /sys/bus/pci/devices/<bdf>/aer_dev_fatal        → errores fatales.

Integridad VRAM (stress test)
 18. Simulado con 0 errores: el test destructivo real no se ejecuta.

Principio de honestidad forense
--------------------------------
Si una fuente de datos no está disponible o no retorna un valor en rango
físico plausible, el campo correspondiente queda en su valor nulo (0, "N/A",
0.0). Nunca se asignan valores plausibles inventados.

stdlib únicamente: subprocess, re, pathlib, threading, time.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Final, Optional

from core.models import GPUData


# ════════════════════════════════════════════════════════════════════════════
#  CONSTANTES
# ════════════════════════════════════════════════════════════════════════════

_LSPCI_TIMEOUT:    int   = 5
_MODINFO_TIMEOUT:  int   = 4
_NSMI_TIMEOUT:     int   = 5

_VRAM_TEST_TIMEOUT_S: Final[int] = 120
"""Tiempo máximo del test de integridad de VRAM en segundos."""

_GUI_PROCESS_PATTERN: Final[str] = (
    r"Xorg|Xwayland|sway|kwin_wayland|kwin_x11|mutter|"
    r"gnome-shell|plasmashell|weston|hyprland|niri|river|openbox"
)
"""Patrón ERE para pgrep -f. Cubre compositors Wayland y X11 de uso habitual."""

# Parámetros de la Prueba Térmica Activa GPU
_GPU_STRESS_DURATION_S:  int   = 20    # segundos de carga via stress-ng --matrix
_GPU_COOLING_DURATION_S: int   = 10    # segundos de enfriamiento pasivo
_GPU_SAMPLE_INTERVAL_S:  float = 2.0   # intervalo de muestreo del hilo térmico

# Clases PCI que corresponden a GPUs (hex, lowercase).
_GPU_PCI_CLASSES: tuple[str, ...] = (
    "0300",   # VGA Compatible Controller
    "0301",   # XGA Controller
    "0302",   # 3D Controller (NVIDIA MXM / datacenter)
    "0380",   # Display Controller (genérico)
)

# Umbrales para clasificar el estado del hotspot.
_DELTA_WARN: float = 20.0
_DELTA_CRIT: float = 35.0

# Rango físico plausible de temperatura de GPU (°C).
_TEMP_MIN: float =  10.0
_TEMP_MAX: float = 110.0

# Ruta raíz del subsistema DRM.
_DRM_ROOT = Path("/sys/class/drm")

# Tabla de conversión de velocidad PCIe ("GT/s") → generación.
_PCIE_SPEED_TO_GEN: dict[str, int] = {
    "2.5 GT/s":  1,
    "5.0 GT/s":  2,
    "8.0 GT/s":  3,
    "16.0 GT/s": 4,
    "32.0 GT/s": 5,
    "64.0 GT/s": 6,
}

# Tabla de ancho de banda PCIe teórico por generación y lanes x16 (GB/s).
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
    """Ejecuta *cmd* y devuelve stdout como str. Lanza en caso de error."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(
            f"{cmd!r} rc={r.returncode} stderr={r.stderr.strip()!r}"
        )
    return r.stdout


def _sysfs(path: Path | str) -> str:
    """Lee un archivo sysfs y devuelve su contenido limpio."""
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
    Parsea ``lspci -mm`` (formato de máquina) para extraer GPUs.

    Formato de salida de ``lspci -mm``::

        Slot [TAB] Class [TAB] Vendor [TAB] Device [TAB] SVendor [TAB] SDevice [TAB] Rev

    Filtramos por clase PCI 03xx (Display/VGA/3D).
    Devuelve lista de dicts con: bdf, class, vendor, device.
    """
    raw = _run(["lspci", "-mm"])
    gpus: list[dict[str, str]] = []

    for line in raw.splitlines():
        parts = [p.strip().strip('"') for p in line.split("\t")]
        if len(parts) < 4:
            continue
        bdf, cls, vendor, device = parts[0], parts[1], parts[2], parts[3]
        cls_lower = cls.lower()
        is_gpu = (
            "vga"     in cls_lower
            or "display" in cls_lower
            or "3d"      in cls_lower
            or any(
                c in cls_lower
                for c in ("0300", "0301", "0302", "0380")
            )
        )
        if is_gpu:
            gpus.append({"bdf": bdf, "class": cls, "vendor": vendor, "device": device})

    return gpus


def _parse_lspci_verbose(bdf: str) -> dict[str, str]:
    """
    Ejecuta ``lspci -v -s <bdf>`` para obtener el driver en uso y los
    detalles del enlace PCIe (LnkCap / LnkSta).
    """
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
    """Intenta obtener la versión del módulo de kernel del driver."""
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
    """
    Localiza el directorio DRM (``/sys/class/drm/cardN``) que corresponde
    al BDF de la GPU detectada por lspci.
    """
    bdf_short = bdf.split(":")[-2] + ":" + bdf.split(":")[-1] if ":" in bdf else bdf

    try:
        cards = sorted(
            d for d in _DRM_ROOT.iterdir()
            if d.name.startswith("card") and not d.name.count("-")
        )
    except Exception:
        return None

    for card in cards:
        try:
            device_link = (card / "device").resolve()
            if bdf_short in str(device_link) or bdf in str(device_link):
                return card
        except Exception:
            continue

    return cards[0] if cards else None


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 3 — VRAM
# ════════════════════════════════════════════════════════════════════════════

def _read_vram_amdgpu(card: Path) -> tuple[int, str]:
    """Lee la VRAM total y tipo desde los archivos sysfs de AMDGPU."""
    vram_gb   = 0
    vram_type = "N/A"

    try:
        raw_bytes = int(_sysfs(card / "device" / "mem_info_vram_total"))
        vram_gb = max(0, round(raw_bytes / 1_000_000_000))
    except Exception:
        pass

    try:
        vram_type = _sysfs(card / "device" / "mem_info_vram_type").strip()
        if not vram_type or vram_type.lower() in ("unknown", "none", "0"):
            vram_type = "N/A"
    except Exception:
        pass

    return vram_gb, vram_type


def _read_vram_nvidia() -> tuple[int, str]:
    """Lee la VRAM total desde ``nvidia-smi`` (fallback para GPUs NVIDIA)."""
    try:
        raw = _run(
            ["nvidia-smi", "--query-gpu=memory.total",
             "--format=csv,noheader,nounits"],
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
    """Localiza el directorio hwmon del dispositivo DRM."""
    try:
        hwmon_base = card / "device" / "hwmon"
        dirs = sorted(hwmon_base.iterdir())
        if dirs:
            return dirs[0]
    except Exception:
        pass

    # Fallback para APUs (comparten silicio con CPU, ej. k10temp, coretemp)
    try:
        for hwmon_dir in sorted(Path("/sys/class/hwmon").iterdir()):
            name_file = hwmon_dir / "name"
            if name_file.exists():
                name = name_file.read_text().strip().lower()
                if name in ("k10temp", "coretemp", "zenpower"):
                    return hwmon_dir
    except Exception:
        pass

    return None


def _read_temp_millic(hwmon: Path, filename: str) -> Optional[float]:
    """
    Lee un archivo ``temp*_input`` (en milligrados Celsius) y lo convierte a °C.

    Aplica la Regla de Honestidad Forense:
    - Si el archivo no existe → None (NO 0.0, NO valor inventado).
    - Si el valor está fuera del rango físico plausible → None.
    """
    try:
        raw = int(_sysfs(hwmon / filename))
        celsius = raw / 1000.0
        if _TEMP_MIN <= celsius <= _TEMP_MAX:
            return round(celsius, 1)
    except Exception:
        pass
    return None


def _read_temp_limit(hwmon: Path) -> int:
    """Lee el límite de temperatura (TjMax) del GPU desde hwmon."""
    for fname in ("temp2_crit", "temp1_crit", "temp1_emergency"):
        try:
            raw = int(_sysfs(hwmon / fname))
            celsius = raw // 1000
            if 70 <= celsius <= 120:
                return celsius
        except Exception:
            continue
    return 110


def _classify_hotspot(delta: float, t_edge: float, t_hotspot: float) -> str:
    """
    Clasifica el estado del hotspot para el bloque condicional Jinja2.

    Si no hay lecturas reales (todo en 0.0), devuelve "info" en lugar de
    fabricar un falso "ok".
    """
    if t_edge == 0.0 and t_hotspot == 0.0:
        return "info"
    if delta >= _DELTA_CRIT:
        return "crit"
    if delta >= _DELTA_WARN:
        return "warn"
    return "ok"


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 4b — Detección de entorno gráfico y test de integridad VRAM
# ════════════════════════════════════════════════════════════════════════════

def _is_gui_active() -> bool:
    """
    Determina si hay un servidor gráfico activo en la sesión actual.

    Política de fallo seguro: ante cualquier ambigüedad o excepción, retorna
    True (asumir GUI presente). El test destructivo de VRAM solo se activa
    con certeza de entorno TTY puro — jamás por default o por omisión.

    Capas de detección (orden creciente de coste)
    ─────────────────────────────────────────────
    1. Variables de entorno del display server — O(1), sin syscall de proceso.
    2. XDG_SESSION_TYPE — discrimina sesiones gráficas de TTY/SSH/container.
    3. pgrep -f — un único proceso hijo contra el patrón de compositors.

    Returns
    -------
    True  → GUI activa o estado indeterminado. Test destructivo prohibido.
    False → TTY pura confirmada. Estrés de VRAM habilitado.
    """
    # Capa 1: variables del display server (Wayland y X11)
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
        return True

    # Capa 2: tipo de sesión XDG
    if os.environ.get("XDG_SESSION_TYPE", "").lower().strip() in ("x11", "wayland", "mir"):
        return True

    # Capa 3: búsqueda de proceso compositor vía pgrep -f (ERE, un solo fork)
    try:
        r = subprocess.run(
            ["pgrep", "-f", _GUI_PROCESS_PATTERN],
            capture_output=True,
            timeout=3,
        )
        # rc=0 → proceso encontrado → GUI activa
        # rc=1 → no encontrado    → TTY pura
        return r.returncode == 0
    except FileNotFoundError:
        return True   # pgrep no disponible → fallo seguro
    except Exception:
        return True   # Cualquier error inesperado → fallo seguro


def _parse_gpu_memtest_errors(output: str) -> int:
    """
    Parsea errores de la salida de gpu_memtest (ROCm / HIP).

    Formatos conocidos
    ------------------
    ``[HIP] ERROR: N bit error(s) found at block 0x...``  → conteo numérico
    ``Test FAILED``                                         → marcador sin conteo
    """
    total = 0
    for m in re.finditer(r"ERROR:\s+(\d+)\s+bit\s+error", output, re.IGNORECASE):
        total += int(m.group(1))
    if total == 0:
        # Sin conteo numérico: un marcador FAILED = 1 unidad de error lógico
        total = len(re.findall(r"\bFAILED\b", output, re.IGNORECASE))
    return total


def _parse_cuda_memtest_errors(output: str) -> int:
    """
    Parsea errores de la salida de cuda-memtest (NVIDIA CUDA tools).

    Formatos conocidos
    ------------------
    ``N error(s) found``                  → conteo explícito preferido
    ``Error at row R col C: expected X``  → error individual, sin conteo global
    """
    m = re.search(r"(\d+)\s+error", output, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return len(re.findall(r"\bError\b", output))


def _run_vram_stress_test(
    vram_total_gb: int,
    driver_name:   str,
) -> tuple[int, int, int, int, bool]:
    """
    Ejecuta test de integridad de VRAM en entorno TTY puro confirmado.

    Estrategia por driver
    ─────────────────────
    amdgpu (discreto) → gpu_memtest     (ROCm ecosystem, AUR: gpu_memtest)
    nvidia            → cuda-memtest    (AUR: cuda-tools)
    i915 / xe (iGPU)  → memtester proxy sobre DRAM compartido (ver nota)
    resto             → sin tool disponible, tested=False

    Nota sobre el proxy Intel iGPU
    ──────────────────────────────
    La iGPU Intel (i915/xe) no posee VRAM dedicada: el driver asigna regiones
    del DRAM del sistema vía GTT (Graphics Translation Tables). memtester
    sobre el DRAM del sistema cubre físicamente el mismo silicio que la iGPU
    accede. No valida la lógica de acceso GTT, pero detecta errores físicos
    de celda. El reporte documenta este hecho mediante gpu_vram_tested.

    Parameters
    ----------
    vram_total_gb : GB de VRAM detectada (0 si iGPU sin valor explícito).
    driver_name   : Driver de kernel activo (ej. "amdgpu", "nvidia", "i915").

    Returns
    -------
    (seq_errors, rand_errors, stress_errors, tested_gb, tested)
        tested=False → ninguna herramienta disponible o no ejecutable.
    """
    d = driver_name.lower()

    # ── AMD amdgpu discreto → gpu_memtest (ROCm) ──────────────────────────
    if "amdgpu" in d:
        try:
            r = subprocess.run(
                ["gpu_memtest"],
                capture_output=True, text=True,
                timeout=_VRAM_TEST_TIMEOUT_S,
            )
            errors = _parse_gpu_memtest_errors(r.stdout + r.stderr)
            # gpu_memtest no desglosa seq/rand/stress → reportar en stress_errors
            return 0, 0, errors, max(vram_total_gb, 1), True
        except FileNotFoundError:
            print("[gpu_reader] INFO gpu_memtest no disponible. Instalar: yay -S gpu_memtest")
        except subprocess.TimeoutExpired:
            print(f"[gpu_reader] WARN gpu_memtest superó {_VRAM_TEST_TIMEOUT_S} s.")
        except Exception as exc:
            print(f"[gpu_reader] WARN gpu_memtest: {exc}")

    # ── NVIDIA → cuda-memtest ──────────────────────────────────────────────
    if "nvidia" in d:
        try:
            r = subprocess.run(
                ["cuda-memtest"],
                capture_output=True, text=True,
                timeout=_VRAM_TEST_TIMEOUT_S,
            )
            errors = _parse_cuda_memtest_errors(r.stdout + r.stderr)
            return 0, 0, errors, max(vram_total_gb, 1), True
        except FileNotFoundError:
            print("[gpu_reader] INFO cuda-memtest no disponible. Instalar: sudo pacman -S cuda-tools")
        except subprocess.TimeoutExpired:
            print(f"[gpu_reader] WARN cuda-memtest superó {_VRAM_TEST_TIMEOUT_S} s.")
        except Exception as exc:
            print(f"[gpu_reader] WARN cuda-memtest: {exc}")

    # ── Intel iGPU (i915 / Xe) → proxy memtester sobre DRAM compartido ───
    if "i915" in d or "xe" in d:
        test_mb = min(max(vram_total_gb * 1024, 256), 1024)
        try:
            r = subprocess.run(
                ["sudo", "memtester", f"{test_mb}M", "1"],
                capture_output=True, text=True,
                timeout=_VRAM_TEST_TIMEOUT_S,
            )
            failures = len(re.findall(r"\bFAILURE\b", r.stdout + r.stderr, re.IGNORECASE))
            tested_gb = max(test_mb // 1024, 1)
            return failures, 0, 0, tested_gb, True
        except FileNotFoundError:
            print("[gpu_reader] INFO memtester no disponible para proxy iGPU Intel.")
        except subprocess.TimeoutExpired:
            print(f"[gpu_reader] WARN memtester iGPU superó {_VRAM_TEST_TIMEOUT_S} s.")
        except Exception as exc:
            print(f"[gpu_reader] WARN memtester iGPU: {exc}")

    return 0, 0, 0, 0, False


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 5 — Prueba Térmica Activa (stress-ng + hilo de muestreo)
# ════════════════════════════════════════════════════════════════════════════

def _active_thermal_test_gpu(
    hwmon: Path,
) -> tuple[float, float]:
    """
    Ejecuta ``stress-ng --matrix 0 --timeout 20s`` para generar carga
    matricial sobre el procesador/iGPU y muestrea las temperaturas de
    Edge y Hotspot desde el hwmon del dispositivo DRM.

    Arquitectura de hilos
    ---------------------
    * Main thread : Popen stress-ng → wait → espera enfriamiento → join.
    * Sampler thread: bucle con ``stop_event.wait(timeout=2)`` como sleep
      interruptible.  Se detiene cuando el main thread llama a
      ``stop_event.set()``.

    El hilo de muestreo registra las temperaturas reales durante la carga
    y el enfriamiento pasivo.  El resultado es el PICO térmico medido,
    no una lectura estática en reposo.

    Nota sobre cargas de iGPU/APU
    ------------------------------
    ``stress-ng --matrix`` estresa la FPU/ALU del procesador con operaciones
    matriciales (GEMM).  En sistemas APU (AMD Ryzen integrado, Intel UHD),
    esto eleva la temperatura del die compartido CPU/GPU y es suficiente
    para revelar problemas de disipación.  En GPUs discretas con VRAM
    dedicada, el calentamiento del die gráfico es mínimo bajo este stressor;
    en ese caso los valores idle son igualmente representativos del estado
    base del sistema de refrigeración.

    Degradación elegante
    --------------------
    * FileNotFoundError → stress-ng no instalado: se detiene el hilo y
      se devuelven las temperaturas idle leídas antes del intento.
    * TimeoutExpired o cualquier otra excepción → mismo comportamiento.

    Parameters
    ----------
    hwmon : Path
        Directorio hwmon del dispositivo DRM (contendrá temp1_input,
        temp2_input, etc.).

    Returns
    -------
    (t_edge_peak, t_hotspot_peak)
        Temperatura máxima de Edge y Hotspot observadas en °C.
        Valores idle si stress-ng no pudo ejecutarse (honestidad forense).
    """
    # ── Lectura baseline (idle antes de cualquier carga) ─────────────────
    t_edge_idle    = _read_temp_millic(hwmon, "temp1_input") or 0.0
    t_hotspot_idle = _read_temp_millic(hwmon, "temp2_input") or 0.0

    samples_edge:    list[float] = [t_edge_idle]    if t_edge_idle    > 0.0 else []
    samples_hotspot: list[float] = [t_hotspot_idle] if t_hotspot_idle > 0.0 else []

    stop_event = threading.Event()
    start_ts   = time.monotonic()

    def _sampler() -> None:
        """Hilo secundario: muestrea temps cada _GPU_SAMPLE_INTERVAL_S segundos."""
        while not stop_event.is_set():
            te = _read_temp_millic(hwmon, "temp1_input")
            th = _read_temp_millic(hwmon, "temp2_input")
            if te is not None:
                samples_edge.append(te)
            if th is not None:
                samples_hotspot.append(th)
            stop_event.wait(timeout=_GPU_SAMPLE_INTERVAL_S)

    sampler = threading.Thread(
        target=_sampler, daemon=True, name="gpu-thermal-sampler"
    )
    sampler.start()

    # ── Ejecutar stress-ng ────────────────────────────────────────────────
    proc: Optional[subprocess.Popen] = None
    stress_ok = False
    try:
        proc = subprocess.Popen(
            ["stress-ng", "--matrix", "0",
             "--timeout", f"{_GPU_STRESS_DURATION_S}s"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=_GPU_STRESS_DURATION_S + 15)
        stress_ok = True

    except FileNotFoundError:
        print("[gpu_reader] WARN stress-ng no encontrado. "
              "Prueba activa GPU omitida; usando lectura idle.")

    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
        print("[gpu_reader] WARN stress-ng GPU excedió el timeout; proceso terminado.")

    except Exception as exc:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        print(f"[gpu_reader] WARN stress-ng GPU: {exc}")

    if not stress_ok:
        # Sin carga activa → devolver baseline y detener el hilo.
        stop_event.set()
        sampler.join(timeout=_GPU_SAMPLE_INTERVAL_S + 2)
        return t_edge_idle, t_hotspot_idle

    # ── Fase de enfriamiento pasivo ───────────────────────────────────────
    # El hilo continúa muestreando durante el cooldown.
    stop_event.wait(timeout=float(_GPU_COOLING_DURATION_S))
    stop_event.set()
    sampler.join(timeout=_GPU_SAMPLE_INTERVAL_S + 2)

    # ── Extraer picos ─────────────────────────────────────────────────────
    t_edge_peak    = round(max(samples_edge),    1) if samples_edge    else 0.0
    t_hotspot_peak = round(max(samples_hotspot), 1) if samples_hotspot else 0.0

    return t_edge_peak, t_hotspot_peak


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 6 — PCIe desde sysfs
# ════════════════════════════════════════════════════════════════════════════

def _pcie_speed_str_to_gen(speed_str: str) -> int:
    """Convierte la cadena de velocidad PCIe a número de generación."""
    m = re.search(r"([\d.]+)\s*GT/s", speed_str, re.IGNORECASE)
    if m:
        speed_val = float(m.group(1))
        if speed_val <= 2.5:  return 1
        if speed_val <= 5.0:  return 2
        if speed_val <= 8.0:  return 3
        if speed_val <= 16.0: return 4
        if speed_val <= 32.0: return 5
        return 6
    return 0


def _read_pcie_sysfs(card: Path) -> dict[str, object]:
    """Lee parámetros PCIe desde sysfs del dispositivo DRM."""
    result: dict[str, object] = {
        "gen_active":  0, "lanes_active": 0,
        "gen_max":     0, "lanes_max":    0,
        "bw_active":   "N/A", "bw_max": "N/A",
    }

    dev = card / "device"

    try:
        cur_speed = _sysfs(dev / "current_link_speed")
        result["gen_active"] = _pcie_speed_str_to_gen(cur_speed)
    except Exception:
        pass

    try:
        result["lanes_active"] = _safe_int(_sysfs(dev / "current_link_width"))
    except Exception:
        pass

    try:
        max_speed = _sysfs(dev / "max_link_speed")
        result["gen_max"] = _pcie_speed_str_to_gen(max_speed)
    except Exception:
        pass

    try:
        result["lanes_max"] = _safe_int(_sysfs(dev / "max_link_width"))
    except Exception:
        pass

    gen_a  = int(result["gen_active"])
    lane_a = int(result["lanes_active"])
    gen_m  = int(result["gen_max"])
    lane_m = int(result["lanes_max"])

    if gen_a > 0 and lane_a > 0:
        result["bw_active"] = _PCIE_BW_TABLE.get((gen_a, lane_a), f"Gen{gen_a} x{lane_a}")
    if gen_m > 0 and lane_m > 0:
        result["bw_max"] = _PCIE_BW_TABLE.get((gen_m, lane_m), f"Gen{gen_m} x{lane_m}")

    return result


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 7 — AER (Advanced Error Reporting)
# ════════════════════════════════════════════════════════════════════════════

def _read_aer(bdf: str) -> tuple[int, int]:
    """Lee los contadores AER desde sysfs PCI."""
    bdf_normalized = bdf if bdf.count(":") == 2 else f"0000:{bdf}"
    dev_path = Path(f"/sys/bus/pci/devices/{bdf_normalized}")

    correctable = 0
    fatal       = 0

    try:
        raw_corr = _sysfs(dev_path / "aer_dev_correctable")
        for line in raw_corr.splitlines():
            parts = line.split()
            if len(parts) == 2:
                correctable += _safe_int(parts[1])
    except Exception:
        pass

    try:
        raw_fatal = _sysfs(dev_path / "aer_dev_fatal")
        for line in raw_fatal.splitlines():
            parts = line.split()
            if len(parts) == 2:
                fatal += _safe_int(parts[1])
    except Exception:
        pass

    return correctable, fatal


# ════════════════════════════════════════════════════════════════════════════
#  API PÚBLICA
# ════════════════════════════════════════════════════════════════════════════

def extract_gpu_data() -> GPUData:
    """
    Extrae y ensambla todos los datos de GPU en una instancia ``GPUData``.

    Arquitectura de extracción en 7 capas independientes
    -----------------------------------------------------
    Cada capa tiene su propio try/except.  Si falla, sus campos quedan en
    el valor nulo honesto del modelo.  El guard externo garantiza
    GPUData() vacío ante cualquier fallo no anticipado.

    Forense Activo (Capa 5 — NUEVO)
    --------------------------------
    Se lanza ``stress-ng --matrix 0 --timeout 20s`` para generar carga
    térmica real en el die CPU/iGPU.  Un hilo secundario muestrea Edge y
    Hotspot cada 2 s.  El resultado es el delta hotspot REAL bajo estrés,
    revelando problemas de disipación que no aparecen en reposo.

    Si stress-ng no está instalado → degradación elegante a lectura idle.

    Honestidad forense
    ------------------
    - VRAM: 0 GB si no hay lectura sysfs.
    - Temperatura: 0.0 °C si no hay sensor; estado "info", no "ok".
    - PCIe: "N/A" / 0 si sysfs no responde.

    Returns
    -------
    GPUData
        Instancia completamente poblada.  Nunca lanza excepciones.
    """
    try:
        # ── Capa 1: identificación por lspci ──────────────────────────────
        gpu_model          = "N/A"
        bdf                = ""
        driver_name        = ""
        driver_version_str = "N/A"
        gpu_pcie_gen_info  = 0
        gpu_pcie_width_info = 0

        try:
            gpus = _parse_lspci_mm()
            if gpus:
                g         = gpus[0]
                bdf       = g["bdf"]
                gpu_model = f"{g['vendor']} {g['device']}".strip()
                verbose   = _parse_lspci_verbose(bdf)
                driver_name         = verbose.get("driver", "")
                cap_speed           = verbose.get("lnkcap_speed", "")
                if cap_speed:
                    gpu_pcie_gen_info   = _pcie_speed_str_to_gen(cap_speed)
                    gpu_pcie_width_info = _safe_int(verbose.get("lnkcap_width", "0"))
        except Exception as exc:
            print(f"[gpu_reader] WARN lspci: {exc}")

        # ── Capa 2: versión del driver ─────────────────────────────────────
        if driver_name:
            try:
                driver_version_str = _driver_version(driver_name)
            except Exception:
                pass

        # ── Capa 3: localizar el nodo DRM ─────────────────────────────────
        card: Optional[Path] = None
        try:
            card = _find_drm_card(bdf)
        except Exception:
            pass

        # ── Capa 4: VRAM + detección de GUI + test de integridad ──────────
        vram_gb       = 0
        vram_type     = "N/A"
        vram_tested   = False
        seq_errors    = 0
        rand_errors   = 0
        stress_errors = 0
        stress_gb     = 0

        try:
            if card is not None:
                vram_gb, vram_type = _read_vram_amdgpu(card)
        except Exception:
            pass

        if vram_gb == 0 and "nvidia" in driver_name.lower():
            try:
                vram_gb, vram_type = _read_vram_nvidia()
            except Exception:
                pass

        # Test de integridad: solo en TTY pura confirmada.
        if _is_gui_active():
            print(
                "[gpu_reader] INFO GUI activa detectada. "
                "Test destructivo de VRAM omitido para preservar el entorno gráfico."
            )
        else:
            print("[gpu_reader] INFO TTY puro detectado. Ejecutando test de integridad de VRAM...")
            try:
                (seq_errors, rand_errors, stress_errors,
                 stress_gb, vram_tested) = _run_vram_stress_test(vram_gb, driver_name)
            except Exception as exc:
                print(f"[gpu_reader] WARN _run_vram_stress_test: {exc}")
                vram_tested = False

        # ── Capa 5: PRUEBA TÉRMICA ACTIVA ─────────────────────────────────
        # Reemplaza la lectura estática original por un muestreo con carga
        # real durante _GPU_STRESS_DURATION_S segundos (stress-ng --matrix).
        gpu_t_edge        = 0.0
        gpu_t_hotspot     = 0.0
        gpu_delta_hotspot = 0.0
        temp_limit        = 110
        hotspot_status    = "info"

        try:
            if card is not None:
                hwmon = _find_gpu_hwmon(card)
                if hwmon is not None:
                    # Leer límite de temperatura antes de la prueba de estrés
                    # (no se modifica bajo carga; es un valor de fábrica).
                    temp_limit = _read_temp_limit(hwmon)

                    # Prueba activa: devuelve picos de Edge y Hotspot.
                    gpu_t_edge, gpu_t_hotspot = _active_thermal_test_gpu(hwmon)

                    # Si la prueba retornó 0.0 porque el sensor no existe,
                    # el resultado es legítimamente "sin datos" → estado "info".
                    gpu_delta_hotspot = round(gpu_t_hotspot - gpu_t_edge, 1)
                    hotspot_status    = _classify_hotspot(
                        gpu_delta_hotspot, gpu_t_edge, gpu_t_hotspot
                    )
        except Exception as exc:
            print(f"[gpu_reader] WARN prueba térmica activa: {exc}")

        # ── Capa 6: PCIe desde sysfs ───────────────────────────────────────
        pcie: dict[str, object] = {
            "gen_active": 0, "lanes_active": 0,
            "gen_max": 0,    "lanes_max":    0,
            "bw_active": "N/A", "bw_max": "N/A",
        }
        try:
            if card is not None:
                pcie = _read_pcie_sysfs(card)
        except Exception as exc:
            print(f"[gpu_reader] WARN PCIe: {exc}")

        if gpu_pcie_gen_info == 0:
            gpu_pcie_gen_info   = int(pcie["gen_max"])
        if gpu_pcie_width_info == 0:
            gpu_pcie_width_info = int(pcie["lanes_max"])

        # ── Capa 7: AER ─────────────────────────────────────────────────────
        aer_corr  = 0
        aer_fatal = 0
        try:
            if bdf:
                aer_corr, aer_fatal = _read_aer(bdf)
        except Exception:
            pass

        # ── Ensamblaje final ──────────────────────────────────────────────
        return GPUData(
            gpu_model          = gpu_model,
            gpu_vram_total     = vram_gb,
            gpu_vram_type      = vram_type,
            gpu_driver_version = driver_version_str,
            gpu_pcie_gen       = gpu_pcie_gen_info,
            gpu_pcie_width     = gpu_pcie_width_info,

            # Temperatura — valores de pico bajo carga real (Forense Activo)
            gpu_t_edge           = gpu_t_edge,
            gpu_t_hotspot        = gpu_t_hotspot,
            gpu_delta_t_hotspot  = gpu_delta_hotspot,
            gpu_temp_limit       = temp_limit,
            gpu_hotspot_status   = hotspot_status,

            # VRAM stress (honesto o ejecutado en TTY)
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

            gpu_aer_correctable = aer_corr,
            gpu_aer_fatal       = aer_fatal,
        )

    except Exception as exc:   # pragma: no cover — guardia absoluta
        print(f"[gpu_reader] ERROR CRÍTICO en extract_gpu_data(): {exc}")
        return GPUData()