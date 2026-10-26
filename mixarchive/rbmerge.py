"""Merge one rekordbox playlist into another (master.db). The target keeps its tracks in its
order and the source's follow in theirs; where the merged list holds copies of the same
recording, only the best copy stays and the others' cues move onto it. pyrekordbox needs a
newer Python than the main venv, so `mixarchive merge` runs this file with rbvenv's Python:

    rbvenv\\Scripts\\python -I mixarchive\\rbmerge.py SOURCE TARGET [--apply] [--keep-source]
        [--ffmpeg=PATH] [--backup-dir=DIR] [--written=rbsync_written.json] [--root=DIR]
        [--db=COPY_OF_master.db]

SOURCE / TARGET: a playlist name, or its path with " / " ("mixarchive / Some Label").

Copies of one recording: two tracks of the merged list (at least one from SOURCE) whose
artist + title or file name words match and whose lengths are within 20 s (2 % for long
mixes), or that share two name words and are the same length to within 2 s (names an old
export cut short), confirmed by ear: the first SECONDS of both, correlated at SR Hz, must line up at
CORR_SAME or better (between CORR_UNSURE and CORR_SAME both stay and are reported). In
each group of copies:
  - tracks rbsync put in a mixarchive TARGET always stay (Sync would bring them back);
    which those are comes from --written (rbsync's state file), or else every track under
    --root (the library) counts
  - of the others, the best one stays: WAV / AIFF, then FLAC, then the highest bitrate.
    Next to a mixarchive copy it stays only if it's lossless or at least 320 kbps (a
    bought file, not a lower-bitrate rip of what mixarchive already has)
  - the rest leave the playlist, not the collection (play histories keep them). Their
    cues are copied to the copy that stays, shifted by the measured offset; a cue is
    skipped where that copy already has one of the same kind within 50 ms (a memory cue:
    MEMORY_NEAR), and a hot cue whose pad is already taken there is skipped and reported
A track listed twice is listed once. SOURCE is deleted afterwards (--keep-source keeps it);
SOURCE can't be one of mixarchive's own playlists, which Sync would bring back.

Without --apply it only reports. With it (rekordbox closed), master.db (+ -wal/-shm) and
masterPlaylists6.xml are copied to --backup-dir/<date> merge first (the last KEEP_BACKUPS
are kept), with plan.csv listing what happened to every track.
"""
import argparse
import csv
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import defaultdict
from uuid import uuid4

import numpy as np
from pyrekordbox import MasterDatabase
from pyrekordbox.masterdb import models
from pyrekordbox.utils import get_rekordbox_pid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # -I leaves this folder out
from rbdb import Ids, key, song_ids, sync_songs    # noqa: E402

FOLDER = "mixarchive"
SR = 4000                  # Hz: plenty to tell recordings apart and place cues to the ms
SECONDS = 240              # compared from the start of each track
CORR_SAME, CORR_UNSURE = 0.85, 0.5
MEMORY_NEAR = 1000         # ms: a memory cue this close to one already there is the same cue
KEEP_BACKUPS = 10
LOSSLESS = {".wav": 2, ".aif": 2, ".aiff": 2, ".flac": 1}
STOP = {"the", "a", "an", "and", "feat", "ft", "original", "mix", "official", "audio", "video"}


def path_of(lists, p):
    parts = []
    while p is not None:
        parts.append(p.Name)
        p = lists.get(str(p.ParentID))
    return " / ".join(reversed(parts))


def find_playlist(lists, name):
    want = re.sub(r"\s*/\s*", " / ", name.strip()).casefold()
    hits = [p for p in lists.values() if p.Attribute == 0 and want in (p.Name.casefold(), path_of(lists, p).casefold())]
    if not hits:
        raise SystemExit(f"no playlist called '{name}'")
    if len(hits) > 1:
        raise SystemExit(f"'{name}' is more than one playlist: " + "; ".join(path_of(lists, p) for p in hits)
                         + " - give the full path")
    return hits[0]


def words(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\[[^\]]*\]|\(\d+m\d+s\)|-\d+$", " ", s)       # [MH019], (4m43s), "-1" second copies
    return frozenset(re.sub(r"[^a-z0-9]+", " ", s).split()) - STOP


def names(c):
    stem = os.path.splitext(os.path.basename(c.FolderPath or ""))[0]
    artist = c.Artist.Name if c.Artist else ""
    return [w for w in (words(f"{artist} {c.Title or ''}"), words(stem)) if len(w) >= 2]


def covers(small, big):
    """Every word of `small` is in `big`, allowing for a cut-off ending ("Expansiv")."""
    return all(any(w == x or (min(len(w), len(x)) >= 4 and (w.startswith(x) or x.startswith(w))) for x in big)
               for w in small)


