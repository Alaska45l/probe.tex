"""
tui.py — INVARIANT Terminal Interface
probe.tex // Live OS Diagnostic — Ring-0 Edition

PILLAR 1: Delta-Update Architecture
───────────────────────────────────
Static components (TargetTopology) are built once and cached.
Only dynamic components (Header clock, LogStream, StatusBar) are
re-instantiated per frame. Live(screen=True) leverages kmscon's
DRM-backed alternate buffer for zero-flicker rendering.
"""
from __future__ import annotations

import os
import platform
import sys
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from threading import Event, Lock
from typing import Callable, Deque, Final, Iterator

from rich import box
from rich.columns import Columns
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.layout import Layout
from rich.live import Live
from rich.measure import Measurement
from rich.panel import Panel
from rich.progress import Progress, ProgressColumn, Task, TextColumn, TimeElapsedColumn
from rich.style import Style
from rich.table import Column
from rich.text import Text


_ACTIVE_STATE: TuiState | None = None

def runtime_log(msg: str) -> None:
    """Envía un mensaje real a la terminal desde cualquier hilo."""
    if _ACTIVE_STATE:
        _ACTIVE_STATE.update(log=msg)

# ── Palette ───────────────────────────────────────────────────────────────────

class C:
    WHITE:   Final[str] = "#FFFFFF"
    GREY:    Final[str] = "#888888"
    DIMGREY: Final[str] = "#333333"
    ORANGE:  Final[str] = "#FF5000"
    BLACK:   Final[str] = "#000000"

    white   = Style(color=WHITE)
    grey    = Style(color=GREY)
    dimgrey = Style(color=DIMGREY)
    accent  = Style(color=ORANGE, bold=True)


# ── Progress bar ───────────────────────────────────────────────────────────────

class _FlatBar:
    def __init__(self, percentage: float) -> None:
        self.percentage = percentage

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> Iterator[Text]:
        width  = max(1, options.max_width)
        filled = int(width * self.percentage / 100)
        bar    = Text(no_wrap=True, overflow="crop")
        style  = C.accent if self.percentage >= 100 else C.white
        bar.append("━" * filled,           style=style)
        bar.append("─" * (width - filled), style=C.dimgrey)
        yield bar

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(1, options.max_width)


class FlatBarColumn(ProgressColumn):
    def __init__(self) -> None:
        super().__init__(table_column=Column(ratio=1, no_wrap=True))

    def render(self, task: Task) -> _FlatBar:
        return _FlatBar(task.percentage)


# ── State machine ──────────────────────────────────────────────────────────────

class Phase(Enum):
    INIT    = auto()
    RUNNING = auto()
    DONE    = auto()
    ERROR   = auto()


