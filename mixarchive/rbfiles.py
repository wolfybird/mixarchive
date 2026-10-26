"""Check, relocate and move the files behind rekordbox's whole collection (master.db),
keeping cues, beat grids and analysis. pyrekordbox needs a newer Python than the main
venv, so `mixarchive files ...` runs this file with rbvenv's Python:

    rbvenv\\Scripts\\python -I mixarchive\\rbfiles.py COMMAND [filters] [options] [--db=COPY]

commands
  status    rekordbox open?, tracks, analyzed, missing files, the mixarchive playlists,
            and where the files live (--json: the same for the GUI)
  audit     tracks / GB / missing / size changed per location and format;
            --list prints every matching file, --csv=PATH writes them all
  verify    every matching file exists with the size rekordbox recorded; --decode
            also decodes each one with ffmpeg (--ffmpeg=PATH)
  relocate  missing files: look for the same name (+ size) under --search DIRs and
            point rekordbox at them
  move      move matching files to --to DIR and point rekordbox at the new place
            (--copy leaves the originals). --layout: keep (folders below --under, or below
            the files' common folder), flat, playlist (DIR/<smallest playlist holding it>),
            artist (DIR/<artist>/<album>)
  flac      convert matching WAV / AIFF files to FLAC under --to DIR (--layout as for
            move) and point rekordbox at them. Each FLAC is decoded and compared sample
            for sample with its original before rekordbox is touched; tags and cover art
            come along, with rekordbox's title / artist / album / genre on top. The
            originals stay where they are. Files FLAC can't hold losslessly (32-bit float)
            are copied as they are.
  undo      reverse a relocate / move / flac: undo JOURNAL.csv (--keep: rekordbox goes
            back to the originals but the copies / FLACs stay where they are)
  flaccopy  keep --to a FLAC copy of the WAV / AIFF files under --from, for backups (same
            audio, about half the size). New or changed files are encoded and checked,
            copies whose source is gone are deleted, files FLAC can't hold losslessly are
            copied as they are. Doesn't touch rekordbox.
  silence   silent gaps inside the matching tracks: stretches of --min-gap seconds or more
            (default 2) at least 30 dB under the track's typical level, between where the
            music first gets going and where it last is (quiet intros and endings don't
            count). Each file is decoded with ffmpeg, --jobs at a time (default 8); results
            are kept in --cache, so a re-run scans only new or changed files. --list prints
            every track with a gap, --csv=PATH writes every gap. --cues --apply puts a memory
            cue (comment "gap N s") at each dropout-like gap at least 20 s from either end;
            otherwise it doesn't touch rekordbox.

filters (any mix): --ext=wav,aif,aiff  --under=DIR  --playlist=NAME (a folder includes
everything below it)  --missing

relocate / move / flac / undo only report unless --apply, and write only while rekordbox is
closed. Before writing they copy master.db, masterPlaylists6.xml and the analysis files
of every touched track to --backup-dir/<date> files-<command>, with journal.csv listing
each change (the input for undo). If anything fails midway, the files are put back,
the analysis files restored and the database left unchanged.

--area=NAME=DIR (repeatable) names locations in reports; --mirror=DIR is the USB
stick's backup folder: tracks playing from inside it are flagged.
"""
import argparse
import csv
import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from pyrekordbox import MasterDatabase
from pyrekordbox.masterdb import models
from pyrekordbox.utils import get_rekordbox_pid

KEEP_BACKUPS = 10
JOURNAL = "journal.csv"
FOLDER, NOT_ANALYZED = "mixarchive", "Not analyzed yet"
MIRROR = "USB stick backup"
PCM_EXTS = (".wav", ".aif", ".aiff")
TO_FLAC = {"pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be"}   # what FLAC holds as it is


def key(path):
    """Case-insensitive like NTFS (no Unicode normalization, as in rbdb.py)."""
    return os.path.abspath(path).replace("\\", "/").casefold()


def native(path):
    return os.path.normpath(path) if path else ""


def fmt(path):
    return os.path.splitext(path)[1].lstrip(".").upper() or "?"


def gb(n):
    return f"{n / 1e9:.1f}"


def safe(part):
    part = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", part or "").strip(" .")
    return part[:120] or "_"


def same_drive(a, b):
    return os.path.splitdrive(os.path.abspath(a))[0].lower() == os.path.splitdrive(os.path.abspath(b))[0].lower()