def maybe_same(a, b, na, nb):
    la, lb = a.Length or 0, b.Length or 0
    if abs(la - lb) > max(20, 0.02 * max(la, lb)):
        return False
    if any(covers(x, y) or covers(y, x) for x in na for y in nb):
        return True
    # names an old export cut short ("Life in the Zone (2"): the same length to the second will do
    return abs(la - lb) <= 2 and len(frozenset().union(*na) & frozenset().union(*nb)) >= 2


def quality(c):
    return LOSSLESS.get(os.path.splitext(c.FolderPath or "")[1].lower(), 0), c.BitRate or 0


def fmt(c):
    ext = os.path.splitext(c.FolderPath or "")[1].lower().lstrip(".")
    return ext.upper() if ext in ("wav", "aif", "aiff", "flac") else f"{c.BitRate or '?'}k"


class Ear:
    """Decoded starts of tracks (mono, SR Hz), a bounded cache."""

    def __init__(self, ffmpeg, size=160):
        self.ffmpeg, self.size, self.cache = ffmpeg, size, {}

    def pcm(self, c):
        if c.ID not in self.cache:
            if len(self.cache) >= self.size:
                self.cache.pop(next(iter(self.cache)))
            r = subprocess.run([self.ffmpeg, "-nostdin", "-v", "error", "-t", str(SECONDS), "-i", c.FolderPath,
                                "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True)
            x = np.frombuffer(r.stdout, dtype=np.float32)
            self.cache[c.ID] = x - x.mean() if len(x) > SR else None
        return self.cache[c.ID]

    def match(self, a, b):
        """(correlation, lag in ms: a's time t is b's time t + lag), or None if either won't decode."""
        x, y = self.pcm(a), self.pcm(b)
        if x is None or y is None:
            return None
        n = 1 << int(np.ceil(np.log2(len(x) + len(y))))
        xc = np.fft.irfft(np.conj(np.fft.rfft(x, n)) * np.fft.rfft(y, n), n)
        k = int(np.argmax(np.abs(xc)))
        lag = k if k < n // 2 else k - n
        u, v = (x[:len(y) - lag], y[lag:lag + len(x)]) if lag >= 0 else (x[-lag:], y[:len(x) + lag])
        m = min(len(u), len(v))
        if m < 0.6 * min(len(x), len(y)):          # barely overlapping: not a copy
            return 0.0, 0
        u, v = u[:m], v[:m]
        return abs(float(np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v) + 1e-9))), round(lag * 1000 / SR)


def plan_merge(db, src, dst, protected, ear):
    """Who stays, who goes where, and which cues move."""
    rows = {c.ID: c for c in db.get_content()}
    s_ids, d_ids = song_ids(db, src), song_ids(db, dst)
    merged = list(dict.fromkeys(c for c in d_ids + s_ids if c in rows))
    repeats = len(d_ids) + len(s_ids) - len(merged) - len(set(d_ids) & set(s_ids))
    from_src = set(s_ids) - set(d_ids)
    pos = {c: i for i, c in enumerate(merged)}
    nm = {c: names(rows[c]) for c in merged}

    pairs = [(a, b) for i, a in enumerate(merged) for b in merged[i + 1:]
             if (a in from_src or b in from_src) and maybe_same(rows[a], rows[b], nm[a], nm[b])]
    print(f"  listening to {len(pairs)} possible copies (same words in the name, similar length)")
    parent = {c: c for c in merged}

    def root(c):
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    unsure, unreadable, heard = [], set(), {}
    for a, b in pairs:
        m = ear.match(rows[a], rows[b])
        if m is None:
            unreadable.update(x for x in (a, b) if ear.pcm(rows[x]) is None)
            continue
        heard[a, b] = m
        if m[0] >= CORR_SAME:
            parent[root(a)] = root(b)
        elif m[0] >= CORR_UNSURE:
            unsure.append((a, b, m[0]))

    groups = defaultdict(list)
    for c in merged:
        groups[root(c)].append(c)
    fate = {}                    # content ID -> keeper ID for the copies that leave
    for members in groups.values():
        if len(members) < 2:
            continue
        mine = [c for c in members if c in protected]
        others = [c for c in members if c not in protected]
        if not others:
            continue
        best = max(others, key=lambda c: (quality(rows[c]), -pos[c]))
        q = quality(rows[best])
        if mine and not (q[0] or q[1] >= 320):
            best = None
        to = best or min(mine, key=lambda c: pos[c])
        for c in others:
            if c != best:
                fate[c] = to

    have = defaultdict(list)
    cues = defaultdict(list)
    for q in db.query(models.DjmdCue):
        if not q.rb_local_deleted:
            cues[q.ContentID].append(q)
    for c in merged:
        have[c] = [(q.Kind, q.InMsec) for q in cues[c]]
    moves, clashes, unaligned = [], [], []
    shift = {}
    for c, to in fate.items():
        if not cues[c]:
            continue
        m = heard.get((c, to)) or heard.get((to, c))
        if m and (c, to) not in heard:
            m = (m[0], -m[1])
        m = m or ear.match(rows[c], rows[to])      # copies by way of a third one
        if not m or m[0] < CORR_SAME:
            unaligned.append((c, to))
            continue
        shift[c] = m[1]
        for q in cues[c]:
            t = q.InMsec + m[1]
            if t < 0 or (rows[to].Length and t > rows[to].Length * 1000):
                continue
            same = [h for h in have[to] if h[0] == q.Kind]
            if any(abs(ht - t) < (MEMORY_NEAR if q.Kind == 0 else 50) for _, ht in same):
                continue
            if q.Kind > 0 and same:
                clashes.append((c, to, q.Kind))
                continue
            moves.append((q, to, m[1]))
            have[to].append((q.Kind, t))

    result = [c for c in merged if c not in fate]
    return {"rows": rows, "src": s_ids, "dst": d_ids, "merged": merged, "result": result,
            "repeats": repeats, "fate": fate, "heard": heard, "unsure": unsure, "unreadable": unreadable,
            "moves": moves, "clashes": clashes, "unaligned": unaligned, "shift": shift, "from_src": from_src}


