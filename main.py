"""
main.py — probe.tex entry point
================================
INVARIANT Hardware Forensic Diagnostic — Ring-0 Edition

Arquitectura de orquestación
-----------------------------
1. ``verify_license()``        → gate criptográfico Ed25519 (fail-closed).
2. ``_enumerate_storage_devices()`` → lsblk, NVMe primero, SATA después.
3. ``ThreadPoolExecutor``      → extractores ligeros en paralelo (no-I/O).
4. ``extract_cpu_data()``      → stress-ng + muestreo térmico (secuencial).
5. ``extract_gpu_data()``      → sensores PCIe (secuencial).
6. ``evaluate_system_entropy()``→ motor de puntuación y badges.
7. Jinja2 + tectonic           → PDF forense, offline, caché pre-cargada.

DIRECTIVA CRÍTICA — LaTeX / tectonic (NO MODIFICAR)
----------------------------------------------------
El motor de renderizado usa **tectonic** con el flag ``--cache-dir`` apuntando
al directorio de caché offline pre-inyectado en la ISO:

    /root/.cache/Tectonic

Esta decisión es arquitectónica e intencional para mantener la ISO < 1 GB.
El sistema opera en entorno air-gapped; tectonic NO debe intentar conexión a
internet ni descargar paquetes en tiempo de ejecución.

Bajo ninguna circunstancia debe modificarse para:
  - Instalar texlive-most / texlive-core.
  - Omitir ``--cache-dir``.
  - Requerir conectividad de red.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import platform
import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import jinja2
from markupsafe import Markup


# ── LaTeX sanitization for Jinja2 finalize callback ──────────────────────────
# All values interpolated into .tex templates pass through this function.
# Values wrapped in markupsafe.Markup (pre-safe LaTeX like \badgeok or pgfplots
# coordinates) bypass escaping. All other values (DMI strings, serial numbers,
# model names from hardware) are escaped to prevent LaTeX injection from
# hostile BIOS fields (e.g. \input{/etc/shadow} as a motherboard serial).

def _tex_escape(value: object) -> str:
    """Escape TeX-active characters, except values marked as Markup (pre-safe)."""
    if isinstance(value, Markup):
        return str(value)
    s = str(value)
    # Order matters: backslash first to avoid double-escaping.
    s = s.replace('\\', r'\textbackslash{}')
    s = s.replace('{',  r'\{')
    s = s.replace('}',  r'\}')
    s = s.replace('$',  r'\$')
    s = s.replace('&',  r'\&')
    s = s.replace('#',  r'\#')
    s = s.replace('%',  r'\%')
    s = s.replace('_',  r'\_')
    s = s.replace('^',  r'\^{}')
    s = s.replace('~',  r'\~{}')
    return s

from core.models import (
    DiagnosticReport, ReportMetadata,
    StorageData, RAMData, MotherboardData, USBData, BatteryData,
)
from core.entropy import evaluate_system_entropy
from core.license_verifier import verify_license, LicenseError
from extractors.cpu_reader import extract_cpu_data
from extractors.disk_reader import extract_disk_data
from extractors.motherboard_reader import extract_motherboard_data
from extractors.ram_reader import extract_ram_data
from extractors.gpu_reader import extract_gpu_data
from extractors.usb_reader import extract_usb_data
from extractors.battery_reader import extract_battery_data
from tui import run_tui, runtime_log

# ── Logging estructurado — silencioso ante el usuario final ──────────────────
# Los mensajes van a un archivo de log en /tmp (o INVARIANT_LOG si existe).
# Nunca se imprimen en stdout.

_LOG_FILE: Final[Path] = Path(os.environ.get("INVARIANT_LOG", "/tmp/probe_tex.log"))
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level    = logging.DEBUG,
    format   = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers = [logging.FileHandler(_LOG_FILE, encoding="utf-8")],
)

_log = logging.getLogger(__name__)

# Directorio de caché offline de tectonic — pre-inyectado en la ISO.
# Cambiar SOLO si la ruta de inyección del forge.sh cambia.
_TECTONIC_CACHE_DIR: Final[Path] = Path("/root/.cache/Tectonic")


# ════════════════════════════════════════════════════════════════════════════
#  HELPERS INTERNOS
# ════════════════════════════════════════════════════════════════════════════

def _enumerate_storage_devices() -> list[str]:
    """
    Enumera todas las unidades físicas internas conectadas a la placa base.

    Fuente canónica: ``lsblk -J -o NAME,TYPE,TRAN``
      - TYPE == "disk"     → dispositivo de bloque raíz (no partición).
      - TRAN == "nvme"     → controladora NVMe (PCIe).
      - TRAN == "sata"     → controladora SATA (SSD o HDD mecánico).
      - TRAN == "usb"      → descartado explícitamente (pendrive, externo).
      - TRAN == None/""    → descartado (dispositivos virtuales, loop, dm).

    Fallback
    --------
    Si lsblk falla o no devuelve dispositivos válidos, retorna
    ['/dev/nvme0n1'] como último recurso conservador.

    Returns
    -------
    list[str]
        Rutas de nodo de bloque ordenadas: NVMe primero, SATA después.
        Ej. ['/dev/nvme0n1', '/dev/sda', '/dev/sdb']
    """
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,TYPE,TRAN"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        data       = json.loads(out)
        nvme_devs: list[str] = []
        sata_devs: list[str] = []

        for dev in data.get("blockdevices", []):
            if dev.get("type") != "disk":
                continue
            tran = (dev.get("tran") or "").lower().strip()
            if tran == "nvme":
                nvme_devs.append(f"/dev/{dev['name']}")
            elif tran == "sata":
                sata_devs.append(f"/dev/{dev['name']}")
            # tran == "usb" o vacío → ignorado explícitamente

        devices = nvme_devs + sata_devs
        if devices:
            _log.info("Block devices detected: %s", devices)
            return devices

    except Exception as exc:
        _log.warning("_enumerate_storage_devices failed: %s", exc)

    _log.warning("Device enumeration failed — falling back to /dev/nvme0n1")
    return ["/dev/nvme0n1"]


def _extract_all_drives(devices: list[str]) -> list[StorageData]:
    """
    Extrae StorageData para cada dispositivo en ``devices`` de forma secuencial.

    La extracción secuencial es correcta para I/O de disco: smartctl y fio
    no deben ejecutarse en paralelo sobre distintas unidades del mismo
    controlador SATA/NVMe porque comparten el bus y las lecturas SMART
    se interferirían con los test de fio activos.

    Si la extracción de un disco individual falla (excepción no anticipada),
    se registra un aviso y se inserta un StorageData() vacío en su posición
    para preservar la correspondencia de índices con ``devices``.

    Returns
    -------
    list[StorageData]
        Longitud == len(devices).  Nunca vacía (mínimo un StorageData()).
    """
    results: list[StorageData] = []
    for dev in devices:
        try:
            _log.info("Extracting storage data: %s", dev)
            results.append(extract_disk_data(dev))
        except Exception as exc:
            _log.warning("extract_disk_data(%s) failed: %s — inserting empty StorageData", dev, exc)
            results.append(StorageData())
    return results if results else [StorageData()]


def _resolve_outdir(args_outdir: str | None) -> Path:
    """
    Resuelve el directorio de salida con verificación de escritura.

    Prioridad: argumento CLI → $INVARIANT_OUT → cwd → /tmp.
    """
    if args_outdir:
        p = Path(args_outdir)
    elif "INVARIANT_OUT" in os.environ:
        p = Path(os.environ["INVARIANT_OUT"])
    else:
        p = Path.cwd()

    test_file = p / ".invariant_write_test"
    try:
        test_file.touch()
        test_file.unlink()
        _log.info("Output directory resolved: %s", p)
        return p
    except Exception:
        _log.warning("Cannot write to %s — falling back to /tmp", p)
        return Path("/tmp")


# ════════════════════════════════════════════════════════════════════════════
#  RENDERIZACIÓN DE REPORTE FORENSE
# ════════════════════════════════════════════════════════════════════════════

def render_pdf(outdir: Path | None = None) -> None:
    """
    Orquesta la extracción de hardware, la evaluación de entropía y la
    compilación del PDF forense mediante tectonic (offline, air-gapped).

    Parameters
    ----------
    outdir:
        Directorio de salida para .tex y .pdf.
        Si None → /tmp (Live OS safe).
    """
    runtime_log("Ring-0: Initiating concurrent hardware sweep...")
    start_time = time.time()

    # Enumerar discos antes del ThreadPoolExecutor (rápido, ~50 ms).
    # Permite calcular el timeout de disco dinámicamente.
    devices      = _enumerate_storage_devices()
    disk_timeout = max(130, 130 * len(devices))   # 130 s por unidad

    _TIMEOUTS: dict[str, int] = {
        "disk": disk_timeout,
        "ram":  620,
        "mobo": 40,
        "usb":  15,
        "bat":  15,
        "cpu":  70,
        "gpu":  45,
    }

    # ── FASE 1: extractores sin carga activa (paralelos, seguros) ────────────
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        runtime_log("Storage: Enumerating physical block devices...")
        future_disk = executor.submit(_extract_all_drives, devices)

        runtime_log("RAM: Analyzing topology and launching memtester...")
        future_ram  = executor.submit(extract_ram_data)
        future_mobo = executor.submit(extract_motherboard_data)
        future_usb  = executor.submit(extract_usb_data)
        future_bat  = executor.submit(extract_battery_data)

        def _safe_result(future: concurrent.futures.Future, label: str, fallback):  # type: ignore[type-arg]
            try:
                return future.result(timeout=_TIMEOUTS[label])
            except concurrent.futures.TimeoutError:
                _log.warning("Extractor '%s' exceeded timeout — using empty fallback", label)
                return fallback

        storage_drives: list[StorageData] = _safe_result(future_disk, "disk", [StorageData()])
        ram_data  = _safe_result(future_ram,  "ram",  RAMData())
        mobo_data = _safe_result(future_mobo, "mobo", MotherboardData())
        usb_data  = _safe_result(future_usb,  "usb",  USBData())
        bat_data  = _safe_result(future_bat,  "bat",  BatteryData())

    # ── FASE 2: extractores con carga activa (secuenciales por diseño) ───────
    runtime_log("CPU: Running thermal profiling and P-State analysis...")
    cpu_data = extract_cpu_data()

    runtime_log("GPU: Verifying Hotspot sensors and PCIe link...")
    gpu_data = extract_gpu_data()

    elapsed: float = round(time.time() - start_time, 1)
    runtime_log(f"Extraction sequence halted. Duration: {elapsed}s.")

    # ── Metadatos del host ───────────────────────────────────────────────────
    runtime_log("DMI: Extracting host metadata and motherboard serial...")
    try:
        sn_raw = subprocess.run(
            ["dmidecode", "-s", "system-serial-number"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        serial_number: str = sn_raw if sn_raw else "Desconocido"
    except Exception as exc:
        _log.warning("dmidecode serial-number failed: %s", exc)
        serial_number = "No accesible"

    kernel_version: str = platform.release()
    fecha_actual: str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Motor de entropía y puntuación ───────────────────────────────────────
    entropy_data = evaluate_system_entropy(
        cpu            = cpu_data,
        gpu            = gpu_data,
        storage_drives = storage_drives,
        ram            = ram_data,
        mobo           = mobo_data,
        usb            = usb_data,
        battery        = bat_data,
    )

    runtime_log("Assembling Ring-0 data contract...")
    report = DiagnosticReport(
        metadata=ReportMetadata(
            report_id          = "INV-2026-001",
            cliente_nombre     = "Taller Local Demo",
            cliente_email      = "contacto@cliente.com",
            cliente_telefono   = "+54 223 000-0000",
            device_brand       = "ASUS",
            device_model       = "Vivobook E1504FA",
            serial_number      = serial_number,
            taller_nombre      = "Invariant Systems",
            tecnico_nombre     = "Admin",
            version            = "1.0.0",
            kernel_version     = kernel_version,
            fecha_reporte      = fecha_actual,
            duracion_analisis  = f"{elapsed} s",
        ),
        cpu            = cpu_data,
        gpu            = gpu_data,
        storage_drives = storage_drives,
        ram            = ram_data,
        motherboard    = mobo_data,
        usb            = usb_data,
        battery        = bat_data,
    )

    # ── Renderización Jinja2 → LaTeX ─────────────────────────────────────────
    runtime_log("Jinja2: Instantiating LaTeX rendering engine...")
    latex_env = jinja2.Environment(
        block_start_string   = '[%',  block_end_string   = '%]',
        variable_start_string= '<<',  variable_end_string= '>>',
        comment_start_string = '[#',  comment_end_string = '#]',
        trim_blocks  = True,
        loader       = jinja2.FileSystemLoader('renderer/templates'),
        finalize     = _tex_escape,
    )

    template = latex_env.get_template('reporte_base.tex')
    context  = report.to_jinja_context()

    # ── Mark pre-formatted LaTeX fields as safe (bypass _tex_escape) ──────
    # These fields contain intentional LaTeX: pgfplots coordinates, table
    # rows with \\ and &, badge macros like \badgeok, and item lists.
    # All other fields (DMI strings, model names, serial numbers) will be
    # escaped by _tex_escape to prevent LaTeX injection from hostile BIOS.
    _LATEX_SAFE_KEYS: Final[frozenset[str]] = frozenset({
        # pgfplots coordinate strings
        "datos_cpu_temp", "datos_vrm_vid", "datos_vrm_medido",
        # Pre-formatted LaTeX table rows
        "mce_tabla_filas", "ram_edac_filas", "usb_tabla_filas",
        # Badge macros (\badgeok, \badgefail, etc.)
        "estado_global_badge", "badge_cpu", "badge_gpu", "badge_nvme",
        "badge_ram", "badge_mobo", "badge_usb", "badge_global",
        # Recommendation item list (LaTeX \item entries)
        "lista_recomendaciones",
    })
    for key in _LATEX_SAFE_KEYS:
        if key in context and isinstance(context[key], str):
            context[key] = Markup(context[key])

    # Claves de entropía — nvme_* son agrupadas (multi-disco aggregado).
    # Badge and list fields from entropy are also LaTeX-safe.
    context.update({
        "indice_anomalia":        entropy_data.total_delta_a,
        "cpu_anomalia":           entropy_data.cpu.delta_a,
        "gpu_anomalia":           entropy_data.gpu.delta_a,
        "nvme_anomalia":          entropy_data.nvme.delta_a,
        "ram_anomalia":           entropy_data.ram.delta_a,
        "mobo_anomalia":          entropy_data.vrm.delta_a,
        "usb_anomalia":           entropy_data.usb.delta_a,
        "bat_anomalia":           entropy_data.battery.delta_a,
        "cpu_estado_badge":       Markup(entropy_data.badge_cpu),
        "gpu_estado_badge":       Markup(entropy_data.badge_gpu),
        "nvme_estado_badge":      Markup(entropy_data.badge_nvme),
        "ram_estado_badge":       Markup(entropy_data.badge_ram),
        "mobo_estado_badge":      Markup(entropy_data.badge_mobo),
        "usb_estado_badge":       Markup(entropy_data.badge_usb),
        "bat_estado_badge":       Markup(entropy_data.badge_bat),
        "accion_cpu":             entropy_data.accion_cpu,
        "accion_gpu":             entropy_data.accion_gpu,
        "accion_nvme":            entropy_data.accion_nvme,
        "accion_ram":             entropy_data.accion_ram,
        "accion_mobo":            entropy_data.accion_mobo,
        "accion_usb":             entropy_data.accion_usb,
        "accion_bat":             entropy_data.accion_bat,
        "accion_global":          entropy_data.accion_global,
        "estado_global_badge":    Markup(entropy_data.estado_global_badge),
        "resumen_ejecutivo":      entropy_data.resumen_ejecutivo,
        "lista_recomendaciones":  Markup(entropy_data.lista_recomendaciones),
    })

    out      = outdir or Path("/tmp")
    tex_path = out / "reporte_generado.tex"

    runtime_log(f"I/O: Writing TeX source → {tex_path.name}...")
    tex_path.write_text(template.render(**context), encoding="utf-8")

    # ── Compilación tectonic (OFFLINE — caché pre-cargada) ───────────────────
    # DIRECTIVA CRÍTICA: --cache-dir apunta al caché offline inyectado en la
    # ISO. NO se añaden --keep-logs, --web, ni otras flags que requieran red.
    runtime_log("Tectonic: Compiling forensic report (offline cache)...")
    _log.info("tectonic: cache=%s tex=%s out=%s", _TECTONIC_CACHE_DIR, tex_path, out)

    try:
        subprocess.run(
            [
                "tectonic",
                "--cache-dir", str(_TECTONIC_CACHE_DIR),
                "--outdir",    str(out),
                str(tex_path),
            ],
            check  = True,
            stdout = subprocess.DEVNULL,
            stderr = subprocess.PIPE,
            text   = True,
            timeout= 120,
        )
        pdf_path = out / "reporte_generado.pdf"
        _log.info("PDF compiled successfully: %s", pdf_path)
        runtime_log(f"Report compiled → {pdf_path.name}")
    except FileNotFoundError:
        raise RuntimeError(
            "tectonic binary not found in PATH. "
            "Verify the ISO build included tectonic."
        )
    except subprocess.CalledProcessError as exc:
        _log.error("tectonic compilation failed:\n%s", exc.stderr)
        raise RuntimeError(
            "LaTeX compilation failed. Check /tmp/probe_tex.log for details."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("tectonic exceeded 120 s compilation timeout.")


# ════════════════════════════════════════════════════════════════════════════
#  LOCKSCREEN DE LICENCIA — Renderizada via Rich (sin print())
# ════════════════════════════════════════════════════════════════════════════

def _render_license_lockscreen(code: str) -> None:
    """
    Muestra una pantalla de bloqueo en la terminal usando Rich y detiene
    la ejecución. No usa print() — salida exclusivamente a Rich Console.

    El error code se registra en el log antes de terminar.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    _log.critical("LICENSE GATE TRIGGERED — code=%s", code)

    console = Console(stderr=True)
    console.print()

    body = Text(justify="center")
    body.append("\n  ◆  SISTEMA BLOQUEADO — LICENCIA INVÁLIDA  ◆\n\n", style="bold white")
    body.append(f"  CÓDIGO DE ERROR:  {code}\n\n", style="white")
    body.append(
        "  Contacte a soporte técnico con el código anterior.\n"
        "  probe.tex // Invariant Systems\n",
        style="dim white",
    )

    console.print(
        Panel(
            body,
            style        = "on dark_red",
            border_style = "bright_red",
            expand       = True,
        )
    )
    console.print()
    sys.exit(1)


