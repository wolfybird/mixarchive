"""Singles: channels/playlists where each video is one track (label channels).

Each single is a row in the editable _mixarchive/tracks.csv (the source of truth, like
a mix's tracklist file). Rows come from the YouTube title at `resolve`, get years
from the metadata after `fetch`, and are exported whole (normalized + tagged) by
`split` into <root>/split/<format>/<singles folder>/Artist - Title.mp3.

Row statuses: ok | check (no artist found - have a look) | dup (same artist + title
as another upload) | skip (trailer, teaser, ...) | manual (hand-edited: never
overwritten). Set a row to manual/skip/dup by hand to override.
"""
import csv
import os
import re

from .util import norm, safe_name

FIELDS = ["video_id", "part", "start", "end", "status", "artist", "title", "album",
          "year", "year_src", "label", "catalog", "duration", "upload", "yt_title", "note"]
EXPORT = ("ok", "check", "manual")
YEAR = re.compile(r"(?<!\d)((?:19[5-9]|20[0-4])\d)(?!\d)")
CAT = re.compile(r"^[A-Z]{2,8}[- ]?\d{2,5}[A-Z]{0,2}$")      # DEEWEE067, HOTC030R, SR12166
VERSION = re.compile(r"\b(re-?mix|mix|edit|version|dub|rework|vip|bootleg|cut|instrumental|"
                     r"live|remaster(?:ed)?|reprise|demo)\b", re.I)
JUNK = re.compile(r"^(?:official(?:\s+(?:music|lyric|hd))?(?:\s+(?:video|audio|visuali[sz]er|"
                  r"clip|stream))?|(?:music|lyric) video|visuali[sz]er|audio|video|hd|hq|4k|"
                  r"premiere|out now|free download|full stream|lyrics?)$", re.I)
DASH = re.compile(r"\s+[-–—]\s+")
QUOTES = {"'": "'", '"': '"', "“": "”", "‘": "’"}


def _groups(title, label_keys):
    """Strip bracketed noise / label / year / catalog groups (innermost first).
    Returns (title, year, catalog, kind) where kind is 'audio'/'video'/''."""
    year = cat = kind = ""
    if title.count("(") > title.count(")"):           # YouTube cuts titles at 100 chars
        title += ")"
    pat = re.compile(r"\s*[\(\[]([^()\[\]]*)[\)\]]")
    while True:
        changed = False
        for m in reversed(list(pat.finditer(title))):
            body = m.group(1).strip()
            words = [w.strip() for w in re.split(r"[,/]", body) if w.strip()]
            drop = False
            if not body or JUNK.match(body):
                kind = kind or ("audio" if "audio" in body.lower() else "video" if body else "")
                drop = True
            elif CAT.match(body):
                cat, drop = cat or body, True
            elif YEAR.search(body) and not VERSION.search(body):
                year = year or YEAR.search(body).group(1)   # "(Strictly Rhythm, 1993)"
                cat = cat or next((w for w in words if CAT.match(w)), "")
                drop = True
            elif any(norm(body).startswith(k) for k in label_keys) and not VERSION.search(body):
                drop = True                                 # "[Mood Hut Records]"
            if drop:
                title = title[:m.start()] + title[m.end():]
                changed = True
        if not changed:
            break
    return title.strip(" -–—|"), year, cat, kind     # keep runs of spaces: lost dashes


def parse_title(yt_title, label="", channel=""):
    """'Artist - Title (Remix) [Official Audio] - DFA' -> dict(artist, title, album,
    year, catalog, kind). Handles quoted titles (Artist 'Title'), leading/trailing
    catalog numbers, label/year groups and 'Series: Artist - Title'."""
    label_keys = [k for k in {norm(label), norm(channel)} if len(k) >= 3]
    first_word = ((label or channel).split() or [""])[0].lower()
    t, album, note = yt_title, "", ""
    if " | " in t:
        left, _, right = t.partition(" | ")
        if DASH.search(left) and not DASH.search(right):
            t, album = left, right                      # "... | Strictly Cityscapes"
        elif DASH.search(right) and left.lower().startswith(first_word):
            t, album = right, left                      # "Strictly Bassment Hours | ..."
    t = re.sub(r"\s*\(?\b(?:full track|preview)\b\)?\s*$", "", t, flags=re.I)
    t, year, cat, kind = _groups(t, label_keys)
    t = re.sub(r"\s+[^\x00-\x7F]$", "", t)             # lone trailing mark: "Wéi ε"
    words = t.split()
    if words and CAT.match(words[0]):                   # "HOTC039 Paul C - ..."
        cat, t = cat or words[0], " ".join(words[1:]).lstrip(" -–—")
    parts = [p.strip() for p in DASH.split(t) if p.strip()]
    short = norm(first_word)                            # "... - DFA" for DFA Records
    while len(parts) > 1 and (CAT.match(parts[-1]) or norm(parts[-1]) in label_keys
                              or JUNK.match(parts[-1]) or (len(parts) > 2 and norm(parts[-1]) == short)):
        cat = cat or (parts[-1] if CAT.match(parts[-1]) else "")
        parts.pop()                                     # "... - DEEWEE030", "... - DFA", "- Video"
    if parts:
        last = parts[-1].split()
        if len(last) > 1 and CAT.match(last[-1]):       # "Phillipi - Tensao DEEWEE067"
            cat, parts[-1] = cat or last[-1], " ".join(last[:-1])
    t = " - ".join(parts)
    artist, title = "", t
    if len(parts) == 1:
        quoted = re.match(r"^(.*?)\s*([\'\"“‘])(.+)$", t)
        if quoted:                                      # Artist 'Title' (Mix) / 'Title' Artist
            before, close, rest = quoted.group(1), QUOTES[quoted.group(2)], quoted.group(3)
            ends = list(re.finditer(re.escape(close) + r"(?=\s|$|\()", rest))
            inner, after = (rest[:ends[-1].start()], rest[ends[-1].end():]) if ends else (rest, "")
            if before:
                artist, title = before, inner + after
            else:
                tail = re.findall(r"\([^()]*\)", after)
                artist = re.sub(r"\([^()]*\)", "", after)
                title = " ".join([inner] + tail)
        elif re.search(r"\S\s{2,}\S", t):               # "Jack J   Only You Know Why"
            artist, title = re.split(r"\s{2,}", t, maxsplit=1)
    elif len(parts) == 3 and re.search(r"\d", parts[0]):
        album, artist, title = parts                    # "Hot Waves 4 - Denney - Shake The Club"
    else:
        artist, title = parts[0], " - ".join(parts[1:])
        if artist[:1] in QUOTES:                        # "'Hoochie' - Tom Flynn"
            artist, title = title, artist
    if ": " in artist:                                  # "Strictly Bassment Hours: The Boss"
        album, artist = artist.split(": ", 1)
    clean = lambda s: re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", re.sub(r"\s+", " ", s))
                             ).strip().strip("'\"“”‘’").strip()
    return {"artist": clean(artist), "title": clean(title) or yt_title.strip(),
            "album": clean(album), "year": year, "catalog": cat, "kind": kind, "note": note}