def report(p, src_name, dst_name, protected):
    rows, fate = p["rows"], p["fate"]
    base = lambda c: os.path.basename(rows[c].FolderPath or "?")
    added = [c for c in p["result"] if c in p["from_src"]]
    print(f"  {dst_name}: {len(set(p['dst']))} -> {len(p['result'])} tracks")
    print(f"    + {len(added)} from {src_name}")
    shared = len(set(p["dst"]) & set(p["src"]))
    if shared:
        print(f"    = {shared} of {src_name}'s were in it already")
    if fate:
        hot = sum(1 for q, *_ in p["moves"] if q.Kind > 0)
        print(f"    - {len(fate)} copies of a track that stays ({len(p['moves'])} cues move to it, {hot} of them hot cues)")
    if p["repeats"]:
        print(f"    - {p['repeats']} rows that listed a track a second time")
    if fate:
        print("  copies left out -> the copy that stays:")
        for c, to in sorted(fate.items(), key=lambda x: base(x[1]).casefold()):
            m = p["heard"].get((c, to)) or p["heard"].get((to, c))
            how = f"corr {m[0]:.2f}" if m else "same group"
            n_cues = sum(1 for q, *_ in p["moves"] if q.ContentID == c)
            print(f"    {base(c)[:52]:52} {fmt(rows[c]):>5} -> {base(to)[:44]:44} {fmt(rows[to]):>5}  {how}"
                  + (f"  {p['shift'][c]:+d} ms, {n_cues} cues" if c in p["shift"] else ""))
    for c, to, kind in p["clashes"]:
        print(f"  ! hot cue {kind} of {base(c)} not copied: {base(to)} already has one on that pad")
    for c, to in p["unaligned"]:
        print(f"  ! cues of {base(c)} not copied: couldn't line it up with {base(to)} directly")
    if p["unsure"]:
        print("  maybe copies, both kept (have a listen):")
        for a, b, corr in p["unsure"]:
            print(f"    {base(a)[:52]}  ~  {base(b)[:52]}  corr {corr:.2f}")
    if p["unreadable"]:
        print(f"  couldn't decode {len(p['unreadable'])} (kept): " + "; ".join(base(c) for c in list(p["unreadable"])[:5]))
    mx = sum(1 for c in p["result"] if c in protected)
    if protected:
        print(f"  ({mx} of the result are mixarchive's own; Sync keeps the rest after them, as they are now)")


def backup(db, backup_dir):
    dest = os.path.join(backup_dir, datetime.datetime.now().strftime("%Y-%m-%d %H%M%S merge"))
    os.makedirs(dest)
    for f in ("master.db", "master.db-wal", "master.db-shm", "masterPlaylists6.xml"):
        src = os.path.join(str(db.db_directory), f)
        if os.path.exists(src):
            shutil.copy2(src, dest)
    old = sorted(d for d in os.listdir(backup_dir) if d.endswith(" merge"))[:-KEEP_BACKUPS]
    for d in old:
        shutil.rmtree(os.path.join(backup_dir, d))
    return dest


