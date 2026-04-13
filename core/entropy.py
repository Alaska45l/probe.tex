"""
core/entropy.py
===============
Motor de evaluación de entropía física y degradación termodinámica
para probe.tex.

CHANGELOG v1.1
--------------
* Import StorageData en lugar de NVMeData (alias en models.py).
* Añadidos umbrales físicos HDD (_THR_HDD_* / _DA_HDD_*).
* _eval_nvme renombrada a _eval_storage con bifurcación interna:
    is_hdd=True  → evaluación de cinemática mecánica.
    is_hdd=False → evaluación de desgaste de estado sólido (sin cambios).
* evaluate_system_entropy: parámetro `nvme` tipado como StorageData.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Optional

from core.models import (
    BatteryData, CPUData, GPUData, MotherboardData,
    StorageData, RAMData, USBData,
)
from tui import runtime_log


# ════════════════════════════════════════════════════════════════════════════
#  ESTADOS
# ════════════════════════════════════════════════════════════════════════════

class EntropyState(Enum):
    UNKNOWN  = "UNKNOWN"
    OPTIMAL  = "OPTIMAL"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


# ════════════════════════════════════════════════════════════════════════════
#  UMBRALES FÍSICOS
# ════════════════════════════════════════════════════════════════════════════

# ── CPU ──────────────────────────────────────────────────────────────────────
_THR_CPU_DELTA_T_DISSIPATION: Final[float] = 15.0
_THR_CPU_RECOVERY_S:          Final[float] = 45.0
_THR_CPU_TJMAX_MARGIN:        Final[float] = 0.0

_DA_CPU_THERMAL_DISSIPATION:  Final[int] = 20
_DA_CPU_THROTTLE:             Final[int] = 20
_DA_CPU_TJMAX_BREACH:         Final[int] = 50
_DA_CPU_MCE:                  Final[int] = 50

# ── GPU ──────────────────────────────────────────────────────────────────────
_THR_GPU_DELTA_HOTSPOT_WARN:  Final[float] = 20.0
_THR_GPU_VRAM_ERRORS_CRIT:    Final[int]   = 0
_THR_GPU_AER_FATAL_CRIT:      Final[int]   = 0

_DA_GPU_HOTSPOT_DISSIPATION:  Final[int] = 20
_DA_GPU_TJMAX_BREACH:         Final[int] = 50
_DA_GPU_VRAM_CORRUPTION:      Final[int] = 50
_DA_GPU_AER_FATAL:            Final[int] = 30
_DA_GPU_PCIE_DEGRADED:        Final[int] = 10

# ── RAM ──────────────────────────────────────────────────────────────────────
_DA_RAM_ECC_FAULT:            Final[int] = 100

# ── NVMe / SSD ───────────────────────────────────────────────────────────────
_THR_NVME_WAF_DEGRADED:       Final[float] = 3.0
_THR_NVME_SPARE_PCT_DEGRADED: Final[int]   = 10
_THR_NVME_LIFE_CRITICAL:      Final[int]   = 5
_THR_NVME_LIFE_DEGRADED:      Final[int]   = 20
_THR_NVME_ECC_DEGRADED:       Final[int]   = 100

_DA_NVME_WAF_EXCESS:          Final[int] = 30
_DA_NVME_SPARE_DEPLETED:      Final[int] = 30
_DA_NVME_LIFE_CRITICAL:       Final[int] = 50
_DA_NVME_LIFE_DEGRADED:       Final[int] = 30
_DA_NVME_ECC_EXCESS:          Final[int] = 30

# ── HDD Mecánico ─────────────────────────────────────────────────────────────
#
# Umbral de seek latency:
#   Un HDD 7200 RPM en buen estado entrega 8–14 ms (fio randread 4K QD1).
#   25 ms indica desgaste apreciable del motor de pasos o rozamiento del
#   cabezal, equivalente a una degradación activa del mecanismo cinemático.
_THR_HDD_SEEK_DEGRADED_MS:   Final[float] = 25.0

# Umbral de superficie/actuador:
#   Cualquier valor > 0 para sectores reasignados (ID 5) o command timeouts
#   (ID 188) implica daño físico real. No existe "degradación tolerable"
#   para estos indicadores: son binarios.
_THR_HDD_SURFACE_CRIT_COUNT: Final[int]   = 0   # > 0 → CRITICAL

_DA_HDD_SURFACE_CRITICAL:    Final[int]   = 50  # ΔA ≥ 50 → colapsa a CRITICAL
_DA_HDD_SEEK_DEGRADED:       Final[int]   = 20  # ΔA ∈ [1,49] → DEGRADED

# ── VRM (Motherboard) ────────────────────────────────────────────────────────
_THR_VRM_DROOP_DEGRADED:      Final[float] = 0.05
_THR_VRM_DROOP_CRITICAL:      Final[float] = 0.10

_DA_VRM_DROOP_DEGRADED:       Final[int] = 15
_DA_VRM_DROOP_CRITICAL:       Final[int] = 50

# ── USB ──────────────────────────────────────────────────────────────────────
_DA_USB_PER_FAILED_PORT:      Final[int] = 10
_DA_USB_PER_UNSTABLE_PORT:    Final[int] = 5

# ── Batería ──────────────────────────────────────────────────────────────────
_THR_BAT_WEAR_DEGRADED:       Final[float] = 20.0
_THR_BAT_WEAR_CRITICAL:       Final[float] = 40.0
_DA_BAT_WEAR_DEGRADED:        Final[int] = 15
_DA_BAT_WEAR_CRITICAL:        Final[int] = 50

# ── Clasificación global ─────────────────────────────────────────────────────
_DA_GLOBAL_CRITICAL_FLOOR:    Final[int] = 50
_DA_GLOBAL_DEGRADED_FLOOR:    Final[int] = 1


# ════════════════════════════════════════════════════════════════════════════
#  MAPAS LATEX
# ════════════════════════════════════════════════════════════════════════════

_BADGE: dict[EntropyState, str] = {
    EntropyState.OPTIMAL:  r"\badgeoptimal",
    EntropyState.DEGRADED: r"\badgedegraded",
    EntropyState.CRITICAL: r"\badgecrit",
    EntropyState.UNKNOWN:  r"\badgeunknown",
}

_BADGE_COMPAT: dict[EntropyState, str] = {
    EntropyState.OPTIMAL:  r"\badgeok",
    EntropyState.DEGRADED: r"\badgewarn",
    EntropyState.CRITICAL: r"\badgefail",
    EntropyState.UNKNOWN:  r"\badgeinfo",
}


# ════════════════════════════════════════════════════════════════════════════
#  DATACLASSES DE SALIDA (sin cambios respecto a v1.0)
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SubsystemVector:
    subsystem:    str
    delta_a:      int
    state:        EntropyState
    badge:        str
    badge_compat: str
    directives:   tuple[str, ...]


@dataclass(frozen=True)
class SystemEntropy:
    cpu:     SubsystemVector
    gpu:     SubsystemVector
    nvme:    SubsystemVector
    ram:     SubsystemVector
    vrm:     SubsystemVector
    usb:     SubsystemVector
    battery: SubsystemVector

    total_delta_a: int
    global_state:  EntropyState
    global_badge:  str

    score_cpu:    int
    score_gpu:    int
    score_nvme:   int
    score_ram:    int
    score_mobo:   int
    score_usb:    int
    score_bat:    int
    score_global: int

    badge_cpu:    str
    badge_gpu:    str
    badge_nvme:   str
    badge_ram:    str
    badge_mobo:   str
    badge_usb:    str
    badge_bat:    str
    badge_global: str

    accion_cpu:    str
    accion_gpu:    str
    accion_nvme:   str
    accion_ram:    str
    accion_mobo:   str
    accion_usb:    str
    accion_bat:    str
    accion_global: str

    lista_recomendaciones: str
    estado_global_badge:   str
    resumen_ejecutivo:     str


# ════════════════════════════════════════════════════════════════════════════
#  UTILIDADES INTERNAS
# ════════════════════════════════════════════════════════════════════════════

def _clamp_score(delta_a: int) -> int:
    return max(0, min(100, 100 - delta_a))


def _state_from_delta_a(delta_a: int) -> EntropyState:
    if delta_a == 0:
        return EntropyState.OPTIMAL
    if delta_a >= _DA_GLOBAL_CRITICAL_FLOOR:
        return EntropyState.CRITICAL
    return EntropyState.DEGRADED


def _worst_state(*states: EntropyState) -> EntropyState:
    priority: dict[EntropyState, int] = {
        EntropyState.UNKNOWN:  0,
        EntropyState.OPTIMAL:  1,
        EntropyState.DEGRADED: 2,
        EntropyState.CRITICAL: 3,
    }
    return max(states, key=lambda s: priority[s])


def _latex_item(subsystem_label: str, body: str) -> str:
    return rf"\item \textbf{{{subsystem_label}:}} {body}"


def _first_directive_text(directives: tuple[str, ...]) -> str:
    if not directives:
        return "Sin anomalías detectadas"
    raw = directives[0]
    import re
    m = re.search(r"\\textbf\{[^}]+\}:\s*(.+)", raw)
    return m.group(1) if m else raw


# ════════════════════════════════════════════════════════════════════════════
#  EVALUADORES DE SUBSISTEMA
# ════════════════════════════════════════════════════════════════════════════

def _eval_cpu(cpu: CPUData) -> SubsystemVector:
    delta_a:    int         = 0
    directives: list[str]   = []

    no_telemetry = (
        cpu.cpu_t_max == 0.0
        and cpu.cpu_t_idle == 0.0
        and cpu.cpu_model == "N/A"
    )
    if no_telemetry:
        return SubsystemVector(
            subsystem="CPU", delta_a=0, state=EntropyState.UNKNOWN,
            badge=_BADGE[EntropyState.UNKNOWN],
            badge_compat=_BADGE_COMPAT[EntropyState.UNKNOWN],
            directives=(
                _latex_item("Sub-sistema CPU",
                    "Telemetría no disponible. Verificar extractor y permisos."),
            ),
        )

    if cpu.mce_count > 0:
        delta_a += _DA_CPU_MCE
        directives.append(_latex_item(
            "Sub-sistema CPU / MCE",
            fr"Se registraron \textbf{{{cpu.mce_count}}} Machine Check Exception(s). "
            r"Fallo de hardware activo. Reemplazo del procesador mandatorio "
            r"si los errores son recurrentes post-microcode update."
        ))

    tjmax_breached: bool = (
        cpu.cpu_t_max > 0.0
        and cpu.cpu_tjmax > 0
        and cpu.cpu_t_max >= cpu.cpu_tjmax
    )
    if tjmax_breached:
        delta_a += _DA_CPU_TJMAX_BREACH
        directives.append(_latex_item(
            "Sub-sistema CPU / Térmico (CRÍTICO)",
            fr"$T_{{\max}}$ = {cpu.cpu_t_max:.1f}°C ≥ TjMax = {cpu.cpu_tjmax}°C. "
            r"Operación en zona de autoprotección térmica. "
            r"Intervención inmediata: reemplazo de TIM y verificación del sistema de refrigeración."
        ))

    thermal_fail = (
        cpu.cpu_delta_t > _THR_CPU_DELTA_T_DISSIPATION
        or cpu.cpu_recovery_time > _THR_CPU_RECOVERY_S
        or cpu.cpu_recovery_time < 0.0
    )
    if thermal_fail and not tjmax_breached:
        delta_a += _DA_CPU_THERMAL_DISSIPATION
        directives.append(_latex_item(
            "Sub-sistema CPU / Disipación",
            fr"$\Delta T_{{\text{{recovery}}}}$ = {cpu.cpu_delta_t:.1f}°C "
            fr"(umbral: {_THR_CPU_DELTA_T_DISSIPATION:.0f}°C), "
            fr"$t_{{\text{{recovery}}}}$ = {cpu.cpu_recovery_time:.1f} s "
            fr"(umbral: {_THR_CPU_RECOVERY_S:.0f} s). "
            r"Capacidad de disipación comprometida. "
            r"Reemplazar pasta térmica. Verificar flujo de aire y disipador."
        ))

    if cpu.cpu_throttle_events > 0:
        delta_a += _DA_CPU_THROTTLE
        directives.append(_latex_item(
            "Sub-sistema CPU / P-States",
            fr"\textbf{{{cpu.cpu_throttle_events}}} evento(s) de throttling "
            fr"(duración acumulada: {cpu.cpu_throttle_dur} ms). "
            fr"Causa registrada: {cpu.cpu_throttle_cause}. "
            r"Degradación de rendimiento bajo carga sostenida. "
            r"Auditar envelope de potencia en BIOS y TIM."
        ))

    state = _state_from_delta_a(delta_a)
    if not directives:
        directives.append(_latex_item(
            "Sub-sistema CPU", "Sin anomalías detectadas en la ventana de análisis."
        ))

    return SubsystemVector(
        subsystem="CPU", delta_a=delta_a, state=state,
        badge=_BADGE[state], badge_compat=_BADGE_COMPAT[state],
        directives=tuple(directives),
    )


def _eval_gpu(gpu: GPUData) -> SubsystemVector:
    delta_a:    int         = 0
    directives: list[str]   = []

    no_telemetry = (
        gpu.gpu_model == "N/A"
        and gpu.gpu_t_edge == 0.0
        and gpu.gpu_t_hotspot == 0.0
        and gpu.gpu_vram_total == 0
    )
    if no_telemetry:
        return SubsystemVector(
            subsystem="GPU", delta_a=0, state=EntropyState.UNKNOWN,
            badge=_BADGE[EntropyState.UNKNOWN],
            badge_compat=_BADGE_COMPAT[EntropyState.UNKNOWN],
            directives=(
                _latex_item("Sub-sistema GPU",
                    "Telemetría no disponible o GPU integrada sin sensores expuestos."),
            ),
        )

    total_vram_errors = (
        gpu.gpu_vram_seq_errors
        + gpu.gpu_vram_rand_errors
        + gpu.gpu_vram_stress_errors
    )
    if total_vram_errors > _THR_GPU_VRAM_ERRORS_CRIT:
        delta_a += _DA_GPU_VRAM_CORRUPTION
        directives.append(_latex_item(
            "Sub-sistema GPU / VRAM",
            fr"\textbf{{{total_vram_errors}}} error(es) de integridad en VRAM "
            fr"({gpu.gpu_vram_total} GB {gpu.gpu_vram_type}). "
            r"Corrupción de datos en memoria gráfica confirmada. "
            r"Reemplazo de GPU mandatorio."
        ))

    if gpu.gpu_aer_fatal > _THR_GPU_AER_FATAL_CRIT:
        delta_a += _DA_GPU_AER_FATAL
        directives.append(_latex_item(
            "Sub-sistema GPU / PCIe AER",
            fr"\textbf{{{gpu.gpu_aer_fatal}}} error(es) AER fatal(es) en el "
            fr"enlace PCIe Gen{gpu.gpu_pcie_gen_active} x{gpu.gpu_pcie_lanes_active}. "
            r"Fallo estructural en el bus. Verificar slot físico y conector de alimentación PCIe."
        ))

    if gpu.gpu_t_hotspot > 0.0 and gpu.gpu_t_hotspot >= gpu.gpu_temp_limit:
        delta_a += _DA_GPU_TJMAX_BREACH
        directives.append(_latex_item(
            "Sub-sistema GPU / Térmico (CRÍTICO)",
            fr"$T_{{\text{{hotspot}}}}$ = {gpu.gpu_t_hotspot:.1f}°C ≥ "
            fr"límite fabricante = {gpu.gpu_temp_limit}°C. "
            r"Zona de throttling de emergencia activa. "
            r"Reemplazar TIM die-heatsink. Verificar pad térmico de VRAM."
        ))
    elif gpu.gpu_delta_t_hotspot > _THR_GPU_DELTA_HOTSPOT_WARN:
        delta_a += _DA_GPU_HOTSPOT_DISSIPATION
        directives.append(_latex_item(
            "Sub-sistema GPU / Delta Térmico",
            fr"$\Delta T_{{\text{{hotspot}}}}$ = {gpu.gpu_delta_t_hotspot:.1f}°C "
            fr"(umbral: {_THR_GPU_DELTA_HOTSPOT_WARN:.0f}°C). "
            r"Gradiente térmico die-edge fuera de especificación. "
            r"Degradación del TIM entre die y heatspreader probable."
        ))

    if (gpu.gpu_pcie_lanes_max > 0
            and gpu.gpu_pcie_lanes_active < gpu.gpu_pcie_lanes_max
            and gpu.gpu_pcie_lanes_active > 0):
        delta_a += _DA_GPU_PCIE_DEGRADED
        directives.append(_latex_item(
            "Sub-sistema GPU / PCIe",
            fr"Enlace activo: x{gpu.gpu_pcie_lanes_active} "
            fr"(máximo: x{gpu.gpu_pcie_lanes_max}). "
            r"Ancho de banda PCIe reducido. Verificar integridad del slot y BIOS PCIe settings."
        ))

    state = _state_from_delta_a(delta_a)
    if not directives:
        directives.append(_latex_item(
            "Sub-sistema GPU", "Sin anomalías detectadas."
        ))

    return SubsystemVector(
        subsystem="GPU", delta_a=delta_a, state=state,
        badge=_BADGE[state], badge_compat=_BADGE_COMPAT[state],
        directives=tuple(directives),
    )


def _eval_ram(ram: RAMData) -> SubsystemVector:
    delta_a:    int         = 0
    directives: list[str]   = []

    no_telemetry = ram.ram_total_gb == 0 and ram.ram_type == "N/A"
    if no_telemetry:
        return SubsystemVector(
            subsystem="RAM", delta_a=0, state=EntropyState.UNKNOWN,
            badge=_BADGE[EntropyState.UNKNOWN],
            badge_compat=_BADGE_COMPAT[EntropyState.UNKNOWN],
            directives=(
                _latex_item("Sub-sistema RAM",
                    "Telemetría no disponible. Verificar permisos de dmidecode."),
            ),
        )

    if ram.ram_total_errors > 0:
        delta_a += _DA_RAM_ECC_FAULT
        directives.append(_latex_item(
            "Sub-sistema RAM / Integridad",
            fr"\textbf{{{ram.ram_total_errors}}} error(es) de memoria detectados "
            r"(EDAC/ECC). La integridad de la memoria DRAM no es negociable: "
            r"un único bit flip no corregible implica potencial corrupción de datos "
            r"en cualquier proceso activo en el sistema. "
            r"Aislar y reemplazar el módulo DIMM defectuoso de forma inmediata. "
            r"Ejecutar memtest86+ (mínimo 2 pasadas) para confirmación."
        ))

    state = _state_from_delta_a(delta_a)
    if not directives:
        directives.append(_latex_item(
            "Sub-sistema RAM",
            fr"Sin errores detectados en {ram.ram_slots_used} módulo(s) "
            fr"({ram.ram_total_gb} GB {ram.ram_type}-{ram.ram_speed})."
        ))

    return SubsystemVector(
        subsystem="RAM", delta_a=delta_a, state=state,
        badge=_BADGE[state], badge_compat=_BADGE_COMPAT[state],
        directives=tuple(directives),
    )


def _eval_storage(storage: StorageData) -> SubsystemVector:
    """
    Evalúa la entropía del subsistema de almacenamiento.

    Bifurcación interna
    -------------------
    storage.is_hdd=True  → evaluación de cinemática mecánica (HDD).
    storage.is_hdd=False → evaluación de desgaste de estado sólido (SSD/NVMe).

    Ruta HDD — Modelo de Degradación Mecánica
    ------------------------------------------
    Jerarquía de severidad (evaluación completa; los ΔA se acumulan):

    1. CRITICAL (ΔA += 50 por evento) si:
         hdd_reallocated_sectors > 0  → daño físico en plato confirmado.
         hdd_command_timeouts    > 0  → fallo del actuador o inestabilidad.

    2. DEGRADED (ΔA += 20) si:
         hdd_seek_latency_ms > 25.0  → cabezal perdiendo agilidad mecánica.

    Si todos los campos hdd_* son None → UNKNOWN (sin telemetría).

    Ruta SSD/NVMe — idéntica a _eval_nvme v1.0
    """

    # ════════════════════════════════════════════════════════════════════
    #  RAMA HDD MECÁNICO
    # ════════════════════════════════════════════════════════════════════
    if storage.is_hdd:
        delta_a:    int       = 0
        directives: list[str] = []

        no_telemetry_hdd = (
            storage.hdd_reallocated_sectors is None
            and storage.hdd_command_timeouts is None
            and storage.hdd_seek_latency_ms  is None
            and storage.nvme_model == "N/A"
        )
        if no_telemetry_hdd:
            return SubsystemVector(
                subsystem="STORAGE-HDD", delta_a=0, state=EntropyState.UNKNOWN,
                badge=_BADGE[EntropyState.UNKNOWN],
                badge_compat=_BADGE_COMPAT[EntropyState.UNKNOWN],
                directives=(
                    _latex_item(
                        "Sub-sistema Almacenamiento / HDD",
                        r"Telemetría SMART y cinemática no disponibles. "
                        r"Verificar permisos de \texttt{smartctl} "
                        r"y disponibilidad de \texttt{fio}.",
                    ),
                ),
            )

        # ── Superficie — Sectores Reasignados (ID 5) ─────────────────────
        reallocated = storage.hdd_reallocated_sectors
        if reallocated is not None and reallocated > _THR_HDD_SURFACE_CRIT_COUNT:
            delta_a += _DA_HDD_SURFACE_CRITICAL
            directives.append(_latex_item(
                r"Sub-sistema Almacenamiento / HDD Superficie (CRÍTICO)",
                fr"\textbf{{{reallocated}}} sector(es) reasignado(s) "
                fr"(SMART ID 5, umbral: $>$ {_THR_HDD_SURFACE_CRIT_COUNT}). "
                r"Daño físico en plato magnético confirmado. "
                r"La reasignación activa implica que el cabezal detectó "
                r"sectores irrecuperables y los redirigió a la zona de reserva. "
                r"\textbf{Iniciar backup inmediato antes de cualquier otra operación.} "
                r"Reemplazo de disco mandatorio."
            ))

        # ── Actuador — Command Timeouts (ID 188) ─────────────────────────
        cmd_timeouts = storage.hdd_command_timeouts
        if cmd_timeouts is not None and cmd_timeouts > _THR_HDD_SURFACE_CRIT_COUNT:
            delta_a += _DA_HDD_SURFACE_CRITICAL
            directives.append(_latex_item(
                r"Sub-sistema Almacenamiento / HDD Actuador (CRÍTICO)",
                fr"\textbf{{{cmd_timeouts}}} timeout(s) de comando "
                fr"(SMART ID 188, umbral: $>$ {_THR_HDD_SURFACE_CRIT_COUNT}). "
                r"Inestabilidad mecánica del actuador o degradación del enlace SATA. "
                r"Riesgo de pérdida de datos bajo escritura sostenida. "
                r"Reemplazo urgente recomendado."
            ))

        # ── Cinemática — Seek Latency (fio) ──────────────────────────────
        seek_ms = storage.hdd_seek_latency_ms
        if seek_ms is not None and seek_ms > _THR_HDD_SEEK_DEGRADED_MS:
            delta_a += _DA_HDD_SEEK_DEGRADED
            directives.append(_latex_item(
                r"Sub-sistema Almacenamiento / HDD Cinemática",
                fr"Seek latency media: \textbf{{{seek_ms:.2f}\ ms}} "
                fr"(umbral: {_THR_HDD_SEEK_DEGRADED_MS:.0f}\ ms, "
                r"fio randread 4K QD1, 10 s). "
                r"El cabezal exhibe pérdida de agilidad mecánica: "
                r"probable desgaste del motor de pasos (stepper) "
                r"o rozamiento inicial del cabezal con la superficie del plato. "
                r"Planificar reemplazo preventivo en el próximo ciclo de mantenimiento."
            ))

        state = _state_from_delta_a(delta_a)
        if not directives:
            spin_str = (
                fr"{storage.hdd_spin_up_time}\ ms"
                if storage.hdd_spin_up_time is not None else r"\textit{N/D}"
            )
            seek_str = (
                fr"{seek_ms:.2f}\ ms" if seek_ms is not None else r"\textit{N/D}"
            )
            realloc_str = (
                str(reallocated) if reallocated is not None else r"\textit{N/D}"
            )
            timeout_str = (
                str(cmd_timeouts) if cmd_timeouts is not None else r"\textit{N/D}"
            )
            directives.append(_latex_item(
                "Sub-sistema Almacenamiento / HDD",
                fr"Sin anomalías mecánicas detectadas. "
                fr"Spin-up (ID 3): {spin_str}. "
                fr"Seek latency: {seek_str}. "
                fr"Sectores reasignados (ID 5): {realloc_str}. "
                fr"Command timeouts (ID 188): {timeout_str}."
            ))

        return SubsystemVector(
            subsystem="STORAGE-HDD", delta_a=delta_a, state=state,
            badge=_BADGE[state], badge_compat=_BADGE_COMPAT[state],
            directives=tuple(directives),
        )

    # ════════════════════════════════════════════════════════════════════
    #  RAMA SSD/NVMe — idéntica a _eval_nvme v1.0
    # ════════════════════════════════════════════════════════════════════
    delta_a_ssd:    int       = 0
    directives_ssd: list[str] = []

    no_telemetry_ssd = (
        storage.nvme_model == "N/A" and storage.nvme_capacity == 0
    )
    if no_telemetry_ssd:
        return SubsystemVector(
            subsystem="NVME", delta_a=0, state=EntropyState.UNKNOWN,
            badge=_BADGE[EntropyState.UNKNOWN],
            badge_compat=_BADGE_COMPAT[EntropyState.UNKNOWN],
            directives=(
                _latex_item("Sub-sistema NVMe",
                    "Telemetría SMART no disponible. Verificar permisos de smartctl."),
            ),
        )

    if storage.nvme_life_pct < _THR_NVME_LIFE_CRITICAL:
        delta_a_ssd += _DA_NVME_LIFE_CRITICAL
        directives_ssd.append(_latex_item(
            "Sub-sistema NAND / Vida Útil (CRÍTICO)",
            fr"Vida útil restante: \textbf{{{storage.nvme_life_pct}\%}} "
            fr"(umbral crítico: {_THR_NVME_LIFE_CRITICAL}\%). "
            r"Falla estructural NAND inminente. Reemplazo mandatorio. "
            r"Iniciar backup inmediato antes de cualquier otra operación."
        ))
    elif storage.nvme_life_pct < _THR_NVME_LIFE_DEGRADED:
        delta_a_ssd += _DA_NVME_LIFE_DEGRADED
        directives_ssd.append(_latex_item(
            "Sub-sistema NAND / Vida Útil",
            fr"Vida útil restante: {storage.nvme_life_pct}\% "
            fr"(umbral: {_THR_NVME_LIFE_DEGRADED}\%). "
            r"Planificar reemplazo en el próximo ciclo de mantenimiento."
        ))

    if storage.nvme_waf > _THR_NVME_WAF_DEGRADED:
        delta_a_ssd += _DA_NVME_WAF_EXCESS
        directives_ssd.append(_latex_item(
            "Sub-sistema NAND / WAF",
            fr"Write Amplification Factor = {storage.nvme_waf:.2f} "
            fr"(umbral: {_THR_NVME_WAF_DEGRADED:.1f}). "
            r"El controlador NAND está realizando un número excesivo de "
            r"reescrituras internas. Analizar patrón de acceso del workload. "
            r"Verificar alineación de particiones."
        ))

    if storage.nvme_spare_blocks < _THR_NVME_SPARE_PCT_DEGRADED:
        delta_a_ssd += _DA_NVME_SPARE_DEPLETED
        directives_ssd.append(_latex_item(
            "Sub-sistema NAND / Bloques de Repuesto",
            fr"Spare blocks disponibles: {storage.nvme_spare_blocks}\% "
            fr"(umbral mínimo: {_THR_NVME_SPARE_PCT_DEGRADED}\%). "
            r"Over-provisioning de la NAND prácticamente agotado. "
            r"Reemplazo programado urgente."
        ))

    if storage.nvme_ecc_errors >= _THR_NVME_ECC_DEGRADED:
        delta_a_ssd += _DA_NVME_ECC_EXCESS
        directives_ssd.append(_latex_item(
            "Sub-sistema NAND / ECC",
            fr"{storage.nvme_ecc_errors} errores ECC corregibles acumulados "
            fr"(umbral: {_THR_NVME_ECC_DEGRADED}). "
            r"Tasa de errores NAND elevada. Indicativo de desgaste de celdas. "
            r"Monitorear con frecuencia creciente."
        ))

    state_ssd = _state_from_delta_a(delta_a_ssd)
    if not directives_ssd:
        directives_ssd.append(_latex_item(
            "Sub-sistema NVMe",
            fr"Sin anomalías de desgaste. Vida restante: {storage.nvme_life_pct}\%. "
            fr"WAF: {storage.nvme_waf:.2f}. TBW restante: {storage.nvme_tbw_remaining:.1f} TB."
        ))

    return SubsystemVector(
        subsystem="NVME", delta_a=delta_a_ssd, state=state_ssd,
        badge=_BADGE[state_ssd], badge_compat=_BADGE_COMPAT[state_ssd],
        directives=tuple(directives_ssd),
    )


def _eval_vrm(mobo: MotherboardData) -> SubsystemVector:
    delta_a:    int         = 0
    directives: list[str]   = []

    if mobo.vrm_status == "info" or mobo.vrm_tol_high == 0.0:
        return SubsystemVector(
            subsystem="VRM", delta_a=0, state=EntropyState.UNKNOWN,
            badge=_BADGE[EntropyState.UNKNOWN],
            badge_compat=_BADGE_COMPAT[EntropyState.UNKNOWN],
            directives=(
                _latex_item("Sub-sistema VRM",
                    r"Sensor de voltaje no expuesto por el hardware (hwmon). "
                    r"Análisis de Vdroop no realizable en este sistema."),
            ),
        )

    v_nominal: float = mobo.vrm_tol_high / 1.05
    if v_nominal <= 0.0:
        return SubsystemVector(
            subsystem="VRM", delta_a=0, state=EntropyState.UNKNOWN,
            badge=_BADGE[EntropyState.UNKNOWN],
            badge_compat=_BADGE_COMPAT[EntropyState.UNKNOWN],
            directives=(
                _latex_item("Sub-sistema VRM",
                    "V_nominal no computable (vrm_tol_high = 0). Sensor inválido."),
            ),
        )

    droop_pct: float = mobo.vrm_vdroop_max / v_nominal

    if droop_pct > _THR_VRM_DROOP_CRITICAL:
        delta_a += _DA_VRM_DROOP_CRITICAL
        directives.append(_latex_item(
            "Sub-sistema VRM (CRÍTICO)",
            fr"$V_{{\text{{droop}}}}$ = {mobo.vrm_vdroop_max * 1000:.1f} mV "
            fr"({droop_pct * 100:.1f}\% del VID = {v_nominal:.3f} V). "
            fr"Supera umbral crítico de {_THR_VRM_DROOP_CRITICAL * 100:.0f}\%. "
            r"Riesgo de crashes bajo carga y daño permanente al procesador. "
            r"Verificar condensadores de desacople, fases del VRM y traces de PCB."
        ))
    elif droop_pct > _THR_VRM_DROOP_DEGRADED:
        delta_a += _DA_VRM_DROOP_DEGRADED
        directives.append(_latex_item(
            "Sub-sistema VRM",
            fr"$V_{{\text{{droop}}}}$ = {mobo.vrm_vdroop_max * 1000:.1f} mV "
            fr"({droop_pct * 100:.1f}\% del VID). "
            fr"Supera umbral de tolerancia de {_THR_VRM_DROOP_DEGRADED * 100:.0f}\%. "
            r"Verificar fases activas del VRM y estado de capacitores de bulk."
        ))

    state = _state_from_delta_a(delta_a)
    if not directives:
        directives.append(_latex_item(
            "Sub-sistema VRM",
            fr"$V_{{\text{{droop}}}}$ = {mobo.vrm_vdroop_max * 1000:.1f} mV "
            fr"({droop_pct * 100:.2f}\% del VID). Dentro de especificación."
        ))

    return SubsystemVector(
        subsystem="VRM", delta_a=delta_a, state=state,
        badge=_BADGE[state], badge_compat=_BADGE_COMPAT[state],
        directives=tuple(directives),
    )


def _eval_usb(usb: USBData) -> SubsystemVector:
    delta_a:    int         = 0
    directives: list[str]   = []

    no_telemetry = (
        usb.usb_ok == 0
        and usb.usb_warn == 0
        and usb.usb_fail == 0
    )
    if no_telemetry:
        return SubsystemVector(
            subsystem="USB", delta_a=0, state=EntropyState.UNKNOWN,
            badge=_BADGE[EntropyState.UNKNOWN],
            badge_compat=_BADGE_COMPAT[EntropyState.UNKNOWN],
            directives=(
                _latex_item("Sub-sistema USB",
                    "Topología USB no disponible. Verificar lsusb y permisos."),
            ),
        )

    if usb.usb_fail > 0:
        increment = _DA_USB_PER_FAILED_PORT * usb.usb_fail
        delta_a  += increment
        directives.append(_latex_item(
            "Sub-sistema USB / Puertos Defectuosos",
            fr"\textbf{{{usb.usb_fail}}} puerto(s) con fallos confirmados "
            fr"($\Delta A$ += {increment}). "
            r"Revisar circuito de alimentación VBUS, fusibles USB y controlador xHCI. "
            r"Verificar continuidad del conector físico."
        ))

    if usb.usb_warn > 0:
        increment = _DA_USB_PER_UNSTABLE_PORT * usb.usb_warn
        delta_a  += increment
        directives.append(_latex_item(
            "Sub-sistema USB / Puertos Inestables",
            fr"{usb.usb_warn} puerto(s) con errores intermitentes "
            fr"($\Delta A$ += {increment}). "
            r"Verificar voltaje VBUS ($5.0 \pm 0.25$ V) y calidad del cableado interno."
        ))

    state = _state_from_delta_a(delta_a)
    if not directives:
        directives.append(_latex_item(
            "Sub-sistema USB",
            fr"{usb.usb_ok} puerto(s) operativos. Sin errores de bus detectados."
        ))

    return SubsystemVector(
        subsystem="USB", delta_a=delta_a, state=state,
        badge=_BADGE[state], badge_compat=_BADGE_COMPAT[state],
        directives=tuple(directives),
    )


def _eval_battery(battery: BatteryData) -> SubsystemVector:
    if not battery.battery_present:
        return SubsystemVector(
            subsystem="BAT", delta_a=0, state=EntropyState.UNKNOWN,
            badge=_BADGE[EntropyState.UNKNOWN],
            badge_compat=_BADGE_COMPAT[EntropyState.UNKNOWN],
            directives=(
                _latex_item("Sub-sistema Batería",
                    r"No presente o no detectada. "
                    r"Equipo de escritorio o batería externa sin \texttt{sysfs} expuesto."),
            ),
        )

    no_telemetry = battery.bat_soh == 0 and battery.bat_design_cap == 0
    if no_telemetry:
        return SubsystemVector(
            subsystem="BAT", delta_a=0, state=EntropyState.UNKNOWN,
            badge=_BADGE[EntropyState.UNKNOWN],
            badge_compat=_BADGE_COMPAT[EntropyState.UNKNOWN],
            directives=(
                _latex_item("Sub-sistema Batería",
                    r"Batería detectada pero capacidad no disponible en sysfs. "
                    r"Verificar permisos de \texttt{/sys/class/power\_supply/BAT*/}."),
            ),
        )

    wear_level: float = 100.0 - battery.bat_soh
    delta_a:    int         = 0
    directives: list[str]   = []

    if wear_level > _THR_BAT_WEAR_CRITICAL:
        delta_a += _DA_BAT_WEAR_CRITICAL
        directives.append(_latex_item(
            r"Sub-sistema Batería / Desgaste Electroquímico (CRÍTICO)",
            fr"Nivel de desgaste: \textbf{{{wear_level:.1f}\%}} "
            fr"(SoH = {battery.bat_soh}\%, umbral crítico: "
            fr"{_THR_BAT_WEAR_CRITICAL:.0f}\% de desgaste). "
            fr"Capacidad de diseño: {battery.bat_design_cap} mWh. "
            fr"Capacidad actual: {battery.bat_full_cap} mWh. "
            fr"Ciclos acumulados: {battery.bat_cycles}. "
            r"Ciclo de vida electroquímico comprometido. "
            r"Riesgo de corte abrupto de voltaje bajo cargas pico. "
            r"Reemplazo de batería mandatorio para garantizar operación fiable."
        ))
    elif wear_level > _THR_BAT_WEAR_DEGRADED:
        delta_a += _DA_BAT_WEAR_DEGRADED
        directives.append(_latex_item(
            r"Sub-sistema Batería / Desgaste Electroquímico",
            fr"Nivel de desgaste: {wear_level:.1f}\% "
            fr"(SoH = {battery.bat_soh}\%, umbral: "
            fr"{_THR_BAT_WEAR_DEGRADED:.0f}\% de desgaste). "
            fr"Capacidad actual: {battery.bat_full_cap} mWh "
            fr"de {battery.bat_design_cap} mWh nominales. "
            fr"Ciclos acumulados: {battery.bat_cycles}. "
            r"Autonomía reducida. Planificar reemplazo en el próximo "
            r"ciclo de mantenimiento preventivo."
        ))

    if battery.bat_resistance is not None and battery.bat_resistance > 200.0 and delta_a == 0:
        directives.append(_latex_item(
            r"Sub-sistema Batería / Resistencia Interna",
            fr"$R_{{\text{{int}}}}$ estimada = {battery.bat_resistance:.1f} m$\Omega$ "
            r"(rango nominal Li-ion: 50–150 m$\Omega$). "
            r"Indicativo de degradación del electrolito o capa SEI. "
            r"Monitorear evolución en revisiones periódicas."
        ))

    state = _state_from_delta_a(delta_a)
    if not directives:
        directives.append(_latex_item(
            "Sub-sistema Batería",
            fr"SoH: {battery.bat_soh}\% (desgaste: {wear_level:.1f}\%). "
            fr"Capacidad: {battery.bat_full_cap} / {battery.bat_design_cap} mWh. "
            fr"Ciclos: {battery.bat_cycles}. "
            r"Dentro de parámetros nominales."
        ))

    return SubsystemVector(
        subsystem="BAT", delta_a=delta_a, state=state,
        badge=_BADGE[state], badge_compat=_BADGE_COMPAT[state],
        directives=tuple(directives),
    )


# ════════════════════════════════════════════════════════════════════════════
#  ENSAMBLAJE DE LA LISTA DE RECOMENDACIONES
# ════════════════════════════════════════════════════════════════════════════

def _build_directives_latex(vectors: list[SubsystemVector]) -> str:
    items: list[str] = []
    for vec in vectors:
        if vec.state in (EntropyState.DEGRADED, EntropyState.CRITICAL, EntropyState.UNKNOWN):
            items.extend(vec.directives)
    if not items:
        items.append(
            _latex_item("Sistema",
                "Todos los subsistemas operan dentro de parámetros nominales. "
                "No se requieren intervenciones.")
        )
    return "\n    ".join(items)


# ════════════════════════════════════════════════════════════════════════════
#  RESUMEN EJECUTIVO
# ════════════════════════════════════════════════════════════════════════════

_RESUMEN_TEMPLATE: dict[EntropyState, str] = {
    EntropyState.OPTIMAL: (
        "Sistema operando dentro de parámetros nominales en todos los subsistemas. "
        "No se registran anomalías físicas en la ventana de diagnóstico."
    ),
    EntropyState.DEGRADED: (
        "Degradación física activa detectada en uno o más subsistemas. "
        "El hardware opera fuera de su especificación óptima. "
        "Se requiere intervención técnica en el próximo ciclo de mantenimiento."
    ),
    EntropyState.CRITICAL: (
        "Estado crítico confirmado. Fallo estructural inminente o en curso. "
        "La continuidad operativa del sistema no está garantizada. "
        "Intervención inmediata mandatoria antes de cualquier operación de producción."
    ),
    EntropyState.UNKNOWN: (
        "Telemetría insuficiente para emitir diagnóstico definitivo. "
        "Verificar permisos de los extractores y la presencia del hardware."
    ),
}


# ════════════════════════════════════════════════════════════════════════════
#  FUNCIÓN PRINCIPAL
# ════════════════════════════════════════════════════════════════════════════

def evaluate_system_entropy(
    cpu:            CPUData,
    gpu:            GPUData,
    storage_drives: list[StorageData],   # ← CAMBIADO (era: nvme: StorageData)
    ram:            RAMData,
    mobo:           MotherboardData,
    usb:            USBData,
    battery:        Optional[BatteryData] = None,
) -> SystemEntropy:
    """
    Evalúa la entropía física y química del sistema completo.

    CHANGELOG v1.2
    --------------
    Parámetro ``nvme: StorageData``  →  ``storage_drives: list[StorageData]``.

    Motor de Entropía Acumulativa Multi-Disco
    -----------------------------------------
    1. Se evalúa cada StorageData individualmente con _eval_storage().
       Cada unidad produce su propio SubsystemVector con ΔA independiente.

    2. Agregación de ΔA:
         ΔA_storage = Σ ΔA_i   (suma de las anomalías de todos los discos)
       Un sistema con un SSD sano (ΔA=0) y un HDD con sector reasignado
       (ΔA=50) reporta ΔA_storage=50 — el daño físico del HDD no se
       enmascara por la salud del SSD.

    3. Estado Clínico del bloque de almacenamiento:
         _worst_state(*[v.state for v in vectors])
       Si el SSD es OPTIMAL y el HDD es CRITICAL → el bloque es CRITICAL.
       El estado más severo prevalece siempre.

    4. Directivas:
       Se concatenan las directivas de todos los discos en orden. Las
       directivas de discos sin anomalías (OPTIMAL) se omiten por
       _build_directives_latex() (solo incluye DEGRADED/CRITICAL/UNKNOWN).

    5. El campo ``nvme`` de SystemEntropy conserva su nombre para
       compatibilidad con el contexto Jinja2 del resumen global (Sección 7).
       Ahora representa el vector agregado del conjunto de almacenamiento,
       no una unidad individual.
    """
    runtime_log("Entropy: Calculando degradación termodinámica global...")
    bat_data: BatteryData = battery if battery is not None else BatteryData()

    # ── Evaluaciones individuales (sin cambios) ───────────────────────────
    vec_cpu     = _eval_cpu(cpu)
    vec_gpu     = _eval_gpu(gpu)
    vec_ram     = _eval_ram(ram)
    vec_vrm     = _eval_vrm(mobo)
    vec_usb     = _eval_usb(usb)
    vec_battery = _eval_battery(bat_data)

    # ════════════════════════════════════════════════════════════════════
    #  MOTOR MULTI-DISCO
    # ════════════════════════════════════════════════════════════════════

    # 1. Evaluar cada disco individualmente
    drive_vectors: list[SubsystemVector] = [
        _eval_storage(drive) for drive in storage_drives
    ]

    # 2. Guard: lista vacía → vector UNKNOWN para no romper el motor global
    if not drive_vectors:
        drive_vectors = [SubsystemVector(
            subsystem    = "STORAGE",
            delta_a      = 0,
            state        = EntropyState.UNKNOWN,
            badge        = _BADGE[EntropyState.UNKNOWN],
            badge_compat = _BADGE_COMPAT[EntropyState.UNKNOWN],
            directives   = (
                _latex_item(
                    "Sub-sistema Almacenamiento",
                    "Ningún dispositivo de almacenamiento detectado o enumerado.",
                ),
            ),
        )]

    # 3. ΔA acumulativo: suma de todas las anomalías individuales
    total_storage_da: int = sum(v.delta_a for v in drive_vectors)

    # 4. Estado clínico: el más severo del conjunto
    worst_storage_state: EntropyState = _worst_state(
        *(v.state for v in drive_vectors)
    )

    # 5. Directivas concatenadas de todos los discos
    all_storage_directives: tuple[str, ...] = tuple(
        directive
        for vec in drive_vectors
        for directive in vec.directives
    )

    # 6. SubsystemVector agregado (expuesto como vec_nvme para compat.)
    vec_nvme = SubsystemVector(
        subsystem    = "STORAGE",
        delta_a      = total_storage_da,
        state        = worst_storage_state,
        badge        = _BADGE[worst_storage_state],
        badge_compat = _BADGE_COMPAT[worst_storage_state],
        directives   = all_storage_directives,
    )

    # ════════════════════════════════════════════════════════════════════

    # ── Ensamblaje global (sin cambios respecto a v1.1) ───────────────────
    vectors: list[SubsystemVector] = [
        vec_cpu, vec_gpu, vec_ram, vec_nvme, vec_vrm, vec_usb, vec_battery
    ]

    total_delta_a: int          = sum(v.delta_a for v in vectors)
    global_state:  EntropyState = _worst_state(*(v.state for v in vectors))
    global_badge:  str          = _BADGE[global_state]

    score_cpu    = _clamp_score(vec_cpu.delta_a)
    score_gpu    = _clamp_score(vec_gpu.delta_a)
    score_nvme   = _clamp_score(vec_nvme.delta_a)
    score_ram    = _clamp_score(vec_ram.delta_a)
    score_mobo   = _clamp_score(vec_vrm.delta_a)
    score_usb    = _clamp_score(vec_usb.delta_a)
    score_bat    = _clamp_score(vec_battery.delta_a)
    score_global = _clamp_score(total_delta_a)

    accion_cpu    = _first_directive_text(vec_cpu.directives)
    accion_gpu    = _first_directive_text(vec_gpu.directives)
    accion_nvme   = _first_directive_text(vec_nvme.directives)
    accion_ram    = _first_directive_text(vec_ram.directives)
    accion_mobo   = _first_directive_text(vec_vrm.directives)
    accion_usb    = _first_directive_text(vec_usb.directives)
    accion_bat    = _first_directive_text(vec_battery.directives)
    accion_global = _RESUMEN_TEMPLATE[global_state].split(".")[0] + "."

    return SystemEntropy(
        cpu     = vec_cpu,
        gpu     = vec_gpu,
        nvme    = vec_nvme,   # vector agregado multi-disco
        ram     = vec_ram,
        vrm     = vec_vrm,
        usb     = vec_usb,
        battery = vec_battery,

        total_delta_a = total_delta_a,
        global_state  = global_state,
        global_badge  = global_badge,

        score_cpu    = score_cpu,
        score_gpu    = score_gpu,
        score_nvme   = score_nvme,
        score_ram    = score_ram,
        score_mobo   = score_mobo,
        score_usb    = score_usb,
        score_bat    = score_bat,
        score_global = score_global,

        badge_cpu    = vec_cpu.badge_compat,
        badge_gpu    = vec_gpu.badge_compat,
        badge_nvme   = vec_nvme.badge_compat,
        badge_ram    = vec_ram.badge_compat,
        badge_mobo   = vec_vrm.badge_compat,
        badge_usb    = vec_usb.badge_compat,
        badge_bat    = vec_battery.badge_compat,
        badge_global = _BADGE_COMPAT[global_state],

        accion_cpu    = accion_cpu,
        accion_gpu    = accion_gpu,
        accion_nvme   = accion_nvme,
        accion_ram    = accion_ram,
        accion_mobo   = accion_mobo,
        accion_usb    = accion_usb,
        accion_bat    = accion_bat,
        accion_global = accion_global,

        lista_recomendaciones = _build_directives_latex(vectors),
        estado_global_badge   = _BADGE_COMPAT[global_state],
        resumen_ejecutivo     = _RESUMEN_TEMPLATE[global_state],
    )