class Library:
    """The collection (content rows), its playlists and who holds what."""

    def __init__(self, db_path, areas, mirror):
        self.db = MasterDatabase(db_path)
        self.rows = [c for c in self.db.get_content() if c.FolderPath]
        self.lists = {str(p.ID): p for p in self.db.get_playlist()}
        self.members = defaultdict(set)              # content ID -> playlist IDs
        for s in self.db.query(models.DjmdSongPlaylist):
            self.members[str(s.ContentID)].add(str(s.PlaylistID))
        self.areas = sorted(((name, key(d).rstrip("/") + "/") for name, d in areas),
                            key=lambda x: -len(x[1]))
        if mirror:
            self.areas.insert(0, (MIRROR, key(mirror).rstrip("/") + "/"))
        self.size = {}                               # disk size, or None when missing

    def disk_size(self, c):
        if c.ID not in self.size:
            p = native(c.FolderPath)
            self.size[c.ID] = os.path.getsize(p) if os.path.exists(p) else None
        return self.size[c.ID]

    def area(self, path):
        k = key(path)
        for name, prefix in self.areas:
            if k.startswith(prefix):
                return name
        return "/".join(native(path).replace("\\", "/").split("/")[:3])

    def path_of(self, pid):
        parts, p = [], self.lists.get(pid)
        while p is not None:
            parts.append(p.Name)
            p = self.lists.get(str(p.ParentID))
        return " / ".join(reversed(parts))

    def playlist_names(self, c):
        return sorted(self.lists[p].Name for p in self.members.get(str(c.ID), ()) if p in self.lists)

    def best_playlist(self, c):
        """The smallest regular playlist holding the track (HISTORY lists aside)."""
        held = [p for p in self.members.get(str(c.ID), ()) if p in self.lists
                and not self.lists[p].Name.upper().startswith("HISTORY")]
        if not held:
            return "No playlist"
        if not hasattr(self, "_sizes"):
            self._sizes = defaultdict(int)
            for pids in self.members.values():
                for p in pids:
                    self._sizes[p] += 1
        return self.lists[min(held, key=lambda p: (self._sizes[p], self.lists[p].Name))].Name

    def in_playlist(self, name):
        want = name.casefold()
        hit = {pid for pid in self.lists if want in (self.lists[pid].Name.casefold(),
                                                     self.path_of(pid).casefold())}
        if not hit:
            raise SystemExit(f"no playlist or folder called '{name}'")
        children = defaultdict(list)
        for pid, p in self.lists.items():
            children[str(p.ParentID)].append(pid)
        todo = list(hit)
        while todo:
            for ch in children[todo.pop()]:
                if ch not in hit:
                    hit.add(ch)
                    todo.append(ch)
        return {cid for cid, pids in self.members.items() if pids & hit}

    def select(self, a):
        exts = tuple("." + e.strip(" .").lower() for e in a.ext.split(",") if e.strip()) if a.ext else None
        under = key(a.under).rstrip("/") + "/" if a.under else None
        held = self.in_playlist(a.playlist) if a.playlist else None
        out = []
        for c in self.rows:
            if exts and not c.FolderPath.lower().endswith(exts):
                continue
            if under and not key(c.FolderPath).startswith(under):
                continue
            if held is not None and str(c.ID) not in held:
                continue
            if a.missing and self.disk_size(c) is not None:
                continue
            out.append(c)
        return out


# ---------------------------------------------------------------- reports

def summary(lib, rows):
    """[{area, format, tracks, bytes, missing, changed}] largest first."""
    groups = {}
    for c in rows:
        g = groups.setdefault((lib.area(c.FolderPath), fmt(c.FolderPath)),
                              {"tracks": 0, "bytes": 0, "missing": 0, "changed": 0})
        size = lib.disk_size(c)
        g["tracks"] += 1
        g["bytes"] += size if size is not None else (c.FileSize or 0)
        if size is None:
            g["missing"] += 1
        elif c.FileSize and size != c.FileSize:
            g["changed"] += 1
    return [{"area": a, "format": f, **g} for (a, f), g in
            sorted(groups.items(), key=lambda x: (x[0][0] != MIRROR, -x[1]["bytes"]))]


def print_summary(rows_by):
    print(f"  {'where':44} {'format':6} {'tracks':>7} {'GB':>7} {'missing':>8} {'size changed':>13}")
    for g in rows_by:
        print(f"  {g['area'][:44]:44} {g['format']:6} {g['tracks']:7,} {gb(g['bytes']):>7} "
              f"{g['missing'] or '':>8} {g['changed'] or '':>13}")


def mirror_note(lib, rows, mirror):
    inside = [c for c in rows if lib.area(c.FolderPath) == MIRROR]
    if inside:
        print(f"\n  ! {len(inside):,} tracks play from inside the USB stick's backup folder ({mirror}).\n"
              "    That folder is a copy of the stick, not a library: rekordbox re-files exports into\n"
              "    its Contents\\ and a stick backup can overwrite or prune them. `move` takes them out.")


def cmd_status(lib, a):
    rows = lib.rows
    folder = next((p for p in lib.lists.values() if p.Name == FOLDER and p.Attribute == 1
                   and str(p.ParentID) == "root"), None)
    by_id = {str(c.ID): c for c in rows}
    mx = []
    if folder:
        for pid, p in sorted(lib.lists.items(), key=lambda x: x[1].Seq or 0):
            if str(p.ParentID) == str(folder.ID) and p.Attribute == 0:
                cids = [cid for cid, pids in lib.members.items() if pid in pids and cid in by_id]
                mx.append({"name": p.Name, "tracks": len(cids),
                           "not_analyzed": sum(1 for cid in cids if not by_id[cid].AnalysisDataPath),
                           "bytes": sum(by_id[cid].FileSize or 0 for cid in cids)})
    out = {"running": bool(get_rekordbox_pid()), "tracks": len(rows),
           "analyzed": sum(1 for c in rows if c.AnalysisDataPath),
           "missing": sum(1 for c in rows if lib.disk_size(c) is None),
           "mixarchive": mx, "areas": summary(lib, rows), "mirror_area": MIRROR,
           "playlists": sorted((lib.path_of(pid) for pid, p in lib.lists.items() if p.Attribute == 0
                                and not lib.path_of(pid).startswith("PLAY HISTORY /")), key=str.casefold)}
    if a.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    print(f"  rekordbox is {'open' if out['running'] else 'closed'}; {out['tracks']:,} tracks, "
          f"{out['tracks'] - out['analyzed']:,} not analyzed, {out['missing']:,} missing files")
    for p in mx:
        print(f"    {FOLDER} / {p['name'][:44]:44} {p['tracks']:6,} tracks {gb(p['bytes']):>6} GB"
              + (f"  {p['not_analyzed']:,} not analyzed" if p["not_analyzed"] else ""))
    print()
    print_summary(out["areas"])
    mirror_note(lib, rows, a.mirror)


def cmd_audit(lib, a):
    rows = lib.select(a)
    print(f"  {len(rows):,} of {len(lib.rows):,} tracks match")
    print_summary(summary(lib, rows))
    if a.list:
        for c in sorted(rows, key=lambda c: key(c.FolderPath)):
            size = lib.disk_size(c)
            state = "MISSING" if size is None else f"{(size or 0) / 1e6:7.0f} MB"
            print(f"  {state:>10}  {c.FolderPath}   [{', '.join(lib.playlist_names(c)[:3])}]")
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "path", "format", "exists", "size_rekordbox", "size_disk", "analyzed",
                        "artist", "title", "playlists"])
            for c in rows:
                size = lib.disk_size(c)
                w.writerow([c.ID, native(c.FolderPath), fmt(c.FolderPath), size is not None,
                            c.FileSize, size, bool(c.AnalysisDataPath), c.Artist.Name if c.Artist else "",
                            c.Title, " | ".join(lib.playlist_names(c))])
        print(f"  wrote {a.csv}")
    mirror_note(lib, rows, a.mirror)


