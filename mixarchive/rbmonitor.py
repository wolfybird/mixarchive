"""Keep tabs on every copy of the DJ library, and say where copies disagree and how.

`mixarchive monitor` runs this file with the Python that has pyrekordbox (like `files`):

    python -I rbmonitor.py --config=JSON [--list=CODE | --all] [--json] [--stick=DIR ...]
                           [--no-sticks] [--deep] [--db=COPY]

What it looks at:
- rekordbox's collection (master.db): its tracks, cues, playlists and play histories, and the
  files behind them (missing, or a different size than rekordbox recorded);
- each backup in the config (a folder mirrored to another, as backup_library.bat does):
  files not copied yet or changed since, WAV / AIFF files without their FLAC copy, and the
  collection's tracks that no backup covers;
- every rekordbox stick plugged in (and each --stick folder): its playlists and cues against
  the collection, the decks' play histories, files missing on it, export.pdb against
  exportLibrary.db, rekfix's structure check, and what exists only on the stick;
- the stick backups (the USB stick mirror and the snapshots folder): which deck histories
  none of them holds.

It only reads. Its own files go to the config's state folder: report.json (the last report,
which the GUI shows), runs.jsonl (a summary of each run, so a report can say what changed
since the one before) and caches that make the next run quicker (--deep ignores them).
"""
import argparse
import ctypes
import datetime as dt
import json
import logging
import os
import shutil
import string
import struct
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict, namedtuple
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

LEVELS = ("ok", "info", "warn", "error")
SHOW = 5                        # examples per finding in the printed report (--list / --all: every one)
LISTS = {"backups.list": 30, "backup.uncovered": 10, "stick.playlists-only": 10}   # info worth showing
KEEP = 1000                     # items per finding kept in report.json
RUNS = 100                      # run summaries kept
PCM = (".wav", ".aif", ".aiff")
HOT = {1: 1, 2: 2, 3: 3, 5: 4, 6: 5, 7: 6, 8: 7, 9: 8}    # djmdCue.Kind -> hot cue 1-8 (A-H); 0 = memory
SLACK_MS = 10                   # cue positions this close are the same cue
MTIME_SLACK = 2                 # seconds (FAT keeps 2-second times)
Cue = namedtuple("Cue", "hot ms loop")
T = namedtuple("T", "id path size title artist")


def norm(s):
    return unicodedata.normalize("NFKC", s or "").casefold().strip()


def fkey(rel):
    """A relative path as a dict key: / separators, case-folded (Windows and FAT ignore case)."""
    return unicodedata.normalize("NFC", rel.replace("\\", "/").strip("/")).casefold()


def gb(n):
    return f"{n / 1e9:,.1f} GB"


def n_of(n, one, many=None):
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def mmss(ms):
    m, s = divmod(ms / 1000, 60)
    return f"{int(m)}:{s:04.1f}"


def cue_text(c):
    what = "memory" if not c.hot else "ABCDEFGH"[c.hot - 1] if c.hot <= 8 else f"hot {c.hot}"
    return f"{what} {mmss(c.ms)}{' loop' if c.loop else ''}"


def who(t):
    return f"{t.artist} - {t.title}" if t.artist else (t.title or os.path.basename(t.path))


def track_key(size, title):
    """How the same track is recognised across rekordbox, sticks and snapshots: its file size
    and title (an export copies the file as it is, and track ids differ per export)."""
    return f"{size}|{norm(title)}"


@dataclass
class Finding:
    code: str
    level: str
    text: str
    items: list = field(default_factory=list)


@dataclass
class Place:
    id: str
    kind: str                   # collection | backup | stick | stick-backups
    name: str
    summary: str = ""
    facts: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)

    @property
    def level(self):
        return max((f.level for f in self.findings), key=LEVELS.index, default="ok")

    def add(self, code, level, text, items=()):
        self.findings.append(Finding(code, level, text, list(items)))


# ---------------------------------------------------------------- comparisons (pure)

def cue_diff(a, b, slack=SLACK_MS):
    """(cues only in a, cues only in b). Two cues are the same when the hot cue number (0 for
    memory cues) and loop-ness match and the positions are within slack ms."""
    rest, only_a = sorted(b), []
    for c in sorted(a):
        hit = next((d for d in rest if d.hot == c.hot and d.loop == c.loop and abs(d.ms - c.ms) <= slack), None)
        if hit is None:
            only_a.append(c)
        else:
            rest.remove(hit)
    return only_a, rest


def match_tracks(stick, coll):
    """{stick id: collection id}: same size and title; else the same size when only one track
    on each side has it (a title changed in rekordbox after the export). Several collection
    tracks with the same key (a file imported twice): the one with the stick file's name."""
    by_key, by_size = defaultdict(list), defaultdict(list)
    for t in coll.values():
        by_key[track_key(t.size, t.title)].append(t)
        by_size[t.size].append(t)
    stick_sizes = Counter(t.size for t in stick.values())
    out = {}
    for sid, t in stick.items():
        hits = by_key.get(track_key(t.size, t.title)) or (
            by_size[t.size] if len(by_size.get(t.size, ())) == 1 and stick_sizes[t.size] == 1 else [])
        if hits:
            name = os.path.basename(t.path.replace("\\", "/")).casefold()
            out[sid] = min(hits, key=lambda c: (os.path.basename(c.path.replace("\\", "/")).casefold() != name,
                                                c.id)).id
    return out


def pair_lists(a, b):
    """Match playlist paths ("Folder / Name") of two libraries: the same path, else the same
    name where only one list on each side has it (rekordbox's mixarchive / X can be a
    top-level X on a stick). -> ([(path a, path b)], only in a, only in b)"""
    pairs = [(p, p) for p in a if p in b]
    left = [p for p in a if p not in b]
    right = [p for p in b if p not in a]
    leaf = lambda p: p.rsplit(" / ", 1)[-1].casefold()
    la, lb = Counter(map(leaf, left)), Counter(map(leaf, right))
    for p in list(left):
        if la[leaf(p)] == 1 and lb[leaf(p)] == 1:
            q = next(q for q in right if leaf(q) == leaf(p))
            pairs.append((p, q))
            left.remove(p)
            right.remove(q)
    return pairs, left, right


