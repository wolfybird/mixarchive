"""Skins for the GUI: import a classic Winamp skin (.wsz) or a Windows Media Player skin (.wmz), keep
it in the app's data folder, and describe it for the page (its colours, font, pictures and credits).

**What's in a skin.** A classic skin is a zip of:
- bitmaps: `main.bmp`, `titlebar.bmp`, `numbers.bmp`, `posbar.bmp`, `cbuttons.bmp`...;
- `pledit.txt`: the playlist's colours and font;
- `viscolor.txt`: 24 colours for the visualizer.

The page decodes the bitmaps itself (browsers read BMP) and turns all of it into the GUI's colours
and a few decorations.

**Windows Media Player skins** are a zip too: a .wms file (XML-like markup naming each view's
colours and pictures) plus images, and usually JScript. Only the colours and pictures are taken:
- the playlist's and text's colours become the page's;
- the main view's background picture becomes the page's backdrop.

The markup is read leniently (WMP's own reader is), never as XML with entities. The scripts are
never kept, let alone run.

**Whose skins.** Skins are other people's artwork: the Winamp community's, and Microsoft's and
others' for WMP. They're never bundled; they're fetched from the Internet Archive on the user's
click, or opened from a file. Each keeps its readme or credits, and where it came from.

**Safety.** A skin is untrusted input:
- only flat file names with known extensions are taken;
- sizes are capped;
- nothing in it is ever run.

Modern (.wal) Winamp skins, which carry scripts, aren't supported.
"""
import collections
import colorsys
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import struct
import urllib.parse
import urllib.request
import zipfile

from . import __version__
from .util import APP_DIR

SKINS_DIR = os.path.join(APP_DIR, "skins")
BITMAPS = ("main", "titlebar", "cbuttons", "numbers", "nums_ex", "text", "posbar", "volume", "balance", "monoster",
           "playpaus", "shufrep", "pledit", "eqmain", "eq_ex", "gen", "genex", "mb", "video", "avs")
KEEP = (".bmp", ".txt", ".diz")                       # from a Winamp skin
IMAGE_TYPES = {".bmp": "image/bmp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}
IMAGES = tuple(IMAGE_TYPES)
KEEP_WMP = IMAGES + (".wms", ".txt")                  # from a WMP skin: never its scripts
SKIN_FILES = (".wsz", ".wmz")
CONFIG = ("pledit.txt", "viscolor.txt", "region.txt")
NAMED = {"black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000", "lime": "#00ff00",
         "blue": "#0000ff", "navy": "#000080", "yellow": "#ffff00", "gray": "#808080", "grey": "#808080",
         "silver": "#c0c0c0", "maroon": "#800000", "purple": "#800080", "teal": "#008080", "olive": "#808000",
         "aqua": "#00ffff", "cyan": "#00ffff", "fuchsia": "#ff00ff", "magenta": "#ff00ff", "orange": "#ffa500"}
FLAT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}$")
MAX_SKIN = 20 * 1024 * 1024         # the .wsz / .wmz itself
MAX_FILE = 4 * 1024 * 1024          # any one file in it
MAX_TOTAL = 24 * 1024 * 1024        # all of it, unpacked
MAX_FILES = 400
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
ITEM = re.compile(r"^[\w.-]{1,200}$")
ARCHIVE = "https://archive.org"
COLLECTION = "winampskins"
# WMP skins have no collection of their own: items tagged as such, some holding a whole set
WMP_QUERY = ('(subject:"windows media player" OR title:"windows media player" OR title:wmp) AND '
             '(subject:skin OR subject:skins OR subject:theme OR title:skin OR title:skins)')


class SkinError(Exception):
    pass


# ------------------------------------------------------------ reading a skin
def _text(data):
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    if len(data) > 1 and data[1:2] == b"\x00":       # UTF-16 without its byte order mark
        return data.decode("utf-16-le", "replace")
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _hex(value):
    m = re.fullmatch(r"\s*#?\s*([0-9a-fA-F]{6})\s*", value or "")
    return "#" + m.group(1).lower() if m else None


def _color(value):
    """A WMP colour: #rrggbb, #rgb or a name -> #rrggbb; None for none, scripts and nonsense."""
    v = (value or "").strip().lower()
    if v in NAMED:
        return NAMED[v]
    m = re.fullmatch(r"#?([0-9a-f]{6})", v) or re.fullmatch(r"#([0-9a-f]{3})", v)
    if not m:
        return None
    h = m.group(1)
    return "#" + (h if len(h) == 6 else "".join(c * 2 for c in h))