@dataclass
class TuiState:
    phase:        Phase      = Phase.INIT
    logs:         Deque[str] = field(default_factory=lambda: deque(maxlen=25))
    progress_pct: float      = 0.0
    start_time:   float      = field(default_factory=time.monotonic)
    _lock:        Lock       = field(default_factory=Lock)

    @property
    def elapsed(self) -> str:
        delta   = time.monotonic() - self.start_time
        minutes = int(delta // 60)
        seconds = delta % 60
        return f"T+ {minutes:02d}:{seconds:04.1f}s"

    def start(self) -> None:
        with self._lock:
            self.start_time = time.monotonic()

    def mission_clock(self) -> str:
        delta   = time.monotonic() - self.start_time
        minutes = int(delta // 60)
        seconds = delta % 60
        return f"{minutes:02d}:{seconds:04.1f}s"

    def update(
        self,
        phase: Phase | None = None,
        log:   str   | None = None,
        pct:   float | None = None,
    ) -> None:
        with self._lock:
            if phase is not None:
                self.phase = phase
            if log is not None:
                self.logs.append(f"[{time.strftime('%H:%M:%S')}]  {log}")
            if pct is not None:
                self.progress_pct = max(0.0, min(100.0, pct))

    def snapshot(self) -> TuiState:
        with self._lock:
            return TuiState(
                phase=self.phase,
                logs=deque(self.logs, maxlen=self.logs.maxlen),
                progress_pct=self.progress_pct,
                start_time=self.start_time,
                _lock=Lock(),
            )


# ── UI components ──────────────────────────────────────────────────────────────

class Header:
    def __init__(self, state: TuiState) -> None:
        self.state = state

    def __rich__(self) -> Panel:
        from rich.table import Table
        
        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        
        t = self.state.mission_clock()
        right_text = Text(f"T+ {t}  |  ", style=C.white)
        right_text.append("probe.tex // Live OS Diagnostic", style=C.accent)
        
        grid.add_row(
            Text("I N V A R I A N T", style=C.white),
            right_text
        )
        return Panel(grid, box=box.SIMPLE, style=C.dimgrey)


class TargetTopology:
    """
    Métricas estáticas del host. Se construye una vez; los valores son
    inmutables durante la vida de probe.tex.
    """

    def __init__(self) -> None:
        uname = platform.uname()
        cpu   = platform.processor() or uname.machine or "N/A"

        def _trim(s: str, n: int = 36) -> str:
            return s[:n].rstrip() + ("…" if len(s) > n else "")

        self._rows: tuple[tuple[str, str], ...] = (
            ("HOST",   uname.node                             or "N/A"),
            ("OS",     f"{uname.system} {uname.release}"      or "N/A"),
            ("KERNEL", _trim(uname.version)),
            ("ARCH",   uname.machine                          or "N/A"),
            ("CPU",    _trim(cpu)),
            ("PY",     platform.python_version()),
            ("PID",    str(os.getpid())),
            ("UID",    str(os.getuid()) if hasattr(os, "getuid") else "N/A"),
        )

    def __rich__(self) -> Panel:
        lines: list[Text] = []
        for i, (label, value) in enumerate(self._rows):
            if i == 4:
                lines.append(Text("─" * 32, style=C.dimgrey))
            row = Text()
            row.append(f"{label:<8}", style=C.grey)
            row.append("  ")
            row.append(value, style=C.white)
            lines.append(row)

        return Panel(
            Group(*lines),
            title="[ TARGET TOPOLOGY ]",
            title_align="left",
            border_style=C.dimgrey,
            box=box.SQUARE,
        )


class LogStream:
    def __init__(self, state: TuiState) -> None:
        self.state = state

    def __rich__(self) -> Panel:
        entries = list(self.state.logs)
        maxlen  = self.state.logs.maxlen or 25
        lines: list[Text] = []

        for i, entry in enumerate(entries):
            active = (i == len(entries) - 1) and self.state.phase == Phase.RUNNING
            lines.append(
                Text(f"● {entry}", style=C.white)
                if active
                else Text(f"○ {entry}", style=C.dimgrey)
            )

        while len(lines) < maxlen:
            lines.append(Text(""))

        return Panel(
            Group(*lines),
            title="[ RING-0 TELEMETRY ]",
            title_align="left",
            border_style=C.dimgrey,
            box=box.SQUARE,
        )


class StatusBar:
    def __init__(self, state: TuiState) -> None:
        self.state = state

    def __rich__(self) -> Panel:
        progress = Progress(
            TextColumn("[{task.percentage:>3.0f}%]", style=C.grey),
            FlatBarColumn(),
            TimeElapsedColumn(),
            expand=True,
        )
        task_id = progress.add_task("probe", total=100)

        if self.state.phase == Phase.DONE:
            progress.update(task_id, completed=100)
            label = Text("PROBE HALTED // REPORT COMPILED", style=C.accent, justify="center")
        elif self.state.phase == Phase.ERROR:
            progress.update(task_id, completed=self.state.progress_pct)
            label = Text("CRITICAL EXCEPTION", style=C.accent, justify="center")
        elif self.state.phase == Phase.RUNNING:
            progress.update(task_id, completed=self.state.progress_pct)
            label = Text("ACQUIRING KERNEL TELEMETRY", style=C.white, justify="center")
        else:
            progress.update(task_id, completed=0)
            label = Text("SYS_IDLE", style=C.grey)

        return Panel(
            Columns([label, progress], expand=True),
            border_style=C.dimgrey,
            box=box.SQUARE,
        )


def _compose(state: TuiState, topology_layout: Layout) -> Layout:
    """Assemble layout reusing the cached static topology panel."""
    root = Layout(name="root")
    root.split_column(
        Layout(Header(state),     name="header", size=3),
        Layout(name="body",       ratio=1),
        Layout(StatusBar(state),  name="status", size=3),
    )
    root["body"].split_row(
        topology_layout,
        Layout(LogStream(state),  name="logs",     ratio=2),
    )
    return root


# ── Orchestration ──────────────────────────────────────────────────────────────

def run_tui(target_func: Callable[[], None]) -> None:
    global _ACTIVE_STATE

    console = Console(force_terminal=True)
    state   = TuiState()
    _ACTIVE_STATE = state
    state.start()

    done_event = Event()
    FPS: Final = 4          # Delta-update: 4 Hz is sufficient for TTY readability
    ASSUMED_S  = 45.0

    # Build static topology once — never rebuilds during the session.
    topology_layout = Layout(TargetTopology(), name="topology", ratio=1)

    def _worker() -> None:
        state.update(phase=Phase.RUNNING, log="Mounting sysfs namespace...")
        try:
            target_func()
            state.update(phase=Phase.DONE, log="Extraction complete. Contract sealed.", pct=100.0)
        except Exception as exc:
            state.update(phase=Phase.ERROR, log=f"FAULT: {exc}")
            raise
        finally:
            done_event.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future: Future[None] = pool.submit(_worker)

        # kmscon provides a DRM-backed terminal where screen=True works reliably.
        with Live(
            _compose(state.snapshot(), topology_layout),
            console=console,
            screen=True,
            refresh_per_second=FPS,
            transient=False,
        ) as live:
            start_t = time.monotonic()

            while not done_event.is_set():
                elapsed = time.monotonic() - start_t
                pct     = min(99.0, (elapsed / ASSUMED_S) * 100)

                state.update(pct=pct)
                live.update(_compose(state.snapshot(), topology_layout), refresh=True)
                time.sleep(1 / FPS)

            live.update(_compose(state.snapshot(), topology_layout), refresh=True)
            time.sleep(0.5)

    exc = future.exception()
    if exc is not None:
        raise RuntimeError(f"probe.tex fault: {exc}") from exc


if __name__ == "__main__":
    def _demo() -> None:
        time.sleep(45)

    run_tui(_demo)
