"""Shared helpers: settings, paths, names, timestamps."""
import json
import os
import re
import unicodedata

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = "_mixarchive"          # per-collection state folder name
AUDIO_EXTS = (".opus", ".m4a", ".mp3", ".flac", ".wav")

DEFAULTS = {
    "ffmpeg": "ffmpeg",            # on PATH, or a full path in settings.json
    "ffprobe": "ffprobe",
    "openmpt123": os.path.join(APP_DIR, "tools", "openmpt", "openmpt123.exe"),
    "ytdlp": os.path.join(APP_DIR, "venv", "Scripts", "yt-dlp.exe"),
    "rbpython": os.path.join(APP_DIR, "rbvenv", "Scripts", "python.exe"),   # has pyrekordbox
    # robocopy backup of the Pioneer USB stick: tracks rekordbox plays from inside it are
    # flagged by `files audit` (a stick backup isn't a library); "" = none
    "usb_mirror": "",
    "library": os.path.join(APP_DIR, "library.json"),
}


def settings():
    s = dict(DEFAULTS)
    p = os.path.join(APP_DIR, "settings.json")
    if os.path.exists(p):
        s.update(load_json(p))
    return s


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
    os.replace(tmp, path)


def norm(s):
    # NFKD first so accents fold the same way whether a title spells "é" as one
    # character or as "e" + a combining accent (both occur in YouTube titles)
    return re.sub(r"[^a-z0-9#]", "", unicodedata.normalize("NFKD", (s or "").lower()))


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
