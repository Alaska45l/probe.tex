#!/usr/bin/env python3
"""
menu.py — INVARIANT Ring-0 TTY Menu
Deterministic, flicker-free curses TUI for framebuffer console.
Falls back to a primitive input() loop if curses initialization fails.
"""
import os
import sys


def _fallback_menu() -> None:
    """Primitive text menu when curses is unavailable."""
    while True:
        print("\n" + "=" * 50)
        print("  PROBE.TEX INVARIANT")
        print("  Hardware Forensic Diagnostic — Ring-0")
        print("=" * 50)
        print("  [1] Iniciar Analisis")
        print("  [2] Apagar sistema")
        print("=" * 50)
        try:
            choice = input("Seleccione opcion: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if choice == "1":
            os.system("bash /root/probe.tex/launcher.sh --run-diagnostic")
        elif choice == "2":
            os.system("sync; poweroff -f")
            sys.exit(0)
        elif choice.lower() == "q":
            break


def draw_menu(stdscr, selected: int) -> None:
    """Render the centered menu. Called once per keypress (minimal diff)."""
    stdscr.clear()

    h, w = stdscr.getmaxyx()
    options = ["Iniciar Analisis", "Apagar sistema"]
    header_lines = [
        "PROBE.TEX INVARIANT",
        "",
        "Hardware Forensic Diagnostic — Ring-0",
    ]

    box_width = max(len(line) for line in header_lines + options) + 8
    box_height = len(header_lines) + len(options) + 4
    start_y = (h - box_height) // 2
    start_x = (w - box_width) // 2

    try:
        stdscr.addstr(start_y, start_x, "+" + "-" * (box_width - 2) + "+")
        for row in range(1, box_height - 1):
            stdscr.addstr(start_y + row, start_x, "|")
            stdscr.addstr(start_y + row, start_x + box_width - 1, "|")
        stdscr.addstr(start_y + box_height - 1, start_x, "+" + "-" * (box_width - 2) + "+")
    except Exception:
        pass

    for i, line in enumerate(header_lines):
        y = start_y + 1 + i
        x = start_x + (box_width - len(line)) // 2
        try:
            stdscr.addstr(y, x, line, 0)
        except Exception:
            pass

    options_start_y = start_y + 1 + len(header_lines) + 1
    for i, option in enumerate(options):
        y = options_start_y + i
        label = f"> {option}" if i == selected else f"  {option}"
        attr = 0x10000 if i == selected else 0  # A_REVERSE
        x = start_x + (box_width - len(label)) // 2
        try:
            stdscr.addstr(y, x, label, attr)
        except Exception:
            pass

    stdscr.refresh()


def _curses_main(stdscr) -> None:
    import curses
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.use_default_colors()

    selected = 0
    draw_menu(stdscr, selected)

    while True:
        key = stdscr.getch()

        if key == curses.KEY_UP:
            selected = (selected - 1) % 2
        elif key == curses.KEY_DOWN:
            selected = (selected + 1) % 2
        elif key in (curses.KEY_ENTER, 10, 13):
            if selected == 0:
                os.system("bash /root/probe.tex/launcher.sh --run-diagnostic")
                draw_menu(stdscr, selected)
            elif selected == 1:
                os.system("sync; poweroff -f")
                sys.exit(0)
        elif key == ord("q"):
            break

        draw_menu(stdscr, selected)


def main() -> None:
    try:
        import curses
        curses.wrapper(_curses_main)
    except Exception as exc:
        print(f"\n[INVARIANT] curses unavailable ({exc}), falling back to text menu.")
        _fallback_menu()


if __name__ == "__main__":
    main()
