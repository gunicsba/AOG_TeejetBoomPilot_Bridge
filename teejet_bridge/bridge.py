"""bridge mode: AgOpenGPS (UDP) <-> Radion 8140 (CAN).

AOG side follows the AOG_AGTronik_bridge pattern (see aog.py for sources):
  in : 239 / 229 section bits (job open), 254 speed, 200 hello  on :8888
  in : 241 (0xF1) AOG ISOBUS section-control button -> BoomPilot on/off
  out: hello reply 123, 0xED actual relays, 240 (0xF0) ISOBUS heartbeat with
       the Radion's actual sections (AOG maps coverage from it), optionally
       0xEA remote switches and the 32618 switch box   to <subnet>.255:9999

Like a real AOG machine module the bridge lives on one subnet (default
192.168.5.x): AOG packets from other networks are ignored, PGN 201 from AgIO's
Ethernet window moves it to another subnet and PGN 202 (scan) is answered.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import can

from . import aog, frames
from .canio import CanIO
from .console import ConsoleTx, RadionMonitor
from .scheduler import Scheduler

log = logging.getLogger(__name__)

MASTER_JOB = "job"      # M=1 while AOG sends 239/229 (job open); sections from mask
MASTER_MASK = "mask"    # M=1 only while at least one section is commanded

FEEDBACK_ONLY = "feedback"   # 0xED + hello carry actual state; AOG not influenced
FEEDBACK_OVERRIDE = "override"  # + 0xEA OFF bits force AOG sections off

# Only AgIO inside the module subnet is obeyed. A second AgIO on the Wi-Fi
# hotspot (all-zero 239/254 at 100 Hz) made the sections flicker every fix.
DEFAULT_SUBNET = "192.168.5"


def normalize_subnet(value: str) -> str:
    """'192.168.5', '192.168.5.0' or '192.168.5.x' -> '192.168.5'."""
    parts = (value or "").strip().split(".")
    if len(parts) == 4:
        parts = parts[:3]
    try:
        octets = [int(x) for x in parts]
    except ValueError:
        octets = []
    if len(octets) != 3 or not all(0 <= o <= 255 for o in octets):
        raise ValueError(f"subnet must be like 192.168.5, got {value!r}")
    return ".".join(str(o) for o in octets)


@dataclass
class BridgeConfig:
    sections: int = 7
    remap: Optional[List[int]] = None          # remap[i] = Radion section for AOG section i+1
    master_mode: str = MASTER_JOB
    aog_timeout: float = 1.0
    feedback: str = FEEDBACK_ONLY
    override_debounce: float = 0.5
    rc_switchbox: bool = False
    isobus: bool = True                         # 0xF0 heartbeat: AOG maps actual coverage
    listen_ip: str = "0.0.0.0"
    subnet: str = DEFAULT_SUBNET                # module subnet; only AgIO in it is obeyed
    listen_port: int = aog.DEFAULT_LISTEN_PORT
    send_port: int = aog.DEFAULT_SEND_PORT
    broadcast: str = ""                         # reply address override; "" = <subnet>.255
    module_src: int = aog.MACHINE_SRC
    radion_timeout: float = 3.0
    claim: bool = False
    probe: bool = True
    mss: bool = False
    hb_speed: bool = False

    def __post_init__(self):
        if not 1 <= self.sections <= frames.MAX_SECTIONS:
            raise ValueError(f"sections must be 1..{frames.MAX_SECTIONS}")
        if self.remap is None:
            self.remap = list(range(1, self.sections + 1))
        if len(self.remap) != self.sections:
            raise ValueError(f"remap needs {self.sections} entries, got {len(self.remap)}")
        if any(not 1 <= r <= frames.MAX_SECTIONS for r in self.remap) or len(set(self.remap)) != len(self.remap):
            raise ValueError("remap entries must be unique Radion section numbers 1..24")
        if self.master_mode not in (MASTER_JOB, MASTER_MASK):
            raise ValueError("master_mode must be 'job' or 'mask'")
        if self.feedback not in (FEEDBACK_ONLY, FEEDBACK_OVERRIDE):
            raise ValueError("feedback must be 'feedback' or 'override'")
        self.subnet = normalize_subnet(self.subnet)
        self.broadcast = (self.broadcast or "").strip()

    @property
    def reply_addr(self) -> str:
        return self.broadcast or f"{self.subnet}.255"


class Bridge:
    def __init__(self, cfg: BridgeConfig, io: CanIO, sched: Scheduler,
                 sock: Optional[socket.socket] = None):
        self.cfg = cfg
        self.io = io
        self.sched = sched
        self.clock = sched.clock

        # AOG state (written by UDP thread)
        self.aog_mask = 0                 # raw AOG section bits (AOG numbering)
        self.aog_speed_kmh: Optional[float] = None
        self.last_aog_job = 0.0           # last 239/229
        self.last_aog_any = 0.0
        self.last_aog_speed = 0.0
        self.agio_version: Optional[int] = None
        self.aog_alive = False
        self.last_udp_tx = 0.0
        self.force_master_off = False     # bench test 2: AOG mask with 02 00
        self.agio_source: Optional[str] = None   # last accepted AgIO address
        self.ignored_sources: Dict[str, int] = {}
        self._local_ips = set()
        self._local_ips_t = -1e9
        self._scan_subnet: Optional[tuple] = None
        self.isobus_sc_enabled = True     # AOG's ISOBUS button (0xF1); off = BoomPilot off
        self._isobus_had_sections = False # see _tx_isobus
        self.recorder = None              # capture.Recorder (optional)
        # called (from the UDP thread) with the new subnet after a PGN 201
        self.on_subnet_change: Optional[Callable[[str], None]] = None

        self.radion = RadionMonitor(self.clock, cfg.radion_timeout, on_probe_reply=self._on_probe_reply)
        self.console = ConsoleTx(io, sched, claim=cfg.claim, probe=cfg.probe, mss=cfg.mss,
                                 hb_speed=cfg.hb_speed, speed_source=self._speed_m_s)

        # override debounce
        self._stable_actual: Optional[int] = None
        self._last_switchbox: Optional[bytes] = None

        sched.add("aog_watch", self._aog_watch, 0.1)
        sched.add("radion_watch", self.radion.check, 0.2)
        sched.add("bus_health", io.check_health, 0.5)
        sched.add("aog_feedback", self._tx_feedback, None)
        sched.add("aog_hello", self._tx_hello_reply, None)
        sched.add("aog_scan", self._tx_scan_reply, None)
        sched.add("isobus", self._tx_isobus, 0.2, enabled=cfg.isobus)

        self.sock = sock or self._open_socket()
        self._stop = threading.Event()
        self._udp_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    #  section mapping
    # ------------------------------------------------------------------
    def aog_to_radion(self, aog_mask: int) -> int:
        out = 0
        for i, r in enumerate(self.cfg.remap):
            if (aog_mask >> i) & 1:
                out |= 1 << (r - 1)
        return out

    def radion_to_aog(self, radion_mask: int) -> int:
        out = 0
        for i, r in enumerate(self.cfg.remap):
            if (radion_mask >> (r - 1)) & 1:
                out |= 1 << i
        return out

    @property
    def aog_section_bits(self) -> int:
        return (1 << self.cfg.sections) - 1

    # ------------------------------------------------------------------
    #  UDP
    # ------------------------------------------------------------------
    def _open_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            # Windows: an ICMP "port unreachable" for one of our replies would
            # otherwise make the next recvfrom() fail with WSAECONNRESET.
            s.ioctl(socket.SIO_UDP_CONNRESET, False)
        s.bind((self.cfg.listen_ip, self.cfg.listen_port))
        s.settimeout(0.2)
        log.info("UDP listening on %s:%d, module subnet %s.x, replies -> %s:%d", self.cfg.listen_ip,
                 self.cfg.listen_port, self.cfg.subnet, self.cfg.reply_addr, self.cfg.send_port)
        self._check_local_subnet()
        return s

    def _check_local_subnet(self):
        if not self.local_ip_in_subnet(self.clock()):
            log.warning("This PC has no network adapter in %s.x, so AgIO data will not arrive. Give the "
                        "Ethernet adapter an address like %s.10, or set AgIO and this bridge to the "
                        "adapter's subnet.", self.cfg.subnet, self.cfg.subnet)

    def _udp_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except (socket.timeout, ConnectionResetError):
                continue
            except OSError:
                if self._stop.is_set():
                    break
                log.exception("UDP receive failed")
                time.sleep(0.2)
                continue
            if self.recorder is not None:
                # before the subnet filter, so a capture also shows ignored senders
                self.recorder.udp("Rx", addr, (self.cfg.reply_addr, self.cfg.listen_port), data)
            self.on_udp(data, addr[0])

    def local_ips(self, now: float) -> set:
        if now - self._local_ips_t > 5.0:
            ips = {"127.0.0.1"}
            try:
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                    ips.add(info[4][0])
            except OSError:
                pass
            self._local_ips, self._local_ips_t = ips, now
        return self._local_ips

    def in_subnet(self, ip: str) -> bool:
        return ip.rsplit(".", 1)[0] == self.cfg.subnet

    def local_ip_in_subnet(self, now: float) -> Optional[str]:
        for ip in sorted(self.local_ips(now)):
            if self.in_subnet(ip):
                return ip
        return None

    def _ignore(self, src_ip: str, now: float):
        n = self.ignored_sources.get(src_ip, 0)
        self.ignored_sources[src_ip] = n + 1
        if n:
            return
        if src_ip in self.local_ips(now):
            log.warning("IGNORING AgOpenGPS packets from this PC's address %s: it is outside the module "
                        "subnet %s.x. Check AgIO's Ethernet subnet / adapter, or change Subnet here.",
                        src_ip, self.cfg.subnet)
        else:
            log.warning("IGNORING AgOpenGPS packets from %s: outside the module subnet %s.x "
                        "(another AgIO on Wi-Fi/LAN?)", src_ip, self.cfg.subnet)

    def on_udp(self, data: bytes, src_ip: Optional[str] = None, now: Optional[float] = None):
        pkt = aog.parse(data)
        if pkt is None:
            return
        now = self.clock() if now is None else now

        # Network management from AgIO's Ethernet window. Accepted from this PC
        # (whichever adapter AgIO sent it out of) or from inside our subnet, so a
        # stray AgIO elsewhere on Wi-Fi cannot move or probe the bridge.
        if pkt.pgn in (aog.PGN_SUBNET_CHANGE, aog.PGN_SCAN):
            if src_ip is not None and not (self.in_subnet(src_ip) or src_ip in self.local_ips(now)):
                self._ignore(src_ip, now)
                return
            if pkt.pgn == aog.PGN_SUBNET_CHANGE:
                self._change_subnet("%d.%d.%d" % pkt.subnet)
            else:
                ref = src_ip if src_ip else self.cfg.subnet + ".0"
                self._scan_subnet = tuple(int(x) for x in ref.split(".")[:3])
                self.sched.kick("aog_scan")
            return

        if src_ip is not None and not self.in_subnet(src_ip):
            self._ignore(src_ip, now)
            return
        if src_ip is not None and src_ip != self.agio_source:
            log.info("AgIO data from %s", src_ip)
            self.agio_source = src_ip
        self.last_aog_any = now
        if pkt.pgn == aog.PGN_HELLO:
            if self.agio_version is None:
                log.info("AgIO hello (version byte %s)", pkt.agio_version)
            self.agio_version = pkt.agio_version
            self.sched.kick("aog_hello")
        elif pkt.carries_job:
            self.aog_mask = pkt.section_mask & self.aog_section_bits
            self.last_aog_job = now
            if pkt.pgn == aog.PGN_MACHINE_DATA and pkt.speed_kmh is not None and self.last_aog_speed < now - 0.5:
                self.aog_speed_kmh = pkt.speed_kmh   # only if no 254 seen recently
            self._apply(now)
            self.sched.kick("aog_feedback")
        elif pkt.pgn == aog.PGN_STEER_DATA:
            self.aog_speed_kmh = pkt.speed_kmh
            self.last_aog_speed = now
        elif pkt.pgn == aog.PGN_ISOBUS_SC_REQUEST and self.cfg.isobus:
            if pkt.sc_request != self.isobus_sc_enabled:
                self.isobus_sc_enabled = pkt.sc_request
                log.warning("AOG ISOBUS button: section control %s -> BoomPilot %s",
                            "ENABLED" if pkt.sc_request else "DISABLED",
                            "follows AOG" if pkt.sc_request else "OFF (Radion manual)")
            self._apply(now)
            if self.sched.task("isobus").enabled:
                self.sched.kick("isobus")      # confirm the new state to AOG at once

    def _change_subnet(self, new: str):
        if new == self.cfg.subnet:
            return
        log.warning("AgIO PGN 201: module subnet %s.x -> %s.x", self.cfg.subnet, new)
        self.cfg.subnet = new
        self.agio_source = None
        self.ignored_sources.clear()
        self._local_ips_t = -1e9
        self._check_local_subnet()
        if self.on_subnet_change:
            try:
                self.on_subnet_change(new)
            except Exception:
                log.exception("saving the new subnet failed")

    def _tx_scan_reply(self, now: float):
        req = self._scan_subnet or tuple(int(x) for x in self.cfg.subnet.split("."))
        ip = self.local_ip_in_subnet(now) or f"{self.cfg.subnet}.{self.cfg.module_src}"
        reply = aog.build_scan_reply(tuple(int(x) for x in ip.split(".")), req, self.cfg.module_src)
        # Machine_UDP_v5 answers the scan to 255.255.255.255:9999
        self._send_udp(reply, self.cfg.broadcast or "255.255.255.255")
        log.info("AgIO scan: replied as machine module %s, subnet %s", ip, self.cfg.subnet)

    def _send_udp(self, payload: bytes, dest: Optional[str] = None):
        dst = (dest or self.cfg.reply_addr, self.cfg.send_port)
        if self.recorder is not None:
            src = self.local_ip_in_subnet(self.clock()) or "0.0.0.0"
            self.recorder.udp("Tx", (src, self.sock.getsockname()[1]), dst, payload)
        try:
            self.sock.sendto(payload, dst)
            self.last_udp_tx = self.clock()
        except OSError as e:
            log.warning("UDP send failed: %s", e)

    # ------------------------------------------------------------------
    #  AOG -> Radion
    # ------------------------------------------------------------------
    def _speed_m_s(self) -> Optional[float]:
        if self.aog_speed_kmh is None or not self.aog_alive:
            return None
        return self.aog_speed_kmh / 3.6

    def _apply(self, now: float):
        fresh = self.last_aog_job > 0 and (now - self.last_aog_job) <= self.cfg.aog_timeout
        if fresh != self.aog_alive:
            self.aog_alive = fresh
            if fresh:
                log.info("AOG section data present (job open) -> BoomPilot follows AOG")
            else:
                log.error("AOG section data LOST for > %.1f s -> master OFF, sections 0 (heartbeat continues)",
                          self.cfg.aog_timeout)
        if fresh and self.isobus_sc_enabled:
            mask = self.aog_to_radion(self.aog_mask)
            master = True if self.cfg.master_mode == MASTER_JOB else mask != 0
            if self.force_master_off:
                master = False
        else:
            master, mask = False, 0
        self.console.set_state(master, mask)

    def _aog_watch(self, now: float):
        self._apply(now)

    def set_force_master_off(self, on: bool):
        self.force_master_off = on
        log.warning("bench: force master OFF %s", "ENABLED" if on else "disabled")
        self._apply(self.clock())

    # ------------------------------------------------------------------
    #  Radion -> AOG
    # ------------------------------------------------------------------
    def on_can(self, msg: can.Message, now: float):
        before = self.radion.actual_mask
        if self.radion.handle(msg, now) and self.radion.actual_mask is not None:
            if self.cfg.rc_switchbox:
                self.sched.kick("aog_feedback")
            if self.cfg.isobus and self.radion.actual_mask != before:
                self.sched.kick("isobus")       # coverage follows the boom at once

    def _on_probe_reply(self):
        self.console.probe_reply()

    def actual_aog_mask(self) -> Optional[int]:
        a = self.radion.actual_mask
        return None if a is None else self.radion_to_aog(a)

    def override_off_mask(self, now: float) -> int:
        """Sections the Radion reports closed, after the debounce (AOG numbering)."""
        a = self.radion.actual_mask
        if a is not None and now - self.radion.mask_changed_at >= self.cfg.override_debounce:
            self._stable_actual = a
        if self._stable_actual is None:
            return 0
        return ~self.radion_to_aog(self._stable_actual) & self.aog_section_bits

    def _tx_feedback(self, now: float):
        if not self.radion.is_up(now):
            return
        actual = self.actual_aog_mask()
        if actual is None:
            return
        self._send_udp(aog.build_from_machine(actual, self.cfg.module_src))
        if self.cfg.feedback == FEEDBACK_OVERRIDE:
            # AGTronik "auto mode" pattern: ON=0 (AOG keeps control),
            # OFF = sections the implement has closed.
            off = self.override_off_mask(now)
            self._send_udp(aog.build_section_switches(0, 0, off, self.cfg.module_src))
        if self.cfg.rc_switchbox:
            master, _ = self.console.snapshot()
            sb = aog.build_switchbox(master, actual != 0, actual)
            if sb != self._last_switchbox:
                self._last_switchbox = sb
                self._send_udp(sb)

    def _tx_isobus(self, now: float):
        """0xF0 like an ISOBUS TC. With the Radion lost, report 0 sections so
        AOG falls back to its own coverage instead of painting stale states."""
        actual = self.actual_aog_mask() if self.radion.is_up(now) else None
        en = self.isobus_sc_enabled
        if actual is None:
            self._isobus_had_sections = False
            self._send_udp(aog.build_isobus_heartbeat(en, 0, 0, 0, self.cfg.module_src))
            return
        if not self._isobus_had_sections:
            # AOG quirk (CISOBUS.DeserializeHeartbeat): going from 0 to N sections
            # with an unchanged enabled flag never redraws the button, so it stays
            # "idle" until pressed twice. Flip the flag once to force the redraw;
            # AOG only uses the flag for the button image.
            self._isobus_had_sections = True
            self._send_udp(aog.build_isobus_heartbeat(not en, 1, self.cfg.sections, actual,
                                                      self.cfg.module_src))
        self._send_udp(aog.build_isobus_heartbeat(en, 1, self.cfg.sections, actual, self.cfg.module_src))

    def _tx_hello_reply(self, now: float):
        # Like AGTronik: only answer while the implement is really there, so the
        # green machine icon in AgIO means "Radion connected".
        if self.radion.is_up(now):
            self._send_udp(aog.build_hello_reply(self.actual_aog_mask() or 0, self.cfg.module_src))

    # ------------------------------------------------------------------
    #  lifecycle
    # ------------------------------------------------------------------
    def start(self):
        if self.cfg.feedback == FEEDBACK_OVERRIDE:
            log.warning("feedback=override: AOG sections are forced OFF when the Radion reports them closed. "
                        "Only valid if bench test 3 shows the Radion reports its SWITCH state; with the AND "
                        "rule a section AOG turns off can never come back on.")
        self.io.start_rx(self.on_can)
        self._udp_thread = threading.Thread(target=self._udp_loop, name="udp-rx", daemon=True)
        self._udp_thread.start()
        self.sched.start()

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        self.sched.stop()
        self.console.send_safe_off()
        self.io.stop()
        try:
            self.sock.close()
        except OSError:
            pass
        if self._udp_thread:
            self._udp_thread.join(1.0)

    def status_line(self) -> str:
        now = self.clock()
        m, cmd = self.console.snapshot()
        act = self.radion.actual_mask
        n = max(self.cfg.sections, 8)

        def age(t):
            return "never" if t <= 0 else f"{now - t:5.1f}s"

        return (f"AOG {'OK ' if self.aog_alive else '---'} [{frames.mask_str(self.aog_mask, self.cfg.sections)}] "
                f"| CMD M={'1' if m else '0'} [{frames.mask_str(cmd, n)}] "
                f"| RADION {'UP  ' if self.radion.is_up(now) else 'LOST'} "
                f"[{frames.mask_str(act, n) if act is not None else '?' * n}] "
                f"| aog rx {age(self.last_aog_job)} can tx {age(self.io.last_tx)} "
                f"radion rx {age(self.radion.last_rx)} udp tx {age(self.last_udp_tx)} | bus {self.io.state}")
