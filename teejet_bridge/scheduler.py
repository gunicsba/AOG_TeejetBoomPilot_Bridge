"""Single scheduler that owns *all* timing.

Tasks run in one thread, in registration order when several are due in the
same pass. Other threads (CAN RX, UDP RX, keyboard) never transmit directly;
they change state and call :meth:`Scheduler.kick` so the task runs as soon as
possible. That gives one place where rates and ordering are enforced, e.g.
"send 00 right after every 02 change": register ``mux02`` before ``mux00`` and
kick both.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class Task:
    name: str
    fn: Callable[[float], None]
    period: Optional[float]           # None = event-only (runs on kick)
    next_due: Optional[float]
    enabled: bool = True
    runs: int = 0
    kicked: bool = False


class Scheduler:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._tasks: List[Task] = []
        self._by_name: Dict[str, Task] = {}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- registration ----
    def add(self, name: str, fn: Callable[[float], None], period: Optional[float],
            delay: float = 0.0, enabled: bool = True) -> Task:
        with self._lock:
            if name in self._by_name:
                raise ValueError(f"duplicate task {name}")
            due = None if period is None else self.clock() + delay
            t = Task(name, fn, period, due, enabled)
            self._tasks.append(t)
            self._by_name[name] = t
            self._wake.set()
            return t

    def task(self, name: str) -> Task:
        return self._by_name[name]

    def set_enabled(self, name: str, enabled: bool, run_now: bool = False):
        with self._lock:
            t = self._by_name[name]
            t.enabled = enabled
            if enabled and (run_now or t.next_due is None) and t.period is not None:
                t.next_due = self.clock() if run_now else self.clock() + t.period
        self._wake.set()

    def set_period(self, name: str, period: float):
        with self._lock:
            t = self._by_name[name]
            t.period = period
            t.next_due = self.clock() + period
        self._wake.set()

    def kick(self, *names: str):
        """Run the named tasks as soon as possible (thread-safe)."""
        with self._lock:
            now = self.clock()
            for n in names:
                t = self._by_name[n]
                t.next_due = now
                t.kicked = True
        self._wake.set()

    def run_at(self, name: str, when: float):
        """Schedule the next run of *name* at absolute time *when*."""
        with self._lock:
            self._by_name[name].next_due = when
        self._wake.set()

    # ---- execution ----
    def run_pending(self, now: Optional[float] = None) -> Optional[float]:
        """Run every due task once. Returns the next due time (or None)."""
        now = self.clock() if now is None else now
        with self._lock:
            due = [t for t in self._tasks
                   if t.enabled and t.next_due is not None and t.next_due <= now]
            for t in due:
                if t.period is None:
                    t.next_due = None
                elif t.kicked:
                    # An on-change send restarts the cadence, like the real devices.
                    t.next_due = now + t.period
                else:
                    # Drift-free: keep the grid unless we fell a whole period behind.
                    nd = t.next_due + t.period
                    t.next_due = nd if nd > now else now + t.period
                t.kicked = False
        for t in due:
            try:
                t.fn(now)
                t.runs += 1
            except Exception:
                log.exception("task %s failed", t.name)
        with self._lock:
            pending = [t.next_due for t in self._tasks if t.enabled and t.next_due is not None]
        return min(pending) if pending else None

    def _loop(self):
        while not self._stop.is_set():
            nxt = self.run_pending()
            timeout = 0.05 if nxt is None else max(0.0, min(0.05, nxt - self.clock()))
            if timeout > 0:
                self._wake.wait(timeout)
            self._wake.clear()

    def start(self):
        if sys.platform == "win32":
            try:  # 1 ms timer resolution so 100 ms rates don't jitter by 15 ms
                import ctypes
                ctypes.windll.winmm.timeBeginPeriod(1)
            except Exception:
                pass
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None