def list_diff(want, have):
    """(in want but not have, in have but not want), as multisets, order ignored."""
    w, h = Counter(want), Counter(have)
    return list((w - h).elements()), list((h - w).elements())


def history_status(keys, saved):
    """How much of one deck history (its tracks' keys, in order) a backup holds: saved is
    [keys] of the same-named histories found in backups. -> (plays saved, total)."""
    best = 0
    for other in saved:
        n = 0
        for x, y in zip(keys, other):
            if x != y:
                break
            n += 1
        if n == len(other) or n == len(keys):      # one is the other's start: it's the same list
            best = max(best, n)
    return best, len(keys)


def tree_diff(src, dst, slack=MTIME_SLACK):
    """Two {key: (rel, size, mtime)} trees -> (not in dst, changed since, only in dst)."""
    missing = [v for k, v in src.items() if k not in dst]
    changed = [v for k, v in src.items() if k in dst and (
        v[1] != dst[k][1] or abs(v[2] - dst[k][2]) > slack)]
    extra = [v for k, v in dst.items() if k not in src]
    return missing, changed, extra


def flac_copies(src, flac_dir):
    """WAV / AIFF files under src (a {key: (rel, size, mtime)} tree, rel from src) without an up
    to date copy under flac_dir (the same tree there), the way `files flaccopy` names them:
    x.wav -> x.flac, else x.wav.flac, or x.wav itself for files FLAC can't hold."""
    out = []
    for k, (rel, size, mtime) in src.items():
        if not k.endswith(PCM):
            continue
        names = (os.path.splitext(k)[0] + ".flac", k + ".flac", k)
        if not any(n in flac_dir and flac_dir[n][2] >= mtime - MTIME_SLACK for n in names):
            out.append((rel, size, mtime))
    return out


def changes(before, after):
    """What changed between two runs' facts ({place id: {name, facts}}) -> [lines]."""
    out = []
    for pid, now in after.items():
        old = before.get(pid)
        if not old:
            out.append(f"{now['name']}: new since the last check")
            continue
        diffs = []
        for k, v in now["facts"].items():
            w = old["facts"].get(k)
            if isinstance(v, int) and isinstance(w, int) and v != w:
                diffs.append(f"{k.replace('_', ' ')} {w:,} -> {v:,} ({v - w:+,})")
            elif isinstance(v, list) and isinstance(w, list) and v != w:
                new = [x for x in v if x not in w]
                if new:
                    diffs.append(f"new {k.replace('_', ' ')}: {', '.join(map(str, new[:6]))}"
                                 + (f" and {len(new) - 6} more" if len(new) > 6 else ""))
        if diffs:
            out.append(f"{now['name']}: " + "; ".join(diffs))
    for pid, old in before.items():
        if pid not in after and old.get("kind") == "stick":
            out.append(f"{old['name']}: not plugged in this time")
    return out


# ---------------------------------------------------------------- reading the disks

def _listing(path):
    files, dirs = [], []
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    dirs.append(e.name)
                elif e.is_file(follow_symlinks=False):
                    st = e.stat()
                    files.append((e.name, st.st_size, st.st_mtime))
    except OSError:
        pass
    return files, dirs


def tree(root, skip=()):
    """{key: (rel, size, mtime)} of the files under root, leaving out the top-level folders
    named in skip. Unreadable folders are left out. Folders are listed 16 at a time: a
    backup of rekordbox's analysis files is tens of thousands of small folders."""
    out, skip = {}, {s.casefold() for s in skip}
    level = [""]
    with ThreadPoolExecutor(16) as pool:
        while level:
            below = []
            for rel, (files, dirs) in zip(level, pool.map(lambda r: _listing(os.path.join(root, r)), level)):
                for name, size, mtime in files:
                    r = os.path.join(rel, name) if rel else name
                    out[fkey(r)] = (r, size, mtime)
                below += [os.path.join(rel, d) if rel else d for d in dirs if rel or d.casefold() not in skip]
            level = below
    return out


def by_folder(rows, depth=3):
    """[(folder, files, bytes)] of (rel, size, mtime) rows, grouped by their first folders,
    biggest first."""
    groups = defaultdict(lambda: [0, 0])
    for rel, size, _ in rows:
        parts = rel.split(os.sep)
        g = groups[os.sep.join(parts[:min(depth, len(parts) - 1)]) or "(top folder)"]
        g[0] += 1
        g[1] += size
    return sorted(((k, n, b) for k, (n, b) in groups.items()), key=lambda x: -x[2])


def disk_sizes(paths):
    """{path: size, or None when it isn't there}, one folder listing per folder."""
    by_dir, out = defaultdict(list), {}
    for p in paths:
        by_dir[os.path.dirname(p)].append(p)
    for d, ps in by_dir.items():
        try:
            with os.scandir(d) as it:
                sizes = {e.name.casefold(): e.stat().st_size for e in it if e.is_file()}
        except OSError:
            sizes = {}
        for p in ps:
            out[p] = sizes.get(os.path.basename(p).casefold())
    return out


def volume_info(root):
    """(label, serial, file system) of the drive at root; blanks off Windows or for a folder."""
    if os.name != "nt" or len(root.rstrip("\\/")) != 2:
        return "", "", ""
    label, fs, serial = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261), ctypes.c_uint32()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root), label, 261, ctypes.byref(serial),
                                                      None, None, fs, 261)
    return (label.value, f"{serial.value:08X}", fs.value) if ok else ("", "", "")


