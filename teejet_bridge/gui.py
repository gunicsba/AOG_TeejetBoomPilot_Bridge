"""tkinter front-end: Main tab (connect + live state) and Settings tab.

Laid out to fit about 400 x 400 px. All visible text comes from lang/*.ini
via i18n.Translator.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import List, Optional

import can

from . import __version__, frames
from .bridge import normalize_subnet
from .i18n import Translator, ensure_user_dir
from .settings import DEFAULT_CONFIG, AppSettings, Session, app_dir, setup_logging

log = logging.getLogger(__name__)

INTERFACES = ["pcan", "socketcan", "gs_usb", "virtual"]

C_ON = "#2e9d3a"
C_OFF = "#d0d0d0"
C_UNKNOWN = "#707070"
C_BAD = "#c62828"
C_WARN = "#e69500"

# Everything is drawn at UI scale ([ui] scale in config.ini, default 1.5):
# fonts via font(), pixel sizes via px(). Set once in App.__init__.
SCALE = 1.0


def px(n: float) -> int:
    return int(round(n * SCALE))


def font(size: float, bold: bool = False, family: str = "Segoe UI"):
    f = (family, max(1, int(round(size * SCALE))))
    return f + ("bold",) if bold else f


def scale_named_fonts():
    import tkinter.font as tkfont
    for name in tkfont.names():
        f = tkfont.nametofont(name)
        size = int(f.cget("size"))
        if size:
            f.configure(size=int(round(size * SCALE)))


WRAP = 370          # px at scale 1; long labels wrap so the window stays narrow
BAR_WIDTH = 200     # px at scale 1 available for the section boxes


class QueueHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.q.put_nowait(self.format(record))
        except queue.Full:
            pass


class SectionBar(tk.Canvas):
    """Row of numbered squares, one per section; boxes shrink for many sections."""

    GAP = 3

    def __init__(self, master, count: int = 7):
        super().__init__(master, height=px(22), highlightthickness=0)
        self.boxes = []
        self.set_count(count)

    def set_count(self, count: int):
        gap = px(self.GAP)
        box = max(px(8), min(px(20), px(BAR_WIDTH) // max(count, 1) - gap))
        self.configure(width=count * (box + gap), height=box + 2)
        self.delete("all")
        self.boxes = []
        for i in range(count):
            x = i * (box + gap)
            r = self.create_rectangle(x, 1, x + box, box + 1, fill=C_UNKNOWN, outline="#555")
            if box >= px(14):
                self.create_text(x + box / 2, box / 2 + 1, text=str(i + 1), font=font(8, True))
            self.boxes.append(r)

    def show(self, mask: Optional[int]):
        for i, r in enumerate(self.boxes):
            c = C_UNKNOWN if mask is None else (C_ON if (mask >> i) & 1 else C_OFF)
            self.itemconfigure(r, fill=c)


class App:
    def __init__(self, root: tk.Tk, config_path: str = DEFAULT_CONFIG):
        self.root = root
        self.config_path = config_path
        self.settings = AppSettings.load(config_path)
        self.session: Optional[Session] = None
        self.log_q: "queue.Queue[str]" = queue.Queue(maxsize=5000)
        self.log_lines: List[str] = []
        logging.getLogger().addHandler(QueueHandler(self.log_q))

        lang_dir = os.path.join(app_dir(), "lang")
        ensure_user_dir(lang_dir)
        self.tr = Translator(lang_dir)
        self.tr.set_language(self.settings.language)

        global SCALE
        SCALE = self.settings.ui_scale
        scale_named_fonts()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.attributes("-topmost", self.settings.always_on_top)
        style = ttk.Style(root)
        style.configure("Big.TButton", font=font(10, True), padding=(px(8), px(2)))
        style.configure("Hint.TLabel", foreground="#666")
        self.channels: List[str] = []
        self.frame: Optional[ttk.Frame] = None
        self._build()
        self.detect_channels(quiet=True)
        self._tick()

    def t(self, key, **kw):
        return self.tr.t(key, **kw)

    # ------------------------------------------------------------------
    #  layout
    # ------------------------------------------------------------------
    def _build(self, tab_index: int = 0):
        if self.frame is not None:
            self.frame.destroy()
        self.root.title(f"{self.t('app.title')} {__version__}")
        pad = {"padx": px(3), "pady": px(1)}
        self.frame = ttk.Frame(self.root, padding=px(3))
        self.frame.pack(fill="both", expand=True)
        # The notebook only provides the tab strip; pages live below it and only
        # the selected one is packed, so the window fits the open tab (a ttk
        # Notebook would always size every tab to the largest one).
        self.nb = ttk.Notebook(self.frame)
        self.nb.pack(fill="x")
        self.nb.add(ttk.Frame(self.nb, height=1), text=self.t("tab.main"))
        self.nb.add(ttk.Frame(self.nb, height=1), text=self.t("tab.settings"))
        body = ttk.Frame(self.frame)
        body.pack(fill="both", expand=True)
        main = ttk.Frame(body, padding=px(4))
        sett = ttk.Frame(body, padding=px(4))
        self.pages = [main, sett]
        self._build_main(main, pad)
        self._build_settings(sett, pad)
        self.nb.select(tab_index)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._show_page(self.nb.index(self.nb.select())))
        self._show_page(tab_index)
        self._apply_connected_state()

    def _show_page(self, index: int):
        for i, page in enumerate(self.pages):
            if i == index:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        self._fit_window()

    def _fit_window(self):
        # forget any manual resize so the window grows/shrinks to its content
        self.root.geometry("")

    def _build_main(self, main, pad):
        top = ttk.Frame(main)
        top.pack(fill="x")
        self.b_connect = ttk.Button(top, style="Big.TButton", command=self.toggle_connect, width=11)
        self.b_connect.pack(side="left", padx=(0, px(4)))
        self.l_state = tk.Label(top, font=font(9, True), bg=C_OFF, relief="groove", pady=px(4))
        self.l_state.pack(side="left", fill="x", expand=True)
        self.l_counters = ttk.Label(main, text="", style="Hint.TLabel")
        self.l_counters.pack(anchor="w")

        live = ttk.LabelFrame(main, text=self.t("live.title"), padding=px(3))
        live.pack(fill="x", pady=(px(3), 0))
        # status boxes as wide as the longest text in the chosen language
        width = 1 + max(len(self.t(k)) for k in ("aog.job", "aog.no_job", "aog.no_data", "master.on",
                                                  "master.off", "radion.connected", "radion.lost"))

        def row(r, key):
            ttk.Label(live, text=self.t(key)).grid(row=r, column=0, sticky="w", **pad)
            bar = SectionBar(live, self.settings.bridge.sections)
            bar.grid(row=r, column=1, sticky="w", **pad)
            lab = tk.Label(live, width=width, bg=C_OFF, relief="groove")
            lab.grid(row=r, column=2, sticky="ew", **pad)
            return lab, bar

        self.l_aog, self.bar_aog = row(0, "live.aog")
        self.l_master, self.bar_cmd = row(1, "live.sent")
        self.l_radion, self.bar_act = row(2, "live.actual")
        live.columnconfigure(2, weight=1)

        # detail lines: only shown together with the log
        self.details = ttk.Frame(live)
        self.details.grid(row=3, column=0, columnspan=3, sticky="w", pady=(px(3), 0))
        self.l_aog_info = ttk.Label(self.details, text="", wraplength=px(WRAP))
        self.l_cmd_info = ttk.Label(self.details, text="", font=font(9, family="Consolas"), wraplength=px(WRAP))
        self.l_act_info = ttk.Label(self.details, text="", font=font(9, family="Consolas"), wraplength=px(WRAP))
        for lab in (self.l_aog_info, self.l_cmd_info, self.l_act_info):
            lab.pack(anchor="w")
        # warnings: always visible, empty in normal operation
        warn = ttk.Frame(live)
        warn.grid(row=4, column=0, columnspan=3, sticky="w")
        self.l_ignored = tk.Label(warn, text="", fg=C_BAD, wraplength=px(WRAP), justify="left")
        self.l_mismatch = tk.Label(warn, text="", fg=C_WARN, wraplength=px(WRAP), justify="left")
        self._warn_state = None

        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(px(3), 0))
        self.b_log = ttk.Button(bottom, command=self.toggle_log)
        self.b_log.pack(side="left")
        ttk.Button(bottom, text=self.t("main.open_logs"), command=self.open_logs).pack(side="left", padx=px(3))
        self.l_capture = tk.Label(bottom, text="", fg=C_BAD, font=font(9, True))
        self.l_capture.pack(side="left", padx=px(3))

        self.log_frame = ttk.LabelFrame(main, text=self.t("main.log"), padding=px(2))
        self.t_log = ScrolledText(self.log_frame, height=8, width=48, font=font(8, family="Consolas"), state="disabled")
        self.t_log.pack(fill="both", expand=True)
        if self.log_lines:
            self._append_log(self.log_lines[-1000:], remember=False)
        self._show_log(self.settings.show_log)

    def _build_settings(self, sett, pad):
        s = self.settings
        b = s.bridge
        self.conn_widgets: List[tk.Widget] = []   # disabled while connected

        can_f = ttk.LabelFrame(sett, text=self.t("settings.can"), padding=px(3))
        can_f.pack(fill="x")
        ttk.Label(can_f, text=self.t("settings.interface")).grid(row=0, column=0, sticky="w", **pad)
        self.v_iface = tk.StringVar(value=s.interface)
        cb = ttk.Combobox(can_f, textvariable=self.v_iface, values=INTERFACES, width=10, state="readonly")
        cb.grid(row=0, column=1, sticky="w", **pad)
        cb.bind("<<ComboboxSelected>>", self._iface_changed)
        ttk.Label(can_f, text=self.t("settings.bitrate_value"), style="Hint.TLabel").grid(
            row=0, column=2, sticky="w", **pad)
        ttk.Label(can_f, text=self.t("settings.channel")).grid(row=1, column=0, sticky="w", **pad)
        self.v_chan = tk.StringVar(value=s.channel)
        self.cb_chan = ttk.Combobox(can_f, textvariable=self.v_chan, values=self.channels, width=16)
        self.cb_chan.grid(row=1, column=1, sticky="w", **pad)
        self.cb_chan.bind("<<ComboboxSelected>>", lambda e: self._save())
        self.cb_chan.bind("<FocusOut>", lambda e: self._save())
        bd = ttk.Button(can_f, text=self.t("settings.detect"), command=self.detect_channels)
        bd.grid(row=1, column=2, sticky="w", **pad)
        self.conn_widgets += [cb, self.cb_chan, bd]

        aog_f = ttk.LabelFrame(sett, text=self.t("settings.network"), padding=px(3))
        aog_f.pack(fill="x", pady=(px(3), 0))
        ttk.Label(aog_f, text=self.t("settings.sections")).grid(row=0, column=0, sticky="w", **pad)
        sec = ttk.Frame(aog_f)
        sec.grid(row=0, column=1, sticky="w", **pad)
        bm = ttk.Button(sec, text="−", width=3, command=lambda: self.change_sections(-1))
        bm.pack(side="left")
        self.l_sections = ttk.Label(sec, text=str(b.sections), width=3, anchor="center", font=font(9, True))
        self.l_sections.pack(side="left")
        bp = ttk.Button(sec, text="+", width=3, command=lambda: self.change_sections(+1))
        bp.pack(side="left")
        ttk.Label(aog_f, text=self.t("settings.subnet")).grid(row=1, column=0, sticky="w", **pad)
        self.v_subnet = tk.StringVar(value=b.subnet)
        es = ttk.Entry(aog_f, textvariable=self.v_subnet, width=14)
        es.grid(row=1, column=1, sticky="w", **pad)
        es.bind("<FocusOut>", lambda e: self._save_subnet())
        es.bind("<Return>", lambda e: self._save_subnet())
        self.v_isobus = tk.BooleanVar(value=b.isobus)
        ci = ttk.Checkbutton(aog_f, text=self.t("settings.isobus"), variable=self.v_isobus, command=self._save)
        ci.grid(row=2, column=0, columnspan=2, sticky="w", **pad)
        self.conn_widgets += [bm, bp, es, ci]

        gen = ttk.LabelFrame(sett, text=self.t("settings.general"), padding=px(3))
        gen.pack(fill="x", pady=(px(3), 0))
        ttk.Label(gen, text=self.t("settings.language")).grid(row=0, column=0, sticky="w", **pad)
        langs = self.tr.languages()
        self._lang_codes = [c for c, _ in langs]
        self.v_lang = tk.StringVar(value=dict(langs).get(self.tr.code, self.tr.code))
        cl = ttk.Combobox(gen, textvariable=self.v_lang, values=[n for _, n in langs], width=14, state="readonly")
        cl.grid(row=0, column=1, sticky="w", **pad)
        cl.bind("<<ComboboxSelected>>", lambda e: self.change_language(self._lang_codes[cl.current()]))
        self.v_capture = tk.BooleanVar(value=s.capture)
        ttk.Checkbutton(gen, text=self.t("settings.capture"), variable=self.v_capture,
                        command=self.toggle_capture).grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        self.v_on_top = tk.BooleanVar(value=s.always_on_top)
        ttk.Checkbutton(gen, text=self.t("settings.on_top"), variable=self.v_on_top,
                        command=self.toggle_on_top).grid(row=2, column=0, columnspan=2, sticky="w", **pad)

        ttk.Label(sett, text=self.t("settings.reconnect_note"), style="Hint.TLabel",
                  wraplength=px(WRAP)).pack(anchor="w", pady=(px(3), 0))

        self.v_adv = tk.BooleanVar(value=s.show_advanced)
        ttk.Checkbutton(sett, text=self.t("settings.show_advanced"), variable=self.v_adv,
                        command=self.toggle_advanced).pack(anchor="w", pady=(px(2), 0))
        self.adv = ttk.LabelFrame(sett, text=self.t("settings.advanced"), padding=px(3))
        self.v_hb = tk.BooleanVar(value=self.session.bridge.console.heartbeat_on if self.session else True)
        self.cb_hb = ttk.Checkbutton(self.adv, text=self.t("adv.heartbeat"), variable=self.v_hb,
                                     command=self.toggle_heartbeat)
        self.cb_hb.pack(anchor="w")
        self.v_force_off = tk.BooleanVar(value=self.session.bridge.force_master_off if self.session else False)
        self.cb_force = ttk.Checkbutton(self.adv, text=self.t("adv.force_off"), variable=self.v_force_off,
                                        command=self.toggle_force_off)
        self.cb_force.pack(anchor="w")
        self.v_claim = tk.BooleanVar(value=b.claim)
        c1 = ttk.Checkbutton(self.adv, text=self.t("adv.claim"), variable=self.v_claim, command=self._save)
        c1.pack(anchor="w")
        self.v_mss = tk.BooleanVar(value=b.mss)
        c2 = ttk.Checkbutton(self.adv, text=self.t("adv.mss"), variable=self.v_mss, command=self._save)
        c2.pack(anchor="w")
        self.conn_widgets += [c1, c2]
        if s.show_advanced:
            self.adv.pack(fill="x", pady=(px(2), 0))

    # ------------------------------------------------------------------
    #  settings handling
    # ------------------------------------------------------------------
    def _save(self):
        s = self.settings
        s.interface = self.v_iface.get()
        s.channel = self.v_chan.get().strip()
        s.capture = self.v_capture.get()
        s.show_advanced = self.v_adv.get()
        if not self.session:   # bridge settings are frozen while connected
            b = s.bridge
            b.isobus = self.v_isobus.get()
            b.claim = self.v_claim.get()
            b.mss = self.v_mss.get()
        try:
            s.save(self.config_path)
        except OSError as e:
            log.warning("could not save %s: %s", self.config_path, e)

    def _save_subnet(self) -> bool:
        if self.session:
            return True
        try:
            self.settings.bridge.subnet = normalize_subnet(self.v_subnet.get())
        except ValueError as e:
            messagebox.showerror(self.t("msg.settings"), self.t("msg.invalid", err=e))
            self.v_subnet.set(self.settings.bridge.subnet)
            return False
        self.v_subnet.set(self.settings.bridge.subnet)
        self._save()
        return True

    def change_sections(self, delta: int):
        b = self.settings.bridge
        n = max(1, min(frames.MAX_SECTIONS, b.sections + delta))
        if n == b.sections:
            return
        identity = b.remap == list(range(1, b.sections + 1))
        b.sections = n
        if identity or len(b.remap) != n:
            b.remap = list(range(1, n + 1))
        self.l_sections.configure(text=str(n))
        for bar in (self.bar_aog, self.bar_cmd, self.bar_act):
            bar.set_count(n)
        self._save()

    def change_language(self, code: str):
        self.settings.language = code
        self.tr.set_language(code)
        self._save()
        tab = self.nb.index(self.nb.select())
        self._build(tab)

    def _iface_changed(self, _e=None):
        self.v_chan.set("")
        self.detect_channels(quiet=True)
        self._save()

    def detect_channels(self, quiet: bool = False):
        iface = self.v_iface.get() or "pcan"
        try:
            cfgs = can.detect_available_configs(interfaces=[iface])
        except Exception as e:  # driver missing etc.
            cfgs = []
            if not quiet:
                log.warning("channel detection failed for %s: %s", iface, e)
        chans = [str(c.get("channel")) for c in cfgs if c.get("channel") is not None]
        if iface == "virtual":
            chans = ["teejet"]
        self.channels = chans
        self.cb_chan.configure(values=chans)
        # keep a saved channel even if its adapter is unplugged right now
        if chans and not self.v_chan.get().strip():
            self.v_chan.set(chans[0])
            self._save()
        if not quiet:
            log.info("%s: %s", iface, ", ".join(chans) if chans else self.t("msg.channels_none"))

    # ------------------------------------------------------------------
    #  actions
    # ------------------------------------------------------------------
    def toggle_connect(self):
        if self.session:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        if not self._save_subnet():
            return
        self._save()
        try:
            session = Session(self.settings, self.config_path)
            session.start()
        except Exception as e:
            log.error("connect failed: %s", e)
            messagebox.showerror(self.t("msg.connect_failed"), str(e))
            return
        self.session = session
        self.v_hb.set(True)
        self.v_force_off.set(False)
        self._apply_connected_state()

    def disconnect(self):
        session, self.session = self.session, None
        if session:
            try:
                session.stop()
            except Exception as e:
                log.error("disconnect: %s", e)
        self._apply_connected_state()

    def _apply_connected_state(self):
        connected = self.session is not None
        self.b_connect.configure(text=self.t("main.disconnect") if connected else self.t("main.connect"))
        for w in self.conn_widgets:
            if isinstance(w, ttk.Combobox) and w is not self.cb_chan:
                w.configure(state="disabled" if connected else "readonly")
            else:
                w.configure(state="disabled" if connected else "normal")
        for w in (self.cb_hb, self.cb_force):
            w.configure(state="normal" if connected else "disabled")

    def toggle_heartbeat(self):
        if self.session:
            self.session.bridge.console.set_heartbeat(self.v_hb.get())

    def toggle_force_off(self):
        if self.session:
            self.session.bridge.set_force_master_off(self.v_force_off.get())

    def toggle_on_top(self):
        self.settings.always_on_top = self.v_on_top.get()
        self.root.attributes("-topmost", self.settings.always_on_top)
        self._save()

    def toggle_capture(self):
        self._save()
        if self.session:
            self.session.recorder.set_enabled(self.v_capture.get())

    def toggle_advanced(self):
        self._save()
        if self.v_adv.get():
            self.adv.pack(fill="x", pady=(px(2), 0))
        else:
            self.adv.pack_forget()
        self._fit_window()

    def toggle_log(self):
        self.settings.show_log = not self.settings.show_log
        self._show_log(self.settings.show_log)
        self._save()

    def _show_log(self, show: bool):
        self.b_log.configure(text=self.t("main.hide_log") if show else self.t("main.show_log"))
        if show:
            self.details.grid()
            self.log_frame.pack(fill="both", expand=True, pady=(px(3), 0))
        else:
            self.details.grid_remove()
            self.log_frame.pack_forget()
        self._fit_window()

    def open_logs(self):
        path = self.settings.log_path
        os.makedirs(path, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)
        else:
            import subprocess
            subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", path])

    def on_close(self):
        self.disconnect()
        self.root.destroy()

    # ------------------------------------------------------------------
    #  refresh
    # ------------------------------------------------------------------
    def _tick(self):
        self._drain_log()
        self._refresh()
        self.root.after(150, self._tick)

    def _drain_log(self):
        lines = []
        try:
            while len(lines) < 200:
                lines.append(self.log_q.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self._append_log(lines)

    def _append_log(self, lines, remember: bool = True):
        if remember:
            self.log_lines.extend(lines)
            del self.log_lines[:-1000]
        self.t_log.configure(state="normal")
        self.t_log.insert("end", "\n".join(lines) + "\n")
        n = int(self.t_log.index("end-1c").split(".")[0])
        if n > 1000:
            self.t_log.delete("1.0", f"{n - 1000}.0")
        self.t_log.see("end")
        self.t_log.configure(state="disabled")

    def _age(self, now, t):
        return self.t("age.never") if t <= 0 else self.t("age.ago", s=f"{now - t:.1f}")

    def _refresh(self):
        s = self.session
        self.l_capture.configure(text=("● " + self.t("main.capturing"))
                                 if s and s.recorder.enabled else "")
        if not s:
            self.l_state.configure(text=self.t("main.disconnected"), bg=C_OFF)
            self.l_counters.configure(text="")
            for lab in (self.l_aog, self.l_master, self.l_radion):
                lab.configure(text="-", bg=C_OFF)
            for bar in (self.bar_aog, self.bar_cmd, self.bar_act):
                bar.show(None)
            for lab in (self.l_aog_info, self.l_cmd_info, self.l_act_info):
                lab.configure(text="")
            self._set_warnings("", "")
            return

        b, io = s.bridge, s.io
        now = b.clock()
        if io.state in ("OK", "WARNING"):
            self.l_state.configure(text=self.t("main.connected", state=io.state),
                                   bg=C_ON if io.state == "OK" else C_WARN)
        else:
            self.l_state.configure(text=self.t("main.bus_problem", state=io.state), bg=C_BAD)
        self.l_counters.configure(text=self.t("main.counters", tx=io.tx_count, rx=io.rx_count,
                                              txerr=io.tx_errors, ef=io.error_frames))

        if b.aog_alive:
            self.l_aog.configure(text=self.t("aog.job"), bg=C_ON)
        elif b.agio_version is not None and now - b.last_aog_any < 3:
            self.l_aog.configure(text=self.t("aog.no_job"), bg=C_WARN)
        else:
            self.l_aog.configure(text=self.t("aog.no_data"), bg=C_BAD)
        self.bar_aog.show(b.aog_mask if b.aog_alive else None)
        if self.v_subnet.get() != b.cfg.subnet:      # changed by AgIO (PGN 201)
            self.v_subnet.set(b.cfg.subnet)
        spd = f"{b.aog_speed_kmh:.1f} km/h" if b.aog_speed_kmh is not None else "-"
        src = b.agio_source or f"{b.cfg.subnet}.x"
        self.l_aog_info.configure(text=self.t("info.aog", source=src, speed=spd, age=self._age(now, b.last_aog_job)))
        ignored = self.t("aog.ignored", ips=", ".join(sorted(b.ignored_sources))) if b.ignored_sources else ""

        master, cmd = b.console.snapshot()
        self.l_master.configure(text=self.t("master.on") if master else self.t("master.off"),
                                bg=C_ON if master else C_OFF)
        self.bar_cmd.show(b.radion_to_aog(cmd))
        extra = ""
        if b.cfg.isobus:
            extra += "  " + (self.t("sent.iso_on") if b.isobus_sc_enabled else self.t("sent.iso_off"))
        if not b.console.heartbeat_on:
            extra += "  " + self.t("sent.hb_stopped")
        self.l_cmd_info.configure(text=self.t("sent.info", b1=f"{cmd & 0xFF:02X}", b2=f"{(cmd >> 8) & 0xFF:02X}",
                                              b3=f"{(cmd >> 16) & 0xFF:02X}", extra=extra))

        r = b.radion
        up = r.is_up(now)
        self.l_radion.configure(text=self.t("radion.connected") if up else self.t("radion.lost"),
                                bg=C_ON if up else C_BAD)
        self.bar_act.show(b.actual_aog_mask() if up else None)
        self.l_act_info.configure(text=self.t(
            "info.radion",
            ss=f"{r.actual_mask:02X}" if up and r.actual_mask is not None else "??",
            status=f"{r.status:02X}" if r.status is not None else "-",
            ident=r.ident.hex(" ").upper() if r.ident else "-",
            extra=" ".join(f"{x:02X}" for x in r.extra) if r.extra else "-",
            age=self._age(now, r.last_rx)))
        mismatch = up and r.actual_mask is not None and master and (cmd & 0xFF) != r.actual_mask
        self._set_warnings(ignored, self.t("radion.mismatch") if mismatch else "")

    def _set_warnings(self, ignored: str, mismatch: str):
        """Warning lines take no space while empty; refit the window when they change."""
        state = (ignored, mismatch)
        if state == self._warn_state:
            return
        shown_before = self._warn_state is not None and tuple(bool(x) for x in self._warn_state)
        self._warn_state = state
        for lab, text in ((self.l_ignored, ignored), (self.l_mismatch, mismatch)):
            lab.configure(text=text)
            lab.pack_forget()
        for lab, text in ((self.l_ignored, ignored), (self.l_mismatch, mismatch)):
            if text:
                lab.pack(anchor="w")
        if shown_before != tuple(bool(x) for x in state):
            self._fit_window()


def enable_dpi_awareness():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def run_gui(config_path: str = DEFAULT_CONFIG, verbose: bool = False):
    enable_dpi_awareness()
    st = AppSettings.load(config_path)
    setup_logging(st.log_path, verbose, console=False)
    root = tk.Tk()
    App(root, config_path)
    root.mainloop()
