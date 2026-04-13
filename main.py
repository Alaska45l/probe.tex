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
    NVMeData, RAMData, MotherboardData, USBData, BatteryData
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

def _detect_nvme_device() -> str:
    """
    Detecta el dispositivo de almacenamiento primario del sistema.

    Prioridad: NVMe (más rápido, más común en sistemas modernos) → SATA SSD → HDD.
    Usa /sys/class/block para enumerar sin depender de herramientas externas.
    El "primario" se define como el disco donde está montado /  (rootfs).
    """
    import subprocess, re
    try:
        # lsblk JSON es el método más robusto y portable
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,TYPE,MOUNTPOINT"],
            capture_output=True, text=True, timeout=5
        ).stdout
        data = json.loads(out)
        for dev in data.get("blockdevices", []):
            if dev.get("type") != "disk":
                continue
            # Buscar si alguna partición tiene mountpoint "/"
            for child in dev.get("children", []):
                if child.get("mountpoint") == "/":
                    return f"/dev/{dev['name']}"
    except Exception:
        pass

    # Fallback: primer NVMe en /sys/class/nvme/
    try:
        nvme_root = Path("/sys/class/nvme")
        if nvme_root.exists():
            for ctrl in sorted(nvme_root.iterdir()):
                # Cada controlador NVMe tiene al menos un namespace nvme0n1
                ns = sorted(ctrl.glob("nvme*n1"))
                if ns:
                    return f"/dev/{ns[0].name}"
    except Exception:
        pass

    print("[main] WARN no se detectó dispositivo de almacenamiento primario. Usando /dev/nvme0n1.")
    return "/dev/nvme0n1"

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

    # Timeouts agresivos por extractor (en segundos)
    _TIMEOUTS: dict[str, int] = {
        "disk": 130,    # fio 15s + smartctl 5s + overhead
        "ram":  620,    # memtester 600s + dmidecode
        "mobo": 40,
        "usb":  15,
        "bat":  15,
        "cpu":  70,     # stress 30s + cooling 15s + overhead
        "gpu":  45,     # stress 20s + cooling 10s + overhead
    }

    # FASE 1: extractores sin carga activa (paralelos, seguros)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_disk = executor.submit(extract_disk_data, _detect_nvme_device())
        future_ram  = executor.submit(extract_ram_data)
        future_mobo = executor.submit(extract_motherboard_data)
        future_usb  = executor.submit(extract_usb_data)
        future_bat  = executor.submit(extract_battery_data)

        try:
            disk_data = future_disk.result(timeout=_TIMEOUTS["disk"])
        except concurrent.futures.TimeoutError:
            print("[main] WARN extractor disk superó timeout. Usando NVMeData() vacío.")
            disk_data = NVMeData()
        try:
            ram_data = future_ram.result(timeout=_TIMEOUTS["ram"])
        except concurrent.futures.TimeoutError:
            print("[main] WARN extractor ram superó timeout. Usando RAMData() vacío.")
            ram_data = RAMData()
        try:
            mobo_data = future_mobo.result(timeout=_TIMEOUTS["mobo"])
        except concurrent.futures.TimeoutError:
            print("[main] WARN extractor mobo superó timeout. Usando MotherboardData() vacío.")
            mobo_data = MotherboardData()
        try:
            usb_data = future_usb.result(timeout=_TIMEOUTS["usb"])
        except concurrent.futures.TimeoutError:
            print("[main] WARN extractor usb superó timeout. Usando USBData() vacío.")
            usb_data = USBData()
        try:
            bat_data = future_bat.result(timeout=_TIMEOUTS["bat"])
        except concurrent.futures.TimeoutError:
            print("[main] WARN extractor bat superó timeout. Usando BatteryData() vacío.")
            bat_data = BatteryData()

    print("[*] Extracción estática completada. Iniciando forense activo secuencial...")

    # FASE 2: tests térmicos SECUENCIALES — CPU primero, luego GPU
    # Sin solapamiento garantizado: el estrés de CPU no contamina GPU y viceversa.
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
    entropy_data = evaluate_system_entropy(
        cpu=cpu_data, 
        gpu=gpu_data, 
        nvme=disk_data, 
        ram=ram_data, 
        mobo=mobo_data, 
        usb=usb_data,
        battery=bat_data
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
        cpu=cpu_data,
        gpu=gpu_data,
        nvme=disk_data,
        ram=ram_data,
        motherboard=mobo_data,
        usb=usb_data,
        battery=bat_data
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
    
    # --- INYECCIÓN DEL MOTOR INVARIANT AL CONTEXTO DE LATEX ---
    context.update({
        "indice_anomalia": entropy_data.total_delta_a,
        "cpu_anomalia": entropy_data.cpu.delta_a,
        "gpu_anomalia": entropy_data.gpu.delta_a,
        "nvme_anomalia": entropy_data.nvme.delta_a,
        "ram_anomalia": entropy_data.ram.delta_a,
        "mobo_anomalia": entropy_data.vrm.delta_a,
        "usb_anomalia": entropy_data.usb.delta_a,
        "bat_anomalia": entropy_data.battery.delta_a,
        "cpu_estado_badge": entropy_data.badge_cpu,
        "gpu_estado_badge": entropy_data.badge_gpu,
        "nvme_estado_badge": entropy_data.badge_nvme,
        "ram_estado_badge": entropy_data.badge_ram,
        "mobo_estado_badge": entropy_data.badge_mobo,
        "usb_estado_badge": entropy_data.badge_usb,
        "bat_estado_badge": entropy_data.badge_bat,
        "accion_cpu": entropy_data.accion_cpu,
        "accion_gpu": entropy_data.accion_gpu,
        "accion_nvme": entropy_data.accion_nvme,
        "accion_ram": entropy_data.accion_ram,
        "accion_mobo": entropy_data.accion_mobo,
        "accion_usb": entropy_data.accion_usb,
        "accion_bat": entropy_data.accion_bat,
        "accion_global": entropy_data.accion_global,
        "estado_global_badge": entropy_data.estado_global_badge,
        "resumen_ejecutivo": entropy_data.resumen_ejecutivo,
        "lista_recomendaciones": entropy_data.lista_recomendaciones,
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