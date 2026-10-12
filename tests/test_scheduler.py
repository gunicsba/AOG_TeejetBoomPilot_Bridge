import time
import uuid

import can

from teejet_bridge import frames
from teejet_bridge.canio import CanIO
from teejet_bridge.console import ConsoleTx
from teejet_bridge.scheduler import Scheduler


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_periodic_drift_free_and_order():
    clk = FakeClock()
    s = Scheduler(clk)
    log = []
    s.add("a", lambda now: log.append(("a", now)), 0.1)
    s.add("b", lambda now: log.append(("b", now)), 0.2)
    for i in range(101):           # 1 s in 10 ms steps
        clk.t = i * 0.01
        s.run_pending()
    a = [t for n, t in log if n == "a"]
    b = [t for n, t in log if n == "b"]
    assert len(a) == 11 and len(b) == 6
    assert log[0][0] == "a" and log[1][0] == "b"      # registration order in one pass


def test_kick_runs_now_and_restarts_cadence():
    clk = FakeClock()
    s = Scheduler(clk)
    runs = []
    s.add("m", lambda now: runs.append(now), 1.0)
    s.run_pending()                 # t=0
    clk.t = 0.35
    s.kick("m")
    s.run_pending()
    clk.t = 1.0
    s.run_pending()                 # not due: restarted at 0.35
    clk.t = 1.35
    s.run_pending()
    assert runs == [0.0, 0.35, 1.35]


def test_event_only_task():
    clk = FakeClock()
    s = Scheduler(clk)
    runs = []
    s.add("e", lambda now: runs.append(now), None)
    s.run_pending()
    assert runs == []
    s.kick("e")
    s.run_pending()
    s.run_pending()
    assert runs == [0.0]


def _collect(bus, seconds):
    out = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        m = bus.recv(0.02)
        if m is not None:
            out.append((m.timestamp, m.arbitration_id, bytes(m.data)))
    return out


def test_console_rates_on_virtual_bus():
    ch = f"t-{uuid.uuid4().hex}"
    tx_bus = can.Bus(interface="virtual", channel=ch)
    rx_bus = can.Bus(interface="virtual", channel=ch)
    try:
        sched = Scheduler()
        con = ConsoleTx(CanIO(tx_bus), sched, probe=False)
        sched.start()
        rx = _collect(rx_bus, 2.05)
        # on-change: 02 must be followed immediately by 00
        con.set_state(True, 0x7F)
        rx2 = _collect(rx_bus, 0.3)
        sched.stop()
        con.send_safe_off(gap_s=0.0)
        tail = _collect(rx_bus, 0.1)
    finally:
        tx_bus.shutdown()
        rx_bus.shutdown()

    def times(frames_, mux):
        return [t for t, i, d in frames_ if i == frames.ID_CONSOLE_GLOBAL and d[0] == mux]

    hb, m02, m00 = times(rx, 0x0A), times(rx, 0x02), times(rx, 0x00)
    assert 19 <= len(hb) <= 22, len(hb)
    assert 2 <= len(m02) <= 3, len(m02)
    assert 10 <= len(m00) <= 12, len(m00)
    mean = (hb[-1] - hb[0]) / (len(hb) - 1)
    assert 0.09 < mean < 0.11, mean
    assert all(d == frames.heartbeat() for t, i, d in rx if d[0] == 0x0A)

    first = [d for t, i, d in rx2 if d[0] in (0x00, 0x02)][:2]
    assert first == [frames.master(True), frames.sections(0x7F)]

    offs = [d for t, i, d in tail]
    assert offs.count(frames.master(False)) == 3 and offs.count(frames.sections(0)) == 3
