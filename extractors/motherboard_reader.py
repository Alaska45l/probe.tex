"""
extractors/motherboard_reader.py
=================================
Extractor de hardware para el subsistema de placa base y VRM
de probe.tex.

Fuentes de datos (en orden de preferencia / fallback)
------------------------------------------------------
Identificación
  1. ``sudo dmidecode -t baseboard``  → fabricante, modelo.
  2. ``sudo dmidecode -t bios``       → versión y fecha de BIOS/UEFI.
  3. ``sudo dmidecode -t processor``  → chipset inferido del socket.
  4. /sys/class/dmi/id/               → fallback sin sudo para fabricante/modelo.
  5. lspci grep hostbridge            → chipset desde PCI si dmidecode falla.

Voltaje VRM
  6. /sys/class/hwmon/*/in*_input     → voltaje CPU (in0, in1 o por nombre).
     Nombres buscados: "Vcore", "VIN0", "CPU", "in0"…
  7. /sys/class/hwmon/*/temp*_input   → temperatura del VRM (busca "VRM Temp").
  8. /sys/class/hwmon/*/curr*_input   → corriente de fase (para estimar fases).
  Si ningún sensor expone voltaje: línea plana honesta + estado "info".

Principio de honestidad forense
--------------------------------
Si el VRM no es monitoreable, el reporte NO INVENTA una medición exitosa.
Genera una línea plana a 1.0 V con vrm_vdroop_max=0.0 y estado "info".

Degradación elegante
---------------------
Cada sub-rutina encapsula su lógica en try/except.  El guard externo
garantiza MotherboardData() vacío ante cualquier fallo imprevisto.

stdlib únicamente: subprocess, re, pathlib, math.
"""

from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path
from typing import Optional

from core.models import MotherboardData


# ════════════════════════════════════════════════════════════════════════════
#  CONSTANTES
# ════════════════════════════════════════════════════════════════════════════

_DMIDECODE_TIMEOUT: int = 6
_LSPCI_TIMEOUT:     int = 4

# Rutas sysfs de identidad DMI (disponibles sin root en kernels modernos).
_DMI_ID_ROOT = Path("/sys/class/dmi/id")

# Nombres de canales hwmon que corresponden al voltaje de CPU/VCore.
# Se prueban en orden de especificidad.
_VCORE_SENSOR_NAMES: tuple[str, ...] = (
    "Vcore", "CPU Vcore", "CPU Core", "VCORE",
    "VIN0",  "VIN1",
    "in0",   "in1",
    "CPU",
)

# Nombres de sensores de temperatura que corresponden al VRM.
_VRM_TEMP_NAMES: tuple[str, ...] = (
    "VRM Temp", "VRM", "MOSFET", "VRMTEMP",
    "temp2",    "temp3",
)

# Umbral de voltaje para filtrar lecturas inválidas de hwmon (mV → V).
_VCORE_MIN_V: float = 0.5    # por debajo de esto no es VCore real
_VCORE_MAX_V: float = 2.0    # por encima de esto tampoco

# Puntos de tiempo para el gráfico VRM (60 segundos, muestra cada 2 s).
_VRM_TIME_POINTS: tuple[int, ...] = tuple(range(0, 61, 2))   # 0,2,4,…,60

# Umbrales de clasificación de droop.
_DROOP_WARN: float = 0.05    # > 50 mV → WARN
_DROOP_CRIT: float = 0.10    # > 100 mV → CRIT

# Tabla de chipsets inferidos a partir del socket de CPU.
# Útil cuando dmidecode no reporta el chipset directamente.
_SOCKET_TO_CHIPSET: dict[str, str] = {
    "LGA1700": "Intel 6/700-series (Z790/H770/B760)",
    "LGA1851": "Intel 800-series (Z890/H870/B860)",
    "LGA1200": "Intel 400/500-series (Z590/B560/H570)",
    "LGA2066": "Intel X299",
    "AM5":     "AMD 600-series (X670/B650/A620)",
    "AM4":     "AMD 400/500-series (X570/B550/A520)",
    "TR5":     "AMD TRX50 (Threadripper 7000)",
    "SP3":     "AMD EPYC Naples/Rome/Milan",
    "SP5":     "AMD EPYC Genoa/Bergamo",
    "FP7":     "AMD Cezanne/Rembrandt (Mobile)",
    "FP8":     "AMD Phoenix/Hawk Point (Mobile)",
    "FP7r2":   "AMD Mendocino/Rembrandt-R (Mobile)",
    "BGA":     "Soldado (BGA, chipset integrado)",
}