def find_sticks():
    """The drives with a rekordbox library on them (PIONEER\\rekordbox\\export.pdb)."""
    if os.name == "nt":
        k32 = ctypes.windll.kernel32
        k32.SetErrorMode(1)                     # an empty card reader: no "insert a disk" box
        mask = k32.GetLogicalDrives()
        roots = [f"{c}:\\" for i, c in enumerate(string.ascii_uppercase)
                 if mask >> i & 1 and k32.GetDriveTypeW(f"{c}:\\") in (2, 3)]    # removable, fixed
    else:
        user = os.environ.get("USER", "")
        bases = ["/Volumes", f"/media/{user}", f"/run/media/{user}", "/media"]
        roots = [os.path.join(b, d) for b in bases if os.path.isdir(b) for d in sorted(os.listdir(b))]
    return [r for r in roots if os.path.isfile(os.path.join(r, "PIONEER", "rekordbox", "export.pdb"))]


def pdb_in(folder):
    """A stick's or a snapshot's export.pdb (snapshots keep it in a few layouts), or None."""
    for parts in (("PIONEER", "rekordbox"), (), ("rekordbox",)):
        p = os.path.join(folder, *parts, "export.pdb")
        if os.path.isfile(p):
            return p
    return None


def cue_sections(path):
    """Just the cue lists of an analysis file (PCOB / PCO2), as a small file of their own for
    rekfix.anlz.read_cues: the waveforms before them (50-200 KB) are skipped, not read."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
            if len(head) < 12 or head[:4] != b"PMAI":
                return None
            hl = struct.unpack_from(">I", head, 4)[0]
            fh.seek(0)
            out = bytearray(fh.read(hl))
            pos = hl
            while True:
                fh.seek(pos)
                h = fh.read(12)
                if len(h) < 12:
                    break
                lh, lt = struct.unpack_from(">II", h, 4)
                if lt < 12 or lh > lt or not h[:4].isalnum():
                    break
                if h[:4] in (b"PCOB", b"PCO2"):
                    out += h + fh.read(lt - 12)
                pos += lt
            return bytes(out)
    except OSError:
        return None


# ---------------------------------------------------------------- rekordbox's collection

class Collection:
    def __init__(self, db_path=None):
        from pyrekordbox import MasterDatabase
        from sqlalchemy import text
        db = MasterDatabase(db_path) if db_path else MasterDatabase()

        def q(sql):
            return db.session.execute(text(sql)).all()

        artists = {str(i): n or "" for i, n in q("select ID, Name from djmdArtist")}
        self.tracks = {}
        for cid, path, size, title, aid in q("select ID, FolderPath, FileSize, Title, ArtistID from djmdContent "
                                             "where rb_local_deleted = 0"):
            if path:
                self.tracks[str(cid)] = T(str(cid), os.path.normpath(path), size or 0, title or "",
                                          artists.get(str(aid), ""))
        self.cues = defaultdict(list)
        for cid, kind, ms, out in q("select ContentID, Kind, InMsec, OutMsec from djmdCue where rb_local_deleted = 0"):
            if kind == 0 or kind in HOT:
                self.cues[str(cid)].append(Cue(HOT.get(kind, 0), ms or 0, out not in (None, -1) and out > (ms or 0)))
        self.playlists = self._lists(q, "djmdPlaylist", "djmdSongPlaylist", "PlaylistID", "TrackNo")
        self.histories = self._lists(q, "djmdHistory", "djmdSongHistory", "HistoryID", "TrackNo")
        db.close()

    @staticmethod
    def _lists(q, table, songs, fk, order):
        """{path: [content ids]} of the playlists (not folders or intelligent lists)."""
        rows = {str(i): (n or "", str(p), a) for i, n, p, a in
                q(f"select ID, Name, ParentID, Attribute from {table} where rb_local_deleted = 0")}
        members = defaultdict(list)
        for lid, cid, no in q(f"select {fk}, ContentID, {order} from {songs} where rb_local_deleted = 0"):
            members[str(lid)].append((no or 0, str(cid)))

        def path(i):
            parts, seen = [], set()
            while i in rows and i not in seen:
                seen.add(i)
                parts.append(rows[i][0])
                i = rows[i][1]
            return " / ".join(reversed(parts))
        return {path(i): [c for _, c in sorted(members[i])] for i, (n, p, a) in rows.items()
                if a == 0}


# ---------------------------------------------------------------- a stick

class Stick:
    def __init__(self, root, cache):
        from rekfix import check, names
        from rekfix.pdb import Pdb
        self.root = root
        self.label, self.serial, self.fs = volume_info(root)
        self.pdb_path = pdb_in(root)
        pdb = Pdb.open(self.pdb_path)
        _, found = check.walk(pdb)
        by_id, more = check.content(pdb)
        self.structure = found + more
        artists = pdb.artists()
        self.tracks, self.anlz = {}, {}
        for tid, ts in by_id.items():
            t = ts[0]
            self.tracks[tid] = T(tid, t.path, t.file_size, t.title, artists.get(t.artist_id, ""))
            if t.strings.get("analyze_path"):
                self.anlz[tid] = t.strings["analyze_path"]
        tree_rows = pdb.playlist_tree()

        def path(i):
            parts, seen = [], set()
            while i in tree_rows and i not in seen:
                seen.add(i)
                parts.append(tree_rows[i][3])
                i = tree_rows[i][0]
            return " / ".join(reversed(parts))
        entries = defaultdict(list)
        for pid, idx, tid, _ in pdb.playlist_entries():
            entries[pid].append((idx, tid))
        self.playlists = {path(pid): [t for _, t in sorted(entries[pid])]
                          for pid, row in tree_rows.items() if not row[2]}
        hist = defaultdict(list)
        for hid, idx, tid, _ in pdb.history_entries():
            hist[hid].append((idx, tid))
        self.histories = [(name, [t for _, t in sorted(hist[hid])])
                          for hid, name in sorted(pdb.history_playlists().items())]
        # files: audio found folder by folder, so a look-alike folder can't hide one (rekfix names.py)
        resolver = names.Resolver(root)
        self.missing_audio = [tid for tid, t in self.tracks.items() if t.path and not resolver.find(t.path)]
        self.lookalikes = resolver.lookalikes()
        self.cues, self.missing_anlz = self._cues(cache)
        self.onelib = self._onelibrary()

    def _cues(self, cache):
        """{track id: [Cue]} from the analysis files, re-reading only files whose size or date
        changed since the last run (cache: {analysis path: [stamp, cues]})."""
        from rekfix import anlz

        def one(item):
            tid, rel = item
            dat = os.path.join(self.root, *[p for p in rel.replace("\\", "/").split("/") if p])
            # a folder can hold several tracks' files (ANLZ0000.*, ANLZ0001.* ...): only this one's
            folder, stem = os.path.dirname(dat), os.path.splitext(os.path.basename(dat))[0].upper()
            try:
                with os.scandir(folder) as it:
                    stamp = sorted([e.name.upper(), e.stat().st_size, int(e.stat().st_mtime)] for e in it
                                   if e.name.upper() in (stem + ".DAT", stem + ".EXT"))
            except OSError:
                stamp = []
            if not stamp:
                return tid, rel, None, None
            hit = cache.get(rel)
            if hit and hit[0] == stamp:
                return tid, rel, stamp, [Cue(*c) for c in hit[1]]
            got = {}
            for name, _, _ in stamp:
                data = cue_sections(os.path.join(folder, name))
                got[name[-3:]] = anlz.read_cues(data, name[-3:]) if data else []
            merged = anlz.merge_cues(got.get("EXT", []), got.get("DAT", []))
            return tid, rel, stamp, sorted({Cue(c.hot, c.start_ms, c.is_loop) for c in merged})

        out, missing = {}, []
        with ThreadPoolExecutor(8) as pool:
            for tid, rel, stamp, cues in pool.map(one, list(self.anlz.items())):
                if stamp is None:
                    missing.append(tid)
                    continue
                out[tid] = cues
                cache[rel] = [stamp, [list(c) for c in cues]]
        return out, missing

    def _onelibrary(self):
        """{playlist path: [audio paths]} from exportLibrary.db (what rekordbox reads from the
        stick), or None when there's none or it can't be read."""
        p = os.path.join(os.path.dirname(self.pdb_path), "exportLibrary.db")
        if not os.path.isfile(p):
            return None
        try:
            from pyrekordbox.onelibrary.database import OneLibrary
            from sqlalchemy import text
            # a copy: SQLite makes (and removes) a journal next to a database it opens, and
            # nothing may be written to a stick
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                copy = os.path.join(tmp, "exportLibrary.db")
                shutil.copyfile(p, copy)
                db = OneLibrary(copy)
                q = lambda sql: db.session.execute(text(sql)).all()
                paths = {cid: path or "" for cid, path in q("select content_id, path from content")}
                rows = {i: (n or "", par, a) for i, n, par, a in
                        q("select playlist_id, name, playlist_id_parent, attribute from playlist")}
                members = defaultdict(list)
                for pid, cid, no in q("select playlist_id, content_id, sequenceNo from playlist_content"):
                    members[pid].append((no or 0, paths.get(cid, "?")))
                db.close()
                db.engine.dispose()
        except Exception:
            return None

        def path(i):
            parts, seen = [], set()
            while i in rows and i not in seen:
                seen.add(i)
                parts.append(rows[i][0])
                i = rows[i][1]
            return " / ".join(reversed(parts))
        return {path(i): [x for _, x in sorted(members[i])] for i, (n, par, a) in rows.items() if a == 0}

    @property
    def name(self):
        if self.serial:
            return f"Stick {self.root.rstrip(chr(92))} ({self.label or 'no label'})"
        return f"Stick copy {self.root}"


# ---------------------------------------------------------------- the checks

def check_collection(coll, cfg):
    p = Place("rekordbox", "collection", "rekordbox collection")
    sizes = disk_sizes([t.path for t in coll.tracks.values()])
    missing = [t for t in coll.tracks.values() if sizes[t.path] is None]
    changed = [t for t in coll.tracks.values() if sizes[t.path] is not None and t.size and sizes[t.path] != t.size]
    p.facts = {"tracks": len(coll.tracks), "playlists": len(coll.playlists), "cues": sum(map(len, coll.cues.values())),
               "missing_files": len(missing)}
    p.summary = (f"{len(coll.tracks):,} tracks, {len(coll.playlists):,} playlists, "
                 f"{sum(map(len, coll.cues.values())):,} cues")
    if missing:
        p.add("collection.missing", "warn", f"the files of {n_of(len(missing), 'track')} aren't where rekordbox looks "
              f"(they won't play or export). `mixarchive files relocate` finds moved ones",
              [f"{who(t)}  [{t.path}]" for t in missing])
    if changed:
        p.add("collection.size", "warn", f"{n_of(len(changed), 'file')} changed size since rekordbox analysed them: "
              f"re-analyse them, or their beat grids and cues may be off",
              [f"{who(t)}  [{t.path}]" for t in changed])
    if not missing and not changed:
        p.add("collection.files", "ok", "every track's file is there, at the size rekordbox recorded")
    return p, sizes


def check_backup(b, coll, sizes):
    src, dst = (os.path.normpath(os.path.expandvars(b[k])) for k in ("from", "to"))
    p = Place(f"backup:{fkey(dst)}", "backup", b.get("name") or f"Backup of {src}")
    if not os.path.isdir(src):
        p.add("backup.source", "warn", f"the folder it backs up isn't there: {src}")
        return p
    if not os.path.isdir(dst):
        p.add("backup.target", "warn", f"the backup isn't reachable: {dst} (drive not connected?)")
        return p
    skip, flac = b.get("skip", []), b.get("flac", {})
    s, d = tree(src, skip), tree(dst, skip)
    missing, changed, extra = tree_diff(s, d)
    newest = max((v[2] for v in d.values()), default=0)
    p.facts = {"files": len(s), "not_backed_up": len(missing), "changed_since": len(changed)}
    p.summary = (f"{len(s):,} files, {gb(sum(v[1] for v in s.values()))} in {src}"
                 + (f" ({', '.join(skip)} not compared)" if skip and not flac else "")
                 + (f"; newest file in the backup is from {dt.datetime.fromtimestamp(newest):%Y-%m-%d}"
                    if newest else ""))
    need = sum(v[1] for v in missing) + sum(max(0, v[1] - d[fkey(v[0])][1]) for v in changed)
    free = shutil.disk_usage(dst).free
    if need > free:
        p.add("backup.space", "error", f"the backup needs about {gb(need)} more and "
              f"{os.path.splitdrive(dst)[0] or dst} has {gb(free)} free: "
              f"running it now fills the drive and leaves it incomplete. Free some space or back up to a "
              f"bigger drive")
    if missing or changed:
        p.add("backup.behind", "warn", f"{n_of(len(missing), 'file')} ({gb(sum(v[1] for v in missing))}) not in the "
              f"backup yet, {len(changed):,} changed since it was made: run the backup",
              [f"{folder}: {n_of(n, 'new file')}, {gb(size)}" for folder, n, size in by_folder(missing)]
              + [f"changed: {v[0]}" for v in sorted(changed)])
    else:
        p.add("backup.behind", "ok", "the backup has every file, as it is now")
    if extra:
        p.add("backup.extra", "info", f"{n_of(len(extra), 'file')} in the backup no longer "
              f"{'exists' if len(extra) == 1 else 'exist'} here "
              f"(the next mirror run deletes them)", [v[0] for v in sorted(extra)])
    if os.path.isfile(os.path.join(src, "master.db")) and os.path.isfile(os.path.join(dst, "master.db")):
        live, copy = (os.path.getmtime(os.path.join(x, "master.db")) for x in (src, dst))
        if live > copy + MTIME_SLACK:
            p.add("backup.database", "warn", f"rekordbox's database changed since its backup "
                  f"({dt.datetime.fromtimestamp(copy):%Y-%m-%d %H:%M}): cues, playlists and edits since then "
                  f"aren't backed up")
    for folder, copies in flac.items():
        pcm, fl = tree(os.path.join(src, folder)), tree(os.path.join(src, copies))
        stale = flac_copies(pcm, fl)
        if stale:
            p.add("backup.flac", "warn", f"{n_of(len(stale), 'WAV / AIFF file')} in {folder} without an up to date "
                  f"FLAC copy in {copies}, so not in the backup: run the backup (it makes them first)",
                  [v[0] for v in stale])
        elif pcm:
            p.add("backup.flac", "ok", f"every WAV / AIFF in {folder} has its FLAC copy in {copies}")
    return p


def check_coverage(coll, sizes, backups, cfg):
    """The collection's tracks that no backup covers -> a finding for the first backup place."""
    roots = []
    for b in backups:
        src = os.path.normcase(os.path.abspath(os.path.expandvars(b["from"]))).rstrip("\\/") + os.sep
        roots.append((src, [os.path.normcase(os.path.join(src, s)).rstrip("\\/") + os.sep
                            for s in b.get("skip", []) if s not in b.get("flac", {})]))
    out = defaultdict(list)
    for t in coll.tracks.values():
        if sizes.get(t.path) is None:
            continue
        p = os.path.normcase(t.path)
        if any(p.startswith(r) and not any(p.startswith(s) for s in skips) for r, skips in roots):
            continue
        out[area(t.path, cfg)].append(t)
    return out


