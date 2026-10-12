import io
import os

import pytest

from teejet_bridge.decode import decode_file, mask_changes, sequence
from teejet_bridge.trc import TrcWriter, read_trc

TRACES = os.path.join(os.path.dirname(__file__), "..", "traces")


def trace(name):
    p = os.path.join(TRACES, name)
    if not os.path.exists(p):
        pytest.skip(f"fixture {name} missing")
    return p


def test_master_on_section1_sequence():
    ch = mask_changes(read_trc(trace("radion section master on then 1 off and on then master off.trc")))
    assert sequence(ch, "radion") == [0x00, 0x7F, 0x7E, 0x7F, 0x00]
    assert sequence(ch, "console-master") == [1]
    assert sequence(ch, "console-mask") == [0x7F]


def test_toggle_sequence():
    ch = mask_changes(read_trc(trace("radion section toggle.trc")))
    assert sequence(ch, "radion") == [0x00,
                                      0x7F, 0x7E, 0x7C, 0x78, 0x70, 0x60, 0x40, 0x00,
                                      0x40, 0x60, 0x70, 0x78, 0x7C, 0x7E, 0x7F,
                                      0x00]


def test_matrix_auto_mode_radion_stays_closed():
    # console commands master ON + 7F, Radion keeps reporting 00 (its own master/switches off)
    ch = mask_changes(read_trc(trace("teejet auto mode all sections ON.trc")))
    assert sequence(ch, "console-master") == [0, 1, 0]
    assert sequence(ch, "console-mask") == [0x00, 0x7F, 0x00]
    assert sequence(ch, "radion") == [0x00]


def test_decode_file_output():
    out = io.StringIO()
    decode_file(trace("radion section toggle.trc"), out)
    text = out.getvalue()
    assert "RADION actual=0x7E" in text
    assert "console MASTER ON" in text
    assert "00 -> 7F -> 7E" in text


def test_trc_writer_roundtrip_and_rotation(tmp_path):
    t = [0.0]
    w = TrcWriter(str(tmp_path / "x.trc"), max_bytes=1500, backup_count=2, clock=lambda: t[0])
    for i in range(60):
        t[0] = i * 0.1
        w.write("Tx" if i % 2 else "Rx", 0x18EFFFC0, bytes([0, i & 0xFF, 0, 0]))
    w.close()
    files = sorted(os.listdir(tmp_path))
    assert "x.trc" in files and "x.1.trc" in files and "x.3.trc" not in files
    frames = list(read_trc(str(tmp_path / "x.trc")))
    assert frames and frames[0].num == 1 and frames[-1].data[1] == 59
    assert frames[-1].can_id == 0x18EFFFC0 and frames[-1].direction == "Tx"
    with open(tmp_path / "x.1.trc") as f:
        assert f.readline().startswith(";$FILEVERSION=1.1")
