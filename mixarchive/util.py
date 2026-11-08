"""Shared helpers: settings, paths, names, timestamps."""
import functools
import glob
import json
import os
import re
import shutil
import sys
import time
import unicodedata

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
STATE = "_mixarchive"          # per-collection state folder name
AUDIO_EXTS = (".opus", ".m4a", ".mp3", ".flac", ".wav")
BIN, EXE = ("Scripts", ".exe") if os.name == "nt" else ("bin", "")   # venv layout
NO_WINDOW = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)


def _data_dir():
    """Where settings.json, library.json, cache\\ and tools\\ live: $MIXARCHIVE_HOME if set;
    else the clone, when running from one (it has pyproject.toml, or its own settings /
    library already); else the user's app-data folder (an install from PyPI, where the
    code's folder is Python's own)."""
    if os.environ.get("MIXARCHIVE_HOME"):
        home = os.path.abspath(os.path.expanduser(os.environ["MIXARCHIVE_HOME"]))
        os.environ["MIXARCHIVE_HOME"] = home    # commands the GUI starts elsewhere get the same
        return home
    checkout = os.path.dirname(PKG_DIR)
    if any(os.path.exists(os.path.join(checkout, f)) for f in ("pyproject.toml", "settings.json", "library.json")):
        return checkout
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "mixarchive")


APP_DIR = _data_dir()


def console_python():
    """This Python, for the commands it starts. Under pythonw (the `mixarchive-app` launcher,
    a shortcut) that's python.exe beside it: a pythonw child has no console to hand on,
    so every ffmpeg / yt-dlp it starts opens a window and their output is lost."""
    exe = sys.executable
    if os.name == "nt" and os.path.basename(exe).lower() == "pythonw.exe":
        console = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.isfile(console):
            return console
    return exe


@functools.lru_cache(maxsize=None)
def _scripts_tool(name):
    """A tool pip installed with mixarchive (yt-dlp, deno): next to this Python, in its
    environment's scripts folder, or on PATH (a full path in each case), else None."""
    import sysconfig
    for d in (os.path.dirname(sys.executable), sysconfig.get_path("scripts")):
        if d and os.path.isfile(os.path.join(d, name + EXE)):
            return os.path.join(d, name + EXE)
    return shutil.which(name)


