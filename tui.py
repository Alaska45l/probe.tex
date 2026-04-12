"""
tui.py — Hardware Audit TUI
Aesthetic: Teenage Engineering × Nothing Tech × Dieter Rams.
"Less, but better."

Python: 3.11+
Dependencias: rich>=13.0
"""
from __future__ import annotations

import itertools
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from threading import Event, Lock
from typing import Callable, Deque, Final

from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.style import Style
from rich.text import Text

# ─────────────────────────────────────────────────────────────────────────────
# Design tokens — single source of truth
# ─────────────────────────────────────────────────────────────────────────────

class C:
    """Color palette. Touch nothing else."""
    WHITE:   Final[str] = "#FFFFFF"
    GREY:    Final[str] = "#666666"
    DIMGREY: Final[str] = "#333333"
    ORANGE:  Final[str] = "#FF5000"
    BLACK:   Final[str] = "#000000"

    # Pre-built Style objects
    white   = Style(color=WHITE)
    grey    = Style(color=GREY)
    dimgrey = Style(color=DIMGREY)
    orange  = Style(color=ORANGE, bold=True)
    muted   = Style(color=GREY, italic=True)
    label   = Style(color=ORANGE, bold=True)


BOX_STYLE: Final = box.SQUARE   # No rounded edges. Ever.

ASCII_LOGO: Final[str] = """\
 ██████╗ ██╗   ██╗██████╗ ██╗████████╗
 ██╔══██╗██║   ██║██╔══██╗██║╚══██╔══╝
 ███████║██║   ██║██║  ██║██║   ██║
 ██╔══██║██║   ██║██║  ██║██║   ██║
 ██║  ██║╚██████╔╝██████╔╝██║   ██║
 ╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═╝   ╚═╝"""

VERSION: Final[str] = "v1.0.0"
FPS:     Final[int] = 10
MAX_LOG_LINES: Final[int] = 16

# Simulated log steps emitted while render_pdf() runs in background
_AUDIT_STEPS: Final[tuple[str, ...]] = (
    "Mounting sysfs namespace…",
    "Reading /proc/cpuinfo…",
    "Parsing CPU topology (SMT / CCD)…",
    "Querying lscpu --json…",
    "Probing SMART data via nvme-cli…",
    "Scanning /sys/class/thermal/…",
    "Reading DMI table (dmidecode)…",
    "Fetching memory topology (decode-dimms)…",
    "Enumerating PCI devices (lspci -vmm)…",
    "Capturing GPU state (nvidia-smi / radeontop)…",
    "Parsing kernel ring buffer (dmesg -l warn,err)…",
    "Sampling power draw (RAPL MSR)…",
    "Resolving network interfaces (ip -j link)…",
    "Auditing storage I/O schedulers…",
    "Snapshotting /proc/meminfo…",
    "Collecting IRQ affinity map…",
    "Initialising LaTeX engine (pdflatex)…",
    "Compiling report template…",
    "Embedding vector assets…",
    "Running final PDF pass…",
    "Verifying output checksum…",
)


# ─────────────────────────────────────────────────────────────────────────────
# State machine
# ─────────────────────────────────────────────────────────────────────────────

class RunState(Enum):
    IDLE    = auto()
    RUNNING = auto()
    DONE    = auto()
    ERROR   = auto()


@dataclass
class UIState:
    """
    All mutable UI state. Protected by `lock`.
    The render loop reads; the worker thread writes.
    """
    run_state:   RunState           = RunState.IDLE
    logs:        Deque[str]         = field(
        default_factory=lambda: deque(maxlen=MAX_LOG_LINES)
    )
    progress_pct: float             = 0.0    # 0.0 – 1.0
    elapsed_s:   float              = 0.0
    error_msg:   str                = ""
    lock:        Lock               = field(default_factory=Lock)

    def push_log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        with self.lock:
            self.logs.append(f"[{ts}]  {msg}")

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "run_state":    self.run_state,
                "logs":         list(self.logs),
                "progress_pct": self.progress_pct,
                "elapsed_s":    self.elapsed_s,
                "error_msg":    self.error_msg,
            }


# ─────────────────────────────────────────────────────────────────────────────
# Layout builders — pure functions, called every frame
# ─────────────────────────────────────────────────────────────────────────────

