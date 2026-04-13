import argparse
import json
import os
import time
import platform
import subprocess
import concurrent.futures
from datetime import datetime
from pathlib import Path
import jinja2

from core.models import (
    DiagnosticReport, ReportMetadata, GlobalSummary,
    StorageData, NVMeData, RAMData, MotherboardData, USBData, BatteryData
)
from core.entropy import evaluate_system_entropy
from extractors.cpu_reader import extract_cpu_data
from extractors.disk_reader import extract_disk_data
from extractors.motherboard_reader import extract_motherboard_data
from extractors.ram_reader import extract_ram_data
from extractors.gpu_reader import extract_gpu_data
from extractors.usb_reader import extract_usb_data
from extractors.battery_reader import extract_battery_data
from tui import run_tui

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
    Si lsblk falla o no devuelve dispositivos válidos, se retorna
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
        data    = json.loads(out)
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
            print(f"[main] INFO dispositivos de almacenamiento detectados: {devices}")
            return devices

    except Exception as exc:
        print(f"[main] WARN _enumerate_storage_devices: {exc}")

    print("[main] WARN enumeración fallida. Fallback a /dev/nvme0n1.")
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
            print(f"[main] INFO extrayendo almacenamiento: {dev}")
            results.append(extract_disk_data(dev))
        except Exception as exc:
            print(f"[main] WARN extract_disk_data({dev}): {exc}. "
                  "Insertando StorageData() vacío.")
            results.append(StorageData())
    return results if results else [StorageData()]

def _resolve_outdir(args_outdir: str | None) -> Path:
    if args_outdir:
        p = Path(args_outdir)
    elif "INVARIANT_OUT" in os.environ:
        p = Path(os.environ["INVARIANT_OUT"])
    else:
        p = Path.cwd()  # PRIMERO intenta usar la carpeta actual

    test_file = p / ".invariant_write_test"
    try:
        test_file.touch()
        test_file.unlink()
        return p
    except Exception:
        print(f"[main] WARN No se puede escribir en {p}. Cayendo a /tmp.")
        return Path("/tmp")