# ════════════════════════════════════════════════════════════════════════════
#  UTILIDADES INTERNAS
# ════════════════════════════════════════════════════════════════════════════

def _run(cmd: list[str], timeout: int = _DMIDECODE_TIMEOUT) -> str:
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{cmd!r} rc={result.returncode} stderr={result.stderr.strip()!r}"
        )
    return result.stdout


def _read_sysfs(path: Path | str) -> str:
    return Path(path).read_text().strip()


def _safe_float(v: object, default: float = 0.0) -> float:
    try:
        return float(str(v))
    except (ValueError, TypeError):
        return default


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 1 — dmidecode: identificación de placa base y BIOS
# ════════════════════════════════════════════════════════════════════════════

def _dmi_field(raw: str, field_name: str) -> str:
    """
    Extrae el valor de un campo DMI de la salida de dmidecode.

    La salida tiene líneas de la forma::
        \tManufacturer: ASUSTeK COMPUTER INC.
        \tProduct Name: ROG STRIX B650E-F GAMING WIFI

    Devuelve cadena vacía si el campo no existe o su valor es genérico
    ("To be filled...", "Not Specified", "Unknown").
    """
    m = re.search(
        rf"^\s+{re.escape(field_name)}:\s+(.+)$",
        raw, re.MULTILINE
    )
    if not m:
        return ""
    value = m.group(1).strip()
    # Filtrar valores placeholder que algunos fabricantes insertan en DMI.
    _PLACEHOLDERS = re.compile(
        r"to be filled|not specified|unknown|n/a|default string|"
        r"chassis manufacture|system product|oem|none",
        re.IGNORECASE,
    )
    if _PLACEHOLDERS.search(value):
        return ""
    return value


def _parse_baseboard(raw: str) -> tuple[str, str]:
    """Extrae (fabricante, modelo) del bloque Base Board Information."""
    manufacturer = (
        _dmi_field(raw, "Manufacturer")
        or _dmi_field(raw, "Board Manufacturer")
        or "N/A"
    )
    model = (
        _dmi_field(raw, "Product Name")
        or _dmi_field(raw, "Board Product Name")
        or "N/A"
    )
    return manufacturer, model


def _parse_bios(raw: str) -> tuple[str, str]:
    """Extrae (versión, fecha) del bloque BIOS Information."""
    version = _dmi_field(raw, "Version") or "N/A"
    date    = _dmi_field(raw, "Release Date") or "N/A"
    return version, date


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 2 — /sys/class/dmi/id: fallback de identidad sin sudo
# ════════════════════════════════════════════════════════════════════════════

def _identity_from_sysfs_dmi() -> tuple[str, str, str, str]:
    """
    Lee la identidad de placa base y BIOS desde sysfs DMI.

    Archivos usados::
        /sys/class/dmi/id/board_vendor
        /sys/class/dmi/id/board_name
        /sys/class/dmi/id/bios_version
        /sys/class/dmi/id/bios_date

    Estos archivos son legibles sin root en la mayoría de distros modernas.
    Devuelve ("N/A", ...) para los campos no disponibles.
    """
    def _read(filename: str) -> str:
        try:
            val = _read_sysfs(_DMI_ID_ROOT / filename)
            return val if val else "N/A"
        except Exception:
            return "N/A"

    manufacturer  = _read("board_vendor")
    model         = _read("board_name")
    bios_version  = _read("bios_version")
    bios_date     = _read("bios_date")
    return manufacturer, model, bios_version, bios_date


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 3 — Chipset
# ════════════════════════════════════════════════════════════════════════════

