"""python-can wrapper: open bus, TX/RX with trace logging, bus health."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import can

from . import frames
from .trc import TrcWriter

log = logging.getLogger(__name__)

# PCAN-Basic status bits (can.interfaces.pcan.basic), duplicated so the module
# imports on systems without the PCAN driver.
_PCAN_BUSLIGHT = 0x00004
_PCAN_BUSHEAVY = 0x00008
_PCAN_BUSOFF = 0x00010
_PCAN_BUSPASSIVE = 0x40000

STATE_OK = "OK"
STATE_WARNING = "WARNING"
STATE_PASSIVE = "ERROR-PASSIVE"
STATE_BUSOFF = "BUS-OFF"
STATE_UNKNOWN = "?"

DEFAULT_CHANNELS = {
    "pcan": "PCAN_USBBUS1",
    "socketcan": "can0",
    "gs_usb": "0",
    "virtual": "teejet",
}


def open_bus(interface: str = "pcan", channel: Optional[str] = None,
             bitrate: int = 250_000, **kwargs) -> can.BusABC:
    channel = channel or DEFAULT_CHANNELS.get(interface, "0")
    if interface == "pcan":
        kwargs.setdefault("auto_reset", True)     # recover from bus-off
    if interface == "gs_usb":
        kwargs.setdefault("index", int(channel))
        kwargs.setdefault("bus", 0)
    log.info("opening CAN interface=%s channel=%s bitrate=%d", interface, channel, bitrate)
    return can.Bus(interface=interface, channel=channel, bitrate=bitrate, **kwargs)


def bus_state(bus: can.BusABC) -> str:
    """Best-effort controller state for PCAN (status bits) or generic BusState."""
    status = getattr(bus, "status", None)
    if callable(status):
        try:
            st = int(status())
        except Exception:
            return STATE_UNKNOWN
        if st & _PCAN_BUSOFF:
            return STATE_BUSOFF
        if st & _PCAN_BUSPASSIVE:
            return STATE_PASSIVE
        if st & (_PCAN_BUSHEAVY | _PCAN_BUSLIGHT):
            return STATE_WARNING
        return STATE_OK
    try:
        st = bus.state
    except Exception:
        return STATE_UNKNOWN
    if st == can.BusState.ERROR:
        return STATE_BUSOFF
    if st == can.BusState.PASSIVE:
        # PASSIVE is also python-can's "listen only" state; treat as warning.
        return STATE_PASSIVE
    return STATE_OK


class CanIO:
    """All CAN TX goes through :meth:`send`; RX runs in its own thread and
    hands frames to *handler*. Every frame is written to the trace logger."""

    def __init__(self, bus: can.BusABC, trace: Optional[TrcWriter] = None,
                 clock: Callable[[], float] = time.monotonic, annotate_log: bool = False):
        self.bus = bus
        self.trace = trace
        self.clock = clock
        self.annotate_log = annotate_log
        self.handler: Optional[Callable[[can.Message, float], None]] = None
        self.last_tx = 0.0
        self.last_rx = 0.0
        self.tx_count = 0
        self.rx_count = 0
        self.tx_errors = 0
        self.error_frames = 0
        self.state = STATE_OK
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_err_log = 0.0

    # ---- TX ----
    def send(self, can_id: int, data: bytes) -> bool:
        msg = can.Message(arbitration_id=can_id, data=data, is_extended_id=True)
        try:
            self.bus.send(msg, timeout=0.05)
        except can.CanError as e:
            self.tx_errors += 1
            now = self.clock()
            if now - self._last_err_log > 1.0:
                self._last_err_log = now
                log.error("!!! CAN TX FAILED (%s) id=%08X data=%s  [%d failures] -- check wiring, "
                          "termination, bitrate and that the Radion is powered !!!",
                          e, can_id, data.hex(" ").upper(), self.tx_errors)
            return False
        self.last_tx = self.clock()
        self.tx_count += 1
        if self.trace:
            self.trace.write("Tx", can_id, data, self.last_tx)
        if self.annotate_log:
            log.debug("TX %08X %s  %s", can_id, data.hex(" ").upper(), frames.decode(can_id, data).text)
        return True

    # ---- RX ----
    def start_rx(self, handler: Callable[[can.Message, float], None]):
        self.handler = handler
        self._stop.clear()
        self._thread = threading.Thread(target=self._rx_loop, name="can-rx", daemon=True)
        self._thread.start()

    def _rx_loop(self):
        while not self._stop.is_set():
            try:
                msg = self.bus.recv(timeout=0.1)
            except can.CanError as e:
                now = self.clock()
                if now - self._last_err_log > 1.0:
                    self._last_err_log = now
                    log.error("!!! CAN RX ERROR: %s !!!", e)
                time.sleep(0.05)
                continue
            if msg is None:
                continue
            now = self.clock()
            if msg.is_error_frame:
                self.error_frames += 1
                if now - self._last_err_log > 1.0:
                    self._last_err_log = now
                    log.warning("CAN error frame received (%d so far)", self.error_frames)
                continue
            if not msg.is_extended_id:
                continue
            self.last_rx = now
            self.rx_count += 1
            if self.trace:
                self.trace.write("Rx", msg.arbitration_id, bytes(msg.data), now)
            if self.annotate_log:
                log.debug("RX %08X %s  %s", msg.arbitration_id, bytes(msg.data).hex(" ").upper(),
                          frames.decode(msg.arbitration_id, bytes(msg.data)).text)
            if self.handler:
                try:
                    self.handler(msg, now)
                except Exception:
                    log.exception("RX handler failed")

    # ---- health ----
    def check_health(self, _now: float = 0.0) -> str:
        st = bus_state(self.bus)
        if st != self.state:
            if st in (STATE_PASSIVE, STATE_BUSOFF):
                log.error("!!!!!!!! CAN BUS %s !!!!!!!! (was %s) -- frames may not reach the Radion. "
                          "Check CAN_H/CAN_L, 120 ohm termination at both ends, 250 kbit/s.", st, self.state)
            elif st == STATE_WARNING:
                log.warning("CAN bus error counters rising (%s)", st)
            elif st == STATE_OK and self.state != STATE_UNKNOWN:
                log.info("CAN bus back to %s (was %s)", st, self.state)
            self.state = st
        return st

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(1.0)
            self._thread = None