def area(path, cfg):
    """A short name for where a file lives: the USB stick backup, the library, or its top folders."""
    p = os.path.normcase(path)
    for name, folder in (("the USB stick backup", cfg.get("usb_mirror")), ("the library", cfg.get("library"))):
        if folder and p.startswith(os.path.normcase(os.path.abspath(folder)).rstrip("\\/") + os.sep):
            return f"{name} ({folder})"
    return os.sep.join(os.path.normpath(path).split(os.sep)[:4])


def check_stick(st, coll, sizes, saved_histories, elsewhere):
    p = Place(f"stick:{st.serial or fkey(st.root)}", "stick", st.name)
    to_coll = match_tracks(st.tracks, coll.tracks) if coll else {}
    plays = sum(len(ids) for _, ids in st.histories)
    p.summary = (f"{n_of(len(st.tracks), 'track')}, {n_of(len(st.playlists), 'playlist')}, "
                 f"{n_of(len(st.histories), 'deck history', 'deck histories')} ({n_of(plays, 'play')})")
    # rekfix's structure check
    errors = [f for f in st.structure if f.level == "error"]
    warns = [f for f in st.structure if f.level == "warn"]
    if errors or warns:
        p.add("stick.structure", "error" if errors else "warn",
              f"rekfix check: {n_of(len(errors), 'error')}, {n_of(len(warns), 'warning')} in export.pdb "
              f"(`rekfix check {st.root}` explains each)", [f"{f.level}: {f.code}: {f.message}" for f in errors + warns])
    else:
        p.add("stick.structure", "ok", "rekfix check: export.pdb is sound")
    if st.missing_audio or st.missing_anlz:
        p.add("stick.files", "warn", f"files the decks' database lists aren't on the stick: audio for "
              f"{n_of(len(st.missing_audio), 'track')}, analysis for {n_of(len(st.missing_anlz), 'track')} "
              f"(those tracks won't load properly)",[f"audio: {who(st.tracks[t])}  [{st.tracks[t].path}]" for t in st.missing_audio]
              + [f"analysis: {who(st.tracks[t])}  [{st.anlz[t]}]" for t in st.missing_anlz])
    if st.lookalikes:
        from rekfix import names
        p.add("stick.lookalike", "warn", f"folder names Windows mixes up: {names.describe(st.lookalikes)}. Copies "
              f"made on Windows can miss files (rekfix and mixarchive read them correctly)")
    # export.pdb (the decks) against exportLibrary.db (rekordbox)
    if st.onelib is not None:
        pdb_lists = {k: [st.tracks[t].path for t in v if t in st.tracks] for k, v in st.playlists.items()}
        pairs, only_pdb, only_one = pair_lists(pdb_lists, st.onelib)
        differ = []
        for a, b in pairs:
            lost, extra = list_diff([fkey(x) for x in st.onelib[b]], [fkey(x) for x in pdb_lists[a]])
            if lost or extra:
                differ.append(f"{a}: export.pdb has {len(pdb_lists[a]):,}, exportLibrary.db {len(st.onelib[b]):,} "
                              f"({len(extra):,} only in export.pdb, {len(lost):,} only in exportLibrary.db)")
        differ += [f"{x}: only in export.pdb" for x in only_pdb] + [f"{x}: only in exportLibrary.db" for x in only_one]
        if differ:
            p.add("stick.databases", "warn", f"the decks' database (export.pdb) and rekordbox's (exportLibrary.db) "
                  f"disagree on {n_of(len(differ), 'playlist')}: decks and rekordbox show different lists", differ)
    if coll:
        # playlists
        mapped = {k: [to_coll.get(t, f"stick:{t}") for t in v] for k, v in st.playlists.items()}
        pairs, only_stick, only_coll = pair_lists(mapped, coll.playlists)
        differ = []
        for a, b in pairs:
            lost, extra = list_diff(coll.playlists[b], mapped[a])
            if lost or extra:
                differ.append((a, b, lost, extra))
        if differ:
            items = []
            for a, b, lost, extra in differ:
                items.append(f"{a}: {len(lost):,} of rekordbox's tracks not on the stick, {len(extra):,} on the stick "
                             f"that rekordbox's list doesn't have" + (f" (rekordbox: {b})" if a != b else ""))
                items += [f"    not on the stick: {who(coll.tracks[c])}" for c in lost[:20] if c in coll.tracks]
                items += [f"    only on the stick: {who(st.tracks[int(c[6:])] if c.startswith('stick:') else coll.tracks[c])}"
                          for c in extra[:20]]
            p.add("stick.playlists", "warn", f"{n_of(len(differ), 'playlist')} on the stick "
                  f"{'differs' if len(differ) == 1 else 'differ'} from rekordbox's: "
                  f"export them again to bring the stick up to date", items)
        else:
            p.add("stick.playlists", "ok", f"every playlist on it matches rekordbox's ({len(pairs):,})")
        if only_stick:
            p.add("stick.playlists-only", "info", f"{n_of(len(only_stick), 'playlist')} on the stick that rekordbox "
                  f"doesn't have", only_stick)
        if only_coll:
            p.add("stick.playlists-not", "info", f"{n_of(len(only_coll), 'rekordbox playlist')} not on this stick",
                  [x for x in only_coll if not x.upper().startswith("HISTORY")])
        # tracks rekordbox doesn't know, and tracks in no playlist
        unknown = [t for t in st.tracks if t not in to_coll]
        if unknown:
            p.add("stick.unknown", "info", f"{n_of(len(unknown), 'track')} on the stick that rekordbox's collection "
                  f"doesn't have (deleted there, or retitled and resized)", [who(st.tracks[t]) for t in unknown])
        # cues
        deck, rb = [], []
        for tid, cues in st.cues.items():
            cid = to_coll.get(tid)
            if cid is None:
                continue
            only_stick_c, only_rb = cue_diff(cues, coll.cues.get(cid, []))
            if only_stick_c:
                deck.append(f"{who(st.tracks[tid])}: {', '.join(map(cue_text, only_stick_c))}")
            if only_rb:
                rb.append(f"{who(st.tracks[tid])}: {', '.join(map(cue_text, only_rb))}")
        p.facts["deck_cue_tracks"] = len(deck)
        if deck:
            p.add("stick.cues-deck", "warn", f"{n_of(len(deck), 'track')} with cues on the stick that rekordbox doesn't "
                  f"have (set on the decks?). A new export from rekordbox would overwrite them", deck)
        if rb:
            p.add("stick.cues-rekordbox", "info", f"{n_of(len(rb), 'track')} with cues in rekordbox that the stick "
                  f"doesn't have (added since the export)", rb)
        if not deck and not rb and st.cues:
            p.add("stick.cues", "ok", "cues match rekordbox's")
    # deck histories: saved in a stick backup? in rekordbox?
    unsaved, partial = [], []
    for name, ids in st.histories:
        keys = [track_key(st.tracks[t].size, st.tracks[t].title) if t in st.tracks else "?" for t in ids]
        done, total = history_status(keys, saved_histories.get(name, []))
        if done == 0:
            unsaved.append(f"{name}: {n_of(total, 'play')}")
        elif done < total:
            partial.append(f"{name}: {done:,} of {n_of(total, 'play')} saved")
    if unsaved or partial:
        p.add("stick.histories", "warn", f"{n_of(len(unsaved) + len(partial), 'deck history', 'deck histories')} "
              f"exist only on this stick{f' ({len(partial)} partly saved)' if partial else ''}: snapshot it "
              f"(`rekfix snapshot`) before rekordbox or a re-export touches it", unsaved + partial)
    elif st.histories:
        p.add("stick.histories", "ok", f"every deck history is in a stick backup too")
    # what only this stick has
    only = []
    for tid, t in st.tracks.items():
        if tid in st.missing_audio:
            continue
        cid = to_coll.get(tid)
        if cid and sizes.get(coll.tracks[cid].path) is not None:
            continue
        if (fkey(t.path), t.size) in elsewhere:
            continue
        only.append(f"{who(t)}  [{t.path}]")
    if only:
        p.add("stick.only", "warn", f"{n_of(len(only), 'track')} whose audio exists only on this stick (not in "
              f"rekordbox's files, the USB stick backup or a snapshot): copy them off", only)
    else:
        p.add("stick.only", "ok", "every track on it also exists somewhere else")
    p.facts.update({"tracks": len(st.tracks), "playlists": len(st.playlists), "plays": plays,
                    "deck_histories": [n for n, _ in st.histories], "missing_audio": len(st.missing_audio),
                    "structure_errors": len(errors)})
    return p


