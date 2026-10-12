import os
import re
import shutil
import struct
import subprocess

import pytest

from teejet_bridge import aog, frames
from teejet_bridge.capture import Recorder, ipv4_udp, socketcan_frame
from teejet_bridge.i18n import Translator, bundled_dir, ensure_user_dir

PLACEHOLDER = re.compile(r"\{(\w+)")


def test_languages_complete_and_placeholders_match():
    tr = Translator()
    en = tr.tables["en"]
    assert "hu" in tr.tables
    for code, table in tr.tables.items():
        missing = set(en) - set(table)
        assert not missing, f"{code} missing {sorted(missing)}"
        for key, text in table.items():
            assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(en[key])), (code, key)


def test_translator_fallback_and_user_override(tmp_path):
    ensure_user_dir(str(tmp_path))
    assert (tmp_path / "en.ini").exists() and (tmp_path / "README.txt").exists()
    (tmp_path / "de.ini").write_text("[meta]\nname = Deutsch\n[strings]\nmain.connect = Verbinden\n",
                                     encoding="utf-8")
    tr = Translator(str(tmp_path))
    assert ("de", "Deutsch") in tr.languages()
    tr.set_language("de")
    assert tr.t("main.connect") == "Verbinden"
    assert tr.t("main.disconnect") == "Disconnect"        # falls back to English
    assert tr.t("age.ago", s="1.5") == "1.5 s ago"
    tr.set_language("xx")
    assert tr.code == "en"


def test_socketcan_and_ipv4_encoding():
    f = socketcan_frame(0x18EFC0B4, bytes.fromhex("007F0000 0080FF".replace(" ", "")))
    assert len(f) == 16 and struct.unpack("!I", f[:4])[0] == 0x18EFC0B4 | 0x80000000 and f[4] == 7
    pkt = ipv4_udp(("192.168.5.13", 9999), ("192.168.5.255", 8888), b"\x80\x81")
    assert len(pkt) == 30 and pkt[9] == 17 and pkt[12:16] == bytes([192, 168, 5, 13])


TSHARK = shutil.which("tshark") or r"C:\Program Files\Wireshark\tshark.exe"


def test_recorder_writes_valid_pcapng(tmp_path):
    rec = Recorder(str(tmp_path))
    rec.write("Tx", frames.ID_CONSOLE_GLOBAL, frames.heartbeat())    # disabled: ignored
    rec.set_enabled(True)
    rec.write("Tx", frames.ID_CONSOLE_GLOBAL, frames.sections(0x7F))
    rec.write("Rx", frames.ID_RADION_TO_CONSOLE, frames.radion_state(0x7E))
    rec.udp("Rx", ("192.168.5.13", 9999), ("192.168.5.255", 8888), aog.build_machine_data(0x7F))
    rec.udp("Tx", ("192.168.5.13", 8888), ("192.168.5.255", 9999), aog.build_isobus_heartbeat(True, 1, 7, 0x7E))
    path = rec.pcap_path
    rec.close()
    files = os.listdir(tmp_path)
    assert any(f.endswith(".pcapng") for f in files) and any(f.endswith(".trc") for f in files)
    data = open(path, "rb").read()
    assert data[:4] == b"\x0a\x0d\x0d\x0a"
    if not os.path.exists(TSHARK):
        pytest.skip("tshark not installed")
    out = subprocess.run([TSHARK, "-r", path, "-T", "fields", "-e", "frame.interface_id",
                          "-e", "can.id", "-e", "udp.dstport", "-e", "frame.len"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    rows = [l.split("\t") for l in out.stdout.strip().splitlines()]
    assert len(rows) == 4
    assert rows[0][0] == "0" and int(rows[0][1], 0) == 0x18EFFFC0
    assert rows[2][0] == "1" and rows[2][2] == "8888" and rows[3][2] == "9999"


def test_language_files_refresh_unless_edited(tmp_path):
    import json
    d = str(tmp_path)
    # a stale copy from before fingerprints existed: replaced, old kept
    (tmp_path / "en.ini").write_text("[meta]\nname = English\n[strings]\nlive.sent = Sent to Radion\n",
                                     encoding="utf-8")
    ensure_user_dir(d)
    assert (tmp_path / "en.ini.old").exists()
    assert Translator(d).t("live.sent") == Translator().t("live.sent")
    shipped = json.loads((tmp_path / ".shipped.json").read_text(encoding="utf-8"))
    assert set(shipped) >= {"en.ini", "hu.ini"}
    # the user edits hu.ini: kept on the next start
    (tmp_path / "hu.ini").write_text("[meta]\nname = Magyar\n[strings]\nmain.connect = Mehet\n",
                                     encoding="utf-8")
    ensure_user_dir(d)
    tr = Translator(d)
    tr.set_language("hu")
    assert tr.t("main.connect") == "Mehet"
    assert tr.t("main.disconnect") == Translator().tables["hu"]["main.disconnect"]  # new keys still there
    # an unmodified copy that is outdated is refreshed silently
    shipped["en.ini"] = "0" * 64
    (tmp_path / ".shipped.json").write_text(json.dumps(shipped), encoding="utf-8")
    (tmp_path / "en.ini").write_text("x", encoding="utf-8")
    shipped["en.ini"] = __import__("hashlib").sha256(b"x").hexdigest()
    (tmp_path / ".shipped.json").write_text(json.dumps(shipped), encoding="utf-8")
    ensure_user_dir(d)
    assert "live.sent" in (tmp_path / "en.ini").read_text(encoding="utf-8")
