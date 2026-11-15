"""Winamp skins for the GUI: import a classic Winamp skin (.wsz), keep it in the app's data folder,
and describe it for the page (its colours, font, bitmaps and credits).

**What's in a skin.** A classic skin is a zip of:
- bitmaps: `main.bmp`, `titlebar.bmp`, `numbers.bmp`, `posbar.bmp`, `cbuttons.bmp`...;
- `pledit.txt`: the playlist's colours and font;
- `viscolor.txt`: 24 colours for the visualizer.

The page decodes the bitmaps itself (browsers read BMP) and turns all of it into the GUI's colours
and a few decorations.

**Whose skins.** Skins are other people's artwork from the Winamp community. They're never
bundled; they're fetched from the Internet Archive's Winamp skin collection on the user's click,
or opened from a file. Each keeps its readme (its credits) and where it came from.

**Safety.** A skin is untrusted input:
- only flat file names with known extensions are taken;
- sizes are capped;
- nothing in it is ever run.

Modern (.wal) skins, which carry scripts, aren't supported.
"""
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import urllib.parse
import urllib.request
import zipfile

from . import __version__
from .util import APP_DIR

SKINS_DIR = os.path.join(APP_DIR, "skins")
BITMAPS = ("main", "titlebar", "cbuttons", "numbers", "nums_ex", "text", "posbar", "volume", "balance", "monoster",
           "playpaus", "shufrep", "pledit", "eqmain", "eq_ex", "gen", "genex", "mb", "video", "avs")
KEEP = (".bmp", ".txt", ".diz")
CONFIG = ("pledit.txt", "viscolor.txt", "region.txt")
MAX_SKIN = 20 * 1024 * 1024         # the .wsz itself
MAX_FILE = 4 * 1024 * 1024          # any one file in it
MAX_TOTAL = 24 * 1024 * 1024        # all of it, unpacked
MAX_FILES = 300
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
ARCHIVE = "https://archive.org"
COLLECTION = "winampskins"


class SkinError(Exception):
    pass


# ------------------------------------------------------------ reading a skin
def _text(data):
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _hex(value):
    m = re.fullmatch(r"\s*#?\s*([0-9a-fA-F]{6})\s*", value or "")
    return "#" + m.group(1).lower() if m else None


def parse_pledit(text):
    """pledit.txt's [Text] section: Normal, Current, NormalBG, SelectedBG, MbFG, MbBG (colours) and
    Font. Returns {lower-case key: value}; colours as #rrggbb, unreadable ones left out."""
    out, section = {}, None
    for line in (text or "").splitlines():
        line = line.split(";", 1)[0].strip()
        if line.startswith("["):
            section = line.strip("[]").strip().lower()
        elif "=" in line and section == "text":
            key, value = (p.strip() for p in line.split("=", 1))
            key = key.lower()
            if key == "font":
                out["font"] = re.sub(r"[^\w .-]", "", value)[:60]
            elif key in ("normal", "current", "normalbg", "selectedbg", "mbfg", "mbbg") and _hex(value):
                out[key] = _hex(value)
    return out


def parse_viscolor(text):
    """viscolor.txt: up to 24 lines of "r,g,b" (a comment may follow) -> ["#rrggbb", ...]."""
    out = []
    for line in (text or "").splitlines():
        nums = re.findall(r"\d+", line.split("//", 1)[0])
        if len(nums) >= 3:
            r, g, b = (min(int(n), 255) for n in nums[:3])
            out.append(f"#{r:02x}{g:02x}{b:02x}")
        if len(out) == 24:
            break
    return out