def render_pdf(outdir: Path | None = None) -> None:
    """
    outdir: directorio de salida para .tex y .pdf.
            Si None → /tmp (Live OS safe).
    """
    print("[*] Iniciando extracción de datos estáticos concurrente...")
    start_time = time.time()

    # ── NUEVO: enumerar discos ANTES de lanzar el ThreadPoolExecutor ──────
    # La enumeración es rápida (lsblk ~50 ms) y nos permite calcular el
    # timeout de disco dinámicamente según el número de unidades.
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

    # FASE 1: extractores sin carga activa (paralelos, seguros)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        # ── CAMBIADO: future único que extrae TODOS los discos ────────────
        future_disk = executor.submit(_extract_all_drives, devices)
        future_ram  = executor.submit(extract_ram_data)
        future_mobo = executor.submit(extract_motherboard_data)
        future_usb  = executor.submit(extract_usb_data)
        future_bat  = executor.submit(extract_battery_data)

        # ── CAMBIADO: resultado es list[StorageData] ──────────────────────
        try:
            storage_drives: list[StorageData] = future_disk.result(
                timeout=_TIMEOUTS["disk"]
            )
        except concurrent.futures.TimeoutError:
            print("[main] WARN extractor disk superó timeout. "
                  "Usando [StorageData()] vacío.")
            storage_drives = [StorageData()]

        try:
            ram_data = future_ram.result(timeout=_TIMEOUTS["ram"])
        except concurrent.futures.TimeoutError:
            print("[main] WARN extractor ram superó timeout.")
            ram_data = RAMData()
        try:
            mobo_data = future_mobo.result(timeout=_TIMEOUTS["mobo"])
        except concurrent.futures.TimeoutError:
            print("[main] WARN extractor mobo superó timeout.")
            mobo_data = MotherboardData()
        try:
            usb_data = future_usb.result(timeout=_TIMEOUTS["usb"])
        except concurrent.futures.TimeoutError:
            print("[main] WARN extractor usb superó timeout.")
            usb_data = USBData()
        try:
            bat_data = future_bat.result(timeout=_TIMEOUTS["bat"])
        except concurrent.futures.TimeoutError:
            print("[main] WARN extractor bat superó timeout.")
            bat_data = BatteryData()

    print("[*] Extracción estática completada. Iniciando forense activo secuencial...")

    print("[*]   → Test térmico CPU (30s carga + 15s enfriamiento)...")
    cpu_data = extract_cpu_data()

    print("[*]   → Test térmico GPU (20s carga + 10s enfriamiento)...")
    gpu_data = extract_gpu_data()

    end_time = time.time()
    duracion_segundos = round(end_time - start_time, 1)
    
    print(f"[*] Extracción finalizada en {duracion_segundos} segundos.")

    print("[*] Extrayendo metadatos de host (anillo 0)...")
    # Número de serie de la placa base vía DMI
    try:
        sn_raw = subprocess.run(["sudo", "dmidecode", "-s", "system-serial-number"], 
                                capture_output=True, text=True, timeout=2).stdout.strip()
        serial_number = sn_raw if sn_raw else "Desconocido"
    except Exception:
        serial_number = "No accesible"

    # Datos de SO y tiempo
    kernel_version = platform.release()
    fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("[*] Ejecutando análisis termodinámico y estructural (Invariant)...")
    # ── CAMBIADO: parámetro renombrado nvme → storage_drives ─────────────
    entropy_data = evaluate_system_entropy(
        cpu            = cpu_data,
        gpu            = gpu_data,
        storage_drives = storage_drives,   # ← CAMBIADO
        ram            = ram_data,
        mobo           = mobo_data,
        usb            = usb_data,
        battery        = bat_data,
    )

    print("[*] Ensamblando contrato de datos...")
    report = DiagnosticReport(
        metadata=ReportMetadata(
            report_id="INV-2026-001",
            cliente_nombre="Taller Local Demo",
            cliente_email="contacto@cliente.com",
            cliente_telefono="+54 223 000-0000",
            device_brand="ASUS",
            device_model="Vivobook E1504FA",
            serial_number=serial_number,
            taller_nombre="Invariant Systems",
            tecnico_nombre="Admin",
            version="1.0.0",
            kernel_version=kernel_version,
            fecha_reporte=fecha_actual,
            duracion_analisis=f"{duracion_segundos} s"
        ),
        cpu            = cpu_data,
        gpu            = gpu_data,
        storage_drives = storage_drives,   # ← CAMBIADO (era nvme=disk_data)
        ram            = ram_data,
        motherboard    = mobo_data,
        usb            = usb_data,
        battery        = bat_data,
    )

    print("[*] Configurando motor de renderizado Jinja2-LaTeX...")
    latex_env = jinja2.Environment(
        block_start_string='[%', block_end_string='%]',
        variable_start_string='<<', variable_end_string='>>',
        comment_start_string='[#', comment_end_string='#]',
        trim_blocks=True,
        loader=jinja2.FileSystemLoader('renderer/templates')
    )

    template = latex_env.get_template('reporte_base.tex')
    context = report.to_jinja_context()
    
    # ── context.update(): las claves nvme_* siguen igual ─────────────────
    # evaluate_system_entropy todavía expone entropy_data.nvme (SubsystemVector
    # agregado), entropy_data.badge_nvme, entropy_data.accion_nvme, etc.
    # El template de la Sección 7 los consume sin modificación.
    context.update({
        "indice_anomalia":   entropy_data.total_delta_a,
        "cpu_anomalia":      entropy_data.cpu.delta_a,
        "gpu_anomalia":      entropy_data.gpu.delta_a,
        "nvme_anomalia":     entropy_data.nvme.delta_a,     # agregado multi-disco
        "ram_anomalia":      entropy_data.ram.delta_a,
        "mobo_anomalia":     entropy_data.vrm.delta_a,
        "usb_anomalia":      entropy_data.usb.delta_a,
        "bat_anomalia":      entropy_data.battery.delta_a,
        "cpu_estado_badge":  entropy_data.badge_cpu,
        "gpu_estado_badge":  entropy_data.badge_gpu,
        "nvme_estado_badge": entropy_data.badge_nvme,       # peor estado del conjunto
        "ram_estado_badge":  entropy_data.badge_ram,
        "mobo_estado_badge": entropy_data.badge_mobo,
        "usb_estado_badge":  entropy_data.badge_usb,
        "bat_estado_badge":  entropy_data.badge_bat,
        "accion_cpu":        entropy_data.accion_cpu,
        "accion_gpu":        entropy_data.accion_gpu,
        "accion_nvme":       entropy_data.accion_nvme,
        "accion_ram":        entropy_data.accion_ram,
        "accion_mobo":       entropy_data.accion_mobo,
        "accion_usb":        entropy_data.accion_usb,
        "accion_bat":        entropy_data.accion_bat,
        "accion_global":     entropy_data.accion_global,
        "estado_global_badge":    entropy_data.estado_global_badge,
        "resumen_ejecutivo":      entropy_data.resumen_ejecutivo,
        "lista_recomendaciones":  entropy_data.lista_recomendaciones,
    })

    out      = outdir or Path("/tmp")
    tex_path = out / "reporte_generado.tex"

    print(f"[*] Inyectando variables y escribiendo {tex_path} ...")
    tex_output = template.render(**context)
    tex_path.write_text(tex_output, encoding="utf-8")

    print("[*] Compilando PDF con tectonic...")
    try:
        subprocess.run(
            ["tectonic", "--outdir", str(out), str(tex_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,  # Capturamos el error
            text=True
        )
        pdf_path = out / "reporte_generado.pdf"
        print(f"[+] ÉXITO: {pdf_path}")
    except FileNotFoundError:
        raise RuntimeError("'tectonic' no encontrado. Instalar: sudo pacman -S tectonic")
    except subprocess.CalledProcessError as e:
        # Si Tectonic falla (ej. sin WiFi en Live OS), rompemos la sonda con el log
        raise RuntimeError(f"Tectonic falló la compilación:\n{e.stderr}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="probe.tex",
        description="INVARIANT — Hardware Forensic Diagnostic",
    )
    parser.add_argument(
        "--outdir",
        metavar="PATH",
        default=None,
        help=(
            "Directorio de salida para reporte_generado.pdf "
            "(default: $INVARIANT_OUT o /tmp)"
        ),
    )
    args   = parser.parse_args()
    final_outdir = _resolve_outdir(args.outdir)
    print(f"[main] INFO outdir: {final_outdir}")

    # Pasamos final_outdir explícitamente a render_pdf
    run_tui(lambda: render_pdf(final_outdir))