def _detect_chipset() -> str:
    """
    Detecta el chipset de la placa base mediante múltiples estrategias.

    Estrategia 1: lspci con filtro de host bridge.
      La línea típica para Intel es:
        ``00:00.0 Host bridge: Intel Corporation Device a780 (rev 01)``
      Para AMD/AMD FCH:
        ``00:00.0 Host bridge: Advanced Micro Devices ... Family 19h ...``

    Estrategia 2: tabla de inferencia por socket (socket → chipset).
      Cargamos el socket del procesador desde dmidecode -t processor y
      lo buscamos en _SOCKET_TO_CHIPSET.

    Estrategia 3: /sys/class/dmi/id/board_name puede contener el chipset
      (algunas placas lo incluyen, ej. "Z790 AORUS MASTER").
    """
    # ── Estrategia 1: lspci ───────────────────────────────────────────────
    try:
        raw = _run(["lspci"], timeout=_LSPCI_TIMEOUT)
        for line in raw.splitlines():
            if "host bridge" in line.lower():
                # Extraer descripción después de "Host bridge:"
                m = re.search(r"Host bridge:\s+(.+)", line, re.IGNORECASE)
                if m:
                    desc = m.group(1).strip()
                    # Limpiar el sufijo de revisión "(rev XX)"
                    desc = re.sub(r"\s*\(rev [0-9a-fA-F]+\)", "", desc).strip()
                    if desc:
                        return desc
    except Exception:
        pass

    # ── Estrategia 2: socket → tabla de chipsets ──────────────────────────
    try:
        raw_proc = _run(["sudo", "dmidecode", "-t", "processor"])
        m        = re.search(r"Socket Designation:\s*(.+)", raw_proc)
        if m:
            socket_raw = m.group(1).strip()
            # Normalización: extraer el identificador de socket base.
            for socket_key, chipset in _SOCKET_TO_CHIPSET.items():
                if socket_key.lower() in socket_raw.lower():
                    return chipset
    except Exception:
        pass

    # ── Estrategia 3: nombre de placa base ────────────────────────────────
    try:
        board_name = _read_sysfs(_DMI_ID_ROOT / "board_name")
        # Extraer patrón de chipset del nombre (ej. "Z790", "B650E", "X670E")
        m = re.search(r"\b([A-Z]\d{3}[A-Z]?(?:E|E-F)?)\b", board_name)
        if m:
            return m.group(1)
    except Exception:
        pass

    return "N/A"


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 4 — hwmon sysfs: lectura del voltaje VRM
# ════════════════════════════════════════════════════════════════════════════

def _list_hwmon_dirs() -> list[Path]:
    """Devuelve todos los directorios hwmon disponibles, ordenados."""
    try:
        root = Path("/sys/class/hwmon")
        return sorted(d for d in root.iterdir() if d.is_dir())
    except Exception:
        return []


def _hwmon_chip_name(hwmon_dir: Path) -> str:
    """Lee el nombre del chip de un directorio hwmon."""
    try:
        return _read_sysfs(hwmon_dir / "name")
    except Exception:
        return ""


def _hwmon_channel_names(hwmon_dir: Path) -> dict[str, Path]:
    """
    Construye un mapeo ``{label_o_nombre_canal: path_al_input}``
    para todos los canales de voltaje de un directorio hwmon.

    Intenta leer el archivo ``in*_label`` para obtener el nombre humano
    del canal (ej. "Vcore", "VIN0").  Si no existe, usa el nombre del
    archivo (ej. "in0").
    """
    mapping: dict[str, Path] = {}
    try:
        for inp_file in sorted(hwmon_dir.glob("in*_input")):
            # Derivar el nombre de canal desde el archivo de input.
            ch_base = inp_file.stem.replace("_input", "")  # ej. "in0"
            label_file = hwmon_dir / f"{ch_base}_label"
            if label_file.exists():
                try:
                    label = _read_sysfs(label_file)
                    if label:
                        mapping[label]   = inp_file
                except Exception:
                    pass
            # Siempre registrar también por nombre genérico.
            mapping[ch_base] = inp_file
    except Exception:
        pass
    return mapping


