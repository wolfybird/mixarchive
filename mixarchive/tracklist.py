"""Tracklists: parse YouTube chapters / description / comments, and read/write the
editable per-mix tracklist file that the split step uses as its source of truth.

File format (UTF-8 text, one per mix, _mixarchive/tracklists/<video id>.txt):
    # comment lines start with '#'
    @album: Peshay - Studio Set (1996)          <- '@key: value' per-mix metadata
    @album_artist: Peshay
    00:00:00 Artist - Title {1996}              <- start, label, optional {year}
    00:05:12 Artist - Title {label: Moving Shadow}   <- optional record label
    --:--:-- Artist - Title                     <- untimed: blocks splitting this mix
"""
import os
import re

from .util import fmt_ts, to_sec

TS = re.compile(r"(?<![\d:])(\d{1,3}:\d{2}(?::\d{2})?)(?![\d:])")
UNTIMED = "--:--:--"
DASH = re.compile(r"\s+[-–—]\s+")
HEADER_KEYS = ("video", "url", "source", "status", "album", "album_artist", "year",
               "genre", "original")
# "01 Artist - Title 05:23" / "A1. Artist - Title (5:23)": per-track LENGTHS (CD listings)
DUR_LINE = re.compile(r"^\s*(?:[A-D]?\d{1,2})[\s.)\-–]+(.+?)\s+[\[\(]?(\d{1,2}:\d{2})[\]\)]?\s*$")


def clean_label(label):
    label = re.sub(r"^[\s\[\(]*\d{1,3}\s*[.):\]\-–—]\s*", "", label)  # "01." "1)" "01 –"
    label = re.sub(r"^[\s\-–—|•*>]+|[\s\-–—|•*]+$", "", label)
    label = re.sub(r"\s+", " ", label).strip()
    for o, c in ("()", "[]"):           # YouTube's chapter parser can drop a closing bracket
        label += c * max(0, label.count(o) - label.count(c))
    return label


def tidy(labels):
    """List-level cleanup: drop running numbers / vinyl sides, lift labels and years."""
    if not labels:
        return labels
    nums = [re.match(r"(\d{1,3})\s+\S", l) for l in labels]
    if sum(1 for m in nums if m) >= 0.8 * len(labels) and \
            [int(m.group(1)) for m in nums if m][:3] == [1, 2, 3][:len([m for m in nums if m][:3])]:
        labels = [re.sub(r"^\d{1,3}\s+", "", l) for l in labels]   # "1 Lazune" (not "4 Hero")
    side = re.compile(r"^[A-H]\d{1,2}\s*[.\-–—:]\s*")
    if sum(1 for l in labels if side.match(l)) >= 0.5 * len(labels):
        labels = [side.sub("", l) for l in labels]                  # "A1–Shifty"
    out = []
    bracket_tail = [re.search(r"\s*\[([^\]]+)\]\s*$", l) for l in labels]
    labelish = sum(1 for m in bracket_tail if m) >= 0.7 * len(labels) and \
        len({m.group(1) for m in bracket_tail if m}) > 1
    for l, bt in zip(labels, bracket_tail):
        m = re.search(r"\s*\(([^()]+?),\s*((?:19|20)\d\d)\)\s*$", l)   # "(Label, 1995)"
        if m:
            l = f"{l[:m.start()]} {{{m.group(2)}}} {{label: {m.group(1).strip()}}}"
        elif labelish and bt:                                       # "... [Fresh 86]"
            l = f"{l[:bt.start()]} {{label: {bt.group(1).strip()}}}"
        else:
            parts = re.split(r"\s+–\s+", l)                     # "A – Title – Label"
            if len(parts) == 3:
                l = f"{parts[0]} - {parts[1]} {{label: {parts[2]}}}"
        out.append(l)
    return out


