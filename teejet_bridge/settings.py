"""Persistent settings (config.ini next to the app) and one bridge session."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from configparser import ConfigParser
from dataclasses import dataclass, field, fields
from typing import Optional

from .bridge import Bridge, BridgeConfig
from .canio import CanIO, open_bus
from .capture import Recorder
from .scheduler import Scheduler

log = logging.getLogger(__name__)


def app_dir() -> str:
    """Directory of the exe when frozen, otherwise the current directory."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.getcwd()


DEFAULT_CONFIG = os.path.join(app_dir(), "config.ini")


@dataclass
class AppSettings:
    interface: str = "pcan"
    channel: str = "PCAN_USBBUS1"
    bitrate: int = 250_000            # fixed for the Radion; kept in config.ini for experts
    log_dir: str = "logs"
    capture: bool = False             # Wireshark .pcapng + PCAN-View .trc
    language: str = ""                # "" = from the system locale
    show_log: bool = False
    show_advanced: bool = False
    ui_scale: float = 1.5             # fonts and sizes of the window
    bridge: BridgeConfig = field(default_factory=BridgeConfig)

    @property
    def log_path(self) -> str:
        return self.log_dir if os.path.isabs(self.log_dir) else os.path.join(app_dir(), self.log_dir)

    # ---- ini ----
    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG) -> "AppSettings":
        s = cls()
        cp = ConfigParser()
        if not os.path.exists(path):
            return s
        cp.read(path)
        if cp.has_section("can"):
            c = cp["can"]
            s.interface = c.get("interface", s.interface)
            s.channel = c.get("channel", s.channel)
            s.bitrate = c.getint("bitrate", s.bitrate)
        if cp.has_section("log"):
            s.log_dir = cp["log"].get("dir", s.log_dir)
            s.capture = cp["log"].getboolean("capture", s.capture)
        if cp.has_section("ui"):
            u = cp["ui"]
            s.language = u.get("language", s.language)
            s.show_log = u.getboolean("show_log", s.show_log)
            s.show_advanced = u.getboolean("show_advanced", s.show_advanced)
            s.ui_scale = min(4.0, max(0.5, u.getfloat("scale", s.ui_scale)))
        if cp.has_section("bridge"):
            b = cp["bridge"]
            kw = {}
            for f in fields(BridgeConfig):
                if f.name not in b:
                    continue
                raw = b[f.name]
                if f.name == "remap":
                    kw[f.name] = parse_remap(raw)
                elif f.type in ("bool", bool):
                    kw[f.name] = b.getboolean(f.name)
                elif f.type in ("int", int):
                    kw[f.name] = int(raw, 0)
                elif f.type in ("float", float):
                    kw[f.name] = float(raw)
                else:
                    kw[f.name] = raw
            if kw.get("broadcast") == "255.255.255.255":
                kw["broadcast"] = ""          # 0.1.x default; now <subnet>.255 like real modules
            if "sections" in kw and "remap" not in kw:
                kw["remap"] = None
            s.bridge = BridgeConfig(**kw)
        return s

    def save(self, path: str = DEFAULT_CONFIG):
        cp = ConfigParser()
        cp["can"] = {"interface": self.interface, "channel": self.channel, "bitrate": str(self.bitrate)}
        cp["log"] = {"dir": self.log_dir, "capture": "1" if self.capture else "0"}
        cp["ui"] = {"language": self.language, "show_log": "1" if self.show_log else "0",
                    "show_advanced": "1" if self.show_advanced else "0", "scale": str(self.ui_scale)}
        b = {}
        for f in fields(BridgeConfig):
            v = getattr(self.bridge, f.name)
            if f.name == "remap":
                v = ",".join(str(x) for x in v)
            elif isinstance(v, bool):
                v = "1" if v else "0"
            b[f.name] = str(v)
        cp["bridge"] = b
        with open(path, "w") as fh:
            cp.write(fh)


def parse_remap(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    return [int(x) for x in raw.replace(";", ",").split(",") if x.strip()]


def setup_logging(log_dir: str, verbose: bool = False, console: bool = True) -> str:
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "teejet-bridge.log")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)
    logging.getLogger("can").setLevel(logging.ERROR)   # silence "uptime library not available"
    return path


class Session:
    """One connected bridge: CAN bus + capture + scheduler + AOG UDP."""

    def __init__(self, settings: AppSettings, config_path: Optional[str] = None):
        self.settings = settings
        self.config_path = config_path
        self.recorder = Recorder(settings.log_path)
        self.bus = open_bus(settings.interface, settings.channel, settings.bitrate)
        try:
            self.io = CanIO(self.bus, self.recorder)
            self.sched = Scheduler()
            self.bridge = Bridge(settings.bridge, self.io, self.sched)
            self.bridge.recorder = self.recorder
            self.bridge.on_subnet_change = self._save_subnet
        except Exception:
            self.bus.shutdown()
            raise
        self.recorder.set_enabled(settings.capture)

    def _save_subnet(self, subnet: str):
        # bridge.cfg is settings.bridge, already updated; persist like the module EEPROM
        if self.config_path:
            self.settings.save(self.config_path)
            log.info("subnet %s saved to %s", subnet, self.config_path)

    def start(self):
        self.bridge.start()
        log.info("bridge running: %s %s @ %d bit/s", self.settings.interface, self.settings.channel,
                 self.settings.bitrate)

    def stop(self):
        try:
            self.bridge.stop()
        finally:
            self.bus.shutdown()
            self.recorder.close()
            log.info("bridge stopped, CAN closed")