def year_from_info(info, allow_upload=True):
    """(year, source) from YouTube music metadata, the description's (P) line, or upload."""
    y = str(info.get("release_year") or "")[:4]
    if y and YEAR.fullmatch(y):
        return y, "youtube"
    m = re.search(r"(?:℗|\(P\)|©)\s*((?:19|20)\d\d)", info.get("description") or "")
    if m:
        return m.group(1), "description"
    if allow_upload and info.get("upload_date"):
        return info["upload_date"][:4], "upload"
    return "", ""


def multi_track(info):
    """Chapters of a short video that is really several tracks ('Ice / Water World').
    Auto-made chapters from vinyl side lengths ('<Untitled Chapter 1>', 'A1 - ...')
    don't count."""
    ch = info.get("chapters") or []
    names = [c["title"] for c in ch]
    if len(ch) < 2 or any(n.startswith("<Untitled") for n in names):
        return []
    starts = [c["start_time"] for c in ch]
    if starts != sorted(set(starts)):
        return []
    return [(c["start_time"], c["end_time"], c["title"].strip()) for c in ch]


def _kind(row):
    return parse_title(row["yt_title"])["kind"]


def rank(row):
    """Which upload of a duplicated track to keep: official audio > plain > video, longest."""
    pref = {"audio": 2, "": 1, "video": 0}.get(_kind(row), 1)
    return (pref, float(row.get("duration") or 0))


def _same_cut(a, b):
    da, db = float(a.get("duration") or 0), float(b.get("duration") or 0)
    return abs(da - db) <= max(8, 0.05 * max(da, db))


def mark_dups(rows):
    """Same artist + title is a duplicate only if it's the same recording (lengths
    within ~5%) or a music video of a track that also has an audio upload. Other
    lengths are other cuts (12" vs radio edit) and are kept. 'dup' is recomputed on
    every resolve; hand-set statuses (manual / skip) are left alone."""
    groups = {}
    for r in rows:
        if r["status"] == "dup":
            r["status"], r["note"] = ("ok" if r["artist"] else "check"), ""
        if r["status"] in ("ok", "check") and str(r.get("part") or 1) == "1" and not r.get("end"):
            groups.setdefault(norm(r["artist"]) + "|" + norm(r["title"]), []).append(r)
    for group in groups.values():
        kept = []
        for r in sorted(group, key=rank, reverse=True):
            twin = next((k for k in kept if _same_cut(k, r)
                         or (_kind(r) == "video" and _kind(k) != "video")), None)
            if twin:
                r["status"], r["note"] = "dup", f"dup of {twin['video_id']}"
            else:
                kept.append(r)


def key(r):
    return r["video_id"], int(r.get("part") or 1)


def read(path):
    if not os.path.exists(path):
        return {}
    for enc in ("utf-8-sig", "cp1252"):                 # Excel may save either
        try:
            with open(path, encoding=enc, newline="") as fh:
                return {key(r): r for r in csv.DictReader(fh)}
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"can't read {path}")


def write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: (r.get("upload") or "", key(r))))
    os.replace(tmp, path)


def file_names(rows, ext):
    """{key: file name}. Different cuts sharing 'Artist - Title' get their length
    added ('... (6m33s)'); anything still clashing gets the video id."""
    base = {key(r): safe_name(f'{r["artist"]} - {r["title"]}' if r["artist"] else r["title"])[:150]
            for r in rows}
    counts = {}
    for b in base.values():
        counts[b.lower()] = counts.get(b.lower(), 0) + 1
    out, taken = {}, set()
    for r in rows:
        name = base[key(r)]
        if counts[name.lower()] > 1:
            start, end = float(r.get("start") or 0), r.get("end")
            sec = int((float(end) if end else float(r.get("duration") or 0)) - start)
            name = f"{name} ({sec // 60}m{sec % 60:02d}s)"
        if name.lower() in taken:
            name = f'{name} [{r["video_id"]}]'
        taken.add(name.lower())
        out[key(r)] = name + ext
    return out