def split_label(label):
    """'Artist - Title {1996} {label: X}' -> (artist, title, year_raw, record_label)."""
    year = rec = ""
    while True:
        m = re.search(r"\s*\{([^}]*)\}\s*$", label)
        if not m:
            break
        body, label = m.group(1).strip(), label[:m.start()]
        if body.lower().startswith("label:"):
            rec = body[6:].strip()
        else:
            year = body
    parts = DASH.split(label, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip(), year, rec
    return "", label.strip(), year, rec


def duration_sections(text):
    """Blocks of consecutive per-track-length lines (e.g. one block per CD)."""
    sections, cur = [], []
    for line in (text or "").splitlines():
        m = DUR_LINE.match(line)
        if m and DASH.search(m.group(1)):
            cur.append((clean_label(m.group(1)), to_sec(m.group(2))))
        elif cur:
            sections.append(cur)
            cur = []
    if cur:
        sections.append(cur)
    return [s for s in sections if len(s) >= 3]


def _overlap(labels, expect):
    import difflib
    keys = [re.sub(r"[^a-z0-9]", "", e.lower()) for e in expect]
    hits = 0
    for label in labels:
        k = re.sub(r"[^a-z0-9]", "", label.lower())
        if any(difflib.SequenceMatcher(None, k, e).ratio() >= 0.75 for e in keys):
            hits += 1
    return hits / max(1, len(labels))


def from_durations(texts, duration, tol_sec=15, expect=None):
    """Start times from a per-track-length list whose total matches the mix length.
    CD listings land within seconds of the upload length (calibrated: 0-8 s), while a
    different CD of similar length was 49 s off. If the mix has its own (untimed)
    tracklist, `expect`, a borrowed list must share at least half its tracks."""
    best = None
    for text in texts:
        for sec in duration_sections(text):
            off = abs(sum(d for _, d in sec) - duration)
            if off > tol_sec or (best is not None and off >= best[0]):
                continue
            if expect and _overlap([l for l, _ in sec], expect) < 0.5:
                continue
            best = (off, sec)
    if not best:
        return []
    out, t = [], 0
    for label, d in best[1]:
        out.append((t, label))
        t += d
    return out


def timed_lines(text):
    """Lines carrying a timestamp and some words -> [(sec, label)], time-sorted."""
    out = []
    for line in (text or "").splitlines():
        m = TS.search(line)
        if not m:
            continue
        rest = TS.sub(" ", line)                         # drop ranges like 00:00-05:12
        rest = re.sub(r"[\[\(]\s*[\]\)]", " ", rest)     # empty brackets left behind
        label = clean_label(rest)
        if re.search(r"[A-Za-z]{2}", label):
            out.append((to_sec(m.group(1)), label))
    out.sort(key=lambda x: x[0])
    dedup = []
    for sec, label in out:
        if not dedup or sec > dedup[-1][0]:
            dedup.append((sec, label))
    return dedup


def untimed_lines(text):
    return [clean_label(l) for l in (text or "").splitlines()
            if DASH.search(l) and not TS.search(l) and 4 < len(l) < 140
            and not re.search(r"https?://|www\.", l)]


def _looks_like_tracks(rows):
    return len(rows) >= 3 and sum(1 for _, l in rows if DASH.search(l)) >= len(rows) / 2


def _tidy_timed(rows):
    return list(zip([s for s, _ in rows], tidy([l for _, l in rows])))


def best_from_info(info):
    """Pick the best tracklist in a yt-dlp info dict.
    Returns (source, [(sec, label)], [untimed labels])."""
    dur = info.get("duration") or 1e9
    chapters = [(int(c["start_time"]), clean_label(c["title"]))
                for c in info.get("chapters") or []]
    # uploader-set markers; many plain titles (e.g. "song (game)") still count as tracks,
    # but a few undashed ones are usually show sections ("Intro", "Mix", "Outro")
    if len(chapters) >= 3 and (_looks_like_tracks(chapters) or len(chapters) >= 8):
        return "chapters", _tidy_timed(chapters), []

    def spans(rows):        # real start times reach well into the mix
        return _looks_like_tracks(rows) and rows[-1][0] >= 0.5 * dur

    desc = [r for r in timed_lines(info.get("description")) if r[0] < dur]
    if spans(desc):
        return "description", _tidy_timed(desc), []
    texts = [info.get("description")] + [c.get("text") for c in info.get("comments") or []]
    lengths = from_durations(texts, dur)
    if lengths:
        return "track lengths", _tidy_timed(lengths), []
    best = []
    for c in info.get("comments") or []:
        rows = [r for r in timed_lines(c.get("text")) if r[0] < dur]
        if spans(rows) and len(rows) > len(best):
            best = rows
    if best:
        return "comment", _tidy_timed(best), []
    for text, src in [(info.get("description"), "description (untimed)")] + \
            [(c.get("text"), "comment (untimed)") for c in info.get("comments") or []]:
        rows = untimed_lines(text)
        if len(rows) >= 3:
            return src, [], tidy(rows)
    return "none", [], []


def write(path, header, timed, untimed=()):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = ["# mixarchive tracklist - edit freely, then re-run `split`.",
             "# Lines: HH:MM:SS Artist - Title {year}.  '--:--:--' = no time yet "
             "(this mix won't be split until every line has one)."]
    lines += [f"@{k}: {header[k]}" for k in HEADER_KEYS if header.get(k)]
    lines += [f"{fmt_ts(sec)} {label}" for sec, label in timed]
    lines += [f"{UNTIMED} {label}" for label in untimed]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def read(path):
    """-> (header dict, [(sec, label)], [untimed labels])"""
    header, timed, untimed = {}, [], []
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("@") and ":" in line:
                k, v = line[1:].split(":", 1)
                header[k.strip()] = v.strip()
            elif line.startswith(UNTIMED):
                untimed.append(line[len(UNTIMED):].strip())
            else:
                m = re.match(r"(\d{1,3}:\d{2}(?::\d{2})?)\s+(.*)", line)
                if m:
                    timed.append((to_sec(m.group(1)), m.group(2).strip()))
    timed.sort(key=lambda x: x[0])
    return header, timed, untimed
