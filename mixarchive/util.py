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


def _data_dir():
    """Where settings.json, library.json, cache\\ and tools\\ live: $MIXARCHIVE_HOME if set;
    else a checkout that already keeps them next to the code (an install from a clone);
    else the user's app-data folder (an install from PyPI, where the code's folder is
    Python's own)."""
    if os.environ.get("MIXARCHIVE_HOME"):
        return os.path.abspath(os.path.expanduser(os.environ["MIXARCHIVE_HOME"]))
    checkout = os.path.dirname(PKG_DIR)
    if any(os.path.exists(os.path.join(checkout, f)) for f in ("settings.json", "library.json")):
        return checkout
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "mixarchive")


APP_DIR = _data_dir()


def _scripts_tool(name):
    """A tool pip installed with mixarchive (yt-dlp, deno): next to this Python, in the
    environment's scripts folder (also for `pip install --user`), or on PATH."""
    import sysconfig
    dirs = [os.path.dirname(sys.executable), sysconfig.get_path("scripts")]
    try:
        dirs.append(sysconfig.get_path("scripts", f"{os.name}_user"))
    except KeyError:
        pass
    for d in dirs:
        if d and os.path.isfile(os.path.join(d, name + EXE)):
            return os.path.join(d, name + EXE)
    return shutil.which(name) or name


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
                                               "*FFmpeg*", "*", "bin", name + ".exe")), reverse=True)
        found += [os.path.join("C:\\ffmpeg", "bin", name + ".exe")]
    else:
        found = [f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"]
    return next((f for f in found if os.path.isfile(f)), name)


def _rbpython():
    """The Python that runs rbdb / rbfiles / rbmerge (pyrekordbox): an rbvenv beside the data
    (the older two-environment setup), else this one (`pip install "mixarchive[rekordbox]"`)."""
    old = os.path.join(APP_DIR, "rbvenv", BIN, "python" + EXE)
    return old if os.path.isfile(old) else sys.executable


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
    for k in ("ffmpeg", "ffprobe"):
        if s[k] == k:
            s[k] = find_tool(k)
    s["ytdlp"] = s["ytdlp"] or _scripts_tool("yt-dlp")
    s["rbpython"] = s["rbpython"] or _rbpython()
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
    missing = [k for k in ("ffmpeg", "ffprobe") if not (os.path.isfile(s[k]) or shutil.which(s[k]))]
    if missing:
        how = ("winget install ffmpeg" if os.name == "nt" else
               "brew install ffmpeg" if sys.platform == "darwin" else "your package manager")
        raise SystemExit(f"{' and '.join(missing)} not found. Install ffmpeg ({how}), then open a "
                         f"new window - or set \"ffmpeg\" and \"ffprobe\" to full paths in "
                         f"{os.path.join(APP_DIR, 'settings.json')}")


def require_pyrekordbox():
    """rbsync / files / merge need pyrekordbox in the Python that runs them."""
    import importlib.util
    if settings()["rbpython"] == sys.executable and importlib.util.find_spec("pyrekordbox") is None:
        raise SystemExit('this needs pyrekordbox: pip install "mixarchive[rekordbox]" '
                         '(with pipx: pipx install --force "mixarchive[app,rekordbox]")')


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
    if os.name != "nt":
        return subprocess.run(["pgrep", "-xq", "rekordbox"]).returncode == 0
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq rekordbox.exe", "/NH"],
                       capture_output=True, text=True, errors="replace")
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
