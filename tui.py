"""
tui.py — INVARIANT Terminal Interface
probe.tex // Live OS Diagnostic — Ring-0 Edition

PILLAR 1: Delta-Update Architecture
───────────────────────────────────
Static components (TargetTopology) are built once and cached.
Only dynamic components (Header clock, LogStream, StatusBar) are
re-instantiated per frame. Live(screen=False) stays on the main
buffer for standard Linux TTY compatibility after input() calls.
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

# ── INVARIANT v2 Palette ──────────────────────────────────────────────────────
# Retro-futuristic Corporate Brutalism — 24-bit True Color

class C:
    PRIMARY: Final[str] = "#E5E5E5"   # High-contrast text
    SLATE:   Final[str] = "#737373"   # Secondary / inactive
    REDTEX:  Final[str] = "#FF4444"   # Critical errors, active cursors
    BORDER:  Final[str] = "#262626"   # Grid lines, separators
    NOTICE:  Final[str] = "#A0A0A0"   # Informational text
    LIGHT:   Final[str] = "#141414"   # Panel backgrounds
    CANVAS:  Final[str] = "#0A0A0A"   # Deep void background

    primary = Style(color=PRIMARY)
    slate   = Style(color=SLATE)
    redtex  = Style(color=REDTEX, bold=True)
    border  = Style(color=BORDER)
    notice  = Style(color=NOTICE)
    light   = Style(color=LIGHT)
    canvas  = Style(color=CANVAS)


# ── Progress bar ───────────────────────────────────────────────────────────────

class _FlatBar:
    """Brutalist solid-block progress bar."""

    def __init__(self, percentage: float) -> None:
        self.percentage = percentage

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> Iterator[Text]:
        width  = max(1, options.max_width)
        filled = int(width * self.percentage / 100)
        bar    = Text(no_wrap=True, overflow="crop")
        # Critical steps (>90%) use redtex; normal progress uses primary.
        style  = C.redtex if self.percentage >= 90 else C.primary
        bar.append("█" * filled,           style=style)
        bar.append("░" * (width - filled), style=C.border)
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
        snap = self.state.snapshot()   # Thread-safe read under lock

        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")

        t = snap.mission_clock()
        # Mission clock in redtex (critical status tag)
        right_text = Text(f"T+ {t}  |  ", style=C.redtex)
        right_text.append("probe.tex // Live OS Diagnostic", style=C.slate)

        grid.add_row(
            Text("I N V A R I A N T", style=C.primary),
            right_text
        )
        return Panel(
            grid,
            box=box.SIMPLE,
            border_style=C.border,
            style=C.canvas,
        )


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
                lines.append(Text("─" * 32, style=C.border))
            row = Text()
            row.append(f"{label:<8}", style=C.slate)
            row.append("  ")
            row.append(value, style=C.primary)
            lines.append(row)

        return Panel(
            Group(*lines),
            title="[bold #E5E5E5] TARGET TOPOLOGY [/bold #E5E5E5]",
            title_align="left",
            border_style=C.border,
            box=box.SQUARE,
            style=C.canvas,
        )


class LogStream:
    def __init__(self, state: TuiState) -> None:
        self.state = state

    def __rich__(self) -> Panel:
        snap = self.state.snapshot()   # Thread-safe read under lock
        entries = list(snap.logs)
        lines: list[Text] = []

        for i, entry in enumerate(entries):
            active = (i == len(entries) - 1) and snap.phase == Phase.RUNNING
            lines.append(
                Text(f"▓ {entry}", style=C.primary)
                if active
                else Text(f"░ {entry}", style=C.slate)
            )

        # REMOVED: while len(lines) < maxlen padding loop.
        # Empty padding forced Rich to position the cursor on 25 lines
        # every frame even when only 3 logs existed, causing TTY churn.

        return Panel(
            Group(*lines),
            title="[bold #E5E5E5] RING-0 TELEMETRY [/bold #E5E5E5]",
            title_align="left",
            border_style=C.border,
            box=box.SQUARE,
            style=C.canvas,
        )


class StatusBar:
    def __init__(self, state: TuiState) -> None:
        self.state = state
        self._progress = Progress(
            TextColumn("[{task.percentage:>3.0f}%]", style=C.slate),
            FlatBarColumn(),
            TimeElapsedColumn(),
            expand=True,
        )
        self._task_id = self._progress.add_task("probe", total=100)

    def __rich__(self) -> Panel:
        snap = self.state.snapshot()   # Thread-safe read under lock

        if snap.phase == Phase.DONE:
            self._progress.update(self._task_id, completed=100)
            label = Text("PROBE HALTED // REPORT COMPILED", style=C.redtex, justify="center")
        elif snap.phase == Phase.ERROR:
            self._progress.update(self._task_id, completed=snap.progress_pct)
            label = Text("CRITICAL EXCEPTION", style=C.redtex, justify="center")
        elif snap.phase == Phase.RUNNING:
            self._progress.update(self._task_id, completed=snap.progress_pct)
            label = Text("ACQUIRING KERNEL TELEMETRY", style=C.primary, justify="center")
        else:
            self._progress.update(self._task_id, completed=0)
            label = Text("SYS_IDLE", style=C.slate)

        return Panel(
            Columns([label, self._progress], expand=True),
            border_style=C.border,
            box=box.SQUARE,
            style=C.canvas,
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
    FPS: Final = 8          # 8 Hz is the TTY sweet spot (smooth, no buffer saturation)
    ASSUMED_S  = 45.0

    # Build static topology once — never rebuilds during the session.
    topology_layout = Layout(TargetTopology(), name="topology", ratio=1)

    # Build dynamic components and layout tree ONCE.
    # These object references remain stable for the entire session.
    header = Header(state)
    logs   = LogStream(state)
    status = StatusBar(state)
    root = Layout(name="root")
    root.split_column(
        Layout(header, name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(status, name="status", size=3),
    )
    root["body"].split_row(
        topology_layout,
        Layout(logs, name="logs", ratio=2),
    )

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

        # auto_refresh=False: we control render timing manually via refresh().
        # screen=False preserves compatibility with raw VT after input() calls.
        with Live(
            root,
            console=console,
            screen=False,
            auto_refresh=False,
            transient=False,
        ) as live:
            start_t = time.monotonic()

            while not done_event.is_set():
                elapsed = time.monotonic() - start_t
                pct     = min(99.0, (elapsed / ASSUMED_S) * 100)

                state.update(pct=pct)
                live.refresh()          # DIFFERENTIAL: only changed cells rewrite
                time.sleep(1 / FPS)

            # Final render after worker completion to show DONE/ERROR state
            live.refresh()
            time.sleep(0.5)

    exc = future.exception()
    if exc is not None:
        raise RuntimeError(f"probe.tex fault: {exc}") from exc


if __name__ == "__main__":
    def _demo() -> None:
        time.sleep(45)

    run_tui(_demo)