def _unpack(data):
    """{flat lower-case name: bytes} of the files a classic skin uses."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkinError("not a skin file: a .wsz is a zip, and this isn't one") from None
    infos = zf.infolist()
    if len(infos) > MAX_FILES:
        raise SkinError(f"{len(infos)} files in it: too many for a skin")
    names = {os.path.basename(i.filename.replace("\\", "/")).lower() for i in infos}
    files, total = {}, 0
    for info in infos:
        base = os.path.basename(info.filename.replace("\\", "/")).lower()
        if info.is_dir() or info.flag_bits & 0x1 or not base.endswith(KEEP) or base in files:
            continue
        if info.file_size > MAX_FILE:
            continue
        total += info.file_size
        if total > MAX_TOTAL:
            raise SkinError("too big unpacked to be a skin")
        try:
            files[base] = zf.read(info)                 # reads no more than the size it declares
        except (zipfile.BadZipFile, ValueError, RuntimeError, OSError):
            continue
    if "main.bmp" not in files:
        if any(n.endswith((".xml", ".maki", ".m")) for n in names):
            raise SkinError("a modern Winamp skin (.wal): only classic skins (.wsz) work here")
        raise SkinError("no main.bmp in it: not a classic Winamp skin")
    return files


def describe(files, name, source=None):
    readme = next((_text(files[n]) for n in sorted(files)
                   if n.endswith((".txt", ".diz")) and n not in CONFIG), "")
    return {
        "name": name,
        "source": source or {},
        "imported": dt.datetime.now().isoformat(timespec="seconds"),
        "colors": parse_pledit(_text(files.get("pledit.txt", b""))),
        "vis": parse_viscolor(_text(files.get("viscolor.txt", b""))),
        "bitmaps": [b for b in BITMAPS if b + ".bmp" in files],
        "readme": readme.strip()[:3000],
    }


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]
    return s or "skin"


def import_bytes(data, name, source=None):
    """Add a skin from its .wsz bytes; returns its description (with "id"). The same file twice is
    the same skin."""
    if len(data) > MAX_SKIN:
        raise SkinError("too big to be a skin")
    files = _unpack(data)
    name = re.sub(r"\.(wsz|zip)$", "", os.path.basename(name or "skin"), flags=re.I).strip() or "skin"
    skin_id = f"{_slug(name)}-{hashlib.sha1(data).hexdigest()[:8]}"
    info = {"id": skin_id, **describe(files, name, source)}
    dest = os.path.join(SKINS_DIR, skin_id)
    tmp = dest + ".importing"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for fname, content in files.items():
        with open(os.path.join(tmp, fname), "wb") as fh:
            fh.write(content)
    with open(os.path.join(tmp, "skin.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=1)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    return info


def list_skins():
    out = []
    if os.path.isdir(SKINS_DIR):
        for d in sorted(os.listdir(SKINS_DIR)):
            if not ID.match(d):
                continue
            try:
                with open(os.path.join(SKINS_DIR, d, "skin.json"), encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, ValueError):
                continue
    return sorted(out, key=lambda s: s.get("name", "").lower())


def remove(skin_id):
    if not ID.match(skin_id or "") or not os.path.isdir(os.path.join(SKINS_DIR, skin_id)):
        raise SkinError("no such skin")
    shutil.rmtree(os.path.join(SKINS_DIR, skin_id))


def asset(skin_id, name):
    """The path of one of a skin's bitmaps, or None: only names a classic skin has."""
    name = (name or "").lower()
    if not ID.match(skin_id or "") or not name.endswith(".bmp") or name[:-4] not in BITMAPS:
        return None
    path = os.path.join(SKINS_DIR, skin_id, name)
    return path if os.path.isfile(path) else None


# ------------------------------------------------------------ the Internet Archive's collection
def _get(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": f"mixarchive/{__version__} (Winamp skins)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise SkinError("the download is too big to be a skin")
    return data


def search(query, page=1, rows=24):
    """Skins in the Internet Archive's Winamp skin collection whose title matches; most-downloaded
    first. Returns {"total", "items": [{"identifier", "title", "thumb"}]}."""
    words = re.sub(r"[^\w\s'-]", " ", query or "").split()[:8]
    q = f"collection:{COLLECTION}" + (" AND title:(" + " ".join(words) + ")" if words else "")
    params = urllib.parse.urlencode({"q": q, "fl[]": ["identifier", "title"], "sort[]": "downloads desc",
                                     "rows": max(1, min(int(rows), 60)), "page": max(1, int(page)),
                                     "output": "json"}, doseq=True)
    res = json.loads(_get(f"{ARCHIVE}/advancedsearch.php?{params}", 2 * 1024 * 1024))["response"]
    items = [{"identifier": d["identifier"],
              "title": re.sub(r"^Winamp Skin:\s*", "", d.get("title") or d["identifier"]),
              "thumb": f"{ARCHIVE}/services/img/{urllib.parse.quote(d['identifier'])}"}
             for d in res.get("docs", []) if re.fullmatch(r"[\w.-]{1,200}", d.get("identifier", ""))]
    return {"total": res.get("numFound", 0), "items": items}


def fetch(identifier):
    """Download one item's skin file from the Internet Archive and import it."""
    if not re.fullmatch(r"[\w.-]{1,200}", identifier or ""):
        raise SkinError("not an Internet Archive item name")
    meta = json.loads(_get(f"{ARCHIVE}/metadata/{identifier}", 4 * 1024 * 1024))
    files = [f for f in meta.get("files", []) if f.get("name", "").lower().endswith(".wsz")] or \
            [f for f in meta.get("files", []) if f.get("name", "").lower().endswith(".zip")]
    if not files:
        raise SkinError("that item has no skin file")
    name = files[0]["name"]
    data = _get(f"{ARCHIVE}/download/{identifier}/{urllib.parse.quote(name)}", MAX_SKIN)
    title = re.sub(r"^Winamp Skin:\s*", "", (meta.get("metadata") or {}).get("title") or name)
    source = {"archive": identifier, "url": f"{ARCHIVE}/details/{identifier}", "title": title}
    return import_bytes(data, os.path.splitext(name)[0], source)
