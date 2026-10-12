"""Matrix-console emulation (TX) and Radion state tracking (RX).

Used by the bridge (GUI and headless). All transmissions are scheduler tasks:

=========  ======  ==========================================================
task       period  frame
=========  ======  ==========================================================
claim      once    18EEFFC0 address claim (only with --claim)
heartbeat  100 ms  0A FF FF FF FF FF FF (or speed/odometer with --hb-speed)
mux02      1 s     02 M            + immediately on change
mux00      200 ms  00 S1 S2 S3     + immediately after mux02 change / mask change
probe      1 s     01              until answered, max 3 tries
mss        100 ms  18F022C0 MSS    (only with --mss)
=========  ======  ==========================================================
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import can

from . import frames
from .canio import CanIO
from .scheduler import Scheduler

log = logging.getLogger(__name__)

HEARTBEAT_PERIOD = 0.100
MASTER_PERIOD = 1.0
SECTIONS_PERIOD = 0.200
PROBE_PERIOD = 1.0
PROBE_TRIES = 3
SHUTDOWN_REPEATS = 3

RADION_LOST_S = 3.0


class ConsoleTx:
    def __init__(self, io: CanIO, sched: Scheduler, *, claim: bool = False, probe: bool = True,
                 mss: bool = False, hb_speed: bool = False,
                 speed_source: Optional[Callable[[], Optional[float]]] = None):
        """*speed_source* returns ground speed in m/s, or None if invalid. It is
        only used for the heartbeat when *hb_speed* is set, and for MSS."""
        self.io = io
        self.sched = sched
        self.hb_speed = hb_speed
        self.speed_source = speed_source or (lambda: None)
        self._lock = threading.Lock()
        self.master = False
        self.mask = 0
        self.heartbeat_on = True
        self.distance_mm = 0.0
        self._last_odo_t: Optional[float] = None
        self.probe_answered = False
        self._probe_tries = 0

        # Registration order == transmit order within one scheduler pass.
        if claim:
            sched.add("claim", self._tx_claim, None)
            sched.kick("claim")
        sched.add("heartbeat", self._tx_heartbeat, HEARTBEAT_PERIOD)
        sched.add("mux02", self._tx_master, MASTER_PERIOD)
        sched.add("mux00", self._tx_sections, SECTIONS_PERIOD)
        sched.add("probe", self._tx_probe, PROBE_PERIOD, delay=1.0, enabled=probe)
        sched.add("mss", self._tx_mss, HEARTBEAT_PERIOD, enabled=mss)

    # ---- state ----
    def set_state(self, master: bool, mask: int):
        """Thread-safe. Kicks 02 (+00) on master change, 00 on mask change."""
        mask &= (1 << frames.MAX_SECTIONS) - 1
        with self._lock:
            m_changed = master != self.master
            s_changed = mask != self.mask
            self.master, self.mask = master, mask
        if m_changed:
            log.info("CMD master %s, sections 0x%06X [%s]", "ON" if master else "OFF", mask,
                     frames.mask_str(mask))
            self.sched.kick("mux02", "mux00")
        elif s_changed:
            log.info("CMD sections 0x%06X [%s]", mask, frames.mask_str(mask))
            self.sched.kick("mux00")

    def snapshot(self):
        with self._lock:
            return self.master, self.mask

    def set_heartbeat(self, on: bool):
        self.heartbeat_on = on
        self.sched.set_enabled("heartbeat", on, run_now=on)
        log.warning("0x0A heartbeat %s at t=%.3f", "RESUMED" if on else "STOPPED", self.sched.clock())

    def probe_reply(self):
        if not self.probe_answered:
            self.probe_answered = True
            self.sched.set_enabled("probe", False)

    # ---- tasks ----
    def _tx_claim(self, _now):
        self.io.send(frames.ID_CONSOLE_CLAIM, frames.CONSOLE_NAME)

    def _speed_mm_s(self, now: float) -> Optional[int]:
        v = self.speed_source()
        if self._last_odo_t is not None and v is not None:
            self.distance_mm += v * 1000.0 * (now - self._last_odo_t)
        self._last_odo_t = now
        return None if v is None else int(round(v * 1000.0))

    def _tx_heartbeat(self, now):
        if self.hb_speed:
            spd = self._speed_mm_s(now)
            data = frames.heartbeat(spd, int(self.distance_mm) if spd is not None else None)
        else:
            data = frames.heartbeat(None)
        self.io.send(frames.ID_CONSOLE_GLOBAL, data)

    def _tx_master(self, _now):
        with self._lock:
            m = self.master
        self.io.send(frames.ID_CONSOLE_GLOBAL, frames.master(m))

    def _tx_sections(self, _now):
        with self._lock:
            mask = self.mask
        self.io.send(frames.ID_CONSOLE_GLOBAL, frames.sections(mask))

    def _tx_probe(self, _now):
        if self.probe_answered or self._probe_tries >= PROBE_TRIES:
            self.sched.set_enabled("probe", False)
            if not self.probe_answered:
                log.info("Radion did not answer the 01 probe (optional, ignored)")
            return
        self._probe_tries += 1
        self.io.send(frames.ID_CONSOLE_GLOBAL, frames.probe())

    def _tx_mss(self, now):
        # _speed_mm_s integrates since its previous call, so sharing it with
        # the heartbeat task does not double-count distance.
        spd = self._speed_mm_s(now)
        self.io.send(frames.ID_CONSOLE_MSS, frames.mss(spd, int(self.distance_mm)))

    # ---- safety ----
    def send_safe_off(self, repeats: int = SHUTDOWN_REPEATS, gap_s: float = 0.02):
        """Master off + mask 0, *repeats* times. Call after the scheduler stopped."""
        with self._lock:
            self.master, self.mask = False, 0
        for _ in range(repeats):
            self.io.send(frames.ID_CONSOLE_GLOBAL, frames.master(False))
            self.io.send(frames.ID_CONSOLE_GLOBAL, frames.sections(0))
            time.sleep(gap_s)
        log.info("sent master OFF + sections 0 x%d", repeats)


class RadionMonitor:
    """Tracks frames from the Radion (18EFC0B4)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, lost_s: float = RADION_LOST_S,
                 on_probe_reply: Optional[Callable[[], None]] = None):
        self.clock = clock
        self.lost_s = lost_s
        self.on_probe_reply = on_probe_reply
        self.actual_mask: Optional[int] = None
        self.extra: Optional[tuple] = None          # (b2, b3, b4, b5)
        self.status: Optional[int] = None
        self.ident: Optional[bytes] = None
        self.last_rx = 0.0
        self.last_state_rx = 0.0
        self.mask_changed_at = 0.0
        self.link = False
        self.history = []                           # [(t, mask)] actual-mask changes

    def handle(self, msg: can.Message, now: float) -> bool:
        if msg.arbitration_id != frames.ID_RADION_TO_CONSOLE:
            return False
        d = frames.decode(msg.arbitration_id, bytes(msg.data))
        self.last_rx = now
        if not self.link:
            self.link = True
            log.info("RADION link UP")
        if d.kind == frames.KIND_RADION_STATE:
            self.last_state_rx = now
            m = d.fields["mask"]
            if m != self.actual_mask:
                log.info("RADION actual 0x%02X [%s] (was %s)", m, frames.mask_str(m),
                         "?" if self.actual_mask is None else f"0x{self.actual_mask:02X}")
                self.actual_mask = m
                self.mask_changed_at = now
                self.history.append((now, m))
            extra = (d.fields["b2"], d.fields["b3"], d.fields["b4"], d.fields["b5"])
            if extra != self.extra:
                log.info("RADION b2-b5 = %s", " ".join(f"{x:02X}" for x in extra))
                self.extra = extra
        elif d.kind == frames.KIND_RADION_STATUS:
            if d.fields["status"] != self.status:
                log.info("RADION status 02 %02X", d.fields["status"])
                self.status = d.fields["status"]
            else:
                log.debug("RADION status 02 %02X", d.fields["status"])
        elif d.kind == frames.KIND_RADION_PROBE_REPLY:
            self.ident = bytes(msg.data)
            log.info("RADION probe reply %s", self.ident.hex(" ").upper())
            if self.on_probe_reply:
                self.on_probe_reply()
        else:
            log.info("RADION unknown frame %s", bytes(msg.data).hex(" ").upper())
        return True

    def is_up(self, now: Optional[float] = None) -> bool:
        now = self.clock() if now is None else now
        return self.last_rx > 0 and (now - self.last_rx) <= self.lost_s

    def check(self, now: float):
        """Scheduler task: log link loss."""
        if self.link and not self.is_up(now):
            self.link = False
            self.actual_mask = None
            log.error("RADION link LOST (no 18EFC0B4 frame for %.1f s)", now - self.last_rx)
