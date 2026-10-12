"""Bridge end-to-end on a virtual CAN bus and loopback UDP.

A minimal fake Radion (test-only) answers like the traces: silent until 0x0A,
reports SS = commanded & switches while commanded master is on, else 00.
"""

import socket
import threading
import time
import uuid

import can
import pytest

from teejet_bridge import aog, frames
from teejet_bridge.bridge import Bridge, BridgeConfig
from teejet_bridge.canio import CanIO
from teejet_bridge.scheduler import Scheduler


class FakeRadion(threading.Thread):
    def __init__(self, channel, switches=0x7F):
        super().__init__(daemon=True)
        self.bus = can.Bus(interface="virtual", channel=channel)
        self.switches = switches
        self.master = False
        self.mask = 0
        self.seen_hb = False
        self.rx = []
        self.stop_ev = threading.Event()

    def ss(self):
        return (self.mask & self.switches) if self.master else 0

    def send(self, data):
        self.bus.send(can.Message(arbitration_id=frames.ID_RADION_TO_CONSOLE, data=data, is_extended_id=True))

    def run(self):
        last_tx, last_ss = 0.0, None
        while not self.stop_ev.is_set():
            m = self.bus.recv(0.01)
            if m is not None and m.arbitration_id == frames.ID_CONSOLE_GLOBAL:
                d = frames.decode(m.arbitration_id, bytes(m.data))
                self.rx.append((time.monotonic(), d))
                if d.kind == frames.KIND_HEARTBEAT:
                    self.seen_hb = True
                elif d.kind == frames.KIND_MASTER:
                    self.master = d.fields["master"]
                elif d.kind == frames.KIND_SECTIONS:
                    self.mask = d.fields["mask"]
                elif d.kind == frames.KIND_PROBE:
                    self.send(frames.RADION_PROBE_REPLY)
            now = time.monotonic()
            if self.seen_hb and (self.ss() != last_ss or now - last_tx > 1.0):
                last_ss, last_tx = self.ss(), now
                self.send(frames.radion_state(last_ss))

    def close(self):
        self.stop_ev.set()
        self.join(1)
        self.bus.shutdown()


@pytest.fixture
def rig():
    ch = f"b-{uuid.uuid4().hex}"
    aog_rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # plays AgIO :9999
    aog_rx.bind(("127.0.0.1", 0))
    aog_rx.settimeout(0.05)
    made = []

    def make(**kw):
        kw.setdefault("subnet", "127.0.0")         # loopback test packets come from 127.0.0.1
        cfg = BridgeConfig(listen_ip="127.0.0.1", listen_port=0, broadcast="127.0.0.1",
                           send_port=aog_rx.getsockname()[1], **kw)
        bus = can.Bus(interface="virtual", channel=ch)
        br = Bridge(cfg, CanIO(bus), Scheduler())
        radion = FakeRadion(ch)
        radion.start()
        br.start()
        made.append((br, bus, radion))
        return br, radion

    yield make, aog_rx
    for br, bus, radion in made:
        br.stop()
        bus.shutdown()
        radion.close()
    aog_rx.close()


def send_aog(br, payload):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(payload, ("127.0.0.1", br.sock.getsockname()[1]))
    s.close()


def drive(br, mask, seconds, rate=0.1):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        send_aog(br, aog.build_machine_data(mask, 5.0))
        time.sleep(rate)


def drain(sock):
    out = []
    while True:
        try:
            out.append(sock.recvfrom(256)[0])
        except socket.timeout:
            return out


def test_aog_to_radion_and_feedback(rig):
    make, aog_rx = rig
    br, radion = make()
    drive(br, 0x05, 1.2)
    assert radion.master is True and radion.mask == 0x05
    assert br.radion.actual_mask == 0x05
    pkts = drain(aog_rx)
    ed = [p for p in pkts if p[3] == aog.PGN_FROM_MACHINE]
    assert ed and ed[-1][5] == 0x05
    assert not [p for p in pkts if p[3] == aog.PGN_SECTION_SWITCHES]   # feedback-only by default

    # hello reply only once the Radion is up, carries the actual mask
    send_aog(br, aog.build_hello())
    time.sleep(0.15)
    hello = [p for p in drain(aog_rx) if p[3] == 123]
    assert hello and len(hello[-1]) == 11 and hello[-1][5] == 0x05

    # Radion switch 1 off -> actual drops bit 0 -> fed back to AOG
    radion.switches = 0x7E
    drive(br, 0x05, 0.5)
    assert br.radion.actual_mask == 0x04
    assert [p for p in drain(aog_rx) if p[3] == aog.PGN_FROM_MACHINE][-1][5] == 0x04

    # headland: all sections off but job open -> master stays on (job mode)
    drive(br, 0x00, 0.4)
    assert radion.master is True and radion.mask == 0