def cmd_verify(lib, a):
    rows = lib.select(a)
    if a.decode and not (a.ffmpeg and os.path.exists(a.ffmpeg)):
        raise SystemExit("--decode needs --ffmpeg=PATH")

    def check(c):
        p, size = native(c.FolderPath), lib.disk_size(c)
        if size is None:
            return c, "missing"
        if c.FileSize and size != c.FileSize:
            return c, f"size changed since rekordbox read it ({c.FileSize:,} -> {size:,} bytes)"
        if a.decode:
            err = subprocess.run([a.ffmpeg, "-nostdin", "-v", "error", "-i", p, "-map", "0:a", "-f", "null", "-"],
                                 capture_output=True, text=True, errors="replace").stderr.strip()
            if err:
                return c, "decode: " + err.splitlines()[0][:150]
        return c, None

    problems = []
    with ThreadPoolExecutor(max_workers=4 if a.decode else 16) as pool:
        for i, (c, why) in enumerate(pool.map(check, rows), 1):
            if why:
                problems.append((c, why))
            if a.decode and i % 50 == 0:
                print(f"    {i}/{len(rows)}", flush=True)
    print(f"  {len(rows):,} files checked{' and decoded' if a.decode else ''}: {len(problems)} problems")
    for c, why in problems[:200]:
        print(f"    {why}: {c.FolderPath}")


# ---------------------------------------------------------------- silent gaps

SCAN_VERSION = 1                # bump when the detector changes: cached results are then redone
SR, HOP, WIN = 8000, 800, 4     # mono 8 kHz; a level every 0.1 s, each the power over 0.4 s
REL, ABS = 30.0, -35.0          # quiet: REL dB under the track's typical level, and under ABS dBFS
GOING = 10.0                    # the music has got going once it's within GOING dB of typical
BLIP, QUIET_BLIP = 0.3, 2.0     # sound a gap can hold: any blip this short, or a quiet one (< typical - 15 dB)
KEEP_GAP = 1.0                  # gaps this long are cached; --min-gap picks the ones reported
ABRUPT, DIGITAL = 20.0, 0.5     # dropout-like: the level falls ABRUPT dB into the gap, or that share is digital silence
LONG = 15 * 60                  # longer files are mixes / tapes, where gaps between tracks are normal
EDGE = 20.0                     # --cues leaves out gaps this close to either end (split edges, stops, endings)
ACAPELLA = re.compile(r"\ba\s?(c+ap+|p+)el+as?\b", re.I)   # acapella, a cappella, "A Pella"


def track_levels(ffmpeg, path):
    """dB every HOP samples (power over WIN hops) and which hops are digital silence."""
    import numpy as np
    r = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", path, "-map", "0:a:0", "-ac", "1",
                        "-ar", str(SR), "-f", "s16le", "-"], capture_output=True)
    if r.returncode and not r.stdout:
        raise RuntimeError((r.stderr.decode(errors="replace").strip().splitlines() or ["decode failed"])[0][:150])
    x = np.frombuffer(r.stdout, dtype=np.int16)
    n = len(x) // HOP
    if n < 10 * SR // HOP:                       # under 10 s
        return None, None
    b = x[:n * HOP].reshape(n, HOP).astype(np.float32) / 32768
    power = np.convolve((b * b).mean(axis=1), np.ones(WIN) / WIN, mode="same")
    return 10 * np.log10(np.maximum(power, 1e-14)), np.abs(b).max(axis=1) <= 1 / 32768


def find_gaps(db, zero):
    """(typical level, [gaps of KEEP_GAP s or more]) - quiet stretches inside the music."""
    import numpy as np
    loud = db[db > -60]
    if len(loud) < 20:
        return None, []
    ref = float(np.median(loud))
    going = np.flatnonzero(db > ref - GOING)
    if not len(going):
        return round(ref, 1), []
    first, last = int(going[0]), int(going[-1])
    quiet = (db < ref - REL) & (db < ABS)
    hop_s, out, i = HOP / SR, [], first
    while i <= last:
        if not quiet[i]:
            i += 1
            continue
        k = i
        while True:                              # the gap goes on over blips of sound
            while k <= last and quiet[k]:
                k += 1
            n = k
            while n <= last and not quiet[n]:
                n += 1
            if n <= last and ((n - k) * hop_s <= BLIP
                              or ((n - k) * hop_s <= QUIET_BLIP and db[k:n].max() < ref - 15)):
                k = n
                continue
            break
        if (k - i) * hop_s >= KEEP_GAP:
            before = float(np.median(db[max(first, i - 5):i])) if i > first else ref
            out.append({"at": round(i * hop_s, 1), "dur": round((k - i) * hop_s, 1),
                        "level": round(float(np.median(db[i:k])), 1),
                        "drop": round(before - float(np.median(db[i:i + 5])), 1),
                        "digital": round(float(zero[i:k].mean()), 2)})
        i = k
    return round(ref, 1), out


def dropout_like(g):
    return g["digital"] >= DIGITAL or g["drop"] >= ABRUPT


def mmss(s):
    return f"{int(s // 60)}:{s % 60:04.1f}"


