"""Traffic capture: Wireshark .pcapng (CAN + AOG UDP) and PCAN-View .trc.

Off by default; switched on/off at runtime from the Settings tab. One .pcapng
holds two interfaces:
  0  "can"  LINKTYPE_CAN_SOCKETCAN (227): Wireshark decodes IDs / J1939
  1  "aog"  LINKTYPE_IPV4 (228): synthesised IPv4/UDP so the AgIO <-> bridge
            packets show up as UDP 8888/9999 with their real addresses
Packet direction is stored in epb_flags (inbound / outbound).
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import struct
import threading
import time
from typing import Optional, Tuple

from .trc import TrcWriter

log = logging.getLogger(__name__)

LINKTYPE_CAN_SOCKETCAN = 227
LINKTYPE_IPV4 = 228
IF_CAN = 0
IF_UDP = 1
CAN_EFF_FLAG = 0x80000000


def _pad4(b: bytes) -> bytes:
    return b + b"\x00" * (-len(b) % 4)


def _option(code: int, value: bytes) -> bytes:
    return struct.pack("<HH", code, len(value)) + _pad4(value)


def _block(block_type: int, body: bytes) -> bytes:
    total = 12 + len(body)
    return struct.pack("<II", block_type, total) + body + struct.pack("<I", total)


def _ipv4_checksum(header: bytes) -> int:
    s = sum(struct.unpack("!10H", header))
    s = (s & 0xFFFF) + (s >> 16)
    s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def ipv4_udp(src: Tuple[str, int], dst: Tuple[str, int], payload: bytes) -> bytes:
    udp = struct.pack("!HHHH", src[1] & 0xFFFF, dst[1] & 0xFFFF, 8 + len(payload), 0) + payload
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0x4000, 64, 17, 0,
                      _ip_bytes(src[0]), _ip_bytes(dst[0]))
    hdr = hdr[:10] + struct.pack("!H", _ipv4_checksum(hdr)) + hdr[12:]
    return hdr + udp


def _ip_bytes(ip: str) -> bytes:
    try:
        return bytes(int(x) for x in ip.split("."))
    except (ValueError, AttributeError):
        return b"\x00\x00\x00\x00"


def socketcan_frame(can_id: int, data: bytes, extended: bool = True) -> bytes:
    cid = (can_id | CAN_EFF_FLAG) if extended else can_id
    # LINKTYPE_CAN_SOCKETCAN: id+flags big-endian, len, pad, res, res, 8 data bytes
    return struct.pack("!IB3x", cid, len(data)) + bytes(data).ljust(8, b"\x00")


class PcapngWriter:
    def __init__(self, path: str):
        self.path = path
        self._f = open(path, "wb")
        shb = struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1) + _option(4, b"teejet-bridge") + _option(0, b"")
        self._f.write(_block(0x0A0D0D0A, shb))
        for linktype, name in ((LINKTYPE_CAN_SOCKETCAN, b"can"), (LINKTYPE_IPV4, b"aog-udp")):
            idb = struct.pack("<HHI", linktype, 0, 0) + _option(2, name) + _option(0, b"")
            self._f.write(_block(1, idb))

    def packet(self, iface: int, wall_time: float, data: bytes, inbound: bool):
        us = int(wall_time * 1_000_000)
        flags = _option(2, struct.pack("<I", 1 if inbound else 2)) + _option(0, b"")
        body = struct.pack("<IIIII", iface, us >> 32, us & 0xFFFFFFFF, len(data), len(data)) + _pad4(data) + flags
        self._f.write(_block(6, body))

    def tell(self) -> int:
        return self._f.tell()

    def flush(self):
        self._f.flush()

    def close(self):
        self._f.close()


class Recorder:
    """Thread-safe; ``write`` has the TrcWriter signature so CanIO can use it
    directly. Does nothing while disabled."""

    def __init__(self, log_dir: str, clock=time.monotonic, max_bytes: int = 50_000_000):
        self.log_dir = log_dir
        self.clock = clock
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._pcap: Optional[PcapngWriter] = None
        self._trc: Optional[TrcWriter] = None
        self._wall0 = time.time()
        self._mono0 = clock()
        self.pcap_path: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return self._pcap is not None

    def set_enabled(self, on: bool):
        with self._lock:
            if on and self._pcap is None:
                self._open()
            elif not on and self._pcap is not None:
                self._close()

    def _open(self):
        os.makedirs(self.log_dir, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.pcap_path = os.path.join(self.log_dir, f"capture-{stamp}.pcapng")
        self._pcap = PcapngWriter(self.pcap_path)
        self._trc = TrcWriter(os.path.join(self.log_dir, f"can-{stamp}.trc"), clock=self.clock)
        self._wall0, self._mono0 = time.time(), self.clock()
        log.info("capture ON: %s", self.pcap_path)

    def _close(self):
        self._pcap.close()
        self._trc.close()
        log.info("capture OFF: %s", self.pcap_path)
        self._pcap = self._trc = None

    def _wall(self, t: Optional[float]) -> float:
        return self._wall0 + ((self.clock() if t is None else t) - self._mono0)

    def write(self, direction: str, can_id: int, data: bytes, t: Optional[float] = None):
        """CAN frame (same signature as TrcWriter.write)."""
        with self._lock:
            if self._pcap is None:
                return
            self._trc.write(direction, can_id, data, t)
            self._pcap.packet(IF_CAN, self._wall(t), socketcan_frame(can_id, data), direction == "Rx")
            self._rotate_if_needed()

    def udp(self, direction: str, src: Tuple[str, int], dst: Tuple[str, int], payload: bytes,
            t: Optional[float] = None):
        with self._lock:
            if self._pcap is None:
                return
            self._pcap.packet(IF_UDP, self._wall(t), ipv4_udp(src, dst, payload), direction == "Rx")
            self._rotate_if_needed()

    def _rotate_if_needed(self):
        if self._pcap.tell() >= self.max_bytes:
            self._close()
            self._open()

    def close(self):
        self.set_enabled(False)