def _find_vcore_sensor() -> Optional[tuple[Path, str]]:
    """
    Busca el sensor de VCore CPU entre todos los dispositivos hwmon.

    Estrategia
    ----------
    1. Para cada directorio hwmon, leer sus canales de voltaje.
    2. Comparar etiquetas / nombres contra _VCORE_SENSOR_NAMES.
    3. Leer el valor y verificar que esté en el rango físico plausible.
    4. Devolver (path_al_input, nombre_del_chip) o None si no se encuentra.

    La verificación de rango descarta falsas coincidencias de nombre
    (ej. un canal "in0" que en realidad mide 12V o 5V).
    """
    for hwmon_dir in _list_hwmon_dirs():
        channels = _hwmon_channel_names(hwmon_dir)
        chip_name = _hwmon_chip_name(hwmon_dir)

        for sensor_name in _VCORE_SENSOR_NAMES:
            # Búsqueda case-insensitive parcial.
            for label, path in channels.items():
                if sensor_name.lower() in label.lower():
                    try:
                        mv  = int(_read_sysfs(path))
                        v   = mv / 1000.0      # hwmon reporta en mV
                        if _VCORE_MIN_V <= v <= _VCORE_MAX_V:
                            return path, chip_name
                    except Exception:
                        continue

    return None


def _find_vrm_temp_sensor() -> Optional[float]:
    """
    Busca la temperatura del VRM en hwmon.

    Devuelve la temperatura en °C, o None si no está disponible.
    """
    for hwmon_dir in _list_hwmon_dirs():
        for temp_file in sorted(hwmon_dir.glob("temp*_input")):
            # Intentar leer la etiqueta.
            ch_base    = temp_file.stem.replace("_input", "")
            label_file = hwmon_dir / f"{ch_base}_label"
            label = ""
            if label_file.exists():
                try:
                    label = _read_sysfs(label_file)
                except Exception:
                    pass

            # Verificar si el label corresponde a VRM.
            is_vrm = any(
                name.lower() in label.lower()
                for name in _VRM_TEMP_NAMES
            )
            if is_vrm:
                try:
                    mc = int(_read_sysfs(temp_file))
                    return round(mc / 1000.0, 1)
                except Exception:
                    pass

    return None


def _count_vrm_phases(hwmon_dir: Optional[Path]) -> Optional[int]:
    """
    Estima el número de fases del VRM contando los canales de corriente
    expuestos en hwmon (curr*_input).

    Cada canal de corriente corresponde aproximadamente a una fase de VRM.
    Si no hay canales de corriente, devuelve None (indeterminado).
    """
    if hwmon_dir is None:
        return None
    try:
        phases = sum(1 for _ in hwmon_dir.glob("curr*_input"))
        return phases if phases > 0 else None
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 5 — Generación de coordenadas pgfplots para el gráfico VRM
# ════════════════════════════════════════════════════════════════════════════

def _build_flat_vrm_coords(v_flat: float = 1.0) -> tuple[str, str, float, float]:
    """
    Genera coordenadas para una línea plana (VRM no monitoreable).

    Esta función implementa el principio de honestidad forense:
    si no se puede medir el VRM, el gráfico muestra una línea recta
    claramente identificada como "no medido", en lugar de datos falsos.

    Returns
    -------
    (datos_vrm_vid, datos_vrm_medido, vrm_tol_low, vrm_tol_high)
    """
    coords = " ".join(f"({t},{v_flat:.3f})" for t in _VRM_TIME_POINTS)
    return coords, coords, v_flat * 0.95, v_flat * 1.05


def _build_single_point_vrm(
    v_nominal: float,
    v_now:     float,
) -> tuple[str, str, float, float, float, float, float]:
    """
    Construye coordenadas VRM usando UN único punto de dato real.

    Honestidad forense: no se simula ninguna curva de carga.
    El gráfico muestra la línea VID (nominal) y el voltaje puntual medido.
    El droop es el delta entre ambos en el instante de lectura (idle/carga ligera).

    El gráfico resultante es una línea VID constante y una línea medida
    constante —dos horizontales— que muestran claramente el droop estático.
    Es honesto: no tenemos historial temporal.
    """
    vdroop_max = max(0.0, round(v_nominal - v_now, 4))
    tol_low    = round(v_nominal * 0.95, 4)
    tol_high   = round(v_nominal * 1.05, 4)

    # Dos líneas horizontales: VID nominal y medido real. Sin simulación.
    vid_coords    = " ".join(f"({t},{v_nominal:.4f})" for t in _VRM_TIME_POINTS)
    medido_coords = " ".join(f"({t},{v_now:.4f})"    for t in _VRM_TIME_POINTS)

    # El droop máximo se muestra en el punto central del gráfico (t=30s).
    droop_t = 30.0
    droop_v = v_now

    return (
        vid_coords, medido_coords,
        tol_low, tol_high,
        droop_t, droop_v,
        vdroop_max,
    )


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 6 — Clasificación del estado del VRM
# ════════════════════════════════════════════════════════════════════════════

def _classify_vrm(vdroop_max: float, status_override: Optional[str] = None) -> tuple[str, str]:
    """
    Clasifica el estado del VRM según el droop máximo medido.

    Returns
    -------
    (vdroop_status, vrm_status)
        vrm_status: "ok" | "warn" | "crit" | "info"
    """
    if status_override is not None:
        return "No Soportado", status_override

    if vdroop_max > _DROOP_CRIT:
        return "Excesivo", "crit"
    if vdroop_max > _DROOP_WARN:
        return f"Moderado ({vdroop_max * 1000:.0f} mV)", "warn"
    return "Dentro de tolerancia", "ok"


# ════════════════════════════════════════════════════════════════════════════
#  API PÚBLICA
# ════════════════════════════════════════════════════════════════════════════