def cmd_silence(lib, a):
    if not a.ffmpeg:
        raise SystemExit("silence needs ffmpeg (--ffmpeg=PATH, or ffmpeg on PATH)")
    if not a.cache:
        raise SystemExit("silence needs --cache=PATH (where results are kept between runs)")
    rows = [c for c in lib.select(a) if lib.disk_size(c) is not None]
    cache = {}
    if os.path.exists(a.cache):
        with open(a.cache, encoding="utf-8") as fh:
            cache = json.load(fh)
    files = cache.get("files", {}) if cache.get("version") == SCAN_VERSION else {}

    def save():
        os.makedirs(os.path.dirname(os.path.abspath(a.cache)), exist_ok=True)
        with open(a.cache + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"version": SCAN_VERSION, "files": files}, fh)
        os.replace(a.cache + ".tmp", a.cache)

    stamp = {}
    for c in rows:
        st = os.stat(native(c.FolderPath))
        stamp[c.ID] = [st.st_size, int(st.st_mtime)]
    todo = [c for c in rows if files.get(key(c.FolderPath), {}).get("stamp") != stamp[c.ID]]
    print(f"  {len(rows):,} tracks: {len(rows) - len(todo):,} scanned before, {len(todo):,} to scan "
          f"({sum(c.Length or 0 for c in todo) / 3600:,.0f} h of audio, {a.jobs} at a time)", flush=True)

    def scan(c):
        try:
            db, zero = track_levels(a.ffmpeg, native(c.FolderPath))
            if db is None:
                return c, {"short": True}
            ref, gaps = find_gaps(db, zero)
            return c, {"ref": ref, "gaps": gaps, "secs": round(len(db) * HOP / SR)}
        except Exception as e:
            return c, {"error": str(e)[:200]}

    try:
        with ThreadPoolExecutor(max_workers=a.jobs) as pool:
            for i, (c, res) in enumerate(pool.map(scan, todo), 1):
                files[key(c.FolderPath)] = {"stamp": stamp[c.ID], **res}
                if i % 500 == 0 or i == len(todo):
                    print(f"    {i:,}/{len(todo):,} scanned", flush=True)
                    save()
    finally:
        save()

    def kind(c, r):
        if (r.get("secs") or c.Length or 0) > LONG:
            return "long"
        return "acapella" if ACAPELLA.search(f"{os.path.basename(c.FolderPath)} {c.Title or ''}") else "track"

    found, errors, short = [], [], 0
    for c in rows:
        r = files.get(key(c.FolderPath), {})
        if "error" in r:
            errors.append((c, r["error"]))
        short += bool(r.get("short"))
        gaps = [g for g in r.get("gaps", []) if g["dur"] >= a.min_gap]
        if gaps:
            found.append((c, r, gaps, kind(c, r)))
    tracks = [f for f in found if f[3] == "track"]
    drop = [f for f in tracks if any(dropout_like(g) for g in f[2])]
    n = len(rows) - len(errors) - short
    pct = lambda k, m: f"{k / m:.1%}" if m else "-"
    print(f"\n  silent gaps of {a.min_gap:g} s or more inside the music ({REL:g} dB under the track's typical level):")
    print(f"    {len(tracks):,} of {n:,} tracks ({pct(len(tracks), n)}): {len(drop):,} dropout-like (an abrupt drop, "
          f"or digital silence), {len(tracks) - len(drop):,} quiet passages only")
    others = {k: sum(1 for f in found if f[3] == k) for k in ("long", "acapella")}
    print(f"    not counted: {others['long']:,} mix-length files (over {LONG // 60} min) and "
          f"{others['acapella']:,} acapellas with gaps")
    if errors or short:
        print(f"    {len(errors):,} couldn't be decoded, {short:,} are under 10 s")

    by = defaultdict(lambda: [0, 0, 0])
    for c in rows:
        r = files.get(key(c.FolderPath), {})
        if "error" not in r and not r.get("short"):
            by[(lib.area(c.FolderPath), fmt(c.FolderPath))][0] += 1
    for c, _, gaps, _ in tracks:
        g = by[(lib.area(c.FolderPath), fmt(c.FolderPath))]
        g[1 if any(dropout_like(x) for x in gaps) else 2] += 1
    print(f"\n  {'where':44} {'format':6} {'tracks':>7} {'dropout-like':>13} {'quiet only':>11}")
    for (area, f), (m, d, q) in sorted(by.items(), key=lambda x: -x[1][0]):
        print(f"  {area[:44]:44} {f:6} {m:7,} {d:7,} {pct(d, m):>5} {q:6,} {pct(q, m):>5}")

    worst = sorted(((g, c) for c, _, gaps, _ in drop for g in gaps if dropout_like(g)), key=lambda x: -x[0]["dur"])
    if worst:
        print(f"\n  longest dropout-like gaps:")
        for g, c in worst[:25]:
            print(f"    {mmss(g['at']):>8} {g['dur']:5.1f} s {g['level']:6.0f} dB {g['digital']:4.0%} digital  "
                  f"{os.path.basename(c.FolderPath)[:70]}")
    if a.list:
        print()
        for c, r, gaps, k in sorted(found, key=lambda f: key(f[0].FolderPath)):
            print(f"  [{k}] {c.FolderPath}  (typical {r['ref']:.0f} dB)")
            for g in gaps:
                print(f"      {mmss(g['at']):>8} {g['dur']:5.1f} s {g['level']:6.0f} dB  drop {g['drop']:3.0f} dB "
                      f"{g['digital']:4.0%} digital  {'dropout-like' if dropout_like(g) else 'quiet'}")
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["path", "artist", "title", "kind", "gap", "at", "at_s", "duration_s", "level_db",
                        "typical_db", "drop_db", "digital_share", "length_s", "bitrate", "where", "playlists"])
            for c, r, gaps, k in found:
                for g in gaps:
                    w.writerow([native(c.FolderPath), c.Artist.Name if c.Artist else "", c.Title, k,
                                "dropout-like" if dropout_like(g) else "quiet", mmss(g["at"]), g["at"], g["dur"],
                                g["level"], r["ref"], g["drop"], g["digital"], r.get("secs"), c.BitRate,
                                lib.area(c.FolderPath), " | ".join(lib.playlist_names(c))])
        print(f"\n  wrote {a.csv}")
    if a.cues:
        add_gap_cues(lib, a, tracks)


