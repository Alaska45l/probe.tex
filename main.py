import time
import platform
import subprocess
import concurrent.futures
from datetime import datetime
from pathlib import Path
import jinja2

from core.models import DiagnosticReport, ReportMetadata, GlobalSummary
from core.entropy import evaluate_system_entropy
from extractors.cpu_reader import extract_cpu_data
from extractors.disk_reader import extract_disk_data
from extractors.motherboard_reader import extract_motherboard_data
from extractors.ram_reader import extract_ram_data
from extractors.gpu_reader import extract_gpu_data
from extractors.usb_reader import extract_usb_data
from extractors.battery_reader import extract_battery_data
from tui import run_tui

def render_pdf():
    print("[*] Iniciando motor de extracción concurrente Invariant...")
    start_time = time.time()

    # Ejecución paralela de todos los extractores
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
        future_cpu  = executor.submit(extract_cpu_data)
        future_gpu  = executor.submit(extract_gpu_data)
        future_disk = executor.submit(extract_disk_data, "/dev/nvme0n1")
        future_ram  = executor.submit(extract_ram_data)
        future_mobo = executor.submit(extract_motherboard_data)
        future_usb  = executor.submit(extract_usb_data)
        future_bat  = executor.submit(extract_battery_data)

        # Se espera a que todos terminen (sincronización de hilos)
        cpu_data  = future_cpu.result()
        gpu_data  = future_gpu.result()
        disk_data = future_disk.result()
        ram_data  = future_ram.result()
        mobo_data = future_mobo.result()
        usb_data  = future_usb.result()
        bat_data  = future_bat.result()

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

    print("[*] Inyectando variables y escribiendo archivo .tex...")
    tex_output = template.render(**context)
    Path('reporte_generado.tex').write_text(tex_output, encoding='utf-8')

    print("[*] Compilando PDF...")
    try:
        # Tectonic no necesita flags complejos, lo hace todo solo
        subprocess.run(
            ['tectonic', 'reporte_generado.tex'], 
            check=True, 
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print("[+] ÉXITO: reporte_generado.pdf creado correctamente.")
    except FileNotFoundError:
        print("[-] ERROR: No se encontró el comando 'tectonic'. Instálalo con: sudo pacman -S tectonic")
    except subprocess.CalledProcessError:
        print("[-] ERROR: Fallo de compilación. Revisa reporte_generado.tex")

if __name__ == '__main__':
    run_tui(render_pdf)