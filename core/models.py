"""
core/models.py
==============
Modelos de datos para probe.tex.

CHANGELOG v1.1
--------------
* NVMeData renombrada a StorageData.
* Añadido flag `is_hdd: bool = False`.
* Añadidos campos mecánicos HDD opcionales:
    hdd_spin_up_time, hdd_seek_latency_ms,
    hdd_reallocated_sectors, hdd_command_timeouts.
* NVMeData conservado como alias de compatibilidad.
* DiagnosticReport.nvme tipado como StorageData.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ════════════════════════════════════════════════════════════════════════════
#  1. METADATOS DEL REPORTE
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class ReportMetadata:
    """Identidad del reporte, cliente, taller y sistema auditado."""

    report_id: str = "N/A"
    report_hash: str = ""
    version: str = "0.0.0"
    kernel_version: str = "N/A"
    fecha_reporte: str = "N/A"
    duracion_analisis: str = "N/A"
    taller_nombre: str = "N/A"
    tecnico_nombre: str = "N/A"
    cliente_nombre: str = "N/A"
    cliente_email: str = ""
    cliente_telefono: str = ""
    device_brand: str = "N/A"
    device_model: str = "N/A"
    serial_number: str = "N/A"
    resumen_ejecutivo: str = ""
    estado_global_badge: str = r"\badgeinfo"


# ════════════════════════════════════════════════════════════════════════════
#  2. CPU
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class CPUData:
    """Datos de procesamiento central: térmico, P-States y MCE."""

    cpu_model: str = "N/A"
    cpu_cores: int = 0
    cpu_threads: int = 0
    cpu_tdp: int = 0
    cpu_socket: str = "N/A"
    cpu_t_max: float = 0.0
    cpu_t_idle: float = 0.0
    cpu_t_idle_plus5: float = 0.0
    cpu_delta_t: float = 0.0
    cpu_recovery_time: float = 0.0
    cpu_tjmax: int = 100
    cpu_tim_status: str = "N/A"
    cpu_thermal_status: str = "ok"
    datos_cpu_temp: str = ""
    cpu_pstate_measured: bool = False
    cpu_base_p0: int = 0
    cpu_sust_p0: int = 0
    cpu_dev_p0: float = 0.0
    cpu_base_p1: int = 0
    cpu_sust_p1: int = 0
    cpu_dev_p1: float = 0.0
    cpu_base_p2: int = 0
    cpu_sust_p2: int = 0
    cpu_dev_p2: float = 0.0
    cpu_throttle_freq: int = 0
    cpu_throttle_events: int = 0
    cpu_throttle_dur: int = 0
    cpu_throttle_cause: str = "N/A"
    mce_count: int = 0
    mce_banks_scanned: int = 0
    mce_last_check: str = "N/A"
    mce_tabla_filas: str = ""
    mce_recomendacion: str = ""


# ════════════════════════════════════════════════════════════════════════════
#  3. GPU
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class GPUData:
    """Datos de la GPU: temperatura, VRAM y enlace PCIe."""

    gpu_model: str = "N/A"
    gpu_vram_total: int = 0
    gpu_vram_type: str = "N/A"
    gpu_driver_version: str = "N/A"
    gpu_pcie_gen: int = 0
    gpu_pcie_width: int = 0
    gpu_t_edge: float = 0.0
    gpu_t_hotspot: float = 0.0
    gpu_delta_t_hotspot: float = 0.0
    gpu_temp_limit: int = 110
    gpu_hotspot_status: str = "ok"
    gpu_vram_tested: bool = False
    gpu_vram_seq_errors: int = 0
    gpu_vram_rand_errors: int = 0
    gpu_vram_stress_gb: int = 0
    gpu_vram_stress_errors: int = 0
    gpu_ecc_correctable: int = 0
    gpu_pcie_gen_max: int = 0
    gpu_pcie_gen_active: int = 0
    gpu_pcie_lanes_max: int = 0
    gpu_pcie_lanes_active: int = 0
    gpu_pcie_bw_max: str = "N/A"
    gpu_pcie_bw_active: str = "N/A"
    gpu_aer_correctable: int = 0
    gpu_aer_fatal: int = 0


# ════════════════════════════════════════════════════════════════════════════
#  4. ALMACENAMIENTO (SSD/NVMe o HDD mecánico)
#
#  CAMBIOS v1.1
#  ------------
#  * Clase renombrada de NVMeData → StorageData.
#  * Añadido flag `is_hdd: bool = False`.
#  * Añadidos campos mecánicos HDD (Optional — None en rutas SSD):
#      hdd_spin_up_time       → SMART ID 3  (ms)
#      hdd_seek_latency_ms    → fio randread clat_ns.mean / 1e6
#      hdd_reallocated_sectors→ SMART ID 5
#      hdd_command_timeouts   → SMART ID 188
#  * El campo `nvme_*` preexistente se preserva íntegro para compatibilidad
#    con el template Jinja2 (to_jinja_context() aplana todos los campos).
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class StorageData:
    """
    Datos de almacenamiento unificado: SSD/NVMe o HDD mecánico.

    Bifurcación de diagnóstico
    --------------------------
    is_hdd=False (default) → ruta SSD/NVMe:
      Los campos `nvme_*` se populan. Los `hdd_*` quedan en None.

    is_hdd=True → ruta HDD mecánico:
      Los campos `hdd_*` se populan (o None si el sensor no expone el dato).
      Los campos `nvme_*` quedan en sus valores por defecto (cero / False).
      El template Jinja2 bifurca la presentación con [% if is_hdd %].

    Honestidad forense HDD
    ----------------------
    None para campos mecánicos significa "atributo SMART no expuesto por
    firmware" o "fio no disponible", NO "valor cero".
    0 para hdd_reallocated_sectors o hdd_command_timeouts es un dato válido
    y positivo (sin daño físico confirmado).
    """

    # ── Tipo de dispositivo ──────────────────────────────────────────────
    is_hdd: bool = False
    """True si rotational=1 en sysfs. Controla la bifurcación completa
    del extractor, el evaluador de entropía y el template LaTeX."""

    # ── Identificación (compartida SSD/HDD) ─────────────────────────────
    nvme_device: str = "N/A"
    """Nodo de dispositivo (ej. /dev/sda, /dev/nvme0n1)."""

    nvme_model: str = "N/A"
    nvme_capacity: int = 0
    nvme_firmware: str = "N/A"
    nvme_tbw_remaining: float = 0.0
    nvme_tbw_rated: float = 0.0
    nvme_hours: int = 0

    # ── Histograma de latencia I/O — buckets (SSD/NVMe únicamente) ──────
    lat_b0: int = 0
    lat_b1: int = 0
    lat_b2: int = 0
    lat_b3: int = 0
    lat_b4: int = 0
    lat_b5: int = 0
    lat_b6: int = 0
    lat_b7: int = 0
    lat_b8: int = 0
    lat_b9: int = 0

    nvme_lat_p50: float = 0.0
    nvme_lat_p95: float = 0.0
    nvme_lat_p99: float = 0.0
    nvme_lat_p999: float = 0.0

    # ── WAF y métricas de desgaste SMART (SSD/NVMe únicamente) ──────────
    nvme_waf: float = 0.0
    nvme_waf_ok: bool = False
    nvme_lba_written: float = 0.0
    nvme_nand_written: float = 0.0
    nvme_bad_blocks: int = 0
    nvme_bad_blocks_ok: bool = False
    nvme_spare_blocks: int = 0
    nvme_spare_ok: bool = False
    nvme_ecc_errors: int = 0
    nvme_ecc_ok: bool = False
    nvme_t_max: float = 0.0
    nvme_temp_ok: bool = False
    nvme_life_pct: int = 0
    nvme_life_ok: bool = False

    # ── Cinemática mecánica HDD (is_hdd=True; None en rutas SSD) ────────
    hdd_spin_up_time: Optional[int] = None
    """Tiempo de arranque del motor (ms). SMART ID 3 (raw value).
    None si el atributo no está expuesto por el firmware del disco."""

    hdd_seek_latency_ms: Optional[float] = None
    """Latencia media de seek aleatorio medida con fio randread 4K QD1 (ms).
    Derivada de clat_ns.mean / 1e6.
    Umbral de degradación física: >25 ms (cabezal con pérdida de agilidad).
    None si fio no está instalado o la medición falló."""

    hdd_reallocated_sectors: Optional[int] = None
    """Sectores reasignados (Reallocated Sector Count, SMART ID 5).
    Cualquier valor >0 implica daño físico confirmado en la superficie
    del plato magnético. 0 es un resultado válido y óptimo.
    None si el atributo no existe en la tabla SMART del disco."""

    hdd_command_timeouts: Optional[int] = None
    """Timeouts de comandos acumulados (Command Timeout, SMART ID 188).
    Indican fallos del actuador o inestabilidad mecánica del conjunto
    cabezal-brazo. 0 es óptimo. None si no disponible."""


# Alias de compatibilidad — permite importar NVMeData en código heredado
# sin modificaciones. Eliminar en v2.0.
NVMeData = StorageData


# ════════════════════════════════════════════════════════════════════════════
#  5. RAM
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class RAMData:
    """Datos de memoria principal: EDAC, integridad y timing."""

    ram_total_gb: int = 0
    ram_type: str = "N/A"
    ram_speed: int = 0
    ram_slots_used: int = 0
    ram_slots_total: int = 0
    ram_dual_channel: bool = False
    ram_edac_filas: str = ""
    ram_total_errors: int = 0
    ram_speed_effective: int = 0
    ram_cas_ns: Optional[float] = None
    ram_test_method: str = "N/A"
    ram_pie_angle: float = 360.0
    ram_integrity_pct: float = 100.0
    ram_fail_pct: float = 0.0


# ════════════════════════════════════════════════════════════════════════════
#  6. PLACA BASE / VRM
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class MotherboardData:
    """Datos de placa base: identificación BIOS y análisis de VRM."""

    mobo_manufacturer: str = "N/A"
    mobo_model: str = "N/A"
    mobo_chipset: str = "N/A"
    bios_version: str = "N/A"
    bios_date: str = "N/A"
    vrm_tol_low: float = 0.0
    vrm_tol_high: float = 0.0
    datos_vrm_vid: str = ""
    datos_vrm_medido: str = ""
    vrm_droop_t: float = 0.0
    vrm_droop_v: float = 0.0
    vrm_vdroop_max: float = 0.0
    vrm_temp: Optional[float] = None
    vrm_phases: Optional[int] = None
    vdroop_status: str = "N/A"
    vrm_status: str = "ok"


# ════════════════════════════════════════════════════════════════════════════
#  7. BATERÍA
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class BatteryData:
    """Datos de batería (sección 5.2). Se omite si battery_present=False."""

    battery_present: bool = False
    bat_design_cap: int = 0
    bat_full_cap: int = 0
    bat_cycles: Optional[int] = None
    bat_soh: int = 0
    bat_voltage_nom: float = 0.0
    bat_voltage_load: float = 0.0
    bat_voltage_drop: Optional[float] = None
    bat_current_load: int = 0
    bat_resistance: Optional[float] = None
    bat_gauge_color: str = "StatusOKMid"
    bat_gauge_fill: float = 0.0


# ════════════════════════════════════════════════════════════════════════════
#  8. BUS USB / PERIFÉRICOS
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class USBData:
    """Datos de integridad del bus USB y periféricos (sección 6.1)."""

    usb_tabla_filas: str = ""
    usb_ok: int = 0
    usb_warn: int = 0
    usb_fail: int = 0


# ════════════════════════════════════════════════════════════════════════════
#  9. RESUMEN GLOBAL
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class GlobalSummary:
    """Scores, badges y acciones por componente + tabla global + recomendaciones."""

    score_cpu: int = 0
    score_gpu: int = 0
    score_nvme: int = 0
    score_ram: int = 0
    score_mobo: int = 0
    score_usb: int = 0
    score_global: int = 0

    badge_cpu: str = r"\badgeinfo"
    badge_gpu: str = r"\badgeinfo"
    badge_nvme: str = r"\badgeinfo"
    badge_ram: str = r"\badgeinfo"
    badge_mobo: str = r"\badgeinfo"
    badge_usb: str = r"\badgeinfo"
    badge_global: str = r"\badgeinfo"

    accion_cpu: str = "N/A"
    accion_gpu: str = "N/A"
    accion_nvme: str = "N/A"
    accion_ram: str = "N/A"
    accion_mobo: str = "N/A"
    accion_usb: str = "N/A"
    accion_global: str = "N/A"

    lista_recomendaciones: str = ""


# ════════════════════════════════════════════════════════════════════════════
#  10. DIAGNOSTIC REPORT — RAÍZ COMPOSITORA
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class DiagnosticReport:
    """
    Raíz del modelo de datos de probe.tex.

    CHANGELOG v1.2
    --------------
    * nvme: StorageData  →  storage_drives: list[StorageData]
      Soporta análisis multi-disco: NVMe + SATA SSD + HDD mecánicos
      detectados por _enumerate_storage_devices() en main.py.
    * to_jinja_context() aplana todos los sub-modelos EXCEPTO storage_drives.
      La lista se serializa por separado como ``storage_list``
      (list[dict]) para habilitar iteración Jinja2 sin colisiones de nombres.
    """

    metadata:       ReportMetadata  = field(default_factory=ReportMetadata)
    cpu:            CPUData         = field(default_factory=CPUData)
    gpu:            GPUData         = field(default_factory=GPUData)
    storage_drives: list            = field(default_factory=list)
    """list[StorageData] — una entrada por unidad física detectada.
    El tipo se declara como ``list`` (sin parámetro) para compatibilidad
    con dataclasses.fields(), que no itera elementos de la lista."""
    ram:            RAMData         = field(default_factory=RAMData)
    motherboard:    MotherboardData = field(default_factory=MotherboardData)
    battery:        BatteryData     = field(default_factory=BatteryData)
    usb:            USBData         = field(default_factory=USBData)
    summary:        GlobalSummary   = field(default_factory=GlobalSummary)

    def to_jinja_context(self) -> dict:
        """
        Aplana el árbol de dataclasses en un dict plano para Jinja2.

        Arquitectura de aplanamiento
        ----------------------------
        Sub-modelos escalares (metadata, cpu, gpu, ram, motherboard,
        battery, usb, summary) → sus campos se inyectan directamente
        en el contexto raíz con el nombre del campo como clave.

        storage_drives → NO se aplana de forma escalar.
          El aplanamiento escalar de una lista de dataclasses colisionaría:
          si dos discos tienen ``nvme_model``, el segundo sobreescribiría
          al primero. En su lugar se construye ``storage_list``:

            storage_list: list[dict]
              Cada elemento es el dict plano de un StorageData individual.
              Ejemplo de acceso en Jinja2:
                [% for drive in storage_list %]
                  << drive.nvme_model >>
                [% endfor %]

        Returns
        -------
        dict
            Contexto plano listo para ``template.render(**context)``.
        """
        import dataclasses

        context: dict = {}

        # ── Aplanamiento escalar de sub-modelos individuales ──────────────
        # storage_drives queda FUERA de esta lista intencionalmente.
        scalar_models = [
            self.metadata,
            self.cpu,
            self.gpu,
            self.ram,
            self.motherboard,
            self.battery,
            self.usb,
            self.summary,
        ]
        for model in scalar_models:
            for f in dataclasses.fields(model):
                context[f.name] = getattr(model, f.name)

        # ── Serialización de storage_drives como lista de dicts ───────────
        # Cada StorageData se convierte en un dict plano {campo: valor}.
        # Jinja2 acepta tanto acceso por atributo (drive.nvme_model)
        # como por clave (drive['nvme_model']) sobre dicts; ambas notaciones
        # funcionan con el motor de plantillas configurado en main.py.
        storage_list: list[dict] = []
        for drive in self.storage_drives:
            drive_dict: dict = {}
            for f in dataclasses.fields(drive):
                drive_dict[f.name] = getattr(drive, f.name)
            storage_list.append(drive_dict)

        # Garantía: al menos un dict vacío para que el loop Jinja2 no rompa.
        if not storage_list:
            import dataclasses as _dc
            from core.models import StorageData as _SD
            storage_list = [
                {f.name: getattr(_SD(), f.name) for f in _dc.fields(_SD())}
            ]

        context["storage_list"] = storage_list
        return context