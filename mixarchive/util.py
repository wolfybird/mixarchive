"""Shared helpers: settings, paths, names, timestamps."""
import json
import os
import re
import time
import unicodedata

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = "_mixarchive"          # per-collection state folder name
AUDIO_EXTS = (".opus", ".m4a", ".mp3", ".flac", ".wav")
BIN, EXE = ("Scripts", ".exe") if os.name == "nt" else ("bin", "")   # venv layout

DEFAULTS = {
    "ffmpeg": "ffmpeg",            # on PATH, or a full path in settings.json
    "ffprobe": "ffprobe",
    "openmpt123": os.path.join(APP_DIR, "tools", "openmpt", "openmpt123" + EXE),
    "ytdlp": os.path.join(APP_DIR, "venv", BIN, "yt-dlp" + EXE),
    "rbpython": os.path.join(APP_DIR, "rbvenv", BIN, "python" + EXE),   # has pyrekordbox
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