# ════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        # 1. Gate criptográfico — fail-closed
        licencia = verify_license()
        if licencia is None:
            # This path should never be reached (verify_license raises on failure),
            # but we guard explicitly to prevent a silent None fall-through.
            raise RuntimeError("verify_license() returned None without raising")

        _log.info(
            "License validated: plan=%s hw=%s...%s expires=%s",
            licencia.plan,
            licencia.hardware_id[:8],
            licencia.hardware_id[-8:],
            datetime.fromtimestamp(licencia.expires_at, tz=timezone.utc).date().isoformat(),
        )

        # 2. Argumentos CLI
        parser = argparse.ArgumentParser(
            prog        = "probe.tex",
            description = "INVARIANT — Hardware Forensic Diagnostic (Ring-0)",
        )
        parser.add_argument(
            "--outdir",
            metavar = "PATH",
            default = None,
            help    = (
                "Output directory for reporte_generado.pdf "
                "(default: $INVARIANT_OUT, then cwd, then /tmp)"
            ),
        )
        args         = parser.parse_args()
        final_outdir = _resolve_outdir(args.outdir)

        # 3. Flush any pending stdout bytes before Rich takes over the terminal
        sys.stdout.flush()

        # 4. Diagnóstico forense dentro del TUI
        run_tui(lambda: render_pdf(final_outdir))

    except LicenseError as exc:
        _render_license_lockscreen(exc.code)

    except SystemExit:
        raise  # Allow intentional exits (e.g., license lockscreen) to propagate.
    except Exception as exc:
        # FIX RING-0: Any unhandled exception is logged and printed to stderr
        # so the technician can see the traceback on the TTY even if the
        # Rich/Live TUI has already exited or crashed.
        import traceback
        _log.critical("FATAL: Unhandled exception in probe.tex main loop", exc_info=True)
        sys.stderr.write("\n[CRITICAL] probe.tex terminated unexpectedly:\n")
        sys.stderr.write(f"  {type(exc).__name__}: {exc}\n")
        sys.stderr.write("\nFull traceback:\n")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.write(f"\nSee also: {_LOG_FILE}\n")
        sys.stderr.flush()
        sys.exit(1)