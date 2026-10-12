from teejet_bridge import aog, frames


def test_ids():
    assert frames.ID_CONSOLE_GLOBAL == 0x18EFFFC0
    assert frames.ID_RADION_TO_CONSOLE == 0x18EFC0B4
    assert frames.split_id(0x18EFC0B4) == (6, 0xEF00, 0xC0, 0xB4)
    assert frames.split_id(0x18F022C0) == (6, 0xF022, 0xFF, 0xC0)


def test_console_encoders():
    assert frames.heartbeat() == bytes.fromhex("0AFFFFFFFFFFFF")
    hb = frames.heartbeat(5364, 0x0026A2A5)
    assert hb == bytes.fromhex("0AF414A5A22600")          # from radion section toggle.trc
    assert frames.master(True) == b"\x02\x01"
    assert frames.master(False) == b"\x02\x00"
    assert frames.sections(0x7F) == bytes.fromhex("007F0000")
    assert frames.sections(0xABCDEF) == bytes.fromhex("00EFCDAB")
    assert frames.sections(1 << 30) == bytes.fromhex("00000000")
    assert frames.probe() == b"\x01"
    assert frames.mss(37330, 0x011DD100) == bytes.fromhex("D29100D11D01FFEB")  # 134_4km_h.trc


def test_decode_console():
    d = frames.decode(0x18EFFFC0, bytes.fromhex("0AF414A5A22600"))
    assert d.kind == frames.KIND_HEARTBEAT and d.fields["speed_mm_s"] == 5364
    d = frames.decode(0x18EFFFC0, bytes.fromhex("0AFFFFFFFFFFFF"))
    assert d.kind == frames.KIND_HEARTBEAT and d.fields["speed_mm_s"] is None
    d = frames.decode(0x18EFFFC0, bytes.fromhex("0AFFFFFFFFFFFFFF"))   # 8-byte variant
    assert d.kind == frames.KIND_HEARTBEAT
    assert frames.decode(0x18EFFFC0, b"\x02\x01").fields["master"] is True
    assert frames.decode(0x18EFFFC0, bytes.fromhex("007F0000")).fields["mask"] == 0x7F
    assert frames.decode(0x18EFFFC0, b"\x01").kind == frames.KIND_PROBE


def test_decode_radion_roundtrip():
    for m in (0, 1, 0x40, 0x7F, 0xFF):
        d = frames.decode(frames.ID_RADION_TO_CONSOLE, frames.radion_state(m))
        assert d.kind == frames.KIND_RADION_STATE and d.fields["mask"] == m and d.fields["b5"] == 0x80
    assert frames.radion_state(0x7E) == bytes.fromhex("007E0000 0080FF".replace(" ", ""))
    d = frames.decode(frames.ID_RADION_TO_CONSOLE, frames.radion_status(0))
    assert d.kind == frames.KIND_RADION_STATUS and d.fields["status"] == 0
    d = frames.decode(frames.ID_RADION_TO_CONSOLE, frames.RADION_PROBE_REPLY)
    assert d.kind == frames.KIND_RADION_PROBE_REPLY


def test_mask_str():
    assert frames.mask_str(0x7E) == ".234567."
    assert frames.mask_str(0, 3) == "..."


def test_aog_builders():
    hello = aog.build_hello_reply(0x7F)
    assert len(hello) == 11 and hello[3] == 123 and hello[5] == 0x7F       # AgIO: data[3]==123 && Length==11
    assert hello[-1] == sum(hello[2:-1]) & 0xFF
    ea = aog.build_section_switches(0x07, 0x0102, 0x0304)
    assert len(ea) == 14 and ea[3] == 0xEA                                  # AOG case 234: Length == 14
    assert ea[5] == 0x03                                                    # bit 2 cleared (button hardware)
    assert (ea[9], ea[10], ea[11], ea[12]) == (0x02, 0x04, 0x01, 0x03)
    ed = aog.build_from_machine(0x017E)
    assert ed[3] == 0xED and ed[5] == 0x7E and ed[6] == 0x01
    sb = aog.build_switchbox(True, False, 0x7E)
    assert sb[:2] == bytes([106, 127]) and sb[2] == 0x05 and sb[5] == sum(sb[:5]) & 0xFF


def test_aog_parse():
    p = aog.parse(aog.build_machine_data(0x0180, 12.3))
    assert p.pgn == 0xEF and p.section_mask == 0x0180 and p.carries_job and abs(p.speed_kmh - 12.3) < 0.051
    e5 = bytes([0x80, 0x81, 0x7F, 0xE5, 10, 0x7F, 0x01, 0x02, 0, 0, 0, 0, 0, 0, 0, 0])
    p = aog.parse(e5)
    assert p.pgn == 0xE5 and p.section_mask == 0x02017F and p.carries_job
    fe = bytes([0x80, 0x81, 0x7F, 0xFE, 8, 0x7B, 0x00, 0, 0, 0, 0, 0x03, 0, 0])
    p = aog.parse(fe)
    assert p.pgn == 0xFE and abs(p.speed_kmh - 12.3) < 1e-6 and not p.carries_job
    assert aog.parse(aog.build_hello(version=56)).agio_version == 56
    assert aog.parse(b"\x01\x02\x03\x04\x05") is None