def write_plan_csv(path, p, src_name, dst_name):
    rows, fate = p["rows"], p["fate"]
    moved = defaultdict(int)
    for q, *_ in p["moves"]:
        moved[q.ContentID] += 1
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["from", "result", "file", "format", "copy that stays", "correlation", "offset ms", "cues moved"])
        for c in p["merged"]:
            to = fate.get(c)
            m = to and (p["heard"].get((c, to)) or p["heard"].get((to, c)))
            w.writerow([src_name if c in p["from_src"] else dst_name, "left out (copy)" if to else "kept",
                        rows[c].FolderPath, fmt(rows[c]), rows[to].FolderPath if to else "",
                        f"{m[0]:.3f}" if m else "", p["shift"].get(c, ""), moved.get(c, "")])


def main(argv):
    ap = argparse.ArgumentParser(prog="mixarchive merge", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("target")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--keep-source", action="store_true")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--backup-dir", default=os.path.join(os.path.expanduser("~"), "rekordbox backups"))
    ap.add_argument("--written", help="rbsync's state file (which tracks it put in each playlist)")
    ap.add_argument("--root", help="the mixarchive library (its tracks count as Sync's without --written)")
    ap.add_argument("--db", help="a copy of master.db to work on instead (tests)")
    a = ap.parse_args(argv)
    if a.apply and get_rekordbox_pid():
        raise SystemExit("rekordbox is running - close it and run this again")

    db = MasterDatabase(a.db)
    lists = {str(p.ID): p for p in db.get_playlist() if not p.rb_local_deleted}
    src, dst = find_playlist(lists, a.source), find_playlist(lists, a.target)
    src_name, dst_name = path_of(lists, src), path_of(lists, dst)
    if src.ID == dst.ID:
        raise SystemExit("that's the same playlist twice")
    in_folder = lambda p: (str(p.ParentID) in lists and lists[str(p.ParentID)].Name == FOLDER
                           and str(lists[str(p.ParentID)].ParentID) == "root")
    if in_folder(src) and not a.keep_source:
        raise SystemExit(f"{src_name} is one of mixarchive's playlists: Sync would bring it back. "
                         "Merge it the other way round, or add --keep-source")
    protected = set()
    if in_folder(dst):
        written = None
        if a.written and os.path.exists(a.written):
            with open(a.written, encoding="utf-8") as fh:
                written = json.load(fh).get(dst.Name)
        if written is not None:
            protected = set(written)
        elif a.root:
            prefix = key(a.root).rstrip("/") + "/"
            protected = {c.ID for c in db.get_content() if key(c.FolderPath or "").startswith(prefix)}

    print(f"  {src_name} ({len(song_ids(db, src))} rows) into {dst_name} ({len(song_ids(db, dst))} rows"
          + (f", {len(protected & set(song_ids(db, dst)))} of them from mixarchive's Sync)" if protected else ")"))
    p = plan_merge(db, src, dst, protected, Ear(a.ffmpeg))
    report(p, src_name, dst_name, protected)
    gone = "kept" if a.keep_source else "deleted (its tracks stay in the collection and in play histories)"
    print(f"  {src_name} will be {gone}")
    if not a.apply:
        print("  dry run - nothing written (add --apply, with rekordbox closed)")
        return

    dest = backup(db, a.backup_dir)
    write_plan_csv(os.path.join(dest, "plan.csv"), p, src_name, dst_name)
    print(f"  backup + plan.csv -> {dest}")
    ids, now = Ids(db), datetime.datetime.now()
    for q, to, shift in p["moves"]:
        t_in = q.InMsec + shift
        out = q.OutMsec + shift if q.OutMsec is not None and q.OutMsec >= 0 else -1
        db.add(models.DjmdCue.create(
            ID=ids.new(models.DjmdCue), ContentID=to, InMsec=t_in, InFrame=t_in * 150 // 1000,
            InMpegFrame=0, InMpegAbs=0, OutMsec=out, OutFrame=out * 150 // 1000 if out >= 0 else 0,
            OutMpegFrame=0, OutMpegAbs=0, Kind=q.Kind, Color=q.Color, ColorTableIndex=q.ColorTableIndex,
            ActiveLoop=q.ActiveLoop, Comment=q.Comment, BeatLoopSize=q.BeatLoopSize, CueMicrosec=q.CueMicrosec,
            ContentUUID=p["rows"][to].UUID, UUID=str(uuid4()), rb_data_status=0, rb_local_data_status=0,
            rb_local_deleted=0, rb_local_synced=0, created_at=now, updated_at=now))
    add, rem = sync_songs(db, dst, p["result"])
    if not a.keep_source:
        db.delete_playlist(src)
    db.commit()
    print(f"  done: {dst_name} +{add} -{rem}, {len(p['moves'])} cues copied"
          + ("" if a.keep_source else f", {src_name} deleted"))
    db.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.getLogger("pyrekordbox").setLevel(logging.ERROR)
    main(sys.argv[1:])
