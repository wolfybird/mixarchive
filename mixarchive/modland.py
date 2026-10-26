"""Modland lookup + libopenmpt rendering for tracker-music collections (opt-in).

Matches are written to _mixarchive/modules.csv, which you can edit: set
`modland_path` by hand, or status `skip`. Only `auto`/`manual` rows render.
"""
import csv
import difflib
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

from .util import APP_DIR, norm, settings

INDEX_URL = "https://ftp.modland.com/allmods.zip"
FILE_URL = "https://ftp.modland.com/pub/modules/"
AMIGA_FORMATS = ("Protracker", "Noisetracker", "Soundtracker", "Startrekker AM",
                 "Protracker IFF", "His Master's Noise")
# Matches off1k's sound: A1200 output filter, ~off1k stereo width (measured), 48 kHz.
RENDER_ARGS = ["--samplerate", "48000", "--stereo", "50",
               "--ctl", "render.resampler.emulate_amiga=1",
               "--ctl", "render.resampler.emulate_amiga_type=a1200"]
FIELDS = ["video_id", "track", "artist", "title", "status", "modland_path", "size",
          "score", "alternates"]
LEET = str.maketrans("013457", "oieast")


def _fold(s):
    return norm(s.lower().translate(LEET))


def load_index(max_age_days=30):
    cache = os.path.join(APP_DIR, "cache", "allmods.txt")
    if not os.path.exists(cache) or time.time() - os.path.getmtime(cache) > max_age_days * 86400:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        data = urllib.request.urlopen(INDEX_URL, timeout=120).read()
        with zipfile.ZipFile(io.BytesIO(data)) as z, open(cache, "wb") as fh:
            fh.write(z.read("allmods.txt"))
    rows = []
    with open(cache, encoding="latin-1") as fh:
        for line in fh:
            size, _, path = line.rstrip("\n").partition("\t")
            if path.split("/", 1)[0] in AMIGA_FORMATS:
                rows.append((int(size), path))
    return rows


BRACKETS = re.compile(r"[\[\(][^\]\)]*[\]\)]")
# scene handles that Modland files under another name; extend in <app>/aliases.json
ALIASES = {"xsm": ["xtraspicymike"], "anorganik": ["anorganic"], "justincase": ["deetail"],
           "deetail": ["justincase"], "h0ffman": ["hoffman"]}


def _aliases():
    extra = {}
    p = os.path.join(APP_DIR, "aliases.json")
    if os.path.exists(p):
        import json
        with open(p, encoding="utf-8") as fh:
            extra = {_fold(k): [_fold(x) for x in v] for k, v in json.load(fh).items()}
    return {**ALIASES, **extra}


def _title_keys(title):
    return {k for k in (_fold(title), _fold(BRACKETS.sub("", title))) if k}


def _artist_keys(artist):
    parts = re.split(r"\s*(?:\^|&|,|/|\band\b|\baka\b|\(|\))\s*", artist or "", flags=re.I)
    keys = {_fold(p) for p in parts if _fold(p)}
    aliases = _aliases()
    return keys | {a for k in keys for a in aliases.get(k, [])}


def prepare(index):
    """[(size, path)] -> [(folded basename, folded dir names, size, path, format)]."""
    out = []
    for size, path in index:
        segs = path.split("/")
        name = re.sub(r"\.(mod|nst|stk|m15)$", "", segs[-1], flags=re.I)
        base = _fold(BRACKETS.sub("", name)) or _fold(name)
        if base:
            dirs = {_fold(BRACKETS.sub("", s.replace("coop-", ""))) for s in segs[1:-1]}
            out.append((base, dirs, size, path, segs[0]))
    return out


def match(artist, title, index):
    """Best Modland candidates for one track (index from prepare())
    -> (status, best, alternates); each candidate is (score, ratio, artist_ok, size, path)."""
    akeys = _artist_keys(artist)
    best_ratio = {}
    for key in _title_keys(title):
        sm = difflib.SequenceMatcher(None, key)
        sm.set_seq2(key)
        for i, (base, _, _, _, _) in enumerate(index):
            sm.set_seq1(base)
            if sm.real_quick_ratio() < 0.8 or sm.quick_ratio() < 0.8:
                continue
            r = sm.ratio()
            if r >= 0.8 and r > best_ratio.get(i, 0):
                best_ratio[i] = r
    scored = []
    for i, ratio in best_ratio.items():
        _, dirs, size, path, fmt = index[i]
        amatch = bool(akeys & dirs)
        score = ratio * 10 + (5 if amatch else 0) + (1 if fmt == "Protracker" else 0)
        scored.append((round(score, 2), round(ratio, 3), amatch, size, path))
    if not any(x[2] for x in scored):
        # nothing by this artist under that title: look inside the artist's own folders
        for base, dirs, size, path, fmt in index:
            if akeys & dirs:
                r = max(difflib.SequenceMatcher(None, k, base).ratio() for k in _title_keys(title))
                if r >= 0.6:
                    scored.append((round(r * 10 + 5, 2), round(r, 3), True, size, path))
    scored.sort(reverse=True)
    if not scored:
        return "none", None, []
    best = scored[0]
    status = "auto" if best[1] >= 0.92 and best[2] else "check"
    return status, best, scored[1:4]


def read_csv(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as fh:
        return {(r["video_id"], int(r["track"])): r for r in csv.DictReader(fh)}


def write_csv(path, rows):
    rows = sorted(rows.values(), key=lambda r: (r["video_id"], int(r["track"])))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)


def download(modland_path, dest_dir):
    dst = os.path.join(dest_dir, modland_path.replace("/", "__"))
    if not os.path.exists(dst):
        os.makedirs(dest_dir, exist_ok=True)
        url = FILE_URL + urllib.parse.quote(modland_path)
        data = urllib.request.urlopen(url, timeout=60).read()
        with open(dst, "wb") as fh:
            fh.write(data)
    return dst


def render(module_path, out_flac):
    """Render a module to 24-bit FLAC with libopenmpt (openmpt123)."""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "song" + os.path.splitext(module_path)[1])
        shutil.copyfile(module_path, src)
        subprocess.run([settings()["openmpt123"], "--render", "--force", "--output-type",
                        "flac"] + RENDER_ARGS + [src], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.makedirs(os.path.dirname(out_flac), exist_ok=True)
        shutil.move(src + ".flac", out_flac)
    return out_flac