@functools.lru_cache(maxsize=None)
def find_tool(name):
    """ffmpeg / ffprobe: on PATH, or where winget, Homebrew or a manual install put it
    (a fresh winget install isn't on PATH until a new window opens). Else just `name`."""
    hit = shutil.which(name)
    if hit:
        return hit
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        found = [os.path.join(local, "Microsoft", "WinGet", "Links", name + ".exe")]
        found += sorted(glob.glob(os.path.join(glob.escape(local), "Microsoft", "WinGet", "Packages",
                                               "*FFmpeg*", "*", "bin", name + ".exe")),
                        key=os.path.getmtime, reverse=True)      # the newest install
        found += [os.path.join("C:\\ffmpeg", "bin", name + ".exe")]
    else:
        found = [f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"]
    return next((f for f in found if os.path.isfile(f)), name)


def runnable(path):
    """A full path that exists, or a name found on PATH."""
    return bool(path) and (os.path.isfile(path) or bool(shutil.which(path)))


@functools.lru_cache(maxsize=None)
def _resolved(name, configured):
    """ffmpeg / ffprobe as configured if that runs, else what find_tool finds; worked out once
    per process (settings() is called per track while splitting)."""
    if runnable(configured):
        return configured
    found = find_tool(name)
    return found if runnable(found) else configured


@functools.lru_cache(maxsize=None)
def _rbpython():
    """The Python that runs rbdb / rbfiles / rbmerge (pyrekordbox): an rbvenv beside the data
    (the older two-environment setup), else this one (pyrekordbox comes with mixarchive)."""
    old = os.path.join(APP_DIR, "rbvenv", BIN, "python" + EXE)
    return old if os.path.isfile(old) else console_python()


DEFAULTS = {
    "ffmpeg": "ffmpeg",            # on PATH (or where winget / Homebrew put it), or a full path
    "ffprobe": "ffprobe",
    "openmpt123": os.path.join(APP_DIR, "tools", "openmpt", "openmpt123" + EXE),
    "ytdlp": None,                 # None: the yt-dlp installed with mixarchive
    "rbpython": None,              # None: see _rbpython
    # robocopy backup of the Pioneer USB stick: tracks rekordbox plays from inside it are
    # flagged by `files audit` (a stick backup isn't a library); "" = none
    "usb_mirror": "",
    "library": os.path.join(APP_DIR, "library.json"),
    "library_root": "",            # where a new library's collections go ("": ~/Music/mixarchive)
}


def settings():
    s = dict(DEFAULTS)
    p = os.path.join(APP_DIR, "settings.json")
    if os.path.exists(p):
        s.update(load_json(p))
    for k in ("ffmpeg", "ffprobe"):     # the default name, or a path that isn't there (any more)
        s[k] = _resolved(k, s[k])
    s["ytdlp"] = s["ytdlp"] or _scripts_tool("yt-dlp") or "yt-dlp"
    s["rbpython"] = s["rbpython"] or _rbpython()
    if s["library_root"]:          # "~/Music/DJ", or relative to mixarchive's own folder
        s["library_root"] = os.path.join(APP_DIR, os.path.expanduser(s["library_root"]))
    return s


def save_setting(key, value):
    """Change one key in settings.json (created if missing), keeping the others."""
    p = os.path.join(APP_DIR, "settings.json")
    s = load_json(p, {})
    s[key] = value
    save_json(p, s)


def require_ffmpeg():
    """Stop with how to install ffmpeg, before a command that needs it gets going."""
    s = settings()
    missing = [k for k in ("ffmpeg", "ffprobe") if not runnable(s[k])]
    if missing:
        how = ("winget install ffmpeg" if os.name == "nt" else
               "brew install ffmpeg" if sys.platform == "darwin" else "your package manager")
        raise SystemExit(f"{' and '.join(missing)} not found. Install ffmpeg ({how}), then open a "
                         f"new window - or set \"ffmpeg\" and \"ffprobe\" to full paths in "
                         f"{os.path.join(APP_DIR, 'settings.json')}")


def has_pyrekordbox():
    """Whether rbsync / files / merge can run: an rbvenv, or pyrekordbox in this Python."""
    import importlib.util
    return settings()["rbpython"] != console_python() or importlib.util.find_spec("pyrekordbox") is not None


def require_pyrekordbox():
    if not has_pyrekordbox():
        raise SystemExit("this needs pyrekordbox, which comes with mixarchive: reinstall it "
                         "(pipx reinstall mixarchive, or pip install --force-reinstall mixarchive)")


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    for attempt in range(40):    # Windows refuses while another reader (the GUI's refresh) has it open
        try:
            return os.replace(tmp, path)
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.25)


def norm(s):
    # NFKD first so accents fold the same way whether a title spells "é" as one
    # character or as "e" + a combining accent (both occur in YouTube titles), and
    # lower() after it: NFKD turns styled letters ("𝗟𝗢𝗦𝗧") into capitals, which the
    # filter would otherwise drop, leaving only the plain-text part of the title
    return re.sub(r"[^a-z0-9#]", "", unicodedata.normalize("NFKD", s or "").lower())


def plain_letters(s):
    """'𝗟𝗢𝗦𝗧 𝗢𝗖𝗘𝗔𝗡 沈殿' -> 'LOST OCEAN 沈殿': styled letters and digits (bold / italic /
    fullwidth...) as plain ones, which DJ players can show; accents, kanji, emoji kept."""
    out = []
    for ch in s or "":
        d = unicodedata.normalize("NFKD", ch)
        out.append(d if not ch.isascii() and len(d) == 1 and d.isascii() and d.isalnum() else ch)
    return "".join(out)


def safe_name(s):
    s = s.replace(":", " -").replace("/", "-").replace("\\", "-")
    s = re.sub(r'[*?"<>|]', "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s


def slug(s):
    """'Mood Hut' -> 'mood-hut' (collection names)."""
    return re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", s.lower())
                  .encode("ascii", "ignore").decode()).strip("-")


def rekordbox_running():
    import subprocess
    if os.name != "nt":           # -x only: -q is BSD's, and Linux may not have pgrep at all
        try:
            return subprocess.run(["pgrep", "-x", "rekordbox"], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode == 0
        except FileNotFoundError:
            return False
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq rekordbox.exe", "/NH"],   # no console flash
                       capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW)
    return "rekordbox.exe" in r.stdout.lower()


def to_sec(ts):
    sec = 0
    for part in ts.split(":"):
        sec = sec * 60 + int(part)
    return sec


def fmt_ts(sec):
    sec = int(round(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def mmss(sec):
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def year_tag(shown):
    """Last plain 4-digit year: '1992? & 2011' -> 2011, '199?' / '???' -> ''."""
    years = re.findall(r"(?<![\d?])(\d{4})(?![\d?])", shown or "")
    return years[-1] if years else ""
