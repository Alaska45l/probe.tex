"""
core/entropy.py
===============
Motor de evaluación de entropía física y degradación termodinámica
para AuditMaster Lite.

Este módulo reemplaza el concepto de "score 0-100" con un modelo basado
en la física real del hardware: el Índice de Anomalía (ΔA).

Modelo matemático
-----------------
ΔA es un entero no negativo que comienza en 0 y se acumula con cada
fallo físico observado en la telemetría de los dataclasses de entrada.
No es una escala arbitraria de satisfacción: cada incremento corresponde
a un evento de degradación material específico con magnitud física definida.

Jerarquía de estados
--------------------
    UNKNOWN   → Telemetría insuficiente para emitir diagnóstico.
    OPTIMAL   → ΔA = 0. Sin anomalías detectadas.
    DEGRADED  → ΔA ∈ [1, 49]. Degradación activa. Intervención programable.
    CRITICAL  → ΔA ≥ 50. Fallo estructural inminente o en curso.

La clasificación global es la del peor subsistema, no un promedio.
Un único error de RAM (ΔA = 100) colapsa el sistema entero a CRITICAL
independientemente del estado de los demás subsistemas.

Convenciones de nomenclatura
-----------------------------
_DA_*     : constante de incremento de Índice de Anomalía (int).
_THR_*    : umbral físico de activación (float o int).
_eval_*() : función pura de evaluación de subsistema → SubsystemVector.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Optional

from core.models import (
    BatteryData, CPUData, GPUData, MotherboardData, NVMeData, RAMData, USBData,
)


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

# ── NVMe ─────────────────────────────────────────────────────────────────────
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

# ── VRM (Motherboard) ────────────────────────────────────────────────────────
_THR_VRM_DROOP_DEGRADED:      Final[float] = 0.05
_THR_VRM_DROOP_CRITICAL:      Final[float] = 0.10

_DA_VRM_DROOP_DEGRADED:       Final[int] = 15
_DA_VRM_DROOP_CRITICAL:       Final[int] = 50

# ── USB ──────────────────────────────────────────────────────────────────────
_DA_USB_PER_FAILED_PORT:      Final[int] = 10
_DA_USB_PER_UNSTABLE_PORT:    Final[int] = 5

# ── Batería (Entropía Química) ───────────────────────────────────────────────
# Wear Level = 100 − SoH (porcentaje de capacidad perdida respecto al diseño).
# Umbrales derivados de la Guía de Mantenimiento de Baterías de Li-ion:
#   > 20 % de desgaste → degradación activa de las celdas electroquímicas.
#   > 40 % de desgaste → ciclo de vida comprometido, riesgo de ciclos
#                         incompletos y degradación de voltaje bajo carga.
_THR_BAT_WEAR_DEGRADED:       Final[float] = 20.0  # % wear level → DEGRADED
_THR_BAT_WEAR_CRITICAL:       Final[float] = 40.0  # % wear level → CRITICAL

_DA_BAT_WEAR_DEGRADED:        Final[int] = 15   # 1 ≤ 15 < 50  → DEGRADED
_DA_BAT_WEAR_CRITICAL:        Final[int] = 50   # 50 ≥ 50      → CRITICAL

# ── Clasificación global por ΔA total ────────────────────────────────────────
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
#  DATACLASSES DE SALIDA
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SubsystemVector:
    """Vector de diagnóstico para un único subsistema de hardware."""

    subsystem:  str
    delta_a:    int
    state:      EntropyState
    badge:      str
    badge_compat: str
    directives: tuple[str, ...]


@dataclass(frozen=True)
class SystemEntropy:
    """
    Resultado del análisis de entropía del sistema completo.

    Campos de evaluación
    --------------------
    subsystems      : Vectores individuales por subsistema (inmutables).
    total_delta_a   : Suma acumulada de todos los ΔA individuales.
    global_state    : Estado del subsistema con mayor ΔA (worst-case).
    global_badge    : Macro LaTeX del estado global.

    Campos de renderizado LaTeX (bridge → GlobalSummary / DiagnosticReport)
    -----------------------------------------------------------------------
    Los campos score_* son derivados: score = clamp(100 − ΔA, 0, 100).
    Los campos badge_* usan las macros del engine de entropía.
    Los campos accion_* contienen la primera directiva de intervención
    del subsistema correspondiente, en texto plano (sin LaTeX).
    lista_recomendaciones: bloque \\enumerate completo listo para inyección.
    estado_global_badge: macro de badge compatible para la portada.
    resumen_ejecutivo: string de diagnóstico global (texto plano).

    Subsistema de Batería (NUEVO)
    -----------------------------
    battery, score_bat, badge_bat, accion_bat: refleja la entropía química
    de las celdas electroquímicas.  En equipos de escritorio (sin batería),
    battery.state = UNKNOWN y score_bat = 100 (no penaliza el índice global).
    """

    cpu:     SubsystemVector
    gpu:     SubsystemVector
    nvme:    SubsystemVector
    ram:     SubsystemVector
    vrm:     SubsystemVector
    usb:     SubsystemVector
    battery: SubsystemVector   # NUEVO: entropía química de celdas Li-ion

    total_delta_a: int
    global_state:  EntropyState
    global_badge:  str

    # ── Bridge → GlobalSummary ───────────────────────────────────────────
    score_cpu:    int
    score_gpu:    int
    score_nvme:   int
    score_ram:    int
    score_mobo:   int
    score_usb:    int
    score_bat:    int    # NUEVO
    score_global: int

    badge_cpu:    str
    badge_gpu:    str
    badge_nvme:   str
    badge_ram:    str
    badge_mobo:   str
    badge_usb:    str
    badge_bat:    str    # NUEVO
    badge_global: str

    accion_cpu:    str
    accion_gpu:    str
    accion_nvme:   str
    accion_ram:    str
    accion_mobo:   str
    accion_usb:    str
    accion_bat:    str   # NUEVO
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
    """Devuelve el estado de mayor severidad. UNKNOWN solo si todos son UNKNOWN."""
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
    """Extrae el contenido de texto de la primera directiva LaTeX."""
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
        or cpu.cpu_recovery_time < 0.0   # centinela: jamás recuperó
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


def _eval_nvme(nvme: NVMeData) -> SubsystemVector:
    delta_a:    int         = 0
    directives: list[str]   = []

    no_telemetry = nvme.nvme_model == "N/A" and nvme.nvme_capacity == 0
    if no_telemetry:
        return SubsystemVector(
            subsystem="NVME", delta_a=0, state=EntropyState.UNKNOWN,
            badge=_BADGE[EntropyState.UNKNOWN],
            badge_compat=_BADGE_COMPAT[EntropyState.UNKNOWN],
            directives=(
                _latex_item("Sub-sistema NVMe",
                    "Telemetría SMART no disponible. Verificar permisos de smartctl."),
            ),
        )

    if nvme.nvme_life_pct < _THR_NVME_LIFE_CRITICAL:
        delta_a += _DA_NVME_LIFE_CRITICAL
        directives.append(_latex_item(
            "Sub-sistema NAND / Vida Útil (CRÍTICO)",
            fr"Vida útil restante: \textbf{{{nvme.nvme_life_pct}\%}} "
            fr"(umbral crítico: {_THR_NVME_LIFE_CRITICAL}\%). "
            r"Falla estructural NAND inminente. Reemplazo mandatorio. "
            r"Iniciar backup inmediato antes de cualquier otra operación."
        ))
    elif nvme.nvme_life_pct < _THR_NVME_LIFE_DEGRADED:
        delta_a += _DA_NVME_LIFE_DEGRADED
        directives.append(_latex_item(
            "Sub-sistema NAND / Vida Útil",
            fr"Vida útil restante: {nvme.nvme_life_pct}\% "
            fr"(umbral: {_THR_NVME_LIFE_DEGRADED}\%). "
            r"Planificar reemplazo en el próximo ciclo de mantenimiento."
        ))

    if nvme.nvme_waf > _THR_NVME_WAF_DEGRADED:
        delta_a += _DA_NVME_WAF_EXCESS
        directives.append(_latex_item(
            "Sub-sistema NAND / WAF",
            fr"Write Amplification Factor = {nvme.nvme_waf:.2f} "
            fr"(umbral: {_THR_NVME_WAF_DEGRADED:.1f}). "
            r"El controlador NAND está realizando un número excesivo de "
            r"reescrituras internas. Analizar patrón de acceso del workload. "
            r"Verificar alineación de particiones."
        ))

    if nvme.nvme_spare_blocks < _THR_NVME_SPARE_PCT_DEGRADED:
        delta_a += _DA_NVME_SPARE_DEPLETED
        directives.append(_latex_item(
            "Sub-sistema NAND / Bloques de Repuesto",
            fr"Spare blocks disponibles: {nvme.nvme_spare_blocks}\% "
            fr"(umbral mínimo: {_THR_NVME_SPARE_PCT_DEGRADED}\%). "
            r"Over-provisioning de la NAND prácticamente agotado. "
            r"Reemplazo programado urgente."
        ))

    if nvme.nvme_ecc_errors >= _THR_NVME_ECC_DEGRADED:
        delta_a += _DA_NVME_ECC_EXCESS
        directives.append(_latex_item(
            "Sub-sistema NAND / ECC",
            fr"{nvme.nvme_ecc_errors} errores ECC corregibles acumulados "
            fr"(umbral: {_THR_NVME_ECC_DEGRADED}). "
            r"Tasa de errores NAND elevada. Indicativo de desgaste de celdas. "
            r"Monitorear con frecuencia creciente."
        ))

    state = _state_from_delta_a(delta_a)
    if not directives:
        directives.append(_latex_item(
            "Sub-sistema NVMe",
            fr"Sin anomalías de desgaste. Vida restante: {nvme.nvme_life_pct}\%. "
            fr"WAF: {nvme.nvme_waf:.2f}. TBW restante: {nvme.nvme_tbw_remaining:.1f} TB."
        ))

    return SubsystemVector(
        subsystem="NVME", delta_a=delta_a, state=state,
        badge=_BADGE[state], badge_compat=_BADGE_COMPAT[state],
        directives=tuple(directives),
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
    """
    Evalúa la entropía química del subsistema de batería.

    Modelo de Degradación Electroquímica
    -------------------------------------
    El Wear Level (WL) representa la fracción de capacidad electroquímica
    perdida de forma irreversible respecto a la especificación de fábrica:

        WL (%) = (1 − bat_full_cap / bat_design_cap) × 100
               = 100 − bat_soh

    Cada ciclo de carga intercala litio entre los electrodos, formando
    gradualmente una capa de SEI (Solid Electrolyte Interphase) que
    reduce la capacidad activa de las celdas.

    Umbrales de ΔA
    --------------
    WL > _THR_BAT_WEAR_CRITICAL (40 %) → ΔA += 50  → CRITICAL
      Ciclo de vida comprometido. Riesgo de corte abrupto bajo cargas pico.
      ΔA = 50 colapsa el estado directamente a CRITICAL (≥ _DA_GLOBAL_CRITICAL_FLOOR).

    WL ∈ (_THR_BAT_WEAR_DEGRADED, _THR_BAT_WEAR_CRITICAL] → ΔA += 15 → DEGRADED
      Autonomía reducida. Intervención planificada recomendada.

    Los umbrales son mutuamente excluyentes: si WL > 40%, solo se aplica
    _DA_BAT_WEAR_CRITICAL (50), no la suma 15 + 50.

    Caso sin batería (escritorio)
    ------------------------------
    Si battery_present=False → UNKNOWN, ΔA = 0.
    Un equipo de escritorio sin batería no tiene entropía química:
    no se penaliza el índice global.

    Caso sin telemetría (batería presente pero datos ilegibles)
    -----------------------------------------------------------
    Si bat_soh=0 Y bat_design_cap=0 → UNKNOWN, ΔA = 0.
    Honestidad forense: sin datos no se emite diagnóstico.

    Parameters
    ----------
    battery : BatteryData
        Instancia del dataclass de batería (presente o no).

    Returns
    -------
    SubsystemVector
        Vector de diagnóstico con ΔA, estado y directivas LaTeX.
    """
    # ── Sin batería: equipo de escritorio o batería no detectada ─────────
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

    # ── Sin telemetría: batería presente pero datos de capacidad ilegibles ─
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

    # ── Cálculo del Wear Level ────────────────────────────────────────────
    wear_level: float = 100.0 - battery.bat_soh   # % degradación

    delta_a:    int         = 0
    directives: list[str]   = []

    if wear_level > _THR_BAT_WEAR_CRITICAL:
        # Nivel crítico: supera el 40 % de desgaste acumulado.
        # No se acumula el ΔA de DEGRADED: se aplica directamente CRITICAL.
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
        # Nivel degradado: supera el 20 % de desgaste.
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

    # ── Resistencia interna elevada (indicador adicional de degradación) ──
    # No suma ΔA propio; es un dato informativo que enriquece la directiva.
    # R_int > 200 mΩ en una celda Li-ion típica indica degradación avanzada
    # del electrolito o del SEI (rango nominal: 50–150 mΩ).
    if battery.bat_resistance is not None and battery.bat_resistance > 200.0 and delta_a == 0:
        # Solo reportar si aún no hay directiva de desgaste (para no redundar).
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
#  ENSAMBLAJE DE LA LISTA DE RECOMENDACIONES LaTeX
# ════════════════════════════════════════════════════════════════════════════

def _build_directives_latex(vectors: list[SubsystemVector]) -> str:
    """
    Construye el bloque de items LaTeX para ``lista_recomendaciones``.

    Solo incluye directivas de subsistemas con ΔA > 0 o estado UNKNOWN.
    Los subsistemas OPTIMAL con la directiva genérica se omiten del log.
    """
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
    EntropyState.OPTIMAL:  (
        "Sistema operando dentro de parámetros nominales en todos los subsistemas. "
        "No se registran anomalías físicas en la ventana de diagnóstico."
    ),
    EntropyState.DEGRADED: (
        "Degradación física activa detectada en uno o más subsistemas. "
        "El hardware opera fuera de su especificación óptima. "
        "Se requiere intervención técnica en el próximo ciclo de mantenimiento."
    ),
    EntropyState.CRITICAL:  (
        "Estado crítico confirmado. Fallo estructural inminente o en curso. "
        "La continuidad operativa del sistema no está garantizada. "
        "Intervención inmediata mandatoria antes de cualquier operación de producción."
    ),
    EntropyState.UNKNOWN:   (
        "Telemetría insuficiente para emitir diagnóstico definitivo. "
        "Verificar permisos de los extractores y la presencia del hardware."
    ),
}


# ════════════════════════════════════════════════════════════════════════════
#  FUNCIÓN PRINCIPAL
# ════════════════════════════════════════════════════════════════════════════

def evaluate_system_entropy(
    cpu:     CPUData,
    gpu:     GPUData,
    nvme:    NVMeData,
    ram:     RAMData,
    mobo:    MotherboardData,
    usb:     USBData,
    battery: Optional[BatteryData] = None,   # NUEVO — opcional para compatibilidad
) -> SystemEntropy:
    """
    Evalúa la entropía física y química del sistema completo.

    El cálculo es determinista y sin estado: la misma telemetría produce
    siempre el mismo resultado. No se realizan operaciones de I/O.

    Algoritmo
    ---------
    1. Evaluar cada subsistema de forma independiente → SubsystemVector.
    2. Sumar los ΔA individuales → total_delta_a.
    3. global_state = estado del subsistema de mayor severidad (worst-case).
    4. Derivar los campos de bridge para el template LaTeX existente.

    Batería (NUEVO)
    ---------------
    Si battery es None (compatibilidad hacia atrás), se usa BatteryData()
    con battery_present=False, lo que produce state=UNKNOWN y delta_a=0.
    El índice global no se penaliza por la ausencia del parámetro.

    Si battery.battery_present=False (desktop/servidor), mismo resultado:
    UNKNOWN, ΔA=0. Un equipo sin batería no tiene entropía química medible.

    Parameters
    ----------
    cpu, gpu, nvme, ram, mobo, usb : Instancias de los dataclasses del modelo.
    battery : BatteryData opcional. None → usa BatteryData() (no presente).

    Returns
    -------
    SystemEntropy
        Resultado inmutable (frozen dataclass). Nunca lanza excepciones
        ante datos de entrada inconsistentes.
    """
    # Normalizar battery: si no se pasó, usar instancia neutra (no presente).
    bat_data: BatteryData = battery if battery is not None else BatteryData()

    vec_cpu     = _eval_cpu(cpu)
    vec_gpu     = _eval_gpu(gpu)
    vec_ram     = _eval_ram(ram)
    vec_nvme    = _eval_nvme(nvme)
    vec_vrm     = _eval_vrm(mobo)
    vec_usb     = _eval_usb(usb)
    vec_battery = _eval_battery(bat_data)   # NUEVO

    vectors: list[SubsystemVector] = [
        vec_cpu, vec_gpu, vec_ram, vec_nvme, vec_vrm, vec_usb, vec_battery
    ]

    total_delta_a: int         = sum(v.delta_a for v in vectors)
    global_state:  EntropyState = _worst_state(*(v.state for v in vectors))
    global_badge:  str          = _BADGE[global_state]

    # ── Bridge: scores derivados de ΔA individual ────────────────────────
    score_cpu    = _clamp_score(vec_cpu.delta_a)
    score_gpu    = _clamp_score(vec_gpu.delta_a)
    score_nvme   = _clamp_score(vec_nvme.delta_a)
    score_ram    = _clamp_score(vec_ram.delta_a)
    score_mobo   = _clamp_score(vec_vrm.delta_a)
    score_usb    = _clamp_score(vec_usb.delta_a)
    score_bat    = _clamp_score(vec_battery.delta_a)   # NUEVO
    score_global = _clamp_score(total_delta_a)

    # ── Bridge: acciones primarias por subsistema (texto plano) ──────────
    accion_cpu    = _first_directive_text(vec_cpu.directives)
    accion_gpu    = _first_directive_text(vec_gpu.directives)
    accion_nvme   = _first_directive_text(vec_nvme.directives)
    accion_ram    = _first_directive_text(vec_ram.directives)
    accion_mobo   = _first_directive_text(vec_vrm.directives)
    accion_usb    = _first_directive_text(vec_usb.directives)
    accion_bat    = _first_directive_text(vec_battery.directives)   # NUEVO
    accion_global = _RESUMEN_TEMPLATE[global_state].split(".")[0] + "."

    return SystemEntropy(
        cpu     = vec_cpu,
        gpu     = vec_gpu,
        nvme    = vec_nvme,
        ram     = vec_ram,
        vrm     = vec_vrm,
        usb     = vec_usb,
        battery = vec_battery,   # NUEVO

        total_delta_a = total_delta_a,
        global_state  = global_state,
        global_badge  = global_badge,

        score_cpu    = score_cpu,
        score_gpu    = score_gpu,
        score_nvme   = score_nvme,
        score_ram    = score_ram,
        score_mobo   = score_mobo,
        score_usb    = score_usb,
        score_bat    = score_bat,    # NUEVO
        score_global = score_global,

        badge_cpu    = vec_cpu.badge_compat,
        badge_gpu    = vec_gpu.badge_compat,
        badge_nvme   = vec_nvme.badge_compat,
        badge_ram    = vec_ram.badge_compat,
        badge_mobo   = vec_vrm.badge_compat,
        badge_usb    = vec_usb.badge_compat,
        badge_bat    = vec_battery.badge_compat,   # NUEVO
        badge_global = _BADGE_COMPAT[global_state],

        accion_cpu    = accion_cpu,
        accion_gpu    = accion_gpu,
        accion_nvme   = accion_nvme,
        accion_ram    = accion_ram,
        accion_mobo   = accion_mobo,
        accion_usb    = accion_usb,
        accion_bat    = accion_bat,    # NUEVO
        accion_global = accion_global,

        lista_recomendaciones = _build_directives_latex(vectors),
        estado_global_badge   = _BADGE_COMPAT[global_state],
        resumen_ejecutivo     = _RESUMEN_TEMPLATE[global_state],
    )