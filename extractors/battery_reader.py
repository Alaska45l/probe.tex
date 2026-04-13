"""
extractors/battery_reader.py
============================
Extractor de hardware para el subsistema de batería de probe.tex.

Fuente de datos principal
--------------------------
/sys/class/power_supply/BAT*/

  Capacidad (fuente primaria)
    energy_full_design  → capacidad de diseño en µWh
    energy_full         → capacidad actual a carga completa en µWh

  Capacidad (fuente secundaria — fallback µAh × V)
    charge_full_design  → capacidad de diseño en µAh
    charge_full         → capacidad actual en µAh
    voltage_now         → voltaje actual en µV (factor de conversión)

  Ciclos y estado
    cycle_count         → ciclos de carga acumulados (0 si no disponible)
    status              → Charging / Discharging / Full / Unknown

  Eléctrico
    voltage_now         → voltaje en tiempo real (µV)
    voltage_min_design  → tensión de corte (voltaje nominal de celda) (µV)
    voltage_max_design  → tensión de carga plena (µV)
    current_now         → corriente en tiempo real (µA, puede ser negativa)

  Metadatos
    manufacturer / model_name / technology → strings del fabricante

Principio de Honestidad Forense
---------------------------------
Si ningún directorio BAT* existe en /sys/class/power_supply/ (o todos son
adaptadores AC), el módulo retorna BatteryData(battery_present=False)
sin ninguna fabricación de datos.

Fórmula de Nivel de Desgaste (Wear Level)
------------------------------------------
  wear_level (%) = (1 − (capacity_full / capacity_full_design)) × 100

  Donde capacity puede ser en µWh (fuente primaria) o
  µAh × V_actual (fuente secundaria).

  bat_soh = 100 − wear_level  →  ya definido en BatteryData.

Estimación de Resistencia Interna
-----------------------------------
  R_int (mΩ) = (V_nom − V_actual) / I_desc × 1000

  Solo se calcula si:
    - La batería está descargando (current_now negativo en ACPI, positivo
      en algunos firmwares: siempre usamos valor absoluto).
    - |I_actual| ≥ _MIN_CURRENT_RESISTANCE_MA (100 mA).
    - ΔV = V_nom − V_actual > 0 (no se invierte el cálculo).

  Si las condiciones no se cumplen → R_int = 0.0 (honestidad forense).

Degradación elegante
---------------------
Cada capa encapsula su lógica en try/except.
El guard externo garantiza BatteryData(battery_present=False) ante
cualquier fallo no anticipado.

stdlib únicamente: pathlib.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from core.models import BatteryData


# ════════════════════════════════════════════════════════════════════════════
#  CONSTANTES
# ════════════════════════════════════════════════════════════════════════════

_POWER_SUPPLY_ROOT: Path = Path("/sys/class/power_supply")

# Longitud máxima del gauge TikZ en cm (constante del template LaTeX).
_BAT_GAUGE_MAX_CM: float = 4.6

# Corriente mínima de descarga para que la estimación de R_int sea válida.
_MIN_CURRENT_RESISTANCE_MA: int = 100

# Factor de conversión µWh → mWh.
_UWH_TO_MWH: int = 1_000


# ════════════════════════════════════════════════════════════════════════════
#  UTILIDADES INTERNAS
# ════════════════════════════════════════════════════════════════════════════

def _read_sysfs(path: Path | str) -> str:
    return Path(path).read_text().strip()


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return default


def _read_field(bat_dir: Path, field: str, default: str = "") -> str:
    """Lee un campo sysfs de la batería. Devuelve ``default`` en cualquier fallo."""
    try:
        return _read_sysfs(bat_dir / field)
    except Exception:
        return default


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 1 — Localización de la batería
# ════════════════════════════════════════════════════════════════════════════

def _find_battery_dirs() -> list[Path]:
    """
    Devuelve los directorios de tipo 'Battery' en /sys/class/power_supply,
    ordenados alfabéticamente (BAT0 < BAT1 < …).

    Estrategia de filtrado
    ----------------------
    Para cada subdirectorio presente, lee el archivo ``type`` y verifica
    que sea exactamente ``Battery``.  Descarta adaptadores AC, puertos
    USB-PD, fuentes de alimentación de mouse/teclado inalámbrico, etc.

    Devuelve lista vacía (no lanza) si el directorio raíz no existe o
    no tiene entradas válidas.
    """
    if not _POWER_SUPPLY_ROOT.exists():
        return []
    try:
        result: list[Path] = []
        for entry in sorted(_POWER_SUPPLY_ROOT.iterdir()):
            type_file = entry / "type"
            if not type_file.exists():
                continue
            try:
                if _read_sysfs(type_file).strip().lower() == "battery":
                    result.append(entry)
            except Exception:
                continue
        return result
    except Exception:
        return []


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 2 — Capacidad, SoH y Nivel de Desgaste
# ════════════════════════════════════════════════════════════════════════════

def _extract_capacity_mwh(bat_dir: Path) -> tuple[int, int]:
    """
    Extrae la capacidad de diseño y la capacidad real en mWh.

    Fuente primaria : ``energy_full_design`` / ``energy_full`` (µWh → mWh).
      Los kernels modernos reportan energía directamente para la mayoría
      de baterías Li-ion de portátiles modernos.

    Fuente secundaria: ``charge_full_design`` / ``charge_full`` (µAh).
      Se convierte a mWh multiplicando por ``voltage_now`` (µV) y
      dividiendo por 10^9 (µAh × µV = µWh → mWh con /1000).

    Honestidad forense: si ninguna fuente está disponible → (0, 0).
    No se asumen valores de capacidad sin telemetría.

    Returns
    -------
    (design_cap_mwh, full_cap_mwh)
        Ambos 0 si la telemetría no está disponible.
    """
    # ── Fuente primaria: energía (µWh → mWh) ─────────────────────────────
    try:
        design_uwh = _safe_int(_read_field(bat_dir, "energy_full_design"))
        full_uwh   = _safe_int(_read_field(bat_dir, "energy_full"))
        if design_uwh > 0 and full_uwh > 0:
            return design_uwh // _UWH_TO_MWH, full_uwh // _UWH_TO_MWH
    except Exception:
        pass

    # ── Fuente secundaria: carga (µAh × µV / 10^9 = mWh) ─────────────────
    try:
        design_uah = _safe_int(_read_field(bat_dir, "charge_full_design"))
        full_uah   = _safe_int(_read_field(bat_dir, "charge_full"))
        voltage_uv = _safe_int(_read_field(bat_dir, "voltage_now", "0"))

        if design_uah > 0 and full_uah > 0 and voltage_uv > 0:
            # µAh × µV = 1e-6 Ah × 1e-6 V = 1e-12 Wh = 1e-9 mWh
            # → dividir por 1e9 para obtener mWh
            design_mwh = int(design_uah * voltage_uv // 1_000_000_000)
            full_mwh   = int(full_uah   * voltage_uv // 1_000_000_000)
            # Guard: si la conversión produjo cero (overflow inverso), abortar
            if design_mwh > 0 and full_mwh > 0:
                return design_mwh, full_mwh
    except Exception:
        pass

    return 0, 0


def _compute_soh(design_cap: int, full_cap: int) -> int:
    """
    SoH = clamp(round((full_cap / design_cap) × 100), 0, 100).

    Retorna 0 si alguno de los parámetros es cero o negativo (honestidad
    forense: no se inventa un SoH sin datos de capacidad válidos).
    """
    if design_cap <= 0 or full_cap <= 0:
        return 0
    return max(0, min(100, round((full_cap / design_cap) * 100)))


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 3 — Voltaje y Corriente
# ════════════════════════════════════════════════════════════════════════════

def _extract_voltage(bat_dir: Path) -> tuple[float, float, float]:
    """
    Extrae (V_nominal, V_actual, ΔV) en Voltios.

    V_nominal
    ----------
    Prioridad:
      1. ``voltage_min_design`` (tensión de corte nominal de la celda).
      2. 85 % de ``voltage_max_design`` (estimación para Li-ion si solo
         hay Vmax).
      3. ``voltage_now`` como fallback de último recurso (sin ΔV).

    V_actual
    ----------
    ``voltage_now`` (lectura en tiempo real en µV → V).

    ΔV = max(0, V_nominal − V_actual).
      Positivo cuando la batería está descargando bajo carga.
      0 si V_actual ≥ V_nominal (cargando o en reposo con boost).

    Returns
    -------
    (v_nominal, v_actual, delta_v)  — todos redondeados a 3 decimales.
    """
    v_now_uv = _safe_int(_read_field(bat_dir, "voltage_now",        "0"))
    v_min_uv = _safe_int(_read_field(bat_dir, "voltage_min_design", "0"))
    v_max_uv = _safe_int(_read_field(bat_dir, "voltage_max_design", "0"))

    v_now = v_now_uv / 1_000_000.0

    if v_min_uv > 500_000:          # > 0.5 V → valor plausible
        v_nom = v_min_uv / 1_000_000.0
    elif v_max_uv > 500_000:        # estimar nominal = 85 % de Vmax
        v_nom = round(v_max_uv / 1_000_000.0 * 0.85, 4)
    else:
        v_nom = v_now               # sin datos de diseño: sin ΔV calculable

    delta_v = round(max(0.0, v_nom - v_now), 4)

    return round(v_nom, 3), round(v_now, 3), delta_v


def _extract_current_ma(bat_dir: Path) -> int:
    """
    Lee ``current_now`` (µA) y lo convierte a mA en valor absoluto.

    Algunos firmwares ACPI reportan corriente negativa durante la descarga
    y positiva durante la carga; otros hacen lo inverso.  Siempre se
    devuelve el valor absoluto: la dirección no es relevante para el
    cálculo de resistencia interna.

    Returns
    -------
    int — corriente en mA (≥ 0). 0 si no disponible.
    """
    raw = _safe_int(_read_field(bat_dir, "current_now", "0"))
    return abs(raw) // 1_000


def _estimate_resistance(v_nom: float, v_now: float, i_ma: int, status: str) -> Optional[float]:
    """
    Estimación de resistencia interna (mΩ).

    Modelo: R = ΔV / I_A = (V_nom − V_now) / (I_mA / 1000) × 1000 (mΩ)

    Condiciones necesarias para cálculo válido:
      1. i_ma ≥ _MIN_CURRENT_RESISTANCE_MA (carga significativa detectada).
      2. ΔV > 0 (batería realmente descargando, no cargando).
      3. status == "Discharging" (no calculable si está conectada a AC).

    Honestidad forense: si las condiciones no se cumplen, devuelve None.
    No se fabrica un valor de R_int.

    Returns
    -------
    Optional[float] — resistencia en mΩ, redondeada a 1 decimal. None si incalculable.
    """
    delta_v = v_nom - v_now
    if status != "Discharging" or i_ma < _MIN_CURRENT_RESISTANCE_MA or delta_v <= 0.0:
        return None
    i_amperes = i_ma / 1_000.0
    return round((delta_v / i_amperes) * 1_000.0, 1)


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 4 — Gauge TikZ y clasificación de color
# ════════════════════════════════════════════════════════════════════════════

def _gauge_color(soh: int) -> str:
    """
    Nombre del color LaTeX/TikZ para el relleno del gauge de SoH.

    Umbrales:
      soh ≥ 80 %   →  StatusOKMid   (verde industrial)
      soh ∈ [60, 80) → StatusWarnMid (naranja)
      soh < 60 %   →  StatusCritMid  (rojo crítico)
    """
    if soh >= 80:
        return "StatusOKMid"
    if soh >= 60:
        return "StatusWarnMid"
    return "StatusCritMid"


# ════════════════════════════════════════════════════════════════════════════
#  API PÚBLICA
# ════════════════════════════════════════════════════════════════════════════

def extract_battery_data() -> BatteryData:
    """
    Extrae y ensambla todos los datos de batería en una instancia ``BatteryData``.

    Arquitectura de extracción en 4 capas independientes
    -----------------------------------------------------
    Capa 1  Localización  — detecta directorios BAT* de tipo Battery en sysfs.
    Capa 2  Capacidad     — mWh de diseño y actuales; computa SoH y Wear Level.
    Capa 3  Eléctrico     — voltaje nominal/actual, ΔV, corriente, R_int.
    Capa 4  Gauge         — color semáforo y longitud del indicador TikZ.

    Honestidad Forense
    ------------------
    - Si no hay batería (desktop, servidor) → BatteryData(battery_present=False).
    - SoH = 0 implica que no se pudo leer la capacidad, no que la batería
      esté muerta: bat_soh=0 AND bat_design_cap=0 → UNKNOWN en entropy.py.
    - La resistencia interna solo se calcula bajo descarga activa con
      corriente ≥ 100 mA.  En cualquier otro estado → 0.0 (honesto).

    Returns
    -------
    BatteryData
        Instancia completamente poblada.  Nunca lanza excepciones.
    """
    try:
        # ── Capa 1: localización ─────────────────────────────────────────
        bat_dirs = _find_battery_dirs()
        if not bat_dirs:
            return BatteryData(battery_present=False)

        bat_dir = bat_dirs[0]   # batería principal (BAT0)

        # ── Capa 2: capacidad y SoH ──────────────────────────────────────
        design_cap, full_cap = _extract_capacity_mwh(bat_dir)
        soh    = _compute_soh(design_cap, full_cap)
        
        _raw_cycles = _read_field(bat_dir, "cycle_count", "")
        if _raw_cycles:
            cycles: Optional[int] = _safe_int(_raw_cycles)
        else:
            _raw_alt = _read_field(bat_dir, "charge_control_cycle_count", "")
            cycles = _safe_int(_raw_alt) if _raw_alt else None

        # ── Capa 3: voltaje y corriente ──────────────────────────────────
        v_nom, v_now, v_drop = _extract_voltage(bat_dir)
        i_ma                 = _extract_current_ma(bat_dir)
        status               = _read_field(bat_dir, "status", "Unknown")
        resistance           = _estimate_resistance(v_nom, v_now, i_ma, status)

        # ── Capa 4: gauge TikZ ───────────────────────────────────────────
        gauge_color = _gauge_color(soh)
        gauge_fill  = round((soh / 100.0) * _BAT_GAUGE_MAX_CM, 2)

        return BatteryData(
            battery_present  = True,
            bat_design_cap   = design_cap,
            bat_full_cap     = full_cap,
            bat_cycles       = cycles,
            bat_soh          = soh,
            bat_voltage_nom  = v_nom,
            bat_voltage_load = v_now,
            bat_voltage_drop = v_drop if (status == "Discharging" and v_drop > 0.0) else None,
            bat_current_load = i_ma,
            bat_resistance   = resistance,
            bat_gauge_color  = gauge_color,
            bat_gauge_fill   = gauge_fill,
        )

    except Exception as exc:   # pragma: no cover — guardia absoluta
        print(f"[battery_reader] ERROR CRÍTICO en extract_battery_data(): {exc}")
        return BatteryData(battery_present=False)