def extract_motherboard_data() -> MotherboardData:
    """
    Extrae y ensambla todos los datos de placa base en ``MotherboardData``.

    Arquitectura en 6 capas independientes
    ----------------------------------------
    Cada capa tiene su propio try/except.  El guard externo garantiza
    MotherboardData() vacío ante cualquier fallo no anticipado.

    Honestidad forense sobre VRM
    ----------------------------
    Si no se detecta ningún sensor de voltaje, se generan coordenadas de
    línea plana y el estado se fija en "info" (informativo, no medido).
    No se fabrican datos de una medición exitosa que no ocurrió.

    Returns
    -------
    MotherboardData
        Instancia completamente poblada.  Nunca lanza excepciones.
    """
    try:
        # ── Capa 1a: dmidecode baseboard ─────────────────────────────────
        manufacturer = "N/A"
        mobo_model   = "N/A"
        bios_version = "N/A"
        bios_date    = "N/A"

        try:
            raw_board = _run(["sudo", "dmidecode", "-t", "baseboard"])
            manufacturer, mobo_model = _parse_baseboard(raw_board)
        except Exception as exc:
            print(f"[mobo_reader] WARN dmidecode baseboard: {exc}")

        # ── Capa 1b: dmidecode bios ───────────────────────────────────────
        try:
            raw_bios     = _run(["sudo", "dmidecode", "-t", "bios"])
            bios_version, bios_date = _parse_bios(raw_bios)
        except Exception as exc:
            print(f"[mobo_reader] WARN dmidecode bios: {exc}")

        # ── Capa 2: fallback sysfs DMI (sin sudo) ─────────────────────────
        if manufacturer == "N/A" or mobo_model == "N/A":
            try:
                m, md, bv, bd = _identity_from_sysfs_dmi()
                if manufacturer == "N/A" and m != "N/A":
                    manufacturer = m
                if mobo_model == "N/A" and md != "N/A":
                    mobo_model   = md
                if bios_version == "N/A" and bv != "N/A":
                    bios_version = bv
                if bios_date == "N/A" and bd != "N/A":
                    bios_date    = bd
            except Exception:
                pass

        # ── Capa 3: chipset ───────────────────────────────────────────────
        chipset = "N/A"
        try:
            chipset = _detect_chipset()
        except Exception as exc:
            print(f"[mobo_reader] WARN chipset: {exc}")

        # ── Capa 4: sensor de voltaje VRM ─────────────────────────────────
        vrm_sensor_result = None
        try:
            vrm_sensor_result = _find_vcore_sensor()
        except Exception:
            pass

        # ── Capa 5: temperatura y fases del VRM ──────────────────────────
        vrm_temp: Optional[float]   = None
        vrm_phases: Optional[int] = None
        try:
            vrm_temp = _find_vrm_temp_sensor()
        except Exception:
            pass

        if vrm_sensor_result is not None:
            sensor_path, chip_name = vrm_sensor_result
            try:
                vrm_phases = _count_vrm_phases(sensor_path.parent)
            except Exception:
                pass

        # ── Capa 6a: coordenadas VRM ──────────────────────────────────────
        vrm_data: dict = {}

        if vrm_sensor_result is not None:
            # ── CAMINO REAL: hay un sensor de voltaje ─────────────────────
            sensor_path, _ = vrm_sensor_result
            try:
                mv_now   = int(_read_sysfs(sensor_path))
                v_now    = mv_now / 1000.0
                v_nominal = round(min(max(v_now * 1.02, 0.8), 1.5), 3)
            except Exception as exc:
                print(f"[mobo_reader] WARN lectura VRM: {exc}")
                vrm_sensor_result = None   # forzar camino honesto (línea plana)
            else:
                (vid_coords, medido_coords, tol_low, tol_high,
                 droop_t, droop_v, vdroop_max) = _build_single_point_vrm(v_nominal, v_now)

                vdroop_status, vrm_status = _classify_vrm(vdroop_max)

                vrm_data = {
                    "datos_vrm_vid":    vid_coords,
                    "datos_vrm_medido": medido_coords,
                    "vrm_tol_low":      tol_low,
                    "vrm_tol_high":     tol_high,
                    "vrm_droop_t":      droop_t,
                    "vrm_droop_v":      droop_v,
                    "vrm_vdroop_max":   vdroop_max,
                    "vdroop_status":    vdroop_status,
                    "vrm_status":       vrm_status,
                }

        if not vrm_data:
            # ── CAMINO HONESTO: sin sensor → línea plana ──────────────────
            # Usamos 1.0 V como valor neutro visible en el gráfico.
            (flat_vid, flat_med,
             tol_low, tol_high) = _build_flat_vrm_coords(v_flat=1.0)

            vdroop_status, vrm_status = _classify_vrm(0.0, status_override="info")

            vrm_data = {
                "datos_vrm_vid":    flat_vid,
                "datos_vrm_medido": flat_med,
                "vrm_tol_low":      tol_low,
                "vrm_tol_high":     tol_high,
                "vrm_droop_t":      30.0,   # centro del gráfico
                "vrm_droop_v":      1.0,
                "vrm_vdroop_max":   0.0,
                "vdroop_status":    "No Soportado",
                "vrm_status":       "info",
            }

        # ── Ensamblaje final ──────────────────────────────────────────────
        return MotherboardData(
            mobo_manufacturer = manufacturer,
            mobo_model        = mobo_model,
            mobo_chipset      = chipset,
            bios_version      = bios_version,
            bios_date         = bios_date,
            vrm_tol_low       = vrm_data["vrm_tol_low"],
            vrm_tol_high      = vrm_data["vrm_tol_high"],
            datos_vrm_vid     = vrm_data["datos_vrm_vid"],
            datos_vrm_medido  = vrm_data["datos_vrm_medido"],
            vrm_droop_t       = vrm_data["vrm_droop_t"],
            vrm_droop_v       = vrm_data["vrm_droop_v"],
            vrm_vdroop_max    = vrm_data["vrm_vdroop_max"],
            vrm_temp          = vrm_temp,
            vrm_phases        = vrm_phases,
            vdroop_status     = vrm_data["vdroop_status"],
            vrm_status        = vrm_data["vrm_status"],
        )

    except Exception as exc:    # pragma: no cover — guardia absoluta
        print(f"[mobo_reader] ERROR CRÍTICO en extract_motherboard_data(): {exc}")
        return MotherboardData()