def _build_header() -> Panel:
    logo = Text(ASCII_LOGO, style=C.white, justify="left")

    meta = Text(justify="right")
    meta.append("HARDWARE AUDIT SYSTEM\n", style=C.orange)
    meta.append(f"{VERSION}  //  JOBBOT SUITE", style=C.grey)

    grid = Columns([logo, meta], expand=True, equal=False)

    return Panel(
        grid,
        box=BOX_STYLE,
        border_style=C.DIMGREY,
        padding=(0, 1),
    )


def _build_progress(snap: dict) -> Panel:
    pct     = snap["progress_pct"]
    elapsed = snap["elapsed_s"]

    # Bar rendered manually for full color control.
    # rich Progress colours are overridden via complete_style.
    progress = Progress(
        SpinnerColumn(
            spinner_name="dots",
            style=Style(color=C.ORANGE),
            finished_text=Text("■", style=C.orange),
        ),
        TextColumn(
            "[progress.description]{task.description}",
            style=C.grey,
        ),
        BarColumn(
            bar_width=None,
            style=Style(color=C.DIMGREY),
            complete_style=Style(color=C.ORANGE),
            finished_style=Style(color=C.ORANGE),
        ),
        TextColumn(
            "{task.percentage:>5.1f}%",
            style=C.white,
        ),
        TimeElapsedColumn(),
        expand=True,
        transient=False,
    )

    task: TaskID = progress.add_task(
        "[ RENDER_PDF ]",
        total=100,
        completed=pct * 100,
    )
    # Freeze elapsed display using our own timer (worker may not be done yet)
    # We patch the task's start time so TimeElapsedColumn shows our value.
    progress.tasks[0].start_time = time.monotonic() - elapsed

    return Panel(
        progress,
        title=Text("  [ PROGRESS ]  ", style=C.label),
        title_align="left",
        box=BOX_STYLE,
        border_style=C.DIMGREY,
        padding=(0, 1),
    )


def _build_logs(snap: dict) -> Panel:
    lines: list[str] = snap["logs"]

    if not lines:
        body = Text("  — awaiting signal —", style=C.muted, justify="left")
    else:
        body = Text(justify="left")
        for i, line in enumerate(lines):
            is_last = (i == len(lines) - 1)
            prefix  = Text("▶ ", style=C.orange if is_last else C.dimgrey)
            content = Text(line, style=C.white if is_last else C.grey)
            body.append_text(prefix)
            body.append_text(content)
            body.append("\n")

    return Panel(
        body,
        title=Text("  [ SYSTEM LOG ]  ", style=C.label),
        title_align="left",
        box=BOX_STYLE,
        border_style=C.DIMGREY,
        padding=(0, 1),
    )


_STATE_LABELS: Final[dict[RunState, tuple[str, Style]]] = {
    RunState.IDLE:    ("■  IDLE",    Style(color=C.GREY,   bold=True)),
    RunState.RUNNING: ("▶  RUNNING", Style(color=C.ORANGE, bold=True)),
    RunState.DONE:    ("●  DONE",    Style(color=C.WHITE,  bold=True)),
    RunState.ERROR:   ("✕  ERROR",   Style(color=C.ORANGE, bold=True, reverse=True)),
}


def _build_footer(snap: dict) -> Panel:
    run_state: RunState = snap["run_state"]
    label, style        = _STATE_LABELS[run_state]

    left  = Text(f"  [ {label} ]  ", style=style)
    right = Text(
        f"  elapsed {snap['elapsed_s']:>7.2f}s  //  AUDIT ENGINE ONLINE  ",
        style=C.grey,
        justify="right",
    )

    if run_state == RunState.ERROR:
        err = Text(f"\n  ERR: {snap['error_msg']}", style=Style(color=C.ORANGE))
        left.append_text(err)

    grid = Columns([left, right], expand=True)

    return Panel(
        grid,
        box=BOX_STYLE,
        border_style=C.DIMGREY,
        padding=(0, 0),
    )


