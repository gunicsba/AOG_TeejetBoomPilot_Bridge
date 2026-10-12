"""Command line: ``gui`` (default), ``run`` (headless bridge), ``decode``."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import time

from . import __version__
from .settings import DEFAULT_CONFIG, AppSettings, Session, parse_remap, setup_logging

log = logging.getLogger("teejet_bridge")


def _add_bridge_args(p: argparse.ArgumentParser):
    g = p.add_argument_group("CAN")
    g.add_argument("--interface", help="python-can interface: pcan, socketcan, gs_usb, virtual")
    g.add_argument("--channel", help="e.g. PCAN_USBBUS1, can0, 0")
    g.add_argument("--bitrate", type=int)
    g.add_argument("--capture", action="store_true", help="record Wireshark .pcapng + PCAN-View .trc")
    g = p.add_argument_group("Radion")
    g.add_argument("--sections", type=int, help="number of sections (default 7)")
    g.add_argument("--remap", help="Radion section for each AOG section, e.g. 7,6,5,4,3,2,1")
    g.add_argument("--master-mode", choices=["job", "mask"])
    g.add_argument("--claim", action="store_true", default=None, help="send C0 address claim")
    g.add_argument("--mss", action="store_true", default=None, help="send Machine Selected Speed 18F022C0")
    g.add_argument("--hb-speed", action="store_true", default=None, help="put AOG speed in the 0x0A heartbeat")
    g.add_argument("--no-probe", dest="probe", action="store_false", default=None)
    g = p.add_argument_group("AgOpenGPS")
    g.add_argument("--feedback", choices=["feedback", "override"])
    g.add_argument("--rc-switchbox", action="store_true", default=None, help="also send PGN 32618")
    g.add_argument("--listen-port", type=int)
    g.add_argument("--send-port", type=int)
    g.add_argument("--broadcast")
    g.add_argument("--subnet", help="module subnet, only AgIO in it is obeyed (default 192.168.5)")
    g.add_argument("--no-isobus", dest="isobus", action="store_false", default=None,
                   help="do not send the 0xF0 ISOBUS heartbeat with the actual sections")
    g.add_argument("--aog-timeout", type=float)


def _apply_args(st: AppSettings, a: argparse.Namespace) -> AppSettings:
    if a.interface is not None and a.interface != st.interface and a.channel is None:
        from .canio import DEFAULT_CHANNELS
        st.channel = DEFAULT_CHANNELS.get(a.interface, "0")
    for k in ("interface", "channel", "bitrate"):
        if getattr(a, k) is not None:
            setattr(st, k, getattr(a, k))
    upd = {}
    for k in ("sections", "master_mode", "claim", "mss", "hb_speed", "probe", "feedback", "rc_switchbox",
              "listen_port", "send_port", "broadcast", "subnet", "isobus", "aog_timeout"):
        v = getattr(a, k, None)
        if v is not None:
            upd[k] = v
    if a.remap is not None:
        upd["remap"] = parse_remap(a.remap)
    elif "sections" in upd and upd["sections"] != st.bridge.sections:
        upd["remap"] = None
    if upd:
        st.bridge = dataclasses.replace(st.bridge, **upd)
    return st


def cmd_run(a) -> int:
    st = _apply_args(AppSettings.load(a.config), a)
    if a.capture:
        st.capture = True
    setup_logging(st.log_path, a.verbose, console=True)
    log.info("teejet-bridge %s headless: %s", __version__, st)
    session = Session(st, a.config)
    session.start()
    tty = sys.stdout.isatty()
    last_print = 0.0
    try:
        while True:
            time.sleep(0.25)
            line = session.bridge.status_line()
            if tty:
                sys.stdout.write("\r" + line[:200] + "  ")
                sys.stdout.flush()
            elif time.monotonic() - last_print > 5:
                last_print = time.monotonic()
                log.info(line)
    except KeyboardInterrupt:
        print()
        log.info("Ctrl-C")
    finally:
        session.stop()
    return 0


def cmd_decode(a) -> int:
    from .decode import decode_file
    for path in a.files:
        decode_file(path, sys.stdout, show_frames=not a.summary, only_teejet=a.teejet_only)
    return 0


def cmd_gui(a) -> int:
    from .gui import run_gui
    run_gui(a.config, a.verbose)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="teejet-bridge", description="AgOpenGPS <-> TeeJet Radion 8140 bridge")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", default=DEFAULT_CONFIG, help="settings file (default: config.ini)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging (every frame)")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("gui", help="window with CAN connection and Radion state (default)").set_defaults(fn=cmd_gui)

    r = sub.add_parser("run", help="headless bridge with a status line")
    _add_bridge_args(r)
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("decode", help="annotate PCAN-View .trc files")
    d.add_argument("files", nargs="+")
    d.add_argument("--summary", action="store_true", help="rates and mask changes only")
    d.add_argument("--teejet-only", action="store_true", help="hide unrelated frames")
    d.set_defaults(fn=cmd_decode)

    a = p.parse_args(argv)
    return (a.fn if a.cmd else cmd_gui)(a)


if __name__ == "__main__":
    raise SystemExit(main())
