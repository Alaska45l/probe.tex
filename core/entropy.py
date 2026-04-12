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
from typing import Final

from core.models import (
    CPUData, GPUData, MotherboardData, NVMeData, RAMData, USBData,
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
_THR_CPU_DELTA_T_DISSIPATION: Final[float] = 15.0   # °C  ΔT recuperación térmica
_THR_CPU_RECOVERY_S:          Final[float] = 45.0   # s   tiempo de enfriamiento
_THR_CPU_TJMAX_MARGIN:        Final[float] = 0.0    # °C  distancia a TjMax (0 = alcanzado)

_DA_CPU_THERMAL_DISSIPATION:  Final[int] = 20   # incapacidad de disipación o throttling
_DA_CPU_THROTTLE:             Final[int] = 20   # eventos de throttling registrados
_DA_CPU_TJMAX_BREACH:         Final[int] = 50   # T_max ≥ TjMax
_DA_CPU_MCE:                  Final[int] = 50   # Machine Check Exception

# ── GPU ──────────────────────────────────────────────────────────────────────
_THR_GPU_DELTA_HOTSPOT_WARN:  Final[float] = 20.0   # °C  ΔT hotspot-edge
_THR_GPU_VRAM_ERRORS_CRIT:    Final[int]   = 0       # cualquier error de VRAM = CRIT
_THR_GPU_AER_FATAL_CRIT:      Final[int]   = 0       # cualquier AER fatal = CRIT

_DA_GPU_HOTSPOT_DISSIPATION:  Final[int] = 20   # ΔT hotspot fuera de rango
_DA_GPU_TJMAX_BREACH:         Final[int] = 50   # T_hotspot ≥ temp_limit
_DA_GPU_VRAM_CORRUPTION:      Final[int] = 50   # errores de integridad VRAM
_DA_GPU_AER_FATAL:            Final[int] = 30   # enlace PCIe con errores fatales
_DA_GPU_PCIE_DEGRADED:        Final[int] = 10   # lanes activos < lanes máximos

# ── RAM ──────────────────────────────────────────────────────────────────────
# La integridad de la memoria no es negociable.
# Un único error de bit es un fallo de hardware activo.
_DA_RAM_ECC_FAULT:            Final[int] = 100  # cualquier error EDAC/ECC/UE

# ── NVMe ─────────────────────────────────────────────────────────────────────
_THR_NVME_WAF_DEGRADED:       Final[float] = 3.0    # Write Amplification Factor
_THR_NVME_SPARE_PCT_DEGRADED: Final[int]   = 10     # % spare blocks mínimos
_THR_NVME_LIFE_CRITICAL:      Final[int]   = 5      # % vida útil restante → CRIT
_THR_NVME_LIFE_DEGRADED:      Final[int]   = 20     # % vida útil restante → DEGRAD
_THR_NVME_ECC_DEGRADED:       Final[int]   = 100    # errores ECC acumulados

_DA_NVME_WAF_EXCESS:          Final[int] = 30   # WAF > 3.0
_DA_NVME_SPARE_DEPLETED:      Final[int] = 30   # spare < 10 %
_DA_NVME_LIFE_CRITICAL:       Final[int] = 50   # vida < 5 %
_DA_NVME_LIFE_DEGRADED:       Final[int] = 30   # vida < 20 % (y ≥ 5 %)
_DA_NVME_ECC_EXCESS:          Final[int] = 30   # errores ECC ≥ 100

# ── VRM (Motherboard) ────────────────────────────────────────────────────────
# El porcentaje de droop se calcula como: vrm_vdroop_max / V_nominal
# donde V_nominal = vrm_tol_high / 1.05 (vrm_tol_high = V_nominal × 1.05)
_THR_VRM_DROOP_DEGRADED:      Final[float] = 0.05   # > 5 % del VID → DEGRADED
_THR_VRM_DROOP_CRITICAL:      Final[float] = 0.10   # > 10 % del VID → CRITICAL

_DA_VRM_DROOP_DEGRADED:       Final[int] = 15   # Vdroop > 5 %
_DA_VRM_DROOP_CRITICAL:       Final[int] = 50   # Vdroop > 10 %

# ── USB ──────────────────────────────────────────────────────────────────────
_DA_USB_PER_FAILED_PORT:      Final[int] = 10   # por cada puerto con fallo
_DA_USB_PER_UNSTABLE_PORT:    Final[int] = 5    # por cada puerto inestable (warn)

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

# Bridge hacia las macros del template reporte_base.tex.
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

    subsystem:  str            # Identificador: "CPU" | "GPU" | "NVME" | "RAM" | "VRM" | "USB"
    delta_a:    int            # Contribución al Índice de Anomalía. Mínimo: 0.
    state:      EntropyState
    badge:      str            # Macro LaTeX del motor de entropía (\badgeoptimal, etc.)
    badge_compat: str          # Macro LaTeX compatible con reporte_base.tex
    directives: tuple[str, ...] # Log de intervención en formato LaTeX \item


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
    lista_recomendaciones: bloque \enumerate completo listo para inyección.
    estado_global_badge: macro de badge compatible para la portada.
    resumen_ejecutivo: string de diagnóstico global (texto plano).
    """

    cpu:  SubsystemVector
    gpu:  SubsystemVector
    nvme: SubsystemVector
    ram:  SubsystemVector
    vrm:  SubsystemVector
    usb:  SubsystemVector

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
    score_global: int

    badge_cpu:    str
    badge_gpu:    str
    badge_nvme:   str
    badge_ram:    str
    badge_mobo:   str
    badge_usb:    str
    badge_global: str

    accion_cpu:    str
    accion_gpu:    str
    accion_nvme:   str
    accion_ram:    str
    accion_mobo:   str
    accion_usb:    str
    accion_global: str

    lista_recomendaciones: str
    estado_global_badge:   str   # para la portada (badge compatible)
    resumen_ejecutivo:     str   # texto plano para el cuadro de veredicto


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
    # Strip \item \textbf{...}: prefix for plain-text usage
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

    # ── MCE: fallo de hardware en el procesador ──────────────────────────
    if cpu.mce_count > 0:
        delta_a += _DA_CPU_MCE
        directives.append(_latex_item(
            "Sub-sistema CPU / MCE",
            fr"Se registraron \textbf{{{cpu.mce_count}}} Machine Check Exception(s). "
            r"Fallo de hardware activo. Reemplazo del procesador mandatorio "
            r"si los errores son recurrentes post-microcode update."
        ))

    # ── TjMax breach: operación en zona de destrucción ───────────────────
    if cpu.cpu_t_max > 0.0 and cpu.cpu_tjmax > 0 and cpu.cpu_t_max >= cpu.cpu_tjmax:
        delta_a += _DA_CPU_TJMAX_BREACH
        directives.append(_latex_item(
            "Sub-sistema CPU / Térmico (CRÍTICO)",
            fr"$T_{{\max}}$ = {cpu.cpu_t_max:.1f}°C ≥ TjMax = {cpu.cpu_tjmax}°C. "
            r"Operación en zona de autoprotección térmica. "
            r"El silicio opera fuera de especificación eléctrica. "
            r"Intervención inmediata: reemplazo de TIM y verificación del sistema de refrigeración."
        ))

    # ── Disipación térmica insuficiente ──────────────────────────────────
    thermal_fail = (
        cpu.cpu_delta_t > _THR_CPU_DELTA_T_DISSIPATION
        or cpu.cpu_recovery_time > _THR_CPU_RECOVERY_S
    )
    if thermal_fail and delta_a < _DA_CPU_TJMAX_BREACH:
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

    # ── Throttling: incapacidad de mantener frecuencia nominal ───────────
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

    # ── Errores de integridad VRAM: corrupción de datos ──────────────────
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

    # ── AER fatales: fallo de enlace PCIe ───────────────────────────────
    if gpu.gpu_aer_fatal > _THR_GPU_AER_FATAL_CRIT:
        delta_a += _DA_GPU_AER_FATAL
        directives.append(_latex_item(
            "Sub-sistema GPU / PCIe AER",
            fr"\textbf{{{gpu.gpu_aer_fatal}}} error(es) AER fatal(es) en el "
            fr"enlace PCIe Gen{gpu.gpu_pcie_gen_active} x{gpu.gpu_pcie_lanes_active}. "
            r"Fallo estructural en el bus. Verificar slot físico y conector de alimentación PCIe."
        ))

    # ── TjMax breach: hotspot ≥ límite de temperatura ────────────────────
    if gpu.gpu_t_hotspot > 0.0 and gpu.gpu_t_hotspot >= gpu.gpu_temp_limit:
        delta_a += _DA_GPU_TJMAX_BREACH
        directives.append(_latex_item(
            "Sub-sistema GPU / Térmico (CRÍTICO)",
            fr"$T_{{\text{{hotspot}}}}$ = {gpu.gpu_t_hotspot:.1f}°C ≥ "
            fr"límite fabricante = {gpu.gpu_temp_limit}°C. "
            r"Zona de throttling de emergencia activa. "
            r"Reemplazar TIM die-heatsink. Verificar pad térmico de VRAM."
        ))

    # ── Delta hotspot: gradiente térmico die-edge excesivo ───────────────
    elif gpu.gpu_delta_t_hotspot > _THR_GPU_DELTA_HOTSPOT_WARN:
        delta_a += _DA_GPU_HOTSPOT_DISSIPATION
        directives.append(_latex_item(
            "Sub-sistema GPU / Delta Térmico",
            fr"$\Delta T_{{\text{{hotspot}}}}$ = {gpu.gpu_delta_t_hotspot:.1f}°C "
            fr"(umbral: {_THR_GPU_DELTA_HOTSPOT_WARN:.0f}°C). "
            r"Gradiente térmico die-edge fuera de especificación. "
            r"Degradación del TIM entre die y heatspreader probable."
        ))

    # ── Lanes PCIe activos < máximos: enlace degradado ───────────────────
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

    # ── Errores ECC / EDAC: la integridad de memoria no es negociable ────
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

    # ── Vida útil crítica: fallo NAND inminente ──────────────────────────
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

    # ── WAF > 3.0: GC agresivo, escrituras amplificadas ─────────────────
    if nvme.nvme_waf > _THR_NVME_WAF_DEGRADED:
        delta_a += _DA_NVME_WAF_EXCESS
        directives.append(_latex_item(
            "Sub-sistema NAND / WAF",
            fr"Write Amplification Factor = {nvme.nvme_waf:.2f} "
            fr"(umbral: {_THR_NVME_WAF_DEGRADED:.1f}). "
            r"El controlador NAND está realizando un número excesivo de "
            r"reescrituras internas. Analizar patrón de acceso del workload "
            r"(escrituras aleatorias pequeñas). Verificar alineación de particiones."
        ))

    # ── Spare blocks agotados: over-provisioning consumido ───────────────
    if nvme.nvme_spare_blocks < _THR_NVME_SPARE_PCT_DEGRADED:
        delta_a += _DA_NVME_SPARE_DEPLETED
        directives.append(_latex_item(
            "Sub-sistema NAND / Bloques de Repuesto",
            fr"Spare blocks disponibles: {nvme.nvme_spare_blocks}\% "
            fr"(umbral mínimo: {_THR_NVME_SPARE_PCT_DEGRADED}\%). "
            r"Over-provisioning de la NAND prácticamente agotado. "
            r"La capacidad de corrección de errores y reasignación de bloques "
            r"está severamente comprometida. Reemplazo programado urgente."
        ))

    # ── Errores ECC acumulados: degradación de la capa NAND ──────────────
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

    # Sensor no disponible o no soportado
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

    # V_nominal reconstruido desde la banda de tolerancia.
    # vrm_tol_high = V_nominal × 1.05  →  V_nominal = vrm_tol_high / 1.05
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

    # ── Puertos con fallo: +10 por cada uno ──────────────────────────────
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

    # ── Puertos inestables: +5 por cada uno ──────────────────────────────
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
    cpu:  CPUData,
    gpu:  GPUData,
    nvme: NVMeData,
    ram:  RAMData,
    mobo: MotherboardData,
    usb:  USBData,
) -> SystemEntropy:
    """
    Evalúa la entropía física del sistema completo y retorna ``SystemEntropy``.

    El cálculo es determinista y sin estado: la misma telemetría produce
    siempre el mismo resultado. No se realizan operaciones de I/O.

    Algoritmo
    ---------
    1. Evaluar cada subsistema de forma independiente → ``SubsystemVector``.
    2. Sumar los ΔA individuales → ``total_delta_a``.
    3. El ``global_state`` es el estado del subsistema de mayor severidad
       (worst-case, no promedio).
    4. Derivar los campos de bridge para el template LaTeX existente.

    Parameters
    ----------
    cpu, gpu, nvme, ram, mobo, usb : Instancias de los dataclasses del modelo.

    Returns
    -------
    SystemEntropy
        Resultado inmutable (frozen dataclass). Nunca lanza excepciones
        ante datos de entrada inconsistentes: el modelo tolera ceros y N/A.
    """
    vec_cpu  = _eval_cpu(cpu)
    vec_gpu  = _eval_gpu(gpu)
    vec_ram  = _eval_ram(ram)
    vec_nvme = _eval_nvme(nvme)
    vec_vrm  = _eval_vrm(mobo)
    vec_usb  = _eval_usb(usb)

    vectors: list[SubsystemVector] = [
        vec_cpu, vec_gpu, vec_ram, vec_nvme, vec_vrm, vec_usb
    ]

    total_delta_a: int = sum(v.delta_a for v in vectors)
    global_state:  EntropyState = _worst_state(*(v.state for v in vectors))
    global_badge:  str = _BADGE[global_state]

    # ── Bridge: scores derivados de ΔA individual ────────────────────────
    score_cpu   = _clamp_score(vec_cpu.delta_a)
    score_gpu   = _clamp_score(vec_gpu.delta_a)
    score_nvme  = _clamp_score(vec_nvme.delta_a)
    score_ram   = _clamp_score(vec_ram.delta_a)
    score_mobo  = _clamp_score(vec_vrm.delta_a)
    score_usb   = _clamp_score(vec_usb.delta_a)
    score_global = _clamp_score(total_delta_a)

    # ── Bridge: acciones primarias por subsistema (texto plano) ──────────
    accion_cpu   = _first_directive_text(vec_cpu.directives)
    accion_gpu   = _first_directive_text(vec_gpu.directives)
    accion_nvme  = _first_directive_text(vec_nvme.directives)
    accion_ram   = _first_directive_text(vec_ram.directives)
    accion_mobo  = _first_directive_text(vec_vrm.directives)
    accion_usb   = _first_directive_text(vec_usb.directives)
    accion_global = _RESUMEN_TEMPLATE[global_state].split(".")[0] + "."

    return SystemEntropy(
        cpu  = vec_cpu,
        gpu  = vec_gpu,
        nvme = vec_nvme,
        ram  = vec_ram,
        vrm  = vec_vrm,
        usb  = vec_usb,

        total_delta_a = total_delta_a,
        global_state  = global_state,
        global_badge  = global_badge,

        score_cpu    = score_cpu,
        score_gpu    = score_gpu,
        score_nvme   = score_nvme,
        score_ram    = score_ram,
        score_mobo   = score_mobo,
        score_usb    = score_usb,
        score_global = score_global,

        badge_cpu    = vec_cpu.badge_compat,
        badge_gpu    = vec_gpu.badge_compat,
        badge_nvme   = vec_nvme.badge_compat,
        badge_ram    = vec_ram.badge_compat,
        badge_mobo   = vec_vrm.badge_compat,
        badge_usb    = vec_usb.badge_compat,
        badge_global = _BADGE_COMPAT[global_state],

        accion_cpu    = accion_cpu,
        accion_gpu    = accion_gpu,
        accion_nvme   = accion_nvme,
        accion_ram    = accion_ram,
        accion_mobo   = accion_mobo,
        accion_usb    = accion_usb,
        accion_global = accion_global,

        lista_recomendaciones = _build_directives_latex(vectors),
        estado_global_badge   = _BADGE_COMPAT[global_state],
        resumen_ejecutivo     = _RESUMEN_TEMPLATE[global_state],
    )