def test_aog_timeout_turns_master_off(rig):
    make, _ = rig
    br, radion = make()
    drive(br, 0x7F, 0.6)
    assert radion.master and radion.mask == 0x7F
    t_stop = time.monotonic()
    time.sleep(1.5)
    assert radion.master is False and radion.mask == 0
    off_t = next(t for t, d in radion.rx if t > t_stop and d.kind == frames.KIND_MASTER and not d.fields["master"])
    assert 0.9 < off_t - t_stop < 1.3
    # heartbeat keeps running
    assert any(d.kind == frames.KIND_HEARTBEAT for t, d in radion.rx if t > t_stop + 1.2)


def test_mask_master_mode_and_remap(rig):
    make, _ = rig
    br, radion = make(master_mode="mask", remap=[7, 6, 5, 4, 3, 2, 1])
    drive(br, 0x00, 0.5)
    assert radion.master is False
    drive(br, 0x01, 0.5)                 # AOG section 1 -> Radion section 7
    assert radion.master is True and radion.mask == 0x40
    assert br.actual_aog_mask() == 0x01


def test_override_sends_off_bits(rig):
    make, aog_rx = rig
    br, radion = make(feedback="override", override_debounce=0.2)
    radion.switches = 0x7C               # sections 1-2 switched off on the Radion
    drive(br, 0x7F, 1.0)
    ea = [p for p in drain(aog_rx) if p[3] == aog.PGN_SECTION_SWITCHES]
    assert ea and len(ea[-1]) == 14
    assert ea[-1][9] == 0 and ea[-1][10] == 0x03     # ON none, OFF sections 1-2


def test_safe_off_on_stop(rig):
    make, _ = rig
    br, radion = make()
    drive(br, 0x7F, 0.5)
    t = time.monotonic()
    br.stop()
    time.sleep(0.2)
    after = [d for ts, d in radion.rx if ts >= t]
    assert sum(1 for d in after if d.kind == frames.KIND_MASTER and not d.fields["master"]) >= 3
    assert sum(1 for d in after if d.kind == frames.KIND_SECTIONS and d.fields["mask"] == 0) >= 3
    assert radion.master is False and radion.mask == 0


def test_force_master_off_bench(rig):
    make, _ = rig
    br, radion = make()
    drive(br, 0x7F, 0.4)
    assert radion.master is True
    br.set_force_master_off(True)
    drive(br, 0x7F, 0.4)
    assert radion.master is False and radion.mask == 0x7F


def _replay_two_agios(br, seconds=1.0):
    """Real AgIO 192.168.5.13: 0x7F at 10 Hz. Foreign AgIO on Wi-Fi: 0x00 at 100 Hz."""
    changes = []
    orig = br.console.set_state

    def spy(master, mask):
        if (master, mask) != br.console.snapshot():
            changes.append((master, mask))
        orig(master, mask)

    br.console.set_state = spy
    end = time.monotonic() + seconds
    i = 0
    while time.monotonic() < end:
        if i % 10 == 0:
            br.on_udp(aog.build_machine_data(0x7F, 5.0), "192.168.5.13")
        br.on_udp(aog.build_machine_data(0x00, 0.0), "10.65.137.118")
        i += 1
        time.sleep(0.01)
    return changes


def test_other_subnet_is_ignored(rig):
    """Field bug: a second AgIO on the Wi-Fi hotspot made the sections flicker."""
    make, _ = rig
    br, radion = make(subnet="192.168.5")
    changes = _replay_two_agios(br)
    assert changes == [(True, 0x7F)]                 # one change, no flicker
    assert br.agio_source == "192.168.5.13"
    assert br.ignored_sources.get("10.65.137.118", 0) > 50
    assert radion.mask == 0x7F


def test_same_subnet_mix_would_flicker(rig):
    make, _ = rig
    br, _ = make(subnet="192.168.5")
    br.cfg.subnet = "10.65.137"   # pretend both senders were inside the subnet
    changes = []
    orig = br.console.set_state
    br.console.set_state = lambda m, k: (changes.append(k), orig(m, k))
    for _ in range(5):
        br.on_udp(aog.build_machine_data(0x7F), "10.65.137.1")
        br.on_udp(aog.build_machine_data(0x00), "10.65.137.118")
    assert len(set(changes)) == 2                    # documents why the filter exists


def test_subnet_change_pgn_201(rig):
    make, _ = rig
    br, _ = make(subnet="192.168.5")
    saved = []
    br.on_subnet_change = saved.append
    br.local_ips = lambda now: {"127.0.0.1", "192.168.5.13", "10.65.137.53"}
    br.on_udp(aog.build_subnet_change(192, 168, 1), "10.65.137.118")   # stray AgIO: ignored
    assert br.cfg.subnet == "192.168.5" and saved == []
    br.on_udp(aog.build_subnet_change(192, 168, 1), "10.65.137.53")    # AgIO on this PC, any adapter
    assert br.cfg.subnet == "192.168.1" and saved == ["192.168.1"]
    assert br.cfg.reply_addr == "127.0.0.1"                              # test override kept
    br.on_udp(aog.build_machine_data(0x01), "192.168.5.13")             # old subnet now foreign
    assert "192.168.5.13" in br.ignored_sources


def test_scan_pgn_202_reply(rig):
    make, aog_rx = rig
    br, _ = make(subnet="192.168.5")
    br.local_ips = lambda now: {"127.0.0.1", "192.168.5.13"}
    drain(aog_rx)
    br.on_udp(aog.build_scan(), "192.168.5.13")
    time.sleep(0.2)
    rep = [p for p in drain(aog_rx) if p[3] == aog.PGN_SCAN_REPLY]
    assert rep and len(rep[-1]) == 13 and rep[-1][2] == 123             # AgIO: data[3]==203 && Length==13
    assert tuple(rep[-1][5:9]) == (192, 168, 5, 13) and tuple(rep[-1][9:12]) == (192, 168, 5)


def test_normalize_subnet():
    from teejet_bridge.bridge import normalize_subnet
    assert normalize_subnet("192.168.5") == "192.168.5"
    assert normalize_subnet(" 192.168.5.0 ") == "192.168.5"
    for bad in ("192.168", "192.168.300", "a.b.c", ""):
        with pytest.raises(ValueError):
            normalize_subnet(bad)
    assert BridgeConfig().reply_addr == "192.168.5.255"


def _isobus(pkts):
    return [p for p in pkts if p[3] == aog.PGN_ISOBUS_HEARTBEAT]


def test_isobus_heartbeat_reports_actual_sections(rig):
    make, aog_rx = rig
    br, radion = make()
    radion.switches = 0x7B                           # Radion switch 3 off
    drive(br, 0x3F, 1.0)
    hb = _isobus(drain(aog_rx))
    assert len(hb) >= 4                              # >= 5 Hz, AOG alive window is 1 s
    last = hb[-1]
    n = last[4]
    payload = last[5:5 + n]
    assert n == 2 + (7 + 7) // 8                     # AOG: data.Length == 2 + ceil(sections/8)
    assert payload[0] & 0x01 and (payload[0] >> 1) & 0x07 == 1 and payload[1] == 7
    assert payload[2] == 0x3B                        # actual, not commanded (0x3F)


def test_isobus_reports_zero_sections_when_radion_lost(rig):
    make, aog_rx = rig
    br, _ = make()
    drive(br, 0x01, 0.5)
    br.radion.last_rx = -100.0                       # pretend nothing heard for a long time
    drain(aog_rx)
    br._tx_isobus(br.clock())
    hb = _isobus(drain(aog_rx))
    assert hb and hb[-1][4] == 2 and hb[-1][6] == 0  # numSections 0: AOG maps by itself


def test_isobus_button_switches_boompilot(rig):
    make, aog_rx = rig
    br, radion = make()
    drive(br, 0x7F, 0.5)
    assert radion.master is True
    send_aog(br, aog.build_isobus_sc_request(False))
    drive(br, 0x7F, 0.5)
    assert radion.master is False and radion.mask == 0
    assert not (_isobus(drain(aog_rx))[-1][5] & 0x01)  # AOG button shows OFF
    send_aog(br, aog.build_isobus_sc_request(True))
    drive(br, 0x7F, 0.5)
    assert radion.master is True and radion.mask == 0x7F


def test_isobus_forces_button_redraw_on_first_sections(rig):
    """AOG only redraws its ISOBUS button when the enabled flag changes, so
    the 0 -> N sections step sends the flag flipped once, then the real one."""
    make, aog_rx = rig
    br, _ = make()
    drive(br, 0x01, 0.8)
    flags = [p[5] & 1 for p in _isobus(drain(aog_rx)) if p[6] > 0]   # heartbeats with sections
    assert flags[:2] == [0, 1] and all(f == 1 for f in flags[1:])
