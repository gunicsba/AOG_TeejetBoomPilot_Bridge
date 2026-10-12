"""TeeJet Matrix Pro 570GS <-> Radion 8140 CAN frame definitions.

Single source of truth for the bridge, the GUI and the trace decoder.
Reverse-engineered from the PCAN-View traces in ``traces/``.

Bus: 250 kbit/s, 29-bit IDs, J1939 data link. Application messages are
TeeJet-proprietary PGN EF00 (Proprietary A) with byte 0 used as a mux.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
#  Addresses / IDs
# ---------------------------------------------------------------------------
CONSOLE_SA = 0xC0           # Matrix console (the bridge impersonates this)
RADION_SA = 0xB4            # Radion 8140 (never claims, slot pre-assigned)
GLOBAL_DA = 0xFF

PGN_PROPA = 0xEF00          # Proprietary A (PDU1, destination specific)
PGN_ADDR_CLAIM = 0xEE00
PGN_MSS = 0xF022            # ISO 11783 Machine Selected Speed

PRIO_6 = 6


def j1939_id(prio: int, pgn: int, sa: int, da: int = GLOBAL_DA) -> int:
    """Build a 29-bit J1939 identifier (PDU1 PGNs take *da* in PS)."""
    pf = (pgn >> 8) & 0xFF
    if pf < 0xF0:
        pgn = (pgn & 0x3FF00) | (da & 0xFF)
    return ((prio & 7) << 26) | ((pgn & 0x3FFFF) << 8) | (sa & 0xFF)


def split_id(can_id: int):
    """Return (prio, pgn, da, sa). For PDU2 PGNs *da* is GLOBAL_DA."""
    prio = (can_id >> 26) & 7
    pgn = (can_id >> 8) & 0x3FFFF
    sa = can_id & 0xFF
    if ((pgn >> 8) & 0xFF) < 0xF0:
        da = pgn & 0xFF
        pgn &= 0x3FF00
    else:
        da = GLOBAL_DA
    return prio, pgn, da, sa


ID_CONSOLE_GLOBAL = j1939_id(PRIO_6, PGN_PROPA, CONSOLE_SA, GLOBAL_DA)   # 18EFFFC0
ID_RADION_TO_CONSOLE = j1939_id(PRIO_6, PGN_PROPA, RADION_SA, CONSOLE_SA)  # 18EFC0B4
ID_CONSOLE_CLAIM = j1939_id(PRIO_6, PGN_ADDR_CLAIM, CONSOLE_SA, GLOBAL_DA)  # 18EEFFC0
ID_CONSOLE_MSS = j1939_id(PRIO_6, PGN_MSS, CONSOLE_SA)                    # 18F022C0

assert ID_CONSOLE_GLOBAL == 0x18EFFFC0
assert ID_RADION_TO_CONSOLE == 0x18EFC0B4
assert ID_CONSOLE_CLAIM == 0x18EEFFC0
assert ID_CONSOLE_MSS == 0x18F022C0

# Console NAME seen in "Matrix PRO570gs boot.trc" for SA C0 (manufacturer 179)
CONSOLE_NAME = bytes.fromhex("00006016078000 2F".replace(" ", ""))

# ---------------------------------------------------------------------------
#  Console -> Radion mux bytes (18EFFFC0, byte 0)
# ---------------------------------------------------------------------------
MUX_SECTIONS = 0x00     # 00 S1-8 S9-16 S17-24          (200 ms + after 02 change)
MUX_PROBE = 0x01        # 01                            (once; Radion answers 01 00 02 61)
MUX_MASTER = 0x02       # 02 M                          (1 s + on change)
MUX_HEARTBEAT = 0x0A    # 0A spd_lo spd_hi d0 d1 d2 d3  (100 ms, required)

# Radion -> console (18EFC0B4)
RADION_STATE_DLC = 7    # 00 SS b2 b3 b4 b5 FF
RADION_STATUS_DLC = 2   # 02 xx
RADION_PROBE_DLC = 4    # 01 00 02 61

RADION_PROBE_REPLY = bytes([0x01, 0x00, 0x02, 0x61])

MAX_SECTIONS = 24


# ---------------------------------------------------------------------------
#  Encoders (console side)
# ---------------------------------------------------------------------------
def heartbeat(speed_mm_s: Optional[int] = None, distance_mm: Optional[int] = None) -> bytes:
    """``0A spd_lo spd_hi d0..d3``. All-FF when there is no valid speed."""
    if speed_mm_s is None:
        return bytes([MUX_HEARTBEAT]) + b"\xFF" * 6
    spd = max(0, min(0xFFFE, int(speed_mm_s)))
    dist = 0xFFFFFFFF if distance_mm is None else int(distance_mm) & 0xFFFFFFFF
    return struct.pack("<BHI", MUX_HEARTBEAT, spd, dist)


def master(on: bool) -> bytes:
    return bytes([MUX_MASTER, 1 if on else 0])


def sections(mask: int) -> bytes:
    """``00 S1-8 S9-16 S17-24``; bit 0 of byte 1 = section 1."""
    mask &= (1 << MAX_SECTIONS) - 1
    return bytes([MUX_SECTIONS, mask & 0xFF, (mask >> 8) & 0xFF, (mask >> 16) & 0xFF])


def probe() -> bytes:
    return bytes([MUX_PROBE])


def mss(speed_mm_s: Optional[int], distance_mm: int = 0) -> bytes:
    """ISO 11783 Machine Selected Speed as sent by the Matrix (18F022C0).

    bytes 0-1 speed mm/s, 2-5 distance mm, 6 = FF, 7 = EB (source: navigation).
    """
    spd = 0xFFFF if speed_mm_s is None else max(0, min(0xFAFF, int(speed_mm_s)))
    return struct.pack("<HI", spd, int(distance_mm) & 0xFFFFFFFF) + b"\xFF\xEB"


# ---------------------------------------------------------------------------
#  Encoders (Radion side, used by tests)
# ---------------------------------------------------------------------------
def radion_state(actual_mask: int, b2: int = 0, b3: int = 0, b4: int = 0, b5: int = 0x80) -> bytes:
    return bytes([0x00, actual_mask & 0xFF, b2 & 0xFF, b3 & 0xFF, b4 & 0xFF, b5 & 0xFF, 0xFF])


def radion_status(value: int = 0) -> bytes:
    return bytes([0x02, value & 0xFF])


# ---------------------------------------------------------------------------
#  Decoder
# ---------------------------------------------------------------------------
@dataclass
class Decoded:
    kind: str                       # see KIND_* below
    text: str                       # human-readable annotation
    fields: dict = field(default_factory=dict)


KIND_HEARTBEAT = "heartbeat"
KIND_MASTER = "master"
KIND_SECTIONS = "sections"
KIND_PROBE = "probe"
KIND_RADION_STATE = "radion_state"
KIND_RADION_STATUS = "radion_status"
KIND_RADION_PROBE_REPLY = "radion_probe_reply"
KIND_MSS = "mss"
KIND_ADDR_CLAIM = "addr_claim"
KIND_CONSOLE_OTHER = "console_other"
KIND_UNKNOWN = "unknown"


def mask_str(mask: int, n: int = 8) -> str:
    """Section mask as '1234567.'-style string, section 1 first."""
    return "".join(str((i + 1) % 10) if (mask >> i) & 1 else "." for i in range(n))


def decode(can_id: int, data: bytes) -> Decoded:
    data = bytes(data)
    dlc = len(data)
    _, pgn, da, sa = split_id(can_id)

    if can_id == ID_CONSOLE_GLOBAL and dlc >= 1:
        mux = data[0]
        # Matrix sends DLC 7; the Radion also accepted an 8-byte all-FF version
        # ("console alive no speed response.trc").
        if mux == MUX_HEARTBEAT and dlc in (7, 8):
            spd, dist = struct.unpack_from("<HI", data, 1)
            if data[1:7] == b"\xFF" * 6:
                return Decoded(KIND_HEARTBEAT, "console heartbeat (no speed)", {"speed_mm_s": None, "distance_mm": None})
            return Decoded(KIND_HEARTBEAT,
                           f"console heartbeat speed={spd} mm/s ({spd * 0.0036:.1f} km/h) odo={dist} mm",
                           {"speed_mm_s": spd, "distance_mm": dist})
        if mux == MUX_MASTER and dlc == 2:
            return Decoded(KIND_MASTER, f"console MASTER {'ON' if data[1] else 'OFF'} (02 {data[1]:02X})",
                           {"master": bool(data[1]), "raw": data[1]})
        if mux == MUX_SECTIONS and dlc == 4:
            mask = data[1] | (data[2] << 8) | (data[3] << 16)
            return Decoded(KIND_SECTIONS, f"console SECTIONS cmd=0x{mask:06X} [{mask_str(mask, 8)}]",
                           {"mask": mask})
        if mux == MUX_PROBE and dlc == 1:
            return Decoded(KIND_PROBE, "console probe (expects 01 00 02 61)")
        return Decoded(KIND_CONSOLE_OTHER, f"console mux 0x{mux:02X} (unknown)")

    if can_id == ID_RADION_TO_CONSOLE and dlc >= 1:
        if dlc == RADION_STATE_DLC and data[0] == 0x00:
            ss = data[1]
            return Decoded(KIND_RADION_STATE,
                           f"RADION actual=0x{ss:02X} [{mask_str(ss)}] b2-b4={data[2]:02X} {data[3]:02X} {data[4]:02X} b5={data[5]:02X}",
                           {"mask": ss, "b2": data[2], "b3": data[3], "b4": data[4], "b5": data[5]})
        if dlc == RADION_STATUS_DLC and data[0] == 0x02:
            return Decoded(KIND_RADION_STATUS, f"RADION status 02 {data[1]:02X}", {"status": data[1]})
        if dlc == RADION_PROBE_DLC and data[0] == 0x01:
            return Decoded(KIND_RADION_PROBE_REPLY, f"RADION probe reply / ID {data.hex(' ').upper()}",
                           {"raw": data})
        return Decoded(KIND_UNKNOWN, "RADION unknown frame")

    if pgn == PGN_MSS and dlc == 8:
        spd, dist = struct.unpack_from("<HI", data, 0)
        return Decoded(KIND_MSS, f"MSS from {sa:02X} speed={spd} mm/s ({spd * 0.0036:.1f} km/h) dist={dist} mm src=0x{data[7]:02X}",
                       {"speed_mm_s": spd, "distance_mm": dist})

    if pgn == PGN_ADDR_CLAIM and dlc == 8:
        return Decoded(KIND_ADDR_CLAIM, f"address claim SA {sa:02X} NAME {data.hex(' ').upper()}", {"sa": sa})

    if sa == CONSOLE_SA and pgn == PGN_PROPA:
        return Decoded(KIND_CONSOLE_OTHER, f"console -> {da:02X} propA mux 0x{data[0]:02X}" if dlc else "console propA")

    return Decoded(KIND_UNKNOWN, "")