def stick_backups(cfg, pdb_cache, deep):
    """The stick backups (each snapshot under stick_snapshots, and the USB stick mirror): a
    place for the report, {history name: [[keys]]} and {(file key, size)} of the audio they hold."""
    p = Place("stick-backups", "stick-backups", "Stick backups")
    folders = []
    snaps = cfg.get("stick_snapshots")
    if snaps and os.path.isdir(snaps):
        folders += [os.path.join(snaps, d) for d in sorted(os.listdir(snaps)) if os.path.isdir(os.path.join(snaps, d))]
    if cfg.get("usb_mirror") and os.path.isdir(cfg["usb_mirror"]):
        folders.append(cfg["usb_mirror"])
    histories, audio, rows = defaultdict(list), set(), []
    for f in folders:
        pdb = pdb_in(f)
        st = os.stat(pdb) if pdb else None
        info = None
        if pdb:
            stamp = [st.st_size, int(st.st_mtime)]
            hit = pdb_cache.get(fkey(pdb))
            if hit and hit["stamp"] == stamp and not deep:
                info = hit
            else:
                info = {"stamp": stamp, **pdb_digest(pdb)}
                pdb_cache[fkey(pdb)] = info
            for name, keys in info["histories"]:
                histories[name].append(keys)
        contents = os.path.join(f, "Contents")
        n_audio = 0
        if os.path.isdir(contents):
            for k, (rel, size, _) in tree(contents).items():
                audio.add((fkey("Contents/" + k), size))
                n_audio += 1
        when = taken(f)
        rows.append((when, f"{when:%Y-%m-%d} {os.path.basename(f.rstrip(chr(92) + '/'))}: "
                     + (f"{info['tracks']:,} tracks, {n_of(len(info['histories']), 'deck history', 'deck histories')}"
                        if info else "no export.pdb") + (f", {n_audio:,} audio files" if n_audio else "")))
    rows.sort(reverse=True)
    p.summary = f"{n_of(len(folders), 'copy', 'copies')} of sticks" + (f" in {snaps}" if snaps else "")
    p.facts = {"backups": len(folders)}
    if rows:
        age = (dt.datetime.now() - rows[0][0]).days
        p.add("backups.list", "info", f"the newest is from {rows[0][0]:%Y-%m-%d}"
              + (f", {n_of(age, 'day')} ago" if age else ", today"), [r for _, r in rows])
    else:
        p.add("backups.list", "warn", "no stick backups found: set stick_snapshots (and usb_mirror) in settings.json")
    return p, histories, audio