def add_gap_cues(lib, a, found):
    """A memory cue ("gap N s") at each dropout-like gap at least EDGE s from either end."""
    plan = []
    for c, r, gaps, _ in found:
        secs = r.get("secs") or c.Length or 0
        have = [q.InMsec for q in lib.db.query(models.DjmdCue).filter_by(ContentID=c.ID, Kind=0)
                if not q.rb_local_deleted]
        for g in gaps:
            ms = int(g["at"] * 1000)
            if (dropout_like(g) and g["at"] >= EDGE and secs - g["at"] - g["dur"] >= EDGE
                    and not any(abs(ms - h) < 1000 for h in have)):
                plan.append((c, ms, g))
    print(f"\n  memory cues: {len(plan)} to add on {len({c.ID for c, *_ in plan})} tracks (dropout-like gaps "
          f"at least {EDGE:g} s from either end; none where a memory cue already sits)")
    if not plan:
        return
    if not a.apply:
        print("  report only - add --apply (rekordbox closed) to add them")
        return
    if get_rekordbox_pid():
        raise SystemExit("rekordbox is running - close it and run this again")
    dest = backup(lib, a, "silence", [])
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from rbdb import Ids
    ids, now = Ids(lib.db), datetime.datetime.now()
    for c, ms, g in plan:
        lib.db.add(models.DjmdCue.create(
            ID=ids.new(models.DjmdCue), ContentID=c.ID, InMsec=ms, InFrame=ms * 150 // 1000, InMpegFrame=0,
            InMpegAbs=0, OutMsec=-1, OutFrame=0, OutMpegFrame=0, OutMpegAbs=0, Kind=0, Color=-1,
            ColorTableIndex=0, ActiveLoop=0, Comment=f"gap {g['dur']:.0f}s", BeatLoopSize=0, CueMicrosec=0,
            ContentUUID=c.UUID, UUID=str(uuid4()), rb_data_status=0, rb_local_data_status=0, rb_local_deleted=0,
            rb_local_synced=0, created_at=now, updated_at=now))
    lib.db.commit()
    print(f"  added {len(plan)} memory cues; master.db as it was before: {dest}")


# ---------------------------------------------------------------- writes

def backup(lib, a, what, rows):
    """master.db (+wal/shm), masterPlaylists6.xml and the rows' analysis files."""
    db = lib.db
    dest = os.path.join(a.backup_dir, datetime.datetime.now().strftime(f"%Y-%m-%d %H%M%S files-{what}"))
    os.makedirs(dest)
    for f in ("master.db", "master.db-wal", "master.db-shm", "masterPlaylists6.xml"):
        src = os.path.join(str(db.db_directory), f)
        if os.path.exists(src):
            shutil.copy2(src, dest)
    share = str(db.share_directory)
    for c in rows:
        for p in db.get_anlz_paths(c).values():
            if p and os.path.exists(p):
                to = os.path.join(dest, "share", os.path.relpath(p, share))
                os.makedirs(os.path.dirname(to), exist_ok=True)
                shutil.copy2(p, to)
    old = sorted(d for d in os.listdir(a.backup_dir) if " files-" in d)[:-KEEP_BACKUPS]
    for d in old:
        shutil.rmtree(os.path.join(a.backup_dir, d))
    return dest


def restore_anlz(lib, dest):
    saved = os.path.join(dest, "share")
    for d, _, files in os.walk(saved):
        for f in files:
            to = os.path.join(str(lib.db.share_directory), os.path.relpath(os.path.join(d, f), saved))
            shutil.copy2(os.path.join(d, f), to)


def refresh_anlz_rows(db, c):
    """update_content_path rewrites the analysis files' path tag, and rekordbox keeps each
    analysis file's size + md5 in contentFile: bring those along."""
    share = str(db.share_directory)
    for f in db.query(models.ContentFile).filter(models.ContentFile.ContentID == c.ID):
        p = os.path.join(share, (f.Path or "").lstrip("/"))
        if not (f.Path or "").startswith("/PIONEER/USBANLZ/") or not os.path.exists(p):
            continue
        with open(p, "rb") as fh:
            data = fh.read()
        if (f.Size, f.Hash) != (len(data), hashlib.md5(data).hexdigest()):
            f.Size, f.Hash = len(data), hashlib.md5(data).hexdigest()


def apply(lib, a, plan, what):
    """plan: [(content, old, new, mode, fields)] - point each track at `new` and set
    `fields` (DjmdContent columns, or None). mode: move (the file goes there), copy (a copy
    goes there), relink (it's already there), flac (a FLAC of `old` is already there),
    drop (it's there; delete `old` once committed - undoing a copy or flac)."""
    if get_rekordbox_pid():
        raise SystemExit("rekordbox is running - close it and run this again")
    if not plan:
        return
    dest = backup(lib, a, what, [c for c, *_ in plan])
    print(f"  backup -> {dest}")
    done, db = [], lib.db
    try:
        with open(os.path.join(dest, JOURNAL), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["content_id", "mode", "old", "new", "was"])
            for i, (c, old, new, mode, fields) in enumerate(plan, 1):
                if mode in ("move", "copy"):
                    if os.path.exists(new):
                        raise RuntimeError(f"{new} exists already")
                    os.makedirs(os.path.dirname(new), exist_ok=True)
                    if mode == "move" and same_drive(old, new):
                        os.rename(old, new)
                    else:                        # other drive: copy now, delete after the commit
                        shutil.copy2(old, new)
                        if os.path.getsize(new) != os.path.getsize(old):
                            raise RuntimeError(f"copy of {old} came out a different size")
                was = {k: getattr(c, k) for k in fields or {}}
                done.append((c, old, new, mode))
                w.writerow([c.ID, mode, old, new, json.dumps(was) if was else ""])
                db.update_content_path(c, new, save=True, check_path=os.path.exists(new), commit=False)
                for k, v in (fields or {}).items():
                    setattr(c, k, v)
                refresh_anlz_rows(db, c)
                if i % 250 == 0:
                    print(f"    {i}/{len(plan)}", flush=True)
        db.commit()
    except BaseException:
        db.rollback()
        for c, old, new, mode in reversed(done):
            if mode == "move" and same_drive(old, new):
                os.rename(new, old)
            elif mode in ("move", "copy", "flac") and os.path.exists(new):
                os.remove(new)
        restore_anlz(lib, dest)
        print("  !! stopped: files put back, analysis files restored, rekordbox unchanged")
        raise
    for c, old, new, mode in done:
        if (mode == "move" and not same_drive(old, new)) or mode == "drop":
            if os.path.exists(old):
                os.remove(old)
    print(f"  done: {len(done):,} tracks repointed; journal: "
          f"{os.path.join(dest, JOURNAL)}")


