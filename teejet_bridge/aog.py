"""AgOpenGPS / AgIO UDP PGNs used by the bridge.

Layouts verified against AgOpenGPS source (github.com/AgOpenGPS-Official/AgOpenGPS,
checked via a local fork at commit d1dad081):

* ``SourceCode/GPS/Forms/PGN.Designer.cs``
    - CPGN_EF (239, machine data):  speed=6 (km/h*10), sc1to8=11, sc9to16=12
    - CPGN_E5 (229, 64 sections):   sc1to8..sc57to64 = bytes 5..12
    - CPGN_FE (254, steer data):    speedLo=5, speedHi=6 (km/h*10), sc1to8=11, sc9to16=12
* ``SourceCode/GPS/Forms/Position.designer.cs``: 239 and 229 are only sent
  ``if (isJobStarted)``, once per GPS fix. 254 is sent regardless.
* ``SourceCode/GPS/Forms/Sections.Designer.cs`` ``BuildMachineByte``: section
  bits are ``section[j].isSectionOn``, which already includes the master /
  auto / manual button state. There is no separate master or auto bit.
* ``SourceCode/GPS/Forms/UDPComm.Designer.cs`` ``case 234``: requires
  ``data.Length == 14``, copies bytes 5..12 into ``mc.ss[1..8]`` then
  ``DoRemoteSwitches()`` (Sections.Designer.cs). Indices from
  ``SourceCode/GPS/Classes/CModuleComm.cs``: swMain=1, swAutoGr0=2,
  swAutoGr1=3, swNumSections=4, swOnGr0=5, swOffGr0=6, swOnGr1=7, swOffGr1=8.
  Main bit 2 clear = "button" hardware (MTZ8302): bit0 = master ON press,
  bit1 = master OFF press. ON bits force a section on, OFF bits force it off;
  when an OFF bit drops while master is Auto the section returns to Auto.
* ``SourceCode/AgIO/Source/Forms/UDP.designer.cs`` ``ReceiveFromUDP``:
  every ``80 81`` frame from a module is forwarded to AOG; a frame with
  ``data[3] == 123 && Length == 11`` is the machine module hello.

* ``SourceCode/AgIO/Source/Forms/FormUDP.cs`` (Ethernet window) and
  AgOpenGPS_Boards ``ArduinoModules/UDP/Machine_UDP_v5/Machine_UDP_v5.ino``:
    - 201 subnet change ``80 81 7F C9 05 C9 C9 a b c CRC`` -> module subnet a.b.c
    - 202 scan          ``80 81 7F CA 03 CA CA 05 CRC``
    - 203 scan reply    ``80 81 7B CB 07 ip0 ip1 ip2 ip3 s0 s1 s2 CRC`` (13 bytes)
      to 255.255.255.255:9999; s0-s2 = subnet of the scanning AgIO
  Both 201 and 202 are sent to 255.255.255.255:8888 from every adapter.
  Modules send everything else to ``<subnet>.255:9999``.

* ``SourceCode/GPS/Classes/CISOBUS.cs`` + ``UDPComm.Designer.cs case 0xF0``:
    - 0xF0 ISOBUS TC heartbeat (TC -> AOG), data =
      ``[flags, numSections, state bits...]``, flags bit0 = section control
      enabled, bits1-3 = number of TC clients; payload length must be exactly
      ``2 + ceil(numSections/8)``. Alive for 1 s. While alive with
      numSections > 0, AOG paints coverage from these *actual* states
      (``OpenGL.Designer.cs``: ``section[j].isMappingOn = isobus.IsSectionOn(j)``).
    - 0xF1 (AOG -> TC) ``RequestSectionControlEnabled``: data[5] = 1/0, sent
      by AOG's ISOBUS section-control button.
  AgIO forwards every AOG frame to ``<subnet>.255:8888`` (``ReceiveFromLoopBack``).

The outbound pattern (Hello reply, 0xED, 0xEA, optional 32618 switch box)
is copied from the AOG_AGTronik_bridge (Agromehanika AG-Tronik), which is
proven to report implement section state back to AOG correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

HEADER = b"\x80\x81"

PGN_HELLO = 0xC8            # 200 AgIO -> modules hello
PGN_STEER_DATA = 0xFE       # 254
PGN_MACHINE_DATA = 0xEF     # 239
PGN_SECTIONS_64 = 0xE5      # 229
PGN_SECTION_SWITCHES = 0xEA  # 234 module -> AOG remote switches
PGN_FROM_MACHINE = 0xED     # 237 module -> AOG (compatibility, AGTronik)
PGN_HELLO_MACHINE = 0x7B    # 123 machine module hello reply
PGN_SUBNET_CHANGE = 0xC9    # 201 AgIO -> modules
PGN_SCAN = 0xCA             # 202 AgIO -> modules
PGN_SCAN_REPLY = 0xCB       # 203 module -> AgIO
PGN_ISOBUS_HEARTBEAT = 0xF0  # 240 TC -> AOG actual section states
PGN_ISOBUS_SC_REQUEST = 0xF1  # 241 AOG -> TC section control on/off

MACHINE_SRC = 0x7B          # 123 = machine module

SWITCHBOX_HEADER = bytes([106, 127])  # PGN 32618 (Rate Controller switch box)

DEFAULT_LISTEN_PORT = 8888
DEFAULT_SEND_PORT = 9999


def checksum(msg: bytes) -> int:
    """AOG CRC: sum of bytes 2..n-1, low 8 bits."""
    return sum(msg[2:]) & 0xFF


def _finish(msg: bytearray) -> bytes:
    msg.append(checksum(msg))
    return bytes(msg)


# ---------------------------------------------------------------------------
#  Inbound
# ---------------------------------------------------------------------------
@dataclass
class AogPacket:
    pgn: int
    section_mask: Optional[int] = None   # set for 239 / 229 / 254
    speed_kmh: Optional[float] = None    # set for 254 / 239
    agio_version: Optional[int] = None   # set for hello
    carries_job: bool = False            # True for 239 / 229 (job open)
    subnet: Optional[tuple] = None       # set for 201 (a, b, c)
    sc_request: Optional[bool] = None    # set for 0xF1


def parse(data: bytes) -> Optional[AogPacket]:
    """Parse one AgIO datagram. Returns None for foreign/short packets."""
    if len(data) < 5 or data[0:2] != HEADER:
        return None
    pgn = data[3]
    if pgn == PGN_HELLO:
        return AogPacket(pgn, agio_version=data[5] if len(data) > 5 else None)
    if pgn == PGN_SUBNET_CHANGE:
        # same strictness as the firmware: "make really sure this is the subnet pgn"
        if len(data) >= 10 and data[4] == 5 and data[5] == 201 and data[6] == 201:
            return AogPacket(pgn, subnet=(data[7], data[8], data[9]))
        return None
    if pgn == PGN_ISOBUS_SC_REQUEST:
        if len(data) >= 6 and data[4] >= 1:
            return AogPacket(pgn, sc_request=bool(data[5] & 0x01))
        return None
    if pgn == PGN_SCAN:
        if len(data) >= 7 and data[4] == 3 and data[5] == 202 and data[6] == 202:
            return AogPacket(pgn)
        return None
    if pgn == PGN_MACHINE_DATA and len(data) >= 13:
        return AogPacket(pgn, section_mask=data[11] | (data[12] << 8),
                         speed_kmh=data[6] / 10.0, carries_job=True)
    if pgn == PGN_SECTIONS_64 and len(data) >= 13:
        mask = int.from_bytes(data[5:13], "little")
        return AogPacket(pgn, section_mask=mask, carries_job=True)
    if pgn == PGN_STEER_DATA and len(data) >= 13:
        spd = int.from_bytes(data[5:7], "little") / 10.0
        return AogPacket(pgn, section_mask=data[11] | (data[12] << 8), speed_kmh=spd)
    return AogPacket(pgn)


def build_machine_data(mask: int, speed_kmh: float = 0.0, src: int = 0x7F) -> bytes:
    """AOG -> module PGN 239 (for tests and simulation)."""
    msg = bytearray([0x80, 0x81, src, PGN_MACHINE_DATA, 8,
                     0, int(speed_kmh * 10) & 0xFF, 0, 0, 0, 0, mask & 0xFF, (mask >> 8) & 0xFF])
    return _finish(msg)


def build_hello(src: int = 0x7F, version: int = 0) -> bytes:
    return _finish(bytearray([0x80, 0x81, src, PGN_HELLO, 3, version & 0xFF, 0, 0]))


def build_subnet_change(a: int, b: int, c: int) -> bytes:
    """AgIO -> modules PGN 201 (for tests)."""
    return _finish(bytearray([0x80, 0x81, 0x7F, PGN_SUBNET_CHANGE, 5, 201, 201, a, b, c]))


def build_scan() -> bytes:
    """AgIO -> modules PGN 202 (for tests); AgIO uses a fixed 0x47 CRC byte."""
    return _finish(bytearray([0x80, 0x81, 0x7F, PGN_SCAN, 3, 202, 202, 5]))


# ---------------------------------------------------------------------------
#  Outbound (module -> AgIO)
# ---------------------------------------------------------------------------
def build_hello_reply(relay: int, src: int = MACHINE_SRC) -> bytes:
    """Machine hello reply, 11 bytes; turns the machine icon green in AgIO."""
    return _finish(bytearray([0x80, 0x81, src, PGN_HELLO_MACHINE, 5,
                              relay & 0xFF, (relay >> 8) & 0xFF, 0, 0, 0]))


def build_from_machine(relay: int, src: int = MACHINE_SRC) -> bytes:
    """PGN 0xED 'From Machine': bytes 5-6 = actual relay state."""
    return _finish(bytearray([0x80, 0x81, src, PGN_FROM_MACHINE, 8,
                              relay & 0xFF, (relay >> 8) & 0xFF, 0, 0, 0, 0, 0, 0]))


def build_section_switches(main_bits: int, on_mask: int, off_mask: int,
                           src: int = MACHINE_SRC) -> bytes:
    """PGN 0xEA (234) remote switches, 14 bytes total.

    byte 5 main (bit0 MasterOn press, bit1 MasterOff press, bit2=0 buttons),
    6-8 reserved, 9 ON 1-8, 10 OFF 1-8, 11 ON 9-16, 12 OFF 9-16.
    """
    return _finish(bytearray([0x80, 0x81, src, PGN_SECTION_SWITCHES, 8,
                              main_bits & 0xFB, 0, 0, 0,
                              on_mask & 0xFF, off_mask & 0xFF,
                              (on_mask >> 8) & 0xFF, (off_mask >> 8) & 0xFF]))


def build_isobus_sc_request(enabled: bool) -> bytes:
    """AOG -> TC PGN 0xF1 (for tests)."""
    return _finish(bytearray([0x80, 0x81, 0x7F, PGN_ISOBUS_SC_REQUEST, 1, 1 if enabled else 0]))


def build_isobus_heartbeat(sc_enabled: bool, num_clients: int, num_sections: int, actual: int,
                           src: int = MACHINE_SRC) -> bytes:
    """PGN 0xF0: what an ISOBUS TC reports to AOG; actual bit n = section n+1."""
    num_sections = max(0, min(255, num_sections))
    states = (actual & ((1 << num_sections) - 1)).to_bytes((num_sections + 7) // 8, "little")
    flags = (0x01 if sc_enabled else 0) | ((max(0, min(7, num_clients)) & 0x07) << 1)
    payload = bytes([flags, num_sections]) + states
    return _finish(bytearray([0x80, 0x81, src, PGN_ISOBUS_HEARTBEAT, len(payload)]) + payload)


def build_scan_reply(module_ip: tuple, agio_subnet: tuple, src: int = MACHINE_SRC) -> bytes:
    """PGN 203: 13 bytes, as Machine_UDP_v5 answers a scan."""
    return _finish(bytearray([0x80, 0x81, src, PGN_SCAN_REPLY, 7, *module_ip[:4], *agio_subnet[:3]]))


def build_switchbox(auto_on: bool, master_on: bool, switches: int) -> bytes:
    """PGN 32618 switch box for the Rate Controller app.

    byte 2 flags: bit0 Auto, bit1 MasterOn, bit2 MasterOff; 3-4 switches; 5 sum CRC.
    """
    flags = (0x01 if auto_on else 0) | (0x02 if master_on else 0x04)
    msg = bytearray(SWITCHBOX_HEADER + bytes([flags, switches & 0xFF, (switches >> 8) & 0xFF]))
    msg.append(sum(msg) & 0xFF)
    return bytes(msg)
