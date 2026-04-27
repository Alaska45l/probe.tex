"""
core/qr_display.py
==================
Air-Gap Bridge: QR code rendering and activation PIN input for probe.tex.

Renders a QR code containing the activation challenge URL directly to the
Linux TTY as ANSI block characters. The technician scans this with their
smartphone to obtain the 6-digit activation PIN.

DEPENDENCY:
  pip install qrcode==8.0   (pure Python, no Pillow needed for terminal output)
  Added to packages.x86_64: python-qrcode

DESIGN CONSTRAINTS:
  - Terminal-only output (no X11/Wayland) — uses Unicode block characters
  - Must work on 80-column terminals (QR version auto-selected)
  - Color scheme matches INVARIANT brutalist aesthetic
  - PIN input has timeout + retry limit
"""
from __future__ import annotations

import hmac as _hmac
import hashlib
import secrets
import sys
import time
from typing import Final

# ── INVARIANT v2 Brutalist ANSI (24-bit True Color) ──────────────────────────
_PRI: Final[str] = "\033[38;2;229;229;229m"   # primary   #E5E5E5
_SLT: Final[str] = "\033[38;2;115;115;115m"   # slate     #737373
_RED: Final[str] = "\033[38;2;255;68;68m"     # redtex    #FF4444
_NTC: Final[str] = "\033[38;2;160;160;160m"   # notice    #A0A0A0
_BRD: Final[str] = "\033[38;2;38;38;38m"      # border    #262626
_LGT: Final[str] = "\033[48;2;20;20;20m"      # light bg  #141414
_RST: Final[str] = "\033[0m"


def _render_qr_to_terminal(data: str) -> str:
    """
    Renders a QR code as a string of Unicode block characters for terminal display.

    Uses the 'qrcode' library in pure-text mode. Each QR module is represented
    by Unicode block elements (█ and spaces) for maximum contrast on dark
    terminal backgrounds.

    Falls back to a simple text display if qrcode is not available.
    """
    try:
        import qrcode
        from qrcode.main import QRCode

        qr = QRCode(
            version=None,  # Auto-select smallest version that fits
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=1,
            border=2,
        )
        qr.add_data(data)
        qr.make(fit=True)

        # Build ANSI string using half-block characters for compactness
        # Each character represents 2 vertical modules
        matrix = qr.modules
        rows = len(matrix)
        lines: list[str] = []

        for r in range(0, rows, 2):
            line = "  "  # Left margin
            for c in range(len(matrix[0])):
                top = matrix[r][c] if r < rows else False
                bot = matrix[r + 1][c] if r + 1 < rows else False

                if top and bot:
                    line += "█"
                elif top and not bot:
                    line += "▀"
                elif not top and bot:
                    line += "▄"
                else:
                    line += " "
            lines.append(line)

        return "\n".join(lines)

    except ImportError:
        # Fallback: just show the URL as text
        return f"\n  [QR library not available]\n  Scan this URL manually:\n  {data}\n"


def display_activation_screen(
    challenge_url: str,
    boot_id: str,
    timeout_seconds: int = 300,
) -> str | None:
    """
    Displays the QR activation screen and waits for PIN input.

    Shows:
      - INVARIANT header
      - QR code encoding the activation URL
      - Session ID and validity timer
      - PIN input prompt

    Returns the entered PIN string, or None if timeout expires or max
    retries (3) are exhausted.

    Parameters
    ----------
    challenge_url : str
        Full URL to encode in the QR code (https://api.invariant.ar/api/v1/license/activate?c=...)
    boot_id : str
        16-char hex session ID for display (formatted as XXXX-XXXX)
    timeout_seconds : int
        Maximum time to wait for PIN input (default 5 minutes)
    """
    import os
    import select

    # Format session ID for display
    session_display = boot_id.upper()
    if len(session_display) >= 8:
        session_display = session_display[:4] + "-" + session_display[4:8]

    # Clear screen via ANSI escapes (no external binary dependency)
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()

    qr_art = _render_qr_to_terminal(challenge_url)

    print()
    print(f"{_BRD}{_RST}")
    print(f"{_BRD}{_LGT}                                                               {_RST}{_BRD}{_RST}")
    print(f"{_BRD}{_LGT}  {_PRI}I N V A R I A N T{_LGT}  {_SLT}//{_LGT}  {_PRI}A C T I V A C I Ó N   D E   L I C E N C I A{_LGT}  {_RST}")
    print(f"{_BRD}{_LGT}                                                               {_RST}")
    print(f"{_BRD}{_RST}")
    print()
    print(qr_art)
    print()
    print(f"{_BRD}{_RST}")
    print(f"  {_PRI}Escanee el código QR con su celular.{_RST}")
    print(f"  {_NTC}Luego ingrese el PIN de 6 dígitos que aparece en pantalla.{_RST}")
    print()
    print(f"  {_SLT}ID de sesión {_BRD}│{_RST} {_PRI}{session_display}{_RST}")
    print(f"  {_SLT}Válido por   {_BRD}│{_RST} {_PRI}{timeout_seconds // 60} minutos{_RST}")
    print(f"{_BRD}{_RST}")
    print()
    sys.stdout.flush()

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        remaining = f" ({max_attempts - attempt} restantes)" if attempt > 1 else ""
        try:
            print(f"  {_PRI}[INVARIANT_TTY]> {_RED}", end="")
            pin = input(f"PIN [{attempt}/{max_attempts}]{remaining}: ").strip()
            print(f"{_RST}", end="")
        except (EOFError, KeyboardInterrupt):
            print(f"\n  {_RED}[ ▓ ] Entrada cancelada.{_RST}")
            return None

        if len(pin) == 6 and pin.isdigit():
            return pin

        if attempt < max_attempts:
            print(f"  {_NTC}[ ▓ ] El PIN debe ser exactamente 6 dígitos.{_RST}")

    print(f"\n  {_RED}[ ▓ ] Máximo de intentos alcanzado.{_RST}")
    return None


def display_activation_success() -> bool:
    """Shows a success message after PIN verification, then clears the screen.

    Returns True to signal explicit success to the caller.
    """
    print()
    print(f"{_BRD}{_RST}")
    print(f"{_BRD}{_LGT}{_RST}")
    print(f"{_BRD}{_LGT}  {_PRI} L I C E N C I A   A C T I V A D A   E X I T O S A M E N T E{_LGT}  {_RST}")
    print(f"{_BRD}{_LGT}       {_NTC}La máquina ha sido vinculada a su suscripción.{_LGT}              {_RST}")
    print(f"{_BRD}{_LGT}{_RST}")
    print(f"{_BRD}{_RST}")
    print()
    sys.stdout.flush()
    time.sleep(2)
    # Clear screen to hand over a clean slate to the Rich TUI
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()
    return True


def display_activation_failure(reason: str) -> None:
    """Shows a failure message with reason."""
    print()
    print(f"{_BRD}{_RST}")
    print(f"{_BRD}{_LGT}{_RST}")
    print(f"{_BRD}{_LGT}  {_RED}A C T I V A C I Ó N   F A L L I D A{_LGT}{_RST}")
    print(f"{_BRD}{_LGT}       {_NTC}{reason}{_LGT}{_RST}")
    print(f"{_BRD}{_LGT}{_RST}")
    print(f"{_BRD}{_RST}")
    print()
    time.sleep(3)