def cmd_relocate(lib, a):
    missing = [c for c in lib.select(a) if lib.disk_size(c) is None]
    print(f"  {len(missing):,} matching tracks are missing")
    if not missing:
        return
    if not a.search:
        raise SystemExit("relocate needs --search=DIR (repeatable)")
    names = defaultdict(list)
    for top in a.search:
        for d, _, files in os.walk(top):
            for f in files:
                names[f.casefold()].append(os.path.join(d, f))
    plan, unsure, none = [], [], []
    for c in missing:
        name = (c.FileNameL or os.path.basename(c.FolderPath)).casefold()
        cands = names.get(name, [])
        same = [p for p in cands if not c.FileSize or os.path.getsize(p) == c.FileSize]
        if len(same) == 1:
            plan.append((c, native(c.FolderPath), same[0], "relink", None))
        elif cands:
            unsure.append((c, f"{len(cands)} files with that name, {len(same)} the same size"))
        else:
            none.append(c)
    print(f"  found {len(plan):,}; {len(unsure)} unsure (not changed); {len(none)} not found")
    for c, old, new, *_ in plan[:15]:
        print(f"    {old}\n      -> {new}")
    for c, why in unsure[:15]:
        print(f"    unsure ({why}): {c.FolderPath}")
    for c in none[:15]:
        print(f"    not found: {c.FolderPath}")
    if a.apply:
        apply(lib, a, plan, "relocate")
    elif plan:
        print("  report only - add --apply (rekordbox closed) to repoint them")


def targets(lib, a, rows, new_ext=lambda c: None):
    """[(content, old, new)]: where each file goes under --to by --layout (new_ext(c) can
    swap the extension), and how many are there already."""
    to = os.path.abspath(a.to)
    base = native(a.under) if a.under else os.path.commonpath([os.path.dirname(native(c.FolderPath)) for c in rows])

    def rel(c):
        name = os.path.basename(native(c.FolderPath))
        if new_ext(c):
            name = os.path.splitext(name)[0] + new_ext(c)
        if a.layout == "flat":
            return name
        if a.layout == "playlist":
            return os.path.join(safe(lib.best_playlist(c)), name)
        if a.layout == "artist":
            return os.path.join(safe(c.Artist.Name if c.Artist else "Unknown Artist"),
                                safe(c.Album.Name if c.Album else "Unknown Album"), name)
        return os.path.join(os.path.dirname(os.path.relpath(native(c.FolderPath), base)), name)

    out, taken, skipped = [], set(), 0
    for c in rows:
        old = native(c.FolderPath)
        new = os.path.join(to, rel(c))
        if key(new) == key(old):
            skipped += 1
            continue
        stem, ext = os.path.splitext(new)
        n = 2
        while key(new) in taken or os.path.exists(new):
            new, n = f"{stem} ({n}){ext}", n + 1
        taken.add(key(new))
        out.append((c, old, new))
    return out, skipped


def print_plan(lib, plan, to):
    folders = defaultdict(lambda: [0, 0])
    for c, _, new, *_ in plan:
        f = folders[os.path.relpath(os.path.dirname(new), to)]
        f[0] += 1
        f[1] += lib.disk_size(c)
    for f, (n, b) in sorted(folders.items(), key=lambda x: -x[1][1])[:30]:
        print(f"    {n:5,} files {gb(b):>6} GB  {f}")
    for c, old, new, *_ in plan[:8]:
        print(f"    e.g. {old}\n         -> {new}")


def check_room(size, to):
    free = shutil.disk_usage(os.path.splitdrive(to)[0] + "\\").free
    if size > free:
        raise SystemExit(f"  only {gb(free)} GB free on {os.path.splitdrive(to)[0]} - not enough")


def cmd_move(lib, a):
    if not a.to:
        raise SystemExit("move needs --to=DIR")
    rows = [c for c in lib.select(a) if lib.disk_size(c) is not None]
    if not rows:
        print("  no (existing) files match")
        return
    to = os.path.abspath(a.to)
    found, skipped = targets(lib, a, rows)
    plan = [(c, old, new, "copy" if a.copy else "move", None) for c, old, new in found]
    size = sum(lib.disk_size(c) for c, *_ in plan)
    fast = all(same_drive(o, n) for _, o, n, *_ in plan) and not a.copy
    print(f"  {'copy' if a.copy else 'move'} {len(plan):,} files ({gb(size)} GB) -> {to}"
          + ("  (same drive: instant renames)" if fast else "  (copied)")
          + (f"; {skipped} already there" if skipped else ""))
    print_plan(lib, plan, to)
    if not fast:
        check_room(size, to)
    if a.apply:
        apply(lib, a, plan, "copy" if a.copy else "move")
    else:
        print("  report only - add --apply (rekordbox closed) to do it")


def codec(a, path):
    r = subprocess.run([a.ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                        "-of", "csv=p=0", path], capture_output=True, text=True, errors="replace")
    return r.stdout.strip()