def _flat(name):
    """A file name in a skin -> its flat, lower-case, safe name ("" if nothing is left of it)."""
    base = os.path.basename((name or "").replace("\\", "/")).strip().lower()
    return re.sub(r"[^a-z0-9._-]+", "_", base).lstrip("._-")[:120]


def image_size(data):
    """(width, height) of a BMP, PNG, GIF or JPEG, from its header; (0, 0) if unreadable."""
    try:
        if data[:2] == b"BM":
            if struct.unpack_from("<I", data, 14)[0] == 12:          # an OS/2 bitmap
                return struct.unpack_from("<HH", data, 18)
            w, h = struct.unpack_from("<ii", data, 18)
            return abs(w), abs(h)
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack_from(">II", data, 16)
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack_from("<HH", data, 6)
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack_from(">HH", data, i + 5)
                    return w, h
                if marker in (0xD8, 0x01, 0xFF) or 0xD0 <= marker <= 0xD7:
                    i += 1 if marker == 0xFF else 2
                    continue
                i += 2 + struct.unpack_from(">H", data, i + 2)[0]
    except struct.error:
        pass
    return 0, 0


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


TAG = re.compile(r"<\s*([A-Za-z][\w:.-]{0,60})((?:[^>\"']|\"[^\"]{0,4000}\"|'[^']{0,4000}'){0,20000})>", re.S)
ATTR = re.compile(r"([A-Za-z_][\w:.-]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
SCRIPTED = ("jscript:", "wmpprop:", "javascript:", "vbscript:")
# which picture is a WMP skin's main one: by name, then size. Its states, masks, hit maps, video
# panes and sprite strips aren't; "main" is, more than a body, back or base, more than drawers.
NOT_MAIN = re.compile(r"mask|map|anim|glare|shadow|hilite|highlight|hov|_down|down_|_over|over_|pressed|disabled|"
                      r"preview|vid|logo|cursor|icon|thumb")
LESS_MAIN = re.compile(r"eq|playlist|pl_|drawer|mini|tray|button|btn|slider|seek|vol|tooltip|menu|tab")
MORE_MAIN = re.compile(r"head|body|back|base|frame|skin|player|center|bkgd|bkgrnd")


def _main_weight(name, size):
    w, h = size
    stem = os.path.splitext(name)[0]
    if not w or not h or max(w, h) > 6 * min(w, h) or NOT_MAIN.search(stem):
        return 0
    k = 3 if re.search(r"(^|[_-])main", stem) else 1.5 if MORE_MAIN.search(stem) else 1
    return w * h * k * (.3 if LESS_MAIN.search(stem) else 1)


def _vivid(hexes):
    """The most colourful of some colours, if any is colourful at all."""
    best, score = None, .3
    for h in hexes:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
        _, s, v = colorsys.rgb_to_hsv(r, g, b)
        if s * v > score:
            best, score = h, s * v
    return best


def parse_wms(text, files=None):
    """A WMP skin's .wms markup, read leniently, as pattern matches (never as XML):
    - "theme": its credits (THEME: title, author, copyright);
    - "colors": like pledit.txt's (normal, normalbg, current, selectedbg, font), from its PLAYLIST
      and TEXT elements;
    - "images": its main picture (the biggest VIEW / SUBVIEW background), its preview picture if it
      has one, and for each the colours WMP leaves out of it (clippingColor, transparencyColor).
    Values computed by its scripts (jscript:, wmpprop:) are skipped. `files`: {name: bytes} of the
    skin's pictures, to measure them."""
    files = files or {}
    text = re.sub(r"<!--.*?-->", "", (text or "")[:1_000_000], flags=re.S)       # real ones: ~100 KB
    theme, playlist, texts, others, fonts, backs = {}, {}, collections.Counter(), [], collections.Counter(), []
    for m in TAG.finditer(text):
        tag = m.group(1).upper()
        attrs = {k.lower(): v.strip("\"'").strip() for k, v in ATTR.findall(m.group(2))}
        attrs = {k: v for k, v in attrs.items() if not v.lower().startswith(SCRIPTED)}
        if tag == "THEME" and not theme:
            theme = {k: re.sub(r"\s+", " ", attrs[k])[:200] for k in ("title", "author", "copyright") if attrs.get(k)}
        elif tag == "PLAYLIST" and not playlist:
            playlist = attrs
        elif tag == "TEXT" or tag.endswith("TEXT"):
            if _color(attrs.get("foregroundcolor")):
                texts[_color(attrs["foregroundcolor"])] += 1
            others += [c for c in (_color(attrs.get("hoverforegroundcolor")),) if c]
            font = re.sub(r"[^\w .-]", "", (attrs.get("fontface") or attrs.get("fonttype") or "").split(",")[0]).strip()
            if font:
                fonts[font[:60]] += 1
        image = _flat(attrs.get("backgroundimage") if tag in ("VIEW", "SUBVIEW")
                      else attrs.get("image") if tag == "BUTTONGROUP" else "")
        if image:
            keys = [c for c in (_color(attrs.get("clippingcolor")), _color(attrs.get("transparencycolor"))) if c]
            backs.append((attrs.get("id", "").lower(), image, keys))

    colors = {}
    normal = _color(playlist.get("foregroundcolor")) or _color(playlist.get("textcolor")) or \
        next(iter(c for c, _ in texts.most_common()), None)
    if normal:
        colors["normal"] = normal
    if _color(playlist.get("backgroundcolor")):
        colors["normalbg"] = _color(playlist["backgroundcolor"])
    current = _color(playlist.get("itemplayingcolor")) or \
        _vivid([c for c in list(texts) + others if c not in (normal, colors.get("normalbg"))])
    if current:
        colors["current"] = current
    selected = _color(playlist.get("itemselectedbackgroundcolor")) or _color(playlist.get("selectedbackgroundcolor"))
    if selected:
        colors["selectedbg"] = selected
    if fonts:
        colors["font"] = fonts.most_common(1)[0][0]

    keys = collections.defaultdict(list)
    for _, image, ks in backs:
        for k in ks:
            if k not in keys[image]:
                keys[image].append(k)

    def weight(name):
        return _main_weight(name, image_size(files.get(name, b"")))
    preview = next((im for vid, im, _ in backs if im in files and ("preview" in vid or "switcher" in vid
                                                                    or im.startswith("preview"))), None)
    main = max((im for _, im, _ in backs if im in files and weight(im)), key=weight, default=None) or         max((n for n in files if n.endswith(IMAGES) and weight(n)), key=weight, default=None)
    images = {"main": main, "preview": preview,
              "keys": {n: keys[n] for n in (main, preview) if n and keys.get(n)}}
    return {"theme": theme, "colors": colors, "images": images}


def _unpack(data, nested=False):
    """("winamp" or "wmp", {flat lower-case name: bytes} of the files that kind of skin uses). A zip
    holding just one skin file is opened too."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkinError("not a skin file: .wsz and .wmz skins are zips, and this isn't one") from None
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_FILES:
        raise SkinError(f"{len(infos)} files in it: too many for a skin")
    flat = [(i, _flat(i.filename)) for i in infos]
    names = {n for _, n in flat}
    if any(n.endswith(".wms") for n in names):
        kind, keep = "wmp", KEEP_WMP
    elif "main.bmp" in names:
        kind, keep = "winamp", KEEP
    else:
        inner = [(i, n) for i, n in flat if n.endswith(SKIN_FILES)]
        if len(inner) == 1 and not nested and inner[0][0].file_size <= MAX_SKIN:
            return _unpack(zf.read(inner[0][0]), nested=True)
        if len(inner) > 1:
            raise SkinError(f"a bundle of {len(inner)} skins: unzip it and open them one at a time")
        if any(n.endswith((".xml", ".maki", ".m")) for n in names):
            raise SkinError("a modern Winamp skin (.wal): only classic skins (.wsz) work here")
        raise SkinError("no main.bmp or .wms in it: not a classic Winamp skin or a Windows Media Player skin")
    files, total = {}, 0
    for info, base in flat:
        if not base or info.flag_bits & 0x1 or not base.endswith(keep) or base in files:
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
    if kind == "winamp" and "main.bmp" not in files:
        raise SkinError("its main.bmp couldn't be read: not a usable Winamp skin")
    if kind == "wmp" and not any(n.endswith(".wms") for n in files):
        raise SkinError("its .wms couldn't be read: not a usable Windows Media Player skin")
    return kind, files


def describe(files, name, source=None, kind="winamp"):
    readme = next((_text(files[n]) for n in sorted(files)
                   if n.endswith((".txt", ".diz")) and n not in CONFIG), "")
    info = {"kind": kind, "name": name, "source": source or {},
            "imported": dt.datetime.now().isoformat(timespec="seconds")}
    if kind == "wmp":
        wms = max((n for n in files if n.endswith(".wms")), key=lambda n: len(files[n]))
        parsed = parse_wms(_text(files[wms]), {n: d for n, d in files.items() if n.endswith(IMAGES)})
        credits = "\n".join(f"{k.title()}: {v}" for k, v in parsed["theme"].items())
        return {**info, "colors": parsed["colors"], "vis": [], "bitmaps": [], "images": parsed["images"],
                "readme": "\n\n".join(p for p in (credits, readme.strip()) if p)[:3000]}
    return {**info,
            "colors": parse_pledit(_text(files.get("pledit.txt", b""))),
            "vis": parse_viscolor(_text(files.get("viscolor.txt", b""))),
            "bitmaps": [b for b in BITMAPS if b + ".bmp" in files],
            "images": {"main": "main.bmp", "preview": None, "keys": {}},
            "readme": readme.strip()[:3000]}


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]
    return s or "skin"


def import_bytes(data, name, source=None):
    """Add a skin from its .wsz / .wmz bytes; returns its description (with "id"). The same file
    twice is the same skin."""
    if len(data) > MAX_SKIN:
        raise SkinError("too big to be a skin")
    kind, files = _unpack(data)
    name = re.sub(r"\.(wsz|wmz|zip)$", "", os.path.basename(name or "skin"), flags=re.I).strip() or "skin"
    skin_id = f"{_slug(name)}-{hashlib.sha1(data).hexdigest()[:8]}"
    info = {"id": skin_id, **describe(files, name, source, kind)}
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


# the skin a new install starts with, bundled (with its credits.txt): a Winamp skin by johnnyg0
DEFAULT_FILE = "v2_technics_white_resting_by_johnnyg0.wsz"
DEFAULT_SOURCE = {"archive": "winampskin_v2_technics_white_resting_by_johnnyg0", "file": DEFAULT_FILE,
                  "url": "https://archive.org/details/winampskin_v2_technics_white_resting_by_johnnyg0",
                  "title": "v2 Technics White Resting by johnnyg0", "bundled": True}
DEFAULT_MARK = ".default"           # in SKINS_DIR: the default was added once (removing it sticks)


def default_skin():
    """The default skin's description, or None once it has been removed. The first call adds it
    to the skins folder; one already there from the Internet Archive is used instead."""
    mark = os.path.join(SKINS_DIR, DEFAULT_MARK)
    if os.path.exists(mark):
        with open(mark, encoding="utf-8") as fh:
            wanted = fh.read().strip()
        return next((s for s in list_skins() if s.get("id") == wanted), None)
    have = next((s for s in list_skins()
                 if (s.get("source") or {}).get("archive") == DEFAULT_SOURCE["archive"]), None)
    if have is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DEFAULT_FILE)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            have = import_bytes(fh.read(), DEFAULT_SOURCE["title"] + ".wsz", DEFAULT_SOURCE)   # its name, readable
    os.makedirs(SKINS_DIR, exist_ok=True)
    with open(mark, "w", encoding="utf-8") as fh:
        fh.write(have["id"])
    return have


def remove(skin_id):
    if not ID.match(skin_id or "") or not os.path.isdir(os.path.join(SKINS_DIR, skin_id)):
        raise SkinError("no such skin")
    shutil.rmtree(os.path.join(SKINS_DIR, skin_id))


def asset(skin_id, name):
    """The path of one of a skin's pictures, or None: only its own image files, by their flat names."""
    name = (name or "").lower()
    if not ID.match(skin_id or "") or not FLAT.match(name) or not name.endswith(IMAGES):
        return None
    path = os.path.join(SKINS_DIR, skin_id, name)
    return path if os.path.isfile(path) else None


def asset_type(path):
    return IMAGE_TYPES[os.path.splitext(path)[1].lower()]


# ------------------------------------------------------------ the Internet Archive
def _get(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": f"mixarchive/{__version__} (skins)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise SkinError("the download is too big to be a skin")
    return data


def _meta(identifier):
    if not ITEM.fullmatch(identifier or ""):
        raise SkinError("not an Internet Archive item name")
    return json.loads(_get(f"{ARCHIVE}/metadata/{identifier}", 8 * 1024 * 1024))


def _title(text):
    return re.sub(r"^Winamp Skin:\s*", "", text or "").strip()


def search(query, page=1, rows=24, kind="winamp"):
    """Skins on the Internet Archive, most-downloaded first: classic Winamp skins from its Winamp skin
    collection (one skin per item), or items tagged as Windows Media Player skins (some holding a
    whole set: see files()). Returns {"total", "items": [{"identifier", "title", "thumb", "kind"}]}."""
    words = [w.strip("-'").lower() for w in re.sub(r"[^\w\s'-]", " ", query or "").split()[:8]]
    words = [w for w in words if w]
    if kind == "wmp":
        q = f"({WMP_QUERY})" + (" AND (" + " ".join(words) + ")" if words else "")
    else:
        q = f"collection:{COLLECTION}" + (" AND title:(" + " ".join(words) + ")" if words else "")
    params = urllib.parse.urlencode({"q": q, "fl[]": ["identifier", "title"], "sort[]": "downloads desc",
                                     "rows": max(1, min(int(rows), 60)), "page": max(1, int(page)),
                                     "output": "json"}, doseq=True)
    res = json.loads(_get(f"{ARCHIVE}/advancedsearch.php?{params}", 2 * 1024 * 1024))["response"]
    items = [{"identifier": d["identifier"], "title": _title(d.get("title") or d["identifier"]),
              "thumb": f"{ARCHIVE}/services/img/{urllib.parse.quote(d['identifier'])}",
              "kind": "wmp" if kind == "wmp" else "winamp"}
             for d in res.get("docs", []) if ITEM.fullmatch(d.get("identifier", ""))]
    return {"total": res.get("numFound", 0), "items": items}


def _skin_files(meta):
    return [f for f in meta.get("files", []) if f.get("name", "").lower().endswith(SKIN_FILES + (".zip",))
            and "/" not in f.get("name", "")]


def files(identifier):
    """The skin files in one Internet Archive item, each with a picture of it when the item has one
    (a same-named .png / .jpg, or a "_thumb" one). Returns {"identifier", "title", "items":
    [{"file", "title", "thumb", "size"}], "too_big": how many are over the size limit}."""
    meta = _meta(identifier)
    names = {f.get("name") for f in meta.get("files", [])}
    out, too_big = [], 0
    for f in _skin_files(meta):
        size = int(f.get("size") or 0)
        if size > MAX_SKIN:
            too_big += 1
            continue
        base = os.path.splitext(f["name"])[0]
        thumb = next((t for t in (base + "_thumb.jpg", base + ".jpg", base + ".png", base + ".gif") if t in names), None)
        out.append({"file": f["name"], "title": base.replace("_", " ").strip(), "size": size,
                    "thumb": f"{ARCHIVE}/download/{identifier}/{urllib.parse.quote(thumb)}" if thumb
                    else f"{ARCHIVE}/services/img/{urllib.parse.quote(identifier)}"})
    return {"identifier": identifier, "title": _title((meta.get("metadata") or {}).get("title") or identifier),
            "items": sorted(out, key=lambda x: x["title"].lower()), "too_big": too_big}


def fetch(identifier, file=None):
    """Download a skin file from an Internet Archive item and import it: the one named (it must be
    one the item lists), else its only or first skin file (.wsz, then .wmz, then .zip)."""
    meta = _meta(identifier)
    listed = _skin_files(meta)
    if file:
        listed = [f for f in listed if f["name"] == file]
        if not listed:
            raise SkinError("that item has no such skin file")
    else:
        listed = sorted(listed, key=lambda f: [".wsz", ".wmz", ".zip"].index(os.path.splitext(f["name"])[1].lower()))
    if not listed:
        raise SkinError("that item has no skin file")
    name = listed[0]["name"]
    if int(listed[0].get("size") or 0) > MAX_SKIN:
        raise SkinError("that skin file is too big to fetch")
    data = _get(f"{ARCHIVE}/download/{identifier}/{urllib.parse.quote(name)}", MAX_SKIN)
    base = os.path.splitext(name)[0]
    several = len(_skin_files(meta)) > 1
    title = base.replace("_", " ").strip() if several else _title((meta.get("metadata") or {}).get("title") or base)
    source = {"archive": identifier, "file": name, "url": f"{ARCHIVE}/details/{identifier}", "title": title}
    return import_bytes(data, title if several else base, source)
