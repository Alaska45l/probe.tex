"""
core/models.py
==============
Modelos de datos para probe.tex.

Cada dataclass representa un dominio de hardware extraído del template
``reporte_base.tex`` (Jinja2, delimitadores ``<< >>`` / ``[% %]``).

Principios de diseño
--------------------
* Stdlib pura — sin dependencias externas.
* Type hints estrictos; ``str | None`` nunca se usa: se prefieren valores
  por defecto seguros ("N/A", 0, 0.0, False) para garantizar
  *Graceful Degradation* cuando un extractor falla.
* Las variables de renderizado LaTeX (filas de tabla, coordenadas pgfplots,
  nombres de color TikZ) se tipan como ``str`` con default ``""``.
* Los flags de control de flujo Jinja2 (``[% if ... %]``) se tipan como
  ``bool`` con default ``False``.
* Los *scores* son ``int`` en rango 0-100; los *badges* son ``str`` con
  la macro LaTeX pre-renderizada (ej. ``\\badgeok``).

Grupos de clases
----------------
1.  ReportMetadata       — identidad, cliente, taller, sistema
2.  CPUData              — térmico, throttling, P-States, MCE
3.  GPUData              — temperatura, VRAM, PCIe/AER
4.  NVMeData             — latencia I/O, desgaste SMART, WAF
5.  RAMData              — EDAC, integridad, timing
6.  MotherboardData      — VRM, BIOS, chipset
7.  BatteryData          — SoH, voltaje, resistencia interna
8.  USBData              — tabla de puertos, resumen de estado
9.  GlobalSummary        — scores, badges y acciones por componente
10. DiagnosticReport     — raíz compositora de todo el informe
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ════════════════════════════════════════════════════════════════════════════
#  1. METADATOS DEL REPORTE
#     Variables: report_id, report_hash, version, kernel_version,
#                fecha_reporte, duracion_analisis, taller_nombre,
#                tecnico_nombre, cliente_nombre, cliente_email,
#                cliente_telefono, device_brand, device_model,
#                serial_number, resumen_ejecutivo, estado_global_badge
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class ReportMetadata:
    """Identidad del reporte, cliente, taller y sistema auditado."""

    # ── Identificadores del reporte ──────────────────────────────────────
    report_id: str = "N/A"
    """ID único del reporte (ej. ``AM-2024-00001``). Aparece en portada,
    encabezado, pie de página y firma técnica."""

    report_hash: str = ""
    """Hash SHA-256 del bundle del reporte para verificación de integridad.
    Se renderiza en fuente ``\\tiny`` en la firma final."""

    version: str = "0.0.0"
    """Versión de probe.tex que generó el informe
    (ej. ``1.4.2``). Aparece en encabezado, pie y firma."""

    kernel_version: str = "N/A"
    """Versión del kernel Linux del entorno Live donde se ejecutó la
    auditoría (ej. ``6.8.0-45-generic``)."""

    # ── Fechas y duración ────────────────────────────────────────────────
    fecha_reporte: str = "N/A"
    """Fecha de emisión en formato legible (ej. ``15 de julio de 2025``).
    Aparece en portada, encabezado y firma."""

    duracion_analisis: str = "N/A"
    """Tiempo total del análisis (ej. ``4 min 32 s``). Sólo en firma."""

    # ── Taller / técnico ─────────────────────────────────────────────────
    taller_nombre: str = "N/A"
    """Nombre del taller o empresa emisora del diagnóstico."""

    tecnico_nombre: str = "N/A"
    """Nombre del técnico certificado responsable del diagnóstico."""

    # ── Datos del cliente ────────────────────────────────────────────────
    cliente_nombre: str = "N/A"
    """Nombre completo del cliente. Aparece en portada y encabezado."""

    cliente_email: str = ""
    """Correo electrónico del cliente (portada)."""

    cliente_telefono: str = ""
    """Teléfono de contacto del cliente (portada)."""

    # ── Dispositivo auditado ─────────────────────────────────────────────
    device_brand: str = "N/A"
    """Marca del equipo (ej. ``Dell``, ``Lenovo``). Portada."""

    device_model: str = "N/A"
    """Modelo del equipo (ej. ``XPS 15 9530``). Portada."""

    serial_number: str = "N/A"
    """Número de serie del equipo. Portada."""

    # ── Resumen ejecutivo (portada) ──────────────────────────────────────
    resumen_ejecutivo: str = ""
    """Párrafo de diagnóstico general de 1-3 líneas que aparece en el
    cuadro de veredicto de la portada."""

    estado_global_badge: str = r"\badgeinfo"
    """Macro LaTeX del badge de estado global para la portada
    (ej. ``\\badgeok``, ``\\badgewarn``, ``\\badgefail``)."""


# ════════════════════════════════════════════════════════════════════════════
#  2. CPU
#     Variables: cpu_model, cpu_cores, cpu_threads, cpu_tdp, cpu_socket,
#                cpu_t_max, cpu_t_idle, cpu_t_idle_plus5, cpu_delta_t,
#                cpu_recovery_time, cpu_tjmax, cpu_tim_status,
#                cpu_thermal_status, datos_cpu_temp,
#                cpu_base_p0/p1/p2, cpu_sust_p0/p1/p2,
#                cpu_dev_p0/p1/p2, cpu_throttle_freq,
#                cpu_throttle_events, cpu_throttle_dur, cpu_throttle_cause,
#                mce_count, mce_banks_scanned, mce_last_check,
#                mce_tabla_filas, mce_recomendacion
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class CPUData:
    """Datos de procesamiento central: térmico, P-States y MCE."""

    # ── Identificación ───────────────────────────────────────────────────
    cpu_model: str = "N/A"
    """Nombre del procesador (ej. ``Intel Core i7-13700H``).
    Aparece en la banda ``componentinfo`` de la sección 1."""

    cpu_cores: int = 0
    """Número de núcleos físicos."""

    cpu_threads: int = 0
    """Número de hilos lógicos (Hyper-Threading)."""

    cpu_tdp: int = 0
    """TDP nominal del procesador en Watts."""

    cpu_socket: str = "N/A"
    """Socket físico (ej. ``LGA1700``, ``AM5``)."""

    # ── Estabilidad térmica (sección 1.1) ────────────────────────────────
    cpu_t_max: float = 0.0
    """Temperatura máxima registrada bajo carga (°C)."""

    cpu_t_idle: float = 0.0
    """Temperatura en reposo medida (°C). Se usa como línea de referencia
    en el gráfico pgfplots y en la tabla de parámetros."""

    cpu_t_idle_plus5: float = 0.0
    """``cpu_t_idle + 5`` (°C). Pre-calculado en Python; se inyecta como
    ``extra y tick`` en pgfplots para la línea de advertencia."""

    cpu_delta_t: float = 0.0
    """ΔT de recuperación = T_max − T_idle (°C). Umbral crítico: 15 °C."""

    cpu_recovery_time: float = 0.0
    """Tiempo en segundos para que la CPU vuelva a T_idle tras la carga."""

    cpu_tjmax: int = 100
    """Temperatura de unión máxima (TjMax) del procesador (°C).
    Define la zona roja en el gráfico."""

    cpu_tim_status: str = "N/A"
    """Estimación del estado de la pasta térmica
    (ej. ``Aceptable``, ``Degradada``, ``Requiere reemplazo``)."""

    cpu_thermal_status: str = "ok"
    """Estado lógico del subsistema térmico para el bloque condicional
    Jinja2: ``"ok"``, ``"warn"`` o ``"crit"``."""

    datos_cpu_temp: str = ""
    """Coordenadas pgfplots de la curva de temperatura (serie temporal).
    Formato: ``(0,45) (10,72) (20,68) ...`` Pre-renderizado por Python."""

    cpu_pstate_measured: bool = False
    """True si los relojes sostenidos fueron medidos vía cpufreq, False si estimados."""

    # ── P-States y throttling (sección 1.2) ─────────────────────────────
    cpu_base_p0: int = 0
    """Reloj base (MHz) del modo P0 (máximo rendimiento)."""

    cpu_sust_p0: int = 0
    """Reloj sostenido (MHz) medido en P0."""

    cpu_dev_p0: float = 0.0
    """Desviación porcentual del reloj sostenido respecto al base en P0."""

    cpu_base_p1: int = 0
    """Reloj base (MHz) del modo P1 (balanceado)."""

    cpu_sust_p1: int = 0
    """Reloj sostenido (MHz) medido en P1."""

    cpu_dev_p1: float = 0.0
    """Desviación porcentual en P1."""

    cpu_base_p2: int = 0
    """Reloj base (MHz) del modo P2 (ahorro de energía)."""

    cpu_sust_p2: int = 0
    """Reloj sostenido (MHz) medido en P2."""

    cpu_dev_p2: float = 0.0
    """Desviación porcentual en P2."""

    cpu_throttle_freq: int = 0
    """Frecuencia de reloj durante el throttling térmico (TJ90) en MHz."""

    cpu_throttle_events: int = 0
    """Número de eventos de throttling registrados durante la prueba.
    Controla el bloque condicional ``[% if cpu_throttle_events == 0 %]``."""

    cpu_throttle_dur: int = 0
    """Duración acumulada de todos los eventos de throttling en ms."""

    cpu_throttle_cause: str = "N/A"
    """Causa principal del throttling
    (ej. ``Temperatura``, ``Límite de potencia``, ``Ninguna``)."""

    # ── Machine Check Exceptions — MCE (sección 1.3) ─────────────────────
    mce_count: int = 0
    """Total de MCE detectados. Controla el bloque
    ``[% if mce_count == 0 %]``."""

    mce_banks_scanned: int = 0
    """Número de bancos MCE escaneados por el kernel."""

    mce_last_check: str = "N/A"
    """Timestamp de la última verificación del log MCE."""

    mce_tabla_filas: str = ""
    """Filas LaTeX pre-renderizadas de la tabla de errores MCE.
    Sólo se inserta cuando ``mce_count > 0``."""

    mce_recomendacion: str = ""
    """Texto de recomendación para el bloque ``amboxcrit`` de MCE."""


# ════════════════════════════════════════════════════════════════════════════
#  3. GPU
#     Variables: gpu_model, gpu_vram_total, gpu_vram_type,
#                gpu_driver_version, gpu_pcie_gen, gpu_pcie_width,
#                gpu_t_edge, gpu_t_hotspot, gpu_delta_t_hotspot,
#                gpu_temp_limit, gpu_hotspot_status,
#                gpu_vram_seq_errors, gpu_vram_rand_errors,
#                gpu_vram_stress_gb, gpu_vram_stress_errors,
#                gpu_ecc_correctable,
#                gpu_pcie_gen_max, gpu_pcie_gen_active,
#                gpu_pcie_lanes_max, gpu_pcie_lanes_active,
#                gpu_pcie_bw_max, gpu_pcie_bw_active,
#                gpu_aer_correctable, gpu_aer_fatal
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class GPUData:
    """Datos de la GPU: temperatura, VRAM y enlace PCIe."""

    # ── Identificación ───────────────────────────────────────────────────
    gpu_model: str = "N/A"
    """Nombre de la GPU (ej. ``NVIDIA GeForce RTX 4060``)."""

    gpu_vram_total: int = 0
    """Capacidad total de VRAM en GB. Aparece en la banda de info y en
    las tablas de integridad."""

    gpu_vram_type: str = "N/A"
    """Tipo de memoria de vídeo (ej. ``GDDR6``, ``GDDR6X``)."""

    gpu_driver_version: str = "N/A"
    """Versión del driver de GPU instalado (ej. ``545.29.06``)."""

    gpu_pcie_gen: int = 0
    """Generación PCIe del slot según la especificación del dispositivo
    (banda de info de la sección, no la tabla AER)."""

    gpu_pcie_width: int = 0
    """Ancho de banda PCIe declarado (×16, ×8…) en la banda de info."""

    # ── Delta de temperatura del hotspot (sección 2.1) ────────────────────
    gpu_t_edge: float = 0.0
    """Temperatura del borde de la GPU bajo carga (°C).
    Barra del gráfico pgfplots xbar."""

    gpu_t_hotspot: float = 0.0
    """Temperatura del hotspot de la GPU bajo carga (°C)."""

    gpu_delta_t_hotspot: float = 0.0
    """ΔT hotspot = T_hotspot − T_edge (°C).
    Umbral de advertencia: 20 °C."""

    gpu_temp_limit: int = 110
    """Límite de temperatura GPU definido por el fabricante (°C).
    Se renderiza como ``extra x tick`` en el gráfico."""

    gpu_hotspot_status: str = "ok"
    """Estado del hotspot para el bloque condicional:
    ``"ok"``, ``"warn"`` o ``"crit"``."""

    gpu_vram_tested: bool = False
    """True solo si se ejecutó un test real de VRAM. Controla el bloque
    condicional [% if gpu_vram_tested %] en el template."""

    # ── Integridad de VRAM (sección 2.2) ─────────────────────────────────
    gpu_vram_seq_errors: int = 0
    """Errores en la prueba de escritura/lectura secuencial de VRAM."""

    gpu_vram_rand_errors: int = 0
    """Errores en la prueba de patrón aleatorio de VRAM."""

    gpu_vram_stress_gb: int = 0
    """GB probados en el test de stress combinado (30 s)."""

    gpu_vram_stress_errors: int = 0
    """Errores en el test de stress combinado de VRAM."""

    gpu_ecc_correctable: int = 0
    """Contador de errores ECC corregibles registrados en hardware."""

    # ── PCIe y errores AER (sección 2.3) ─────────────────────────────────
    gpu_pcie_gen_max: int = 0
    """Generación PCIe máxima soportada por la GPU (tabla AER)."""

    gpu_pcie_gen_active: int = 0
    """Generación PCIe activa negociada con el slot."""

    gpu_pcie_lanes_max: int = 0
    """Número máximo de lanes PCIe soportados (tabla AER)."""

    gpu_pcie_lanes_active: int = 0
    """Lanes PCIe activos en la sesión. Si < gpu_pcie_lanes_max → WARN."""

    gpu_pcie_bw_max: str = "N/A"
    """Ancho de banda PCIe máximo teórico (ej. ``32 GB/s``)."""

    gpu_pcie_bw_active: str = "N/A"
    """Ancho de banda PCIe activo medido."""

    gpu_aer_correctable: int = 0
    """Errores AER corregibles detectados en el enlace PCIe."""

    gpu_aer_fatal: int = 0
    """Errores AER fatales. Si > 0 → bloque ``amboxcrit``."""


# ════════════════════════════════════════════════════════════════════════════
#  4. NVMe / SSD
#     Variables: nvme_device, nvme_model, nvme_capacity, nvme_firmware,
#                nvme_tbw_remaining, nvme_tbw_rated, nvme_hours,
#                lat_b0..lat_b9,
#                nvme_lat_p50, nvme_lat_p95, nvme_lat_p99, nvme_lat_p999,
#                nvme_waf, nvme_waf_ok,
#                nvme_lba_written, nvme_nand_written,
#                nvme_bad_blocks, nvme_bad_blocks_ok,
#                nvme_spare_blocks, nvme_spare_ok,
#                nvme_ecc_errors, nvme_ecc_ok,
#                nvme_t_max, nvme_temp_ok,
#                nvme_life_pct, nvme_life_ok
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class NVMeData:
    """Datos del almacenamiento NVMe/SSD: latencia I/O y desgaste SMART."""

    # ── Identificación ───────────────────────────────────────────────────
    nvme_device: str = "N/A"
    """Nodo de dispositivo (ej. ``/dev/nvme0n1``).
    Se renderiza en ``\\texttt`` en la banda de info."""

    nvme_model: str = "N/A"
    """Modelo del dispositivo NVMe (ej. ``Samsung 990 Pro 1TB``)."""

    nvme_capacity: int = 0
    """Capacidad total del dispositivo en GB."""

    nvme_firmware: str = "N/A"
    """Versión de firmware del dispositivo."""

    nvme_tbw_remaining: float = 0.0
    """TBW (Terabytes Written) restantes según el rated vs. el uso real."""

    nvme_tbw_rated: float = 0.0
    """TBW total con garantía del fabricante."""

    nvme_hours: int = 0
    """Horas de encendido acumuladas (Power-On Hours)."""

    # ── Histograma de latencia I/O — buckets (sección 3.1) ───────────────
    # Cada bucket corresponde a un rango de latencia en µs:
    # b0: <1, b1: 1-2, b2: 2-4, b3: 4-8, b4: 8-16,
    # b5: 16-32, b6: 32-64, b7: 64-128, b8: 128-256, b9: >256
    # Si el valor es 0, Jinja2 inyecta 1 para evitar log(0) en pgfplots.
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

    # ── Percentiles de latencia ──────────────────────────────────────────
    nvme_lat_p50: float = 0.0
    """Latencia P50 (mediana) en µs."""

    nvme_lat_p95: float = 0.0
    """Latencia P95 en µs."""

    nvme_lat_p99: float = 0.0
    """Latencia P99 en µs."""

    nvme_lat_p999: float = 0.0
    """Latencia P99.9 en µs."""

    # ── WAF y métricas de desgaste SMART (sección 3.2) ───────────────────
    nvme_waf: float = 0.0
    """Write Amplification Factor. 0.0 significa "no disponible", NO waf=1.0."""

    nvme_waf_ok: bool = False
    """``True`` si nvme_waf ≤ 3.0. Controla el badge en la tabla."""

    nvme_lba_written: float = 0.0
    """Total de datos escritos por el host en TB (LBA host writes)."""

    nvme_nand_written: float = 0.0
    """Total de datos escritos físicamente en NAND en TB. 0.0 significa "no disponible"."""

    nvme_bad_blocks: int = 0
    """Bloques NAND defectuosos. Umbral de OK: ≤ 50."""

    nvme_bad_blocks_ok: bool = False
    """``True`` si nvme_bad_blocks ≤ 50."""

    nvme_spare_blocks: int = 0
    """Spare blocks (bloques de repuesto) disponibles. Umbral: ≥ 10."""

    nvme_spare_ok: bool = False
    """``True`` si nvme_spare_blocks ≥ 10."""

    nvme_ecc_errors: int = 0
    """Errores ECC corregibles acumulados. Umbral de OK: < 100."""

    nvme_ecc_ok: bool = False
    """``True`` si nvme_ecc_errors < 100."""

    nvme_t_max: float = 0.0
    """Temperatura máxima registrada en el historial SMART (°C).
    Umbral de OK: < 70 °C."""

    nvme_temp_ok: bool = False
    """``True`` si nvme_t_max < 70."""

    nvme_life_pct: int = 0
    """Porcentaje de vida útil restante según SMART. Umbral de OK: ≥ 20 %."""

    nvme_life_ok: bool = False
    """``True`` si nvme_life_pct ≥ 20."""


# ════════════════════════════════════════════════════════════════════════════
#  5. RAM
#     Variables: ram_total_gb, ram_type, ram_speed, ram_slots_used,
#                ram_slots_total, ram_dual_channel, ram_edac_filas,
#                ram_total_errors, ram_speed_effective, ram_cas_ns,
#                ram_test_method, ram_pie_angle, ram_integrity_pct,
#                ram_fail_pct
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class RAMData:
    """Datos de memoria principal: EDAC, integridad y timing."""

    # ── Identificación ───────────────────────────────────────────────────
    ram_total_gb: int = 0
    """Capacidad total de RAM instalada en GB."""

    ram_type: str = "N/A"
    """Tipo de memoria (ej. ``DDR4``, ``DDR5``, ``LPDDR5``)."""

    ram_speed: int = 0
    """Velocidad nominal en MHz (ej. ``3200``, ``4800``)."""

    ram_slots_used: int = 0
    """Número de slots de DIMM ocupados."""

    ram_slots_total: int = 0
    """Número de slots de DIMM disponibles en la placa."""

    ram_dual_channel: bool = False
    """``True`` si el controlador de memoria opera en modo Dual Channel.
    Controla el badge ``\\badgeok`` / ``\\badgefail`` en la banda de info."""

    # ── EDAC / errores (sección 4.1) ─────────────────────────────────────
    ram_edac_filas: str = ""
    """Filas LaTeX pre-renderizadas de la tabla EDAC por módulo DIMM.
    Formato de cada fila: ``DIMM-A1 & 16 GB & 0 & \\badgeok \\\\``."""

    ram_total_errors: int = 0
    """Total de errores detectados en todos los módulos DIMM.
    Controla el bloque condicional ``[% if ram_total_errors == 0 %]``."""

    # ── Timing medido ────────────────────────────────────────────────────
    ram_speed_effective: int = 0
    """Velocidad efectiva medida durante la prueba (MHz)."""

    ram_cas_ns: Optional[float] = None
    """
    Latencia CAS real medida por Intel MLC (``mlc --idle_latency``) en ns.

    None si mlc no está instalado, no pudo completar la medición o
    el resultado quedó fuera del rango físico plausible [10, 500] ns.
    Nunca 0.0: cero nanosegundos es físicamente imposible para DRAM.
    """

    ram_test_method: str = "N/A"
    """Método/herramienta de prueba usada
    (ej. ``memtester``, ``memtest86+``, ``edac-utils``)."""

    # ── Gráfico donut de integridad ───────────────────────────────────────
    ram_pie_angle: float = 360.0
    """Ángulo del sector íntegro del donut TikZ en grados [0, 360].
    Pre-calculado por Python: ``(ram_integrity_pct / 100) * 360``."""

    ram_integrity_pct: float = 100.0
    """Porcentaje de memoria íntegra (sin errores). Aparece en el centro
    del donut y en las leyendas."""

    ram_fail_pct: float = 0.0
    """Porcentaje de memoria con defectos. Aparece en la leyenda del donut."""


# ════════════════════════════════════════════════════════════════════════════
#  6. PLACA BASE / VRM
#     Variables: mobo_manufacturer, mobo_model, mobo_chipset,
#                bios_version, bios_date,
#                vrm_tol_low, vrm_tol_high, datos_vrm_vid,
#                datos_vrm_medido, vrm_droop_t, vrm_droop_v,
#                vrm_vdroop_max, vrm_temp, vrm_phases, vdroop_status,
#                vrm_status
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class MotherboardData:
    """Datos de placa base: identificación BIOS y análisis de VRM."""

    # ── Identificación ───────────────────────────────────────────────────
    mobo_manufacturer: str = "N/A"
    """Fabricante de la placa base (ej. ``ASUS``, ``Gigabyte``)."""

    mobo_model: str = "N/A"
    """Modelo de la placa base (ej. ``ROG STRIX B650E-F``)."""

    mobo_chipset: str = "N/A"
    """Chipset de la placa base (ej. ``B650E``, ``Z790``)."""

    bios_version: str = "N/A"
    """Versión del firmware BIOS/UEFI (ej. ``2803``)."""

    bios_date: str = "N/A"
    """Fecha de compilación del firmware (ej. ``05/14/2024``)."""

    # ── Análisis VRM — gráfico pgfplots (sección 5.1) ────────────────────
    vrm_tol_low: float = 0.0
    """Límite inferior de la banda de tolerancia ±5 % del VID (V).
    Define el borde inferior del relleno verde translúcido."""

    vrm_tol_high: float = 0.0
    """Límite superior de la banda de tolerancia ±5 % del VID (V)."""

    datos_vrm_vid: str = ""
    """Coordenadas pgfplots de la serie VID (voltaje solicitado).
    Formato: ``(0,1.25) (10,1.30) ...``"""

    datos_vrm_medido: str = ""
    """Coordenadas pgfplots de la serie de voltaje medido por sensor."""

    vrm_droop_t: float = 0.0
    """Tiempo (s) en el que ocurre el droop máximo.
    Determina la posición X de la anotación de flecha en el gráfico."""

    vrm_droop_v: float = 0.0
    """Voltaje (V) en el punto de droop máximo.
    Determina la posición Y de la anotación de flecha."""

    # ── Métricas VRM — texto (sección 5.1) ───────────────────────────────
    vrm_vdroop_max: float = 0.0
    """Caída de voltaje máxima medida (V). Umbral warn: ~0.05 V."""

    vrm_temp: Optional[float] = None
    """
    Temperatura del VRM bajo carga (°C) leída desde hwmon.

    None si ningún sensor ``temp*_input`` con etiqueta VRM está expuesto
    por el hardware o el controlador. La ausencia del sensor no implica
    VRM frío: es simplemente telemetría no disponible.
    Nota: 0.0 °C tampoco es una lectura plausible de VRM; None es el único
    valor honesto cuando el sensor no existe.
    """

    vrm_phases: Optional[int] = None
    """
    Número de fases del VRM detectadas vía conteo de ``curr*_input`` en hwmon.

    None si no hay canales de corriente expuestos (controlador de VRM propietario
    o driver sin soporte de lectura de fases). No existe VRM de cero fases;
    0 nunca debe aparecer aquí.
    """

    vdroop_status: str = "N/A"
    """Etiqueta textual del estado del droop para la línea de resumen
    (ej. ``Dentro de tolerancia``, ``Moderado``, ``Excesivo``)."""

    vrm_status: str = "ok"
    """Estado lógico para el bloque condicional Jinja2:
    ``"ok"``, ``"warn"`` o ``"crit"``."""


# ════════════════════════════════════════════════════════════════════════════
#  7. BATERÍA
#     Variables: battery_present, bat_design_cap, bat_full_cap,
#                bat_cycles, bat_soh, bat_voltage_nom, bat_voltage_load,
#                bat_voltage_drop, bat_current_load, bat_resistance,
#                bat_gauge_color, bat_gauge_fill
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class BatteryData:
    """Datos de batería (sección 5.2). Se omite si ``battery_present=False``."""

    battery_present: bool = False
    """Controla el bloque ``[% if battery_present %]``.
    ``False`` para equipos de escritorio o cuando no se detecta batería."""

    # ── Capacidad y ciclos ───────────────────────────────────────────────
    bat_design_cap: int = 0
    """Capacidad de diseño de fábrica en mWh."""

    bat_full_cap: int = 0
    """Capacidad actual de carga completa en mWh (se degrada con el uso)."""

    bat_cycles: Optional[int] = None
    """
    Ciclos de carga acumulados (``cycle_count`` o ``charge_control_cycle_count``).

    None si el BMS no expone ninguno de los dos archivos en sysfs.
    0 es un valor válido (batería nueva) y se diferencia explícitamente de None.
    Semántica: None = «sin dato», 0 = «cero ciclos registrados».
    """

    bat_soh: int = 0
    """State of Health (SoH) en porcentaje.
    Controla los bloques ``[% if bat_soh >= 80 %]``, etc."""

    # ── Voltaje y corriente ──────────────────────────────────────────────
    bat_voltage_nom: float = 0.0
    """Voltaje nominal de la batería en V."""

    bat_voltage_load: float = 0.0
    """Voltaje medido bajo carga de descarga en V."""

    bat_voltage_drop: Optional[float] = None
    """
    Caída de voltaje ΔV bajo carga en V (V_nominal − V_actual).

    None si la batería no estaba en modo Discharging durante el análisis.
    Una caída de 0.0 V con la batería conectada al cargador no es información
    útil: el cargador mantiene el voltaje independientemente de la impedancia
    interna. Solo tiene sentido bajo descarga activa.
    """

    bat_current_load: int = 0
    """Corriente de descarga medida en mA."""

    bat_resistance: Optional[float] = None
    """
    Resistencia interna estimada en mΩ.  Modelo: R = (ΔV / I_A) × 1000.

    None si las condiciones de medición no se cumplieron:
      - status ≠ "Discharging"  (sin corriente de descarga real)
      - |I| < 100 mA            (carga insuficiente para estimación fiable)
      - ΔV ≤ 0                  (V_actual ≥ V_nominal; batería o cargador en boost)
    Nunca 0.0: una resistencia de cero ohmios es una superconducción, no
    una batería de portátil.
    """

    # ── Gauge TikZ ───────────────────────────────────────────────────────
    bat_gauge_color: str = "StatusOKMid"
    """Nombre del color LaTeX/TikZ para el relleno del gauge de SoH.
    Opciones típicas: ``StatusOKMid``, ``StatusWarnMid``, ``StatusCritMid``."""

    bat_gauge_fill: float = 0.0
    """Longitud del relleno del gauge en cm [0.0, 4.6].
    Pre-calculado por Python: ``(bat_soh / 100) * 4.6``."""


# ════════════════════════════════════════════════════════════════════════════
#  8. BUS USB / PERIFÉRICOS
#     Variables: usb_tabla_filas, usb_ok, usb_warn, usb_fail
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class USBData:
    """Datos de integridad del bus USB y periféricos (sección 6.1)."""

    usb_tabla_filas: str = ""
    """Filas LaTeX pre-renderizadas de la tabla de puertos USB.
    Formato de cada fila:
    ``Puerto1 & USB 3.2 & xHCI Intel & 10 & 0 & \\badgeok \\\\``"""

    usb_ok: int = 0
    """Número de puertos USB estables (sin errores)."""

    usb_warn: int = 0
    """Número de puertos con errores intermitentes (inestables).
    Controla el bloque ``[% elif usb_warn > 0 %]``."""

    usb_fail: int = 0
    """Número de puertos con fallos graves o cortocircuito.
    Controla el bloque ``[% if usb_fail > 0 %]``."""


# ════════════════════════════════════════════════════════════════════════════
#  9. RESUMEN GLOBAL
#     Variables: score_*/badge_*/accion_* por componente,
#                score_global, badge_global, accion_global,
#                lista_recomendaciones
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class GlobalSummary:
    """Scores, badges y acciones por componente + tabla global + recomendaciones."""

    # ── Scores por componente (0-100) ────────────────────────────────────
    score_cpu: int = 0
    score_gpu: int = 0
    score_nvme: int = 0
    score_ram: int = 0
    score_mobo: int = 0
    score_usb: int = 0
    score_global: int = 0
    """Score ponderado global del sistema. Aparece en portada y tabla."""

    # ── Badges por componente (macro LaTeX pre-renderizada) ───────────────
    badge_cpu: str = r"\badgeinfo"
    badge_gpu: str = r"\badgeinfo"
    badge_nvme: str = r"\badgeinfo"
    badge_ram: str = r"\badgeinfo"
    badge_mobo: str = r"\badgeinfo"
    badge_usb: str = r"\badgeinfo"
    badge_global: str = r"\badgeinfo"
    """Badge global para la fila resumen de la tabla."""

    # ── Acciones recomendadas por componente ─────────────────────────────
    accion_cpu: str = "N/A"
    """Acción sugerida para CPU (ej. ``Monitorear``, ``Reemplazar pasta``)."""

    accion_gpu: str = "N/A"
    accion_nvme: str = "N/A"
    accion_ram: str = "N/A"
    accion_mobo: str = "N/A"
    accion_usb: str = "N/A"
    accion_global: str = "N/A"
    """Acción global en la fila del score total (negrita blanca en tabla)."""

    # ── Lista de recomendaciones técnicas ────────────────────────────────
    lista_recomendaciones: str = ""
    """Items LaTeX pre-renderizados para el enumerate de recomendaciones.
    Cada ítem: ``\\item Descripción de la acción recomendada.``"""


# ════════════════════════════════════════════════════════════════════════════
#  10. DIAGNOSTIC REPORT — RAÍZ COMPOSITORA
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class DiagnosticReport:
    """
    Raíz del modelo de datos de probe.tex.

    Compone todos los sub-modelos de hardware en un único objeto que el
    motor Jinja2 desempaqueta al renderizar ``reporte_base.tex``.

    Uso típico
    ----------
    ::

        report = DiagnosticReport(
            metadata=ReportMetadata(
                report_id="AM-2025-00042",
                cliente_nombre="Empresa S.A.",
                ...
            ),
            cpu=CPUData(cpu_model="Intel Core i9-14900K", ...),
            ...
        )

        # Conversión a dict plano para Jinja2:
        context = report.to_jinja_context()
        template.render(**context)

    """

    metadata: ReportMetadata = field(default_factory=ReportMetadata)
    cpu: CPUData = field(default_factory=CPUData)
    gpu: GPUData = field(default_factory=GPUData)
    nvme: NVMeData = field(default_factory=NVMeData)
    ram: RAMData = field(default_factory=RAMData)
    motherboard: MotherboardData = field(default_factory=MotherboardData)
    battery: BatteryData = field(default_factory=BatteryData)
    usb: USBData = field(default_factory=USBData)
    summary: GlobalSummary = field(default_factory=GlobalSummary)

    # ────────────────────────────────────────────────────────────────────
    def to_jinja_context(self) -> dict:
        """
        Aplana el árbol de dataclasses en un ``dict`` plano listo para
        ser desempaquetado como contexto de Jinja2.

        El template usa variables de primer nivel (ej. ``<< cpu_model >>``),
        por lo que todas las claves deben vivir en el mismo espacio de nombres.
        Si dos sub-modelos definen la misma clave, la última en el orden de
        iteración gana (actualmente no ocurre por diseño).

        Returns
        -------
        dict
            Mapeo ``{variable_name: value}`` con todos los campos de todos
            los sub-modelos más los del propio ``DiagnosticReport``.
        """
        import dataclasses

        context: dict = {}

        sub_models = [
            self.metadata,
            self.cpu,
            self.gpu,
            self.nvme,
            self.ram,
            self.motherboard,
            self.battery,
            self.usb,
            self.summary,
        ]

        for model in sub_models:
            for f in dataclasses.fields(model):
                context[f.name] = getattr(model, f.name)

        return context