def pcm_md5(a, path):
    """md5 of the decoded audio as 32-bit samples, so 16- and 24-bit files compare as they are."""
    r = subprocess.run([a.ffmpeg, "-nostdin", "-v", "error", "-i", path, "-map", "0:a:0", "-c:a", "pcm_s32le",
                        "-f", "md5", "-"], capture_output=True, text=True, errors="replace")
    if r.returncode or not r.stdout.startswith("MD5="):
        raise RuntimeError("can't decode: " + (r.stderr.strip().splitlines() or ["?"])[0][:150])
    return r.stdout.strip()


def rb_tags(c):
    return [(k, v) for k, v in (("title", c.Title), ("artist", c.Artist and c.Artist.Name),
                                ("album", c.Album and c.Album.Name), ("genre", c.Genre and c.Genre.Name)) if v]


def to_flac(a, old, new, tags=()):
    """Encode `old` as `new` (the file's own tags, `tags` on top); it only appears there once
    it decodes to the same samples."""
    tags = [x for k, v in tags for x in ("-metadata", f"{k}={v}")]
    os.makedirs(os.path.dirname(new), exist_ok=True)
    part = new + ".part"
    try:
        r = subprocess.run([a.ffmpeg, "-nostdin", "-v", "error", "-y", "-i", old, "-map", "0:a:0", "-map", "0:v?",
                            "-c:a", "flac", "-compression_level", "8", "-c:v", "copy", "-disposition:v", "attached_pic",
                            "-map_metadata", "0", *tags, "-f", "flac", part],
                           capture_output=True, text=True, errors="replace")
        if r.returncode:
            raise RuntimeError("encode: " + (r.stderr.strip().splitlines() or ["?"])[0][:150])
        if pcm_md5(a, part) != pcm_md5(a, old):
            raise RuntimeError("the FLAC doesn't decode to the same audio")
        os.replace(part, new)
    finally:
        if os.path.exists(part):
            os.remove(part)


def cmd_flac(lib, a):
    if not a.to:
        raise SystemExit("flac needs --to=DIR")
    if not (a.ffmpeg and os.path.exists(a.ffmpeg) and a.ffprobe and os.path.exists(a.ffprobe)):
        raise SystemExit("flac needs --ffmpeg=PATH and --ffprobe=PATH")
    rows = [c for c in lib.select(a) if c.FolderPath.lower().endswith(PCM_EXTS) and lib.disk_size(c) is not None]
    if not rows:
        print("  no (existing) WAV / AIFF files match")
        return
    to = os.path.abspath(a.to)
    with ThreadPoolExecutor(max_workers=8) as pool:
        codecs = dict(zip([c.ID for c in rows], pool.map(lambda c: codec(a, native(c.FolderPath)), rows)))
    as_is = {cid for cid, k in codecs.items() if k not in TO_FLAC}
    found, skipped = targets(lib, a, rows, lambda c: None if c.ID in as_is else ".flac")
    size = sum(lib.disk_size(c) for c, *_ in found)
    n_as_is = sum(1 for c, *_ in found if c.ID in as_is)
    print(f"  {len(found) - n_as_is:,} files to FLAC, {n_as_is} copied as they are "
          f"({gb(size)} GB of WAV / AIFF) -> {to}" + (f"; {skipped} already there" if skipped else ""))
    for c, old, _ in found:
        if c.ID in as_is:
            print(f"    as is ({codecs[c.ID] or 'unreadable'}: FLAC can't hold it losslessly): {old}")
    print_plan(lib, found, to)
    check_room(size, to)
    if not a.apply:
        print("  report only - add --apply (rekordbox closed) to do it; the originals stay where they are")
        return
    if get_rekordbox_pid():
        raise SystemExit("rekordbox is running - close it and run this again")

    def encode(item):
        c, old, new = item
        if c.ID in as_is:
            return item, None
        try:
            to_flac(a, old, new, rb_tags(c))
            return item, None
        except Exception as e:
            return item, str(e)

    made, failed = [], []
    try:                    # every FLAC first (4 at a time), checked; rekordbox after
        with ThreadPoolExecutor(max_workers=4) as pool:
            for i, (item, err) in enumerate(pool.map(encode, found), 1):
                (failed if err else made).append((item, err))
                if i % 10 == 0 or i == len(found):
                    print(f"    {i}/{len(found)} encoded and checked", flush=True)
        for (c, old, _), err in failed:
            print(f"    ! left as it is ({err}): {old}")
        plan = [(c, old, new, "copy", None) if c.ID in as_is else
                (c, old, new, "flac", {"FileType": int(models.FileType.FLAC), "BitRate": 0,
                                       "FileSize": os.path.getsize(new)})
                for (c, old, new), _ in made]
        apply(lib, a, plan, "flac")
    except BaseException:   # the FLACs rekordbox doesn't point at (apply() rolled back, or never ran)
        for (c, old, new), _ in made:
            if c.ID not in as_is and os.path.exists(new) and key(c.FolderPath or "") != key(new):
                os.remove(new)
        raise
    flac = [(lib.disk_size(c), os.path.getsize(new)) for (c, _, new), _ in made if c.ID not in as_is]
    if flac:
        print(f"  {gb(sum(x for x, _ in flac))} GB of WAV / AIFF -> {gb(sum(y for _, y in flac))} GB of FLAC")


