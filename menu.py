#!/usr/bin/env python3
"""
INVARIANT probe.tex — Ring-0 Boot Menu
======================================
Fullscreen curses TUI with graceful fallback.

Critical: curses MUST be completely torn down before handing the terminal
to an external subprocess (launcher.sh), otherwise the child inherits a
corrupted TTY state (alternate buffer, cbreak mode, no echo) and hangs.
"""

import curses
import os
import subprocess
import sys

# ── ASCII Art Banner ────────────────────────────────────────────────────────
BANNER_LINES = [
    "██████╗ ██████╗  ██████╗ ██████╗ ███████╗    ████████╗███████╗██╗  ██╗",
    "██╔══██╗██╔══██╗██╔═══██╗██╔══██╗██╔════╝    ╚══██╔══╝██╔════╝╚██╗██╔╝",
    "██████╔╝██████╔╝██║   ██║██████╔╝█████╗         ██║   █████╗   ╚███╔╝ ",
    "██╔═══╝ ██╔══██╗██║   ██║██╔══██╗██╔══╝         ██║   ██╔══╝   ██╔██╗ ",
    "██║     ██║  ██║╚██████╔╝██████╔╝███████╗       ██║   ███████╗██╔╝ ██╗",
    "╚═╝     ╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝       ╚═╝   ╚══════╝╚═╝  ╚═╝",
    "",
    "      I  N  V  A  R  I  A  N  T   S Y S T E M S",
]

MENU_OPTIONS = [
    "Iniciar Analisis",
    "Apagar sistema",
]

LAUNCHER_SCRIPT = "/root/launcher.sh"


def _center_x(stdscr, text: str) -> int:
    """Return the X coordinate to center `text` horizontally."""
    _, cols = stdscr.getmaxyx()
    return max(0, (cols - len(text)) // 2)


def _draw_banner(stdscr, start_y: int, color_red: int, color_white: int) -> int:
    """Draw the ASCII banner starting at `start_y`. Returns next Y position."""
    for i, line in enumerate(BANNER_LINES):
        if not line:
            start_y += 1
            continue
        x = _center_x(stdscr, line)
        # First 6 lines are the PROBE.TEX art in red, subtitle in white
        pair = color_red if i < 6 else color_white
        try:
            stdscr.addstr(start_y, x, line, curses.color_pair(pair))
        except curses.error:
            pass  # Off-screen — terminal too small
        start_y += 1
    return start_y + 1


def _draw_options(stdscr, start_y: int, selected: int, color_red: int, color_white: int) -> int:
    """Draw the menu options. Returns next Y position."""
    for idx, option in enumerate(MENU_OPTIONS):
        marker = "> " if idx == selected else "  "
        line = f"{marker}{option}"
        x = _center_x(stdscr, line)
        pair = color_red if idx == selected else color_white
        try:
            stdscr.addstr(start_y, x, line, curses.color_pair(pair) | (curses.A_BOLD if idx == selected else 0))
        except curses.error:
            pass
        start_y += 2  # spacing between options
    return start_y


def _draw_footer(stdscr, color_white: int):
    """Draw a minimal footer at the bottom."""
    rows, _ = stdscr.getmaxyx()
    footer = "[ ENTER ] seleccionar   [ ESC ] salir"
    x = _center_x(stdscr, footer)
    try:
        stdscr.addstr(rows - 2, x, footer, curses.color_pair(color_white) | curses.A_DIM)
    except curses.error:
        pass


def _require_min_size(stdscr) -> bool:
    """Return True if terminal is large enough to render the menu."""
    rows, cols = stdscr.getmaxyx()
    max_banner_width = max(len(line) for line in BANNER_LINES)
    required_rows = len(BANNER_LINES) + len(MENU_OPTIONS) * 2 + 4
    return rows >= required_rows and cols >= max_banner_width + 4


def _curses_main(stdscr):
    """
    Main curses event loop.

    Critical invariant: before handing the terminal to an external subprocess,
    curses MUST be fully dismantled via _teardown_curses().
    """
    # ── Setup ─────────────────────────────────────────────────────────────
    curses.curs_set(0)          # Hide cursor
    stdscr.nodelay(False)       # Blocking getch
    stdscr.keypad(True)         # Enable arrow keys, function keys
    curses.start_color()
    curses.use_default_colors()

    # Color pairs: 1 = red on black, 2 = white on black
    curses.init_pair(1, curses.COLOR_RED, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLACK)

    COLOR_RED   = 1
    COLOR_WHITE = 2

    selected = 0

    while True:
        stdscr.clear()

        if not _require_min_size(stdscr):
            msg = "Terminal demasiado pequeno. Redimensione la ventana."
            try:
                stdscr.addstr(0, 0, msg, curses.color_pair(COLOR_RED))
            except curses.error:
                pass
            stdscr.refresh()
            curses.napms(500)
            continue

        # Calculate vertical centering
        rows, _ = stdscr.getmaxyx()
        total_height = len(BANNER_LINES) + len(MENU_OPTIONS) * 2 + 4
        start_y = max(2, (rows - total_height) // 2)

        y = _draw_banner(stdscr, start_y, COLOR_RED, COLOR_WHITE)
        y = _draw_options(stdscr, y, selected, COLOR_RED, COLOR_WHITE)
        _draw_footer(stdscr, COLOR_WHITE)

        stdscr.refresh()

        # ── Input handling ──────────────────────────────────────────────
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord('k')):
            selected = (selected - 1) % len(MENU_OPTIONS)
        elif key in (curses.KEY_DOWN, ord('j')):
            selected = (selected + 1) % len(MENU_OPTIONS)
        elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
            if selected == 0:
                # ════════════════════════════════════════════════════════
                #  CRITICAL: Full curses teardown BEFORE subprocess
                # ════════════════════════════════════════════════════════
                _teardown_curses(stdscr)
                try:
                    subprocess.run([LAUNCHER_SCRIPT, "--run-diagnostic"], check=False)
                finally:
                    # Reinitialize curses from scratch after child returns
                    stdscr = _reinit_curses()
                    curses.noecho()          # RESTORED: suppress character echo
                    curses.cbreak()          # RESTORED: enable single-char input
                    curses.curs_set(0)
                    stdscr.keypad(True)
                    curses.start_color()
                    curses.use_default_colors()
                    curses.init_pair(1, curses.COLOR_RED, curses.COLOR_BLACK)
                    curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLACK)
                    curses.flushinp()        # CRITICAL: discard ghost keypresses
            elif selected == 1:
                _teardown_curses(stdscr)
                subprocess.run(["poweroff", "-f"], check=False)
                sys.exit(0)
        elif key == 27:  # ESC
            break


def _teardown_curses(stdscr):
    """
    Fully dismantle the curses environment and restore the original TTY state.

    Order matters:
      1. keypad(False)  — stop intercepting escape sequences
      2. nocbreak()     — restore line-buffered (cooked) mode
      3. echo()         — restore character echo
      4. endwin()       — restore original terminal, exit alternate buffer
    """
    stdscr.keypad(False)
    curses.nocbreak()
    curses.echo()
    curses.endwin()


def _reinit_curses():
    """Reinitialize curses after a temporary handoff. Returns the new stdscr."""
    return curses.initscr()


def _fallback_menu():
    """
    Primitive fallback if curses fails to initialize.
    Used when the terminal does not support curses (e.g., serial console).
    """
    print("\n" + "=" * 60)
    print("INVARIANT probe.tex // Ring-0 Diagnostic")
    print("=" * 60)
    print("\n1) Iniciar Analisis")
    print("2) Apagar sistema")
    print("\n" + "-" * 60)

    try:
        choice = input("[INVARIANT_TTY]> Seleccione opcion: ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit(0)

    if choice == "1":
        subprocess.run([LAUNCHER_SCRIPT, "--run-diagnostic"], check=False)
    elif choice == "2":
        subprocess.run(["poweroff", "-f"], check=False)
    else:
        print("Opcion invalida.")


def main():
    """Entry point with curses wrapper and fallback."""
    try:
        curses.wrapper(_curses_main)
    except Exception as e:
        # If curses fails entirely (e.g., missing terminal, $TERM unset),
        # fall back to the primitive input() loop so the user is never stuck.
        sys.stderr.write(f"[menu.py] curses init failed ({e}), falling back to primitive menu.\n")
        _fallback_menu()


if __name__ == "__main__":
    main()