def taken(folder):
    """When a stick backup was made: its own record files (rekfix's manifest, a robocopy log),
    else the folder's creation. Not the database's date: decks write theirs with odd ones."""
    stamps = []
    try:
        with os.scandir(folder) as it:
            stamps = [e.stat().st_mtime for e in it if e.is_file() and e.name.lower().endswith(
                (".csv", ".md5", ".log", ".txt"))]
    except OSError:
        pass
    return dt.datetime.fromtimestamp(max(stamps) if stamps else os.stat(folder).st_ctime)


def pdb_digest(path):
    from rekfix.pdb import Pdb
    pdb = Pdb.open(path)
    tracks = {tid: ts[0] for tid, ts in pdb.tracks().items()}
    if not tracks:                              # broken table chains: find the rows page by page
        pdb = Pdb.open(path, scan=True)
        tracks = {tid: ts[0] for tid, ts in pdb.tracks().items()}
    entries = defaultdict(list)
    for hid, idx, tid, _ in pdb.history_entries():
        entries[hid].append((idx, tid))
    hist = []
    for hid, name in sorted(pdb.history_playlists().items()):
        hist.append([name, [track_key(tracks[t].file_size, tracks[t].title) if t in tracks else "?"
                            for _, t in sorted(entries[hid])]])
    return {"tracks": len(tracks), "histories": hist}