def cmd_flaccopy(a):
    if not (a.from_ and a.to):
        raise SystemExit("flaccopy needs --from=DIR (the WAV / AIFF files) and --to=DIR (their FLAC copies)")
    if not (a.ffmpeg and os.path.exists(a.ffmpeg) and a.ffprobe and os.path.exists(a.ffprobe)):
        raise SystemExit("flaccopy needs --ffmpeg=PATH and --ffprobe=PATH")
    src, dst = os.path.abspath(a.from_), os.path.abspath(a.to)
    if not os.path.isdir(src):
        raise SystemExit(f"no folder {src}")
    if key(dst).startswith(key(src) + "/") or key(src).startswith(key(dst) + "/") or key(src) == key(dst):
        raise SystemExit("--from and --to must be separate folders (--to gets cleaned up)")
    todo, keep, n = [], set(), 0
    for d, _, files in os.walk(src):
        for f in sorted(files):
            if not f.lower().endswith(PCM_EXTS):
                continue
            n += 1
            s = os.path.join(d, f)
            rel = os.path.relpath(s, src)
            flac, as_is = os.path.join(dst, os.path.splitext(rel)[0] + ".flac"), os.path.join(dst, rel)
            if key(flac) in keep:                 # x.wav next to x.aif: the second keeps its extension
                flac = os.path.join(dst, rel + ".flac")
            have = next((p for p in (flac, as_is) if os.path.exists(p)
                         and os.path.getmtime(p) >= os.path.getmtime(s)), None)
            keep.add(key(have or flac))
            if not have:
                todo.append((s, flac, as_is))
    stale = [os.path.join(d, f) for d, _, files in os.walk(dst) for f in files
             if f.lower().endswith(PCM_EXTS + (".flac", ".part")) and key(os.path.join(d, f)) not in keep]
    print(f"  {n:,} WAV / AIFF under {src}: {n - len(todo):,} copies up to date, {len(todo):,} to make, "
          f"{len(stale):,} to delete (their source is gone)")

    def make(item):
        s, flac, as_is = item
        try:
            if codec(a, s) in TO_FLAC:
                to_flac(a, s, flac)
                return flac, None
            os.makedirs(os.path.dirname(as_is), exist_ok=True)
            shutil.copy2(s, as_is + ".part")
            os.replace(as_is + ".part", as_is)
            return as_is, None
        except Exception as e:
            return s, str(e)

    failed = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for i, (p, err) in enumerate(pool.map(make, todo), 1):
            if err:
                failed.append((p, err))
                print(f"    ! {err}: {p}")
            elif i % 10 == 0 or i == len(todo):
                print(f"    {i}/{len(todo)} made and checked", flush=True)
    for p in stale:
        os.remove(p)
    for d, dirs, files in sorted(os.walk(dst), key=lambda x: -len(x[0])):
        if d != dst and not os.listdir(d):
            os.rmdir(d)
    if failed:
        raise SystemExit(f"  {len(failed)} copies failed - their WAV / AIFF is not covered")


def cmd_undo(lib, a):
    if not a.journal or not os.path.exists(a.journal):
        raise SystemExit("undo needs the journal.csv of a relocate / move / flac")
    by_id = {str(c.ID): c for c in lib.rows}
    plan, problems = [], []
    with open(a.journal, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            c = by_id.get(r["content_id"])
            if c is None or key(c.FolderPath) != key(r["new"]):
                problems.append(f"changed since: {r['new']}")
                continue
            mode = {"move": "move", "copy": "drop", "flac": "drop", "relink": "relink"}[r["mode"]]
            if mode == "drop" and a.keep:        # back to the originals, the copies stay
                mode = "relink"
            if mode == "move" and (not os.path.exists(r["new"]) or os.path.exists(r["old"])):
                problems.append(f"can't move back: {r['new']}")
                continue
            if mode in ("drop", "relink") and not os.path.exists(r["old"]):
                problems.append(f"original gone, keeping the copy: {r['new']}")
                continue
            plan.append((c, r["new"], r["old"], mode, json.loads(r.get("was") or "null")))
    print(f"  undo {len(plan):,} changes" + (f"; {len(problems)} can't be undone:" if problems else ""))
    for p in problems[:20]:
        print(f"    {p}")
    if a.apply:
        apply(lib, a, plan, "undo")
    else:
        print("  report only - add --apply (rekordbox closed) to undo")


def main(argv):
    ap = argparse.ArgumentParser(prog="mixarchive files", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("status", "audit", "verify", "relocate", "move", "flac", "undo", "flaccopy",
                                        "silence"))
    ap.add_argument("journal", nargs="?", help="undo: the journal.csv to reverse")
    ap.add_argument("--ext")
    ap.add_argument("--under")
    ap.add_argument("--playlist")
    ap.add_argument("--missing", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--csv")
    ap.add_argument("--decode", action="store_true")
    ap.add_argument("--search", action="append")
    ap.add_argument("--to")
    ap.add_argument("--layout", choices=("keep", "flat", "playlist", "artist"), default="keep")
    ap.add_argument("--copy", action="store_true")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--from", dest="from_")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", help="a copy of master.db to work on instead (tests)")
    ap.add_argument("--backup-dir", default=os.path.join(os.path.expanduser("~"), "rekordbox backups"))
    ap.add_argument("--ffmpeg")
    ap.add_argument("--ffprobe")
    ap.add_argument("--mirror")
    ap.add_argument("--area", action="append", default=[], help="NAME=DIR")
    ap.add_argument("--min-gap", type=float, default=2.0, help="silence: shortest gap reported, in seconds")
    ap.add_argument("--jobs", type=int, default=8, help="silence: files decoded at a time")
    ap.add_argument("--cache", help="silence: where results are kept between runs")
    ap.add_argument("--cues", action="store_true", help="silence: a memory cue at each dropout-like gap (--apply)")
    a = ap.parse_args(argv)
    for tool in ("ffmpeg", "ffprobe"):      # a full path, or a name on PATH
        path = getattr(a, tool)
        setattr(a, tool, path if path and os.path.isfile(path) else shutil.which(path) if path else None)
    if a.command == "flaccopy":             # files on disk only, no database
        return cmd_flaccopy(a)
    areas = [tuple(x.split("=", 1)) for x in a.area]
    lib = Library(a.db, areas, a.mirror)
    {"status": cmd_status, "audit": cmd_audit, "verify": cmd_verify, "relocate": cmd_relocate,
     "move": cmd_move, "flac": cmd_flac, "undo": cmd_undo, "silence": cmd_silence}[a.command](lib, a)
    lib.db.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.getLogger("pyrekordbox").setLevel(logging.ERROR)
    main(sys.argv[1:])