def _compose_layout(snap: dict) -> Layout:
    root = Layout()
    root.split_column(
        Layout(name="header",   size=10),
        Layout(name="progress", size=5),
        Layout(name="logs"),
        Layout(name="footer",   size=3),
    )
    root["header"].update(_build_header())
    root["progress"].update(_build_progress(snap))
    root["logs"].update(_build_logs(snap))
    root["footer"].update(_build_footer(snap))
    return root


# ─────────────────────────────────────────────────────────────────────────────
# Worker: runs render_pdf() in a ThreadPoolExecutor
# ─────────────────────────────────────────────────────────────────────────────

def _worker(
    render_fn:  Callable[[], None],
    state:      UIState,
    done_event: Event,
) -> None:
    """
    Background thread:
      1. Feeds simulated log steps at irregular intervals.
      2. Calls render_fn() (blocking — may shell out to subprocesses).
      3. Updates UIState on completion or failure.

    The log simulation and the real work run truly concurrently:
    log steps fire from a second inner thread so they appear even if
    render_fn() blocks the GIL for extended periods.
    """
    import threading

    n_steps    = len(_AUDIT_STEPS)
    step_cycle = itertools.cycle(_AUDIT_STEPS)
    stop_logs  = threading.Event()
    start_t    = time.monotonic()

    def _log_ticker() -> None:
        """Emits log lines independently of render_fn progress."""
        for step in step_cycle:
            if stop_logs.wait(timeout=_jitter()):
                break
            elapsed = time.monotonic() - start_t
            frac    = min(elapsed / 20.0, 0.95)   # asymptotic approach to 100%
            with state.lock:
                state.progress_pct = frac
                state.elapsed_s    = elapsed
            state.push_log(step)

    def _jitter() -> float:
        import random
        return random.uniform(0.45, 1.20)

    log_thread = threading.Thread(target=_log_ticker, daemon=True)
    log_thread.start()

    try:
        with state.lock:
            state.run_state = RunState.RUNNING

        render_fn()

        stop_logs.set()
        log_thread.join(timeout=2)

        with state.lock:
            state.run_state    = RunState.DONE
            state.progress_pct = 1.0
            state.elapsed_s    = time.monotonic() - start_t

        state.push_log("render_pdf() completed — output written.")

    except Exception as exc:  # noqa: BLE001
        stop_logs.set()
        log_thread.join(timeout=2)

        with state.lock:
            state.run_state = RunState.ERROR
            state.error_msg = str(exc)[:120]
            state.elapsed_s = time.monotonic() - start_t

        state.push_log(f"FATAL: {exc!s:.100}")

    finally:
        done_event.set()


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_tui(render_fn: Callable[[], None]) -> None:
    """
    Wrap `render_fn` in a full-screen TUI.

    Args:
        render_fn: Blocking callable (e.g. your `render_pdf()`).
                   It will run in a background thread; the TUI
                   renders at `FPS` frames per second until it finishes.

    Raises:
        RuntimeError: Re-raised after the TUI exits if render_fn failed.
    """
    console    = Console(highlight=False)
    state      = UIState()
    done_event = Event()

    # Kick off the worker
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="audit") as pool:
        future: Future[None] = pool.submit(_worker, render_fn, state, done_event)

        initial_snap = state.snapshot()
        with Live(
            _compose_layout(initial_snap),
            console=console,
            screen=True,          # full-screen — no scroll
            refresh_per_second=FPS,
            transient=False,
        ) as live:
            while not done_event.is_set():
                snap = state.snapshot()
                live.update(_compose_layout(snap), refresh=True)
                time.sleep(1 / FPS)

            # One final frame to show DONE / ERROR state
            live.update(_compose_layout(state.snapshot()), refresh=True)
            time.sleep(1.2)   # hold final state visible

    # Propagate worker exception to the caller
    exc = future.exception()
    if exc is not None:
        raise RuntimeError(f"render_pdf() failed: {exc}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Standalone demo — python tui.py
# ─────────────────────────────────────────────────────────────────────────────

def _demo_render_pdf() -> None:
    """Simulates a slow subprocess pipeline (15 s total)."""
    import subprocess, sys

    cmds = [
        ["sleep", "3"],
        ["sleep", "4"],
        ["sleep", "5"],
        ["sleep", "3"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"Command {cmd} exited {result.returncode}")


if __name__ == "__main__":
    run_tui(_demo_render_pdf)