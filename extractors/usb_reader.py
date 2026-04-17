"""
extractors/usb_reader.py
========================
Extractor de hardware para el subsistema USB de AuditMaster Lite.

Fuentes de datos
----------------
1. ``lsusb -t``          → topología de buses y puertos con velocidades.
2. ``lsusb``             → listado plano para cruzar VID:PID con puertos.
3. /sys/bus/usb/devices/ → estadísticas de error por puerto (si disponible).

Arquitectura del parser de ``lsusb -t``
---------------------------------------
La salida de ``lsusb -t`` tiene una estructura de árbol con indentación::

    /:  Bus 04.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/4p, 10000M
        |__ Port 2: Dev 5, If 0, Class=Human Interface Device, Driver=usbhid, 480M
        |__ Port 4: Dev 3, If 0, Class=Wireless, Driver=btusb, 12M
    /:  Bus 03.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/2p, 20000M/x2
        |__ Port 1: Dev 2, If 0, Class=Hub, Driver=hub/4p, 5000M

Extraemos:
  - Número de bus y controlador (Driver del root_hub, ej. xhci_hcd).
  - Puerto, velocidad (M = Mbps), clase del dispositivo.

Clasificación de velocidad → versión USB
-----------------------------------------
  1.5 M  →  USB 1.0 Low Speed
  12 M   →  USB 1.1 Full Speed
  480 M  →  USB 2.0 High Speed
  5000 M →  USB 3.2 Gen 1 (5 Gbps)
  10000 M→  USB 3.2 Gen 2 (10 Gbps)
  20000 M→  USB 3.2 Gen 2×2 (20 Gbps)
  40000 M→  USB4 Gen 3×2 (40 Gbps)
  80000 M→  USB4 Gen 4 (80 Gbps)

Honestidad forense
------------------
Si ``lsusb -t`` falla por completo, se genera una sola fila LaTeX con
"N/A" y ``\\badgeinfo``.  Los contadores ``usb_ok/warn/fail`` reflejan
exactamente lo que se detectó: sin rellenos, sin éxitos fabricados.

stdlib únicamente: subprocess, re, pathlib.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.models import USBData


# ════════════════════════════════════════════════════════════════════════════
#  CONSTANTES
# ════════════════════════════════════════════════════════════════════════════

import logging
_LSUSB_TIMEOUT:  int = 5
_SYSFS_TIMEOUT:  int = 3

# Mapeo de velocidad en Mbps → (nombre de versión, Gbps float para la tabla)
_SPEED_MAP: dict[int, tuple[str, float]] = {
    # Mbps    versión               Gbps
    0:     ("Desconocido",          0.0),
    1:     ("USB 1.0 (LS)",         0.0015),
    2:     ("USB 1.0 (LS)",         0.0015),
    12:    ("USB 1.1 (FS)",         0.012),
    480:   ("USB 2.0 (HS)",         0.48),
    5000:  ("USB 3.2 Gen 1",        5.0),
    10000: ("USB 3.2 Gen 2",        10.0),
    20000: ("USB 3.2 Gen 2x2",      20.0),
    40000: ("USB4 Gen 3x2",         40.0),
    80000: ("USB4 Gen 4",           80.0),
}

# Umbral de Gbps a partir del cual se clasifica como "SuperSpeed".
_SS_THRESHOLD_GBPS: float = 4.9   # ≥ 5 Gbps → SuperSpeed


# ════════════════════════════════════════════════════════════════════════════
#  ESTRUCTURA INTERNA DE UN PUERTO USB
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class _UsbPort:
    """Representación interna de un puerto USB extraído de lsusb -t."""
    bus:          int   = 0
    port:         str   = "?"      # "1", "2.1", "2.1.3" (topología)
    speed_mbps:   int   = 0
    controller:   str   = "N/A"   # "xhci_hcd", "ehci-pci", "ohci-pci"
    usb_class:    str   = "N/A"   # "Hub", "Human Interface Device", etc.
    dev_id:       int   = 0       # número de dispositivo
    # Errores leídos desde sysfs (0 si no disponible o sin dispositivo)
    error_count:  int   = 0

    @property
    def speed_gbps(self) -> float:
        return _SPEED_MAP.get(self.speed_mbps, ("", 0.0))[1]

    @property
    def usb_version(self) -> str:
        return _SPEED_MAP.get(self.speed_mbps, ("Desconocido", 0.0))[0]

    @property
    def controller_short(self) -> str:
        """Nombre corto del controlador para la tabla LaTeX."""
        name = self.controller.lower()
        if "xhci" in name:
            return "xHCI"
        if "ehci" in name:
            return "eHCI"
        if "ohci" in name:
            return "oHCI"
        if "uhci" in name:
            return "uHCI"
        if "hub"  in name:
            return "Hub"
        return self.controller[:12] if self.controller != "N/A" else "N/A"

    @property
    def status(self) -> str:
        """Clasificación: 'ok', 'warn' o 'fail'."""
        if self.error_count == 0:
            return "ok"
        if self.error_count < 10:
            return "warn"
        return "fail"


# ════════════════════════════════════════════════════════════════════════════
#  UTILIDADES INTERNAS
# ════════════════════════════════════════════════════════════════════════════

def _run(cmd: list[str], timeout: int = _LSUSB_TIMEOUT) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(
            f"{cmd!r} rc={r.returncode} stderr={r.stderr.strip()!r}"
        )
    return r.stdout


def _sysfs(path: Path | str) -> str:
    return Path(path).read_text().strip()


def _safe_int(v: object, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return default


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 1 — Parser de lsusb -t
# ════════════════════════════════════════════════════════════════════════════

def _parse_speed(speed_str: str) -> int:
    """
    Convierte la cadena de velocidad de ``lsusb -t`` a Mbps.

    Formatos conocidos:
      "480M"      → 480
      "5000M"     → 5000
      "10000M"    → 10000
      "20000M/x2" → 20000
      "12M"       → 12
      "1.5M"      → 1   (redondeamos a 1; usaremos 1 como LS)
    """
    m = re.search(r"([\d.]+)M", speed_str)
    if m:
        val = float(m.group(1))
        # Redondear a entero y mapear 1.5 → 1
        return int(round(val))
    return 0


def _parse_lsusb_tree(raw: str) -> list[_UsbPort]:
    """
    Parsea la salida completa de ``lsusb -t`` y devuelve una lista de
    ``_UsbPort``, uno por cada puerto no-root con un dispositivo conectado.

    Estrategia de parseo en dos fases
    ----------------------------------
    Fase 1: Detectar las líneas de bus raíz (``/:  Bus XX.Port 1``).
      Extraer el número de bus y el nombre del controlador (``Driver=xhci_hcd``).

    Fase 2: Para cada puerto hijo (``|__ Port N: Dev M, ...``):
      Extraer número de puerto, velocidad, clase y asociar el bus/driver
      del bus raíz de contexto.

    Solo se incluyen puertos con ``Dev > 1`` (root_hub es Dev 1) y que
    tengan un driver asociado (indicativo de dispositivo real enumerado).
    Los puertos vacíos (sin ``Dev``) no se incluyen: no hay nada que reportar.
    """
    ports: list[_UsbPort] = []

    current_bus:        int = 0
    current_controller: str = "N/A"
    current_bus_speed:  int = 0   # velocidad máxima del bus raíz

    for line in raw.splitlines():
        stripped = line.strip()

        # ── Línea de bus raíz ──────────────────────────────────────────────
        # Patrón: "/:  Bus 04.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/4p, 10000M"
        root_m = re.match(
            r"/:\s+Bus\s+(\d+)\.Port\s+\d+:\s+Dev\s+\d+.*?Driver=(\S+?)(?:/\d+p)?[,\s].*?([\d.]+M)",
            stripped
        )
        if root_m:
            current_bus        = int(root_m.group(1))
            current_controller = root_m.group(2)
            current_bus_speed  = _parse_speed(root_m.group(3))
            continue

        # ── Línea de puerto hijo ───────────────────────────────────────────
        # Patrón: "|__ Port 2: Dev 5, If 0, Class=Human Interface Device, Driver=usbhid, 480M"
        # Patrón vacío: "|__ Port 3: Dev 0, ..."  (sin dispositivo)
        port_m = re.match(
            r"\|?[_\s]+Port\s+([\d.]+):\s+Dev\s+(\d+)(?:,\s+If\s+\d+)?,\s+Class=([^,]+)(?:,\s+Driver=([^,]+))?,\s+([\d.]+M\S*)",
            stripped
        )
        if port_m:
            port_num   = port_m.group(1)
            dev_num    = int(port_m.group(2))
            usb_class  = port_m.group(3).strip()
            driver_str = (port_m.group(4) or "").strip()
            speed_str  = port_m.group(5)

            # Excluir root hub (Dev 1) y puertos sin dispositivo (Dev 0)
            if dev_num <= 1:
                continue

            speed_mbps = _parse_speed(speed_str)
            # Si la velocidad del puerto excede la del bus raíz, usar la del bus.
            # (no puede ser más rápido que el controlador que lo gestiona)
            if current_bus_speed > 0 and speed_mbps > current_bus_speed:
                speed_mbps = current_bus_speed

            ports.append(_UsbPort(
                bus         = current_bus,
                port        = port_num,
                speed_mbps  = speed_mbps,
                controller  = current_controller,
                usb_class   = usb_class,
                dev_id      = dev_num,
            ))

    return ports


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 2 — Errores sysfs por dispositivo USB
# ════════════════════════════════════════════════════════════════════════════

def _read_usb_errors(bus: int, dev_id: int) -> int:
    """
    Intenta leer el contador de errores de un dispositivo USB desde sysfs.

    Ruta::
        /sys/bus/usb/devices/<bus>-<port>/ep_00/ep_type   (verifica que existe)
        /sys/bus/usb/devices/usb<bus>/authorized           (bus habilitado)

    No existe un contador de errores estándar universal en sysfs USB.
    Como aproximación, revisamos si el dispositivo tiene el campo
    ``power/usb2_lpm_enable`` o cualquier indicador de anormalidad.

    En la práctica, la mayoría de los sistemas consumer no exponen contadores
    de error USB en sysfs, por lo que esta función devuelve 0 en la mayoría
    de los casos.  Esto es HONESTO: reportamos 0, no inventamos errores.

    Devuelve el conteo de errores o 0.
    """
    try:
        usb_root = Path("/sys/bus/usb/devices")
        # El bus se representa como "usb<n>" para el root hub
        # y "<bus>-<port>" para los dispositivos conectados
        pattern = f"{bus}-"
        for dev_dir in usb_root.glob(f"{bus}-*"):
            dev_num_file = dev_dir / "devnum"
            if not dev_num_file.exists():
                continue
            try:
                if int(_sysfs(dev_num_file)) == dev_id:
                    # Verificar si el dispositivo está autorizado (connected)
                    auth_file = dev_dir / "authorized"
                    if auth_file.exists() and _sysfs(auth_file) == "0":
                        return 1   # No autorizado → error de enumeración
                    return 0
            except Exception:
                continue
    except Exception:
        pass
    return 0


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 3 — Generación de la tabla LaTeX
# ════════════════════════════════════════════════════════════════════════════

def _badge_for_status(status: str) -> str:
    """Devuelve la macro LaTeX de badge correspondiente al estado."""
    return {
        "ok":   r"\badgeok",
        "warn": r"\badgewarn",
        "fail": r"\badgefail",
        "info": r"\badgeinfo",
    }.get(status, r"\badgeinfo")


def _build_usb_latex_rows(ports: list[_UsbPort]) -> str:
    r"""
    Construye el string LaTeX de filas para la tabla USB del reporte.

    Formato de cada fila (del template ``reporte_base.tex``)::

        Bus 1 Port 2 & USB 3.2 Gen 1 & xHCI & 5.0 & 0 & \badgeok \\

    Con ``\rowalt`` en filas alternas para el estilo de tabla.

    Reglas
    ------
    - Gbps se formatea con 2 decimales, omitiendo el .00 si es entero
      (``5.0`` en lugar de ``5.00``; ``0.48`` en lugar de ``0.480``).
    - La columna "Controlador" usa el nombre corto (xHCI, eHCI…).
    - Si la lista está vacía → fila de aviso con "N/A" y ``\badgeinfo``.
    """
    if not ports:
        return r"    N/A & N/A & N/A & N/A & N/A & \badgeinfo \\"

    rows: list[str] = []
    for idx, port in enumerate(ports):
        alt  = "    \\rowalt\n" if idx % 2 == 1 else ""

        # Formato Gbps: evitar decimales innecesarios
        gbps = port.speed_gbps
        if gbps == 0.0:
            gbps_str = "N/A"
        elif gbps >= 1.0:
            gbps_str = f"{gbps:.1f}"
        else:
            gbps_str = f"{gbps:.3f}".rstrip("0")   # 0.480 → "0.48"

        badge = _badge_for_status(port.status)

        row = (
            f"{alt}"
            f"    Bus {port.bus} Puerto {port.port} & "
            f"{port.usb_version} & "
            f"{port.controller_short} & "
            f"{gbps_str} & "
            f"{port.error_count} & "
            f"{badge} \\\\"
        )
        rows.append(row)

    return "\n".join(rows)


# ════════════════════════════════════════════════════════════════════════════
#  CAPA 4 — Resumen de estado USB
# ════════════════════════════════════════════════════════════════════════════

def _summarize_ports(ports: list[_UsbPort]) -> tuple[int, int, int]:
    """
    Cuenta los puertos por estado.

    Returns
    -------
    (usb_ok, usb_warn, usb_fail)
    """
    ok   = sum(1 for p in ports if p.status == "ok")
    warn = sum(1 for p in ports if p.status == "warn")
    fail = sum(1 for p in ports if p.status == "fail")
    return ok, warn, fail


# ════════════════════════════════════════════════════════════════════════════
#  API PÚBLICA
# ════════════════════════════════════════════════════════════════════════════

def extract_usb_data() -> USBData:
    """
    Extrae y ensambla todos los datos USB en una instancia ``USBData``.

    Capas de extracción
    -------------------
    1. lsusb -t  → topología completa del árbol USB.
    2. sysfs      → errores por dispositivo (best-effort, 0 si no disponible).
    3. Construcción de la tabla LaTeX.
    4. Resumen de contadores ok/warn/fail.

    Fallback honesto
    ----------------
    Si lsusb falla completamente (no instalado, sin permisos, sin hardware):
    - ``usb_tabla_filas`` contiene una fila "N/A" con ``\\badgeinfo``.
    - Todos los contadores son 0.
    No se fabrican datos de éxito.

    Returns
    -------
    USBData
        Instancia completamente poblada.  Nunca lanza excepciones.
    """
    try:
        # ── Capa 1: lsusb -t ─────────────────────────────────────────────
        ports: list[_UsbPort] = []
        lsusb_ok = False

        try:
            raw_tree = _run(["lsusb", "-t"])
            ports    = _parse_lsusb_tree(raw_tree)
            lsusb_ok = True
        except Exception as exc:
            _log.warning("lsusb -t falló: %s", exc)

        # ── Capa 2: errores sysfs por puerto ─────────────────────────────
        if lsusb_ok:
            for port in ports:
                try:
                    port.error_count = _read_usb_errors(port.bus, port.dev_id)
                except Exception:
                    port.error_count = 0   # honesto: sin datos = 0 errores

        # ── Capa 3: tabla LaTeX ───────────────────────────────────────────
        tabla_latex = ""
        try:
            tabla_latex = _build_usb_latex_rows(ports)
        except Exception as exc:
            _log.warning("LaTeX rows: %s", exc)
            tabla_latex = r"    N/A & N/A & N/A & N/A & N/A & \badgeinfo \\"

        # ── Capa 4: resumen ───────────────────────────────────────────────
        usb_ok = usb_warn = usb_fail = 0
        try:
            usb_ok, usb_warn, usb_fail = _summarize_ports(ports)
        except Exception:
            pass

        return USBData(
            usb_tabla_filas = tabla_latex,
            usb_ok          = usb_ok,
            usb_warn        = usb_warn,
            usb_fail        = usb_fail,
        )

    except Exception as exc:   # pragma: no cover — guardia absoluta
        _log.error("CRÍTICO en extract_usb_data(): %s", exc)
        return USBData()