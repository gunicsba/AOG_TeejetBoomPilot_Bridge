"""UI translations from plain ``key = text`` INI files.

Bundled files live in ``teejet_bridge/lang``. On start they are copied (once,
never overwritten) to a ``lang`` folder next to the app, where users can edit
them or add a new language: copy en.ini to e.g. de.ini, set ``[meta] name``,
translate the values, restart. Keys missing from a file fall back to English.
"""

from __future__ import annotations

import glob
import hashlib
import json
import locale
import logging
import os
import shutil
import sys
from configparser import ConfigParser
from typing import Dict, List, Tuple

log = logging.getLogger(__name__)

FALLBACK = "en"
README = """Translations for the TeeJet Radion BoomPilot Bridge.

Each <code>.ini file is one language, e.g. en.ini, hu.ini.
To add a language: copy en.ini to a new name (e.g. de.ini), change "name" under
[meta], translate the text after each "=", save as UTF-8 and restart the app.
Keep {placeholders} such as {age} or {ip} unchanged.
Missing lines fall back to English.
"""


def bundled_dir() -> str:
    # PyInstaller sets __file__ under sys._MEIPASS too (data added with
    # --add-data teejet_bridge/lang;teejet_bridge/lang)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "lang")


def _read(path: str) -> Tuple[str, Dict[str, str]]:
    cp = ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(path, encoding="utf-8")
    name = cp.get("meta", "name", fallback=os.path.splitext(os.path.basename(path))[0])
    strings = dict(cp["strings"]) if cp.has_section("strings") else {}
    return name, strings


def _sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def ensure_user_dir(user_dir: str):
    """Copy bundled languages next to the app so people can see and edit them.

    Files the app wrote and nobody edited since (fingerprint in .shipped.json)
    are refreshed when a new version ships different text. Edited files are
    kept; their missing keys fall back to the bundled text. Files from before
    the fingerprint existed are replaced, keeping the old copy as *.ini.old.
    """
    try:
        os.makedirs(user_dir, exist_ok=True)
        record_path = os.path.join(user_dir, ".shipped.json")
        try:
            with open(record_path, encoding="utf-8") as f:
                shipped = json.load(f)
        except (OSError, ValueError):
            shipped = {}
        for src in glob.glob(os.path.join(bundled_dir(), "*.ini")):
            name = os.path.basename(src)
            dst = os.path.join(user_dir, name)
            new = _sha(src)
            if os.path.exists(dst):
                cur = _sha(dst)
                if cur == new:
                    shipped[name] = new
                    continue
                if name in shipped and cur != shipped[name]:
                    continue                      # edited by the user: keep it
                if name not in shipped:
                    shutil.copyfile(dst, dst + ".old")
                log.info("updating %s to the new built-in text", dst)
            shutil.copyfile(src, dst)
            shipped[name] = new
        with open(record_path, "w", encoding="utf-8") as f:
            json.dump(shipped, f, indent=1)
        readme = os.path.join(user_dir, "README.txt")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8") as f:
                f.write(README)
    except OSError as e:
        log.warning("could not prepare %s: %s", user_dir, e)


def system_language() -> str:
    try:
        if sys.platform == "win32":
            import ctypes
            code = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), "")
        else:
            code = (locale.getlocale()[0] or "")
        return code.split("_")[0].lower() or FALLBACK
    except Exception:
        return FALLBACK


class Translator:
    def __init__(self, user_dir: str = ""):
        self.names: Dict[str, str] = {}
        self.tables: Dict[str, Dict[str, str]] = {}
        dirs = [bundled_dir()] + ([user_dir] if user_dir else [])
        for d in dirs:                      # user files override bundled ones key by key
            for path in sorted(glob.glob(os.path.join(d, "*.ini"))):
                code = os.path.splitext(os.path.basename(path))[0].lower()
                try:
                    name, strings = _read(path)
                except Exception as e:
                    log.warning("bad language file %s: %s", path, e)
                    continue
                self.names[code] = name
                self.tables.setdefault(code, {}).update(strings)
        self.code = FALLBACK

    def languages(self) -> List[Tuple[str, str]]:
        """[(code, display name)] sorted by name."""
        return sorted(self.names.items(), key=lambda kv: kv[1].lower())

    def set_language(self, code: str):
        code = (code or "").lower()
        self.code = code if code in self.tables else (system_language() if not code else FALLBACK)
        if self.code not in self.tables:
            self.code = FALLBACK

    def t(self, key: str, **kw) -> str:
        text = self.tables.get(self.code, {}).get(key) or self.tables.get(FALLBACK, {}).get(key) or key
        if kw:
            try:
                return text.format(**kw)
            except (KeyError, IndexError, ValueError):
                return text
        return text