# ---------------------------------------------------------------- state, report

def load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save(path, data):
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def run(cfg, sticks=(), auto=True, deep=False, db=None, say=print):
    t0 = time.time()
    state = cfg["state"]
    os.makedirs(state, exist_ok=True)
    places = []
    say("reading rekordbox's collection ...")
    coll, sizes = None, {}
    try:
        coll = Collection(db)
        place, sizes = check_collection(coll, cfg)
        places.append(place)
    except Exception as e:
        p = Place("rekordbox", "collection", "rekordbox collection")
        p.add("collection.read", "error", f"couldn't read rekordbox's database: {e}")
        places.append(p)
    backups = cfg.get("backups") or []
    for b in backups:
        say(f"comparing {b.get('name') or b['from']} ...")
        places.append(check_backup(b, coll, sizes))
    if coll and backups:
        uncovered = check_coverage(coll, sizes, backups, cfg)
        if uncovered:
            n = sum(map(len, uncovered.values()))
            size = sum(t.size for ts in uncovered.values() for t in ts)
            items = [f"{a}: {n_of(len(ts), 'track')}, {gb(sum(t.size for t in ts))}"
                     for a, ts in sorted(uncovered.items(), key=lambda x: -sum(t.size for t in x[1]))]
            places[1].add("backup.uncovered", "warn", f"{n_of(n, 'track')} ({gb(size)}) rekordbox plays from folders no "
                          f"backup covers", items)
    say("reading the stick backups ...")
    pdb_cache = {} if deep else load(os.path.join(state, "pdbs.json"), {})
    backup_place, saved_histories, audio = stick_backups(cfg, pdb_cache, deep)
    roots = list(dict.fromkeys(list(sticks) + (find_sticks() if auto else [])))
    for root in roots:
        say(f"reading the stick at {root} ...")
        try:
            label, serial, _ = volume_info(root)
            cache_file = os.path.join(state, f"cues-{serial or fkey(root).replace('/', '_').replace(':', '')}.json")
            cache = {} if deep else load(cache_file, {})
            st = Stick(root, cache)
            save(cache_file, cache)
            places.append(check_stick(st, coll, sizes, saved_histories, audio))
        except Exception as e:
            p = Place(f"stick:{fkey(root)}", "stick", f"Stick {root}")
            p.add("stick.read", "error", f"couldn't read it: {type(e).__name__}: {e}")
            places.append(p)
    places.append(backup_place)
    save(os.path.join(state, "pdbs.json"), pdb_cache)
    # since the last run
    runs_file = os.path.join(state, "runs.jsonl")
    try:
        with open(runs_file, encoding="utf-8") as fh:
            runs = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        runs = []
    now = {p.id: {"name": p.name, "kind": p.kind, "facts": p.facts, "level": p.level} for p in places}
    seen = {}
    for r in runs:                       # a stick not plugged in this time: compare with when it last was
        seen.update(r["places"])
    before = runs[-1]["places"] if runs else {}
    before = {**{k: v for k, v in seen.items() if v.get("kind") == "stick"}, **before}
    report = {"at": dt.datetime.now().isoformat(timespec="seconds"), "took": round(time.time() - t0, 1),
              "previous": runs[-1]["at"] if runs else None,
              "changes": changes(before, now) if runs else [],
              "places": [{**asdict(p), "level": p.level} for p in places]}
    runs.append({"at": report["at"], "places": now})
    saved = json.loads(json.dumps(report))          # the GUI's copy: long lists cut to KEEP items
    for p in saved["places"]:
        for f in p["findings"]:
            f["total"] = len(f["items"])
            f["items"] = f["items"][:KEEP]
    with open(runs_file + ".part", "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in runs[-RUNS:])
    os.replace(runs_file + ".part", runs_file)
    save(os.path.join(state, "report.json"), saved)
    return report


MARK = {"ok": "ok", "info": " -", "warn": " !", "error": " X"}


def render(report, list_code=None, every=False):
    out = [f"mixarchive monitor, {report['at'].replace('T', ' ')} (took {report['took']:.0f} s)"]
    for p in report["places"]:
        out.append("")
        out.append(f"{MARK[p['level']]} {p['name']} - {p['summary']}")
        for f in p["findings"]:
            out.append(f"   {MARK[f['level']]} {f['text']}")
            total = f.get("total", len(f["items"]))
            show = (total if (every or f["code"] == list_code) else
                    SHOW if f["level"] in ("warn", "error") else LISTS.get(f["code"], 0))
            out += [f"        {x}" for x in f["items"][:show]]
            if total > show and show:
                out.append(f"        ... {total - show:,} more: mixarchive monitor --list {f['code']}")
    if report["previous"]:
        out.append("")
        out.append(f"Since the last check ({report['previous'].replace('T', ' ')}):")
        out += [f"   {c}" for c in report["changes"]] or ["   nothing changed"]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mixarchive monitor", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="JSON (or a JSON file): state, library, usb_mirror, "
                    "stick_snapshots, backups")
    ap.add_argument("--stick", action="append", default=[], help="also check this folder as a stick (repeatable)")
    ap.add_argument("--no-sticks", action="store_true", help="don't look for sticks plugged in")
    ap.add_argument("--deep", action="store_true", help="read everything again instead of using the caches")
    ap.add_argument("--db", help="a copy of master.db to read instead of rekordbox's own")
    ap.add_argument("--list", metavar="CODE", help="print every item of these findings (e.g. stick.cues-deck)")
    ap.add_argument("--all", action="store_true", help="print every item of every finding")
    ap.add_argument("--last", action="store_true", help="print the last report again instead of checking")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    a = ap.parse_args(argv)
    logging.disable(logging.WARNING)            # pyrekordbox's chatter
    for stream in (sys.stdout, sys.stderr):     # titles in any script, on a cp1252 pipe too
        if stream:
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        import rekfix  # noqa: F401  (the sticks are read with it)
    except ImportError:
        raise SystemExit(f"monitor needs rekfix next to pyrekordbox; install it with:\n"
                         f"  \"{sys.executable}\" -m pip install rekfix")
    cfg = json.loads(a.config) if a.config.lstrip().startswith("{") else load(a.config, None)
    if not cfg:
        raise SystemExit(f"no config in {a.config}")
    if a.last:
        report = load(os.path.join(cfg["state"], "report.json"), None)
        if not report:
            raise SystemExit("no report yet: run `mixarchive monitor` first")
    else:
        t0, out = time.time(), sys.stderr if a.json else sys.stdout
        report = run(cfg, a.stick, not a.no_sticks, a.deep, a.db,
                     say=lambda m: print(f"[{time.time() - t0:4.0f} s] {m}", file=out, flush=True))
    print(json.dumps(report, ensure_ascii=False) if a.json else render(report, a.list, a.all))
    return 0                    # what it found is in the report; a failure to look raises


if __name__ == "__main__":
    sys.exit(main())
