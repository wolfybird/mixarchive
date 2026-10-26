"""Sync mixarchive's playlists straight into rekordbox's own database (master.db), so new
tracks and playlist changes appear without an XML import. pyrekordbox needs a newer Python
than the main venv, so `mixarchive rbsync` runs this file with rbvenv's Python:

    rbvenv\\Scripts\\python -I mixarchive\\rbdb.py PLAN.json [--apply] [--db=COPY_OF_master.db]

PLAN.json: {"folder": "mixarchive", "root": LIBRARY_ROOT, "backup_dir": DIR,
             "state": JSON_PATH, "playlists": [{"name", "files"}]}
(state: where rbsync records which tracks it put in each playlist, so it only ever removes
those and leaves the ones you added by hand)

Only while rekordbox is closed (pyrekordbox refuses to commit otherwise):
  - files not in the collection yet are added with their tags and cover (written as
    rekordbox's own artwork files); rekordbox analyzes them when asked to
  - a file mixarchive renamed (tracklist / tracks.csv edits) is followed: the rekordbox
    entry under `root` whose file is gone and whose comment has the same YouTube link
    + start time is pointed at the new name (cues and analysis kept), not added again
  - MP3s under `root` that changed since rekordbox read them (size differs), in a plan
    playlist or not, get tags, cover and size refreshed, like rekordbox's Reload Tag;
    cues, grids and analysis are kept
  - each plan playlist lives in the `folder` playlist folder and holds its files, in order,
    then any tracks you added to it yourself (in your order). Other playlists, including
    any of your own in that folder, are left alone
  - NOT_ANALYZED in that folder lists the synced tracks rekordbox hasn't analyzed yet;
    REANALYZE collects refreshed / followed tracks whose length changed (their analysis is
    of the old audio) until you delete it
Without --apply it only reports. With it, master.db and masterPlaylists6.xml (+ the
analysis files of followed renames) are copied to backup_dir first (the last
KEEP_BACKUPS of these are kept).
"""
import datetime
import json
import logging
import os
import re
import secrets
import shutil
import sys
from collections import defaultdict
from io import BytesIO
from uuid import uuid4

from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from PIL import Image
from pyrekordbox import MasterDatabase
from pyrekordbox.masterdb import models
from pyrekordbox.masterdb.models import FileType
from pyrekordbox.utils import get_rekordbox_pid, to_posix_path

NOT_ANALYZED = "Not analyzed yet"
REANALYZE = "Re-analyze (audio changed)"
KEEP_BACKUPS = 10
ART_SIZES = (("", 600), ("_m", 240), ("_s", 80))     # what rekordbox writes per cover


def key(path):
    # case-insensitive like NTFS, but no Unicode normalization: "é" as one character and
    # "e" + combining accent are two different files there (DFA has such pairs)
    return os.path.abspath(path).replace("\\", "/").casefold()


def youtube_link(comment):
    """The YouTube link + start time split writes into every track's comment."""
    m = re.search(r"https://youtu\.be/[\w-]{11}(\?t=\d+)?", comment or "")
    return m.group(0) if m else None


def _text(tags, frame):
    f = tags.get(frame)
    return str(f.text[0]).strip() if f and f.text else ""


def read_tags(path):
    tags, info = ID3(path), MP3(path).info
    year, trck = _text(tags, "TDRC")[:4], _text(tags, "TRCK").split("/")[0]
    apic = tags.getall("APIC")
    return {"Title": _text(tags, "TIT2") or os.path.splitext(os.path.basename(path))[0],
            "artist": _text(tags, "TPE1"), "album": _text(tags, "TALB"),
            "album_artist": _text(tags, "TPE2"), "genre": _text(tags, "TCON"),
            "label": _text(tags, "TPUB"),
            "Commnt": next((f.text[0] for f in tags.getall("COMM") if not f.desc and f.text), ""),
            "ReleaseYear": int(year) if year.isdigit() else 0,
            "TrackNo": int(trck) if trck.isdigit() else 0,
            "Length": int(round(info.length)), "BitRate": info.bitrate // 1000,
            "SampleRate": info.sample_rate, "cover": apic[0].data if apic else None}


class Ids:
    """Unused random 28-bit row IDs, as strings like rekordbox's own. (pyrekordbox's add_*
    helpers make int IDs, which SQLAlchemy can't flush alongside the existing str ones.)"""

    def __init__(self, db):
        self.db, self.used = db, {}

    def new(self, model, field="ID"):
        if (model, field) not in self.used:
            self.used[model, field] = {str(v) for (v,) in self.db.query(getattr(model, field))}
        used = self.used[model, field]
        while True:
            i = str(secrets.randbits(28))
            if int(i) >= 100 and i not in used:
                used.add(i)
                return i


class Names:
    """Artist / genre / label / album rows by name, created on first use."""
    MODELS = {"artist": models.DjmdArtist, "genre": models.DjmdGenre, "label": models.DjmdLabel}

    def __init__(self, db, ids):
        self.db, self.new_id = db, ids.new
        self.ids = {kind: {r.Name: r.ID for r in db.query(m)} for kind, m in self.MODELS.items()}
        self.albums = {(r.Name, r.AlbumArtistID): r.ID for r in db.query(models.DjmdAlbum)}
        self.created = defaultdict(int)

    def _add(self, model, **fields):
        row = model.create(ID=self.new_id(model), UUID=str(uuid4()), **fields)
        self.db.add(row)
        return row.ID

    def get(self, kind, name):
        if not name:
            return None
        ids = self.ids[kind]
        if name not in ids:
            ids[name] = self._add(self.MODELS[kind], Name=name)
            self.created[kind] += 1
        return ids[name]

    def album(self, name, album_artist):
        if not name:
            return None
        k = (name, self.get("artist", album_artist))
        if k not in self.albums:
            self.albums[k] = self._add(models.DjmdAlbum, Name=name, AlbumArtistID=k[1])
            self.created["album"] += 1
        return self.albums[k]


def add_track(db, ids, path, device, link):
    """A new collection row for an MP3, shaped like the ones rekordbox's own import makes
    (pyrekordbox's add_content, with str IDs); tags and cover come from apply_tags."""
    cid, today = ids.new(models.DjmdContent), datetime.date.today().isoformat()
    c = models.DjmdContent.create(
        ID=cid, UUID=str(uuid4()), MasterSongID=cid, MasterDBID=device.MasterDBID,
        DeviceID=device.ID, rb_file_id=ids.new(models.DjmdContent, "rb_file_id"),
        ContentLink=link,
        FolderPath=to_posix_path(path), FileNameL=os.path.basename(path),
        FileSize=os.path.getsize(path), FileType=FileType.MP3.value, DateCreated=today,
        StockDate=today, HotCueAutoLoad="on", DeliveryControl="on", ColorID="0",
        VideoAssociate="0", ExtInfo="null", Analysed=0, BitDepth=16, **FRESH)
    db.add(c)
    return c


# columns rekordbox's own import fills with ''/0 before analysis (copied from one of its rows)
FRESH = dict(FileNameS="", BPM=0, Rating=0, DJPlayCount=0, AnalysisDataPath="", DiscNo=0,
             Subtitle="", ReleaseDate="", ModifiedByRBM="", DeliveryComment="", Lyricist="",
             ISRC="", SamplerTrackInfo=0, SamplerPlayOffset=0, SamplerGain=0.0, LyricStatus=0,
             ServiceID=0, OrgFolderPath="", Reserved1="", rb_data_status=0,
             rb_local_data_status=0, rb_local_deleted=0, rb_local_synced=0)


def write_art(share, image_path, uuid, data):
    """rekordbox's three cover sizes; returns the content's ImagePath."""
    if not image_path:
        image_path = f"/PIONEER/Artwork/{uuid[:3]}/{uuid[3:]}/artwork.jpg"
    stem = os.path.join(share, image_path.lstrip("/").replace("/", os.sep))[:-len(".jpg")]
    os.makedirs(os.path.dirname(stem), exist_ok=True)
    im = Image.open(BytesIO(data)).convert("RGB")
    for suffix, px in ART_SIZES:
        j = im.copy()
        j.thumbnail((px, px), Image.LANCZOS)
        j.save(f"{stem}{suffix}.jpg", "JPEG", quality=90)
    return image_path


def apply_tags(db, names, share, c, t):
    """Set a content row's tag fields (+ cover) from read_tags() output."""
    c.Title = t["Title"]
    c.ArtistID = names.get("artist", t["artist"])
    c.AlbumID = names.album(t["album"], t["album_artist"])
    c.GenreID = names.get("genre", t["genre"])
    c.LabelID = names.get("label", t["label"])
    for f in ("Commnt", "ReleaseYear", "TrackNo", "Length", "BitRate", "SampleRate"):
        setattr(c, f, t[f])
    if t["cover"]:
        c.ImagePath = write_art(share, c.ImagePath, c.UUID, t["cover"])


def sync_songs(db, playlist, want):
    """Make the playlist hold exactly `want` (content IDs) in order. Returns (+added, -removed)."""
    songs = (db.query(models.DjmdSongPlaylist).filter_by(PlaylistID=playlist.ID)
             .order_by(models.DjmdSongPlaylist.TrackNo).all())
    if [s.ContentID for s in songs] == want:
        return 0, 0
    pool = defaultdict(list)
    for s in songs:
        pool[s.ContentID].append(s)
    now, added = datetime.datetime.now(), 0
    for n, cid in enumerate(want, 1):
        if pool[cid]:
            s = pool[cid].pop(0)
            if s.TrackNo != n:
                s.TrackNo, s.updated_at = n, now
        else:
            db.add(models.DjmdSongPlaylist.create(
                ID=str(uuid4()), PlaylistID=str(playlist.ID), ContentID=str(cid), TrackNo=n,
                UUID=str(uuid4()), created_at=now, updated_at=now))
            added += 1
    removed = [s for rest in pool.values() for s in rest]
    for s in removed:
        db.delete(s)
    playlist.updated_at = now
    return added, len(removed)


def backup(db, backup_dir, relinked=()):
    """master.db + masterPlaylists6.xml, and the analysis files update_content_path
    rewrites for `relinked` entries."""
    dest = os.path.join(backup_dir, datetime.datetime.now().strftime("%Y-%m-%d %H%M%S rbsync"))
    os.makedirs(dest)
    for f in ("master.db", "master.db-wal", "master.db-shm", "masterPlaylists6.xml"):
        src = os.path.join(str(db.db_directory), f)
        if os.path.exists(src):
            shutil.copy2(src, dest)
    share = str(db.share_directory)
    for c in relinked:
        for p in db.get_anlz_paths(c).values():
            if p and os.path.exists(p):
                to = os.path.join(dest, "share", os.path.relpath(p, share))
                os.makedirs(os.path.dirname(to), exist_ok=True)
                shutil.copy2(p, to)
    old = sorted(d for d in os.listdir(backup_dir) if d.endswith(" rbsync"))[:-KEEP_BACKUPS]
    for d in old:
        shutil.rmtree(os.path.join(backup_dir, d))
    return dest


def song_ids(db, playlist):
    return [s.ContentID for s in db.query(models.DjmdSongPlaylist).filter_by(PlaylistID=playlist.ID)
            .order_by(models.DjmdSongPlaylist.TrackNo)]


def load_written(plan):
    """{playlist: content IDs} rbsync put there last time (plan["state"]); None without a
    state file in the plan."""
    if not plan.get("state"):
        return None
    try:
        with open(plan["state"], encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def save_written(plan, index):
    """Record each plan playlist's own files as what rbsync put there (after a write)."""
    if plan.get("state"):
        with open(plan["state"], "w", encoding="utf-8") as fh:
            json.dump({p["name"]: [index[key(f)].ID for f in p["files"]] for p in plan["playlists"]}, fh)


def target(plan, index, by_id, members, written=None):
    """What the folder's playlists should hold: {name: content IDs}. Plan playlists get
    their files, then the tracks added to them by hand (any member rbsync didn't put there
    itself, per `written`; a playlist missing from it counts as written = its files.
    Without `written`, only members from outside the library root count as hand-added).
    Other playlists in the folder keep theirs, and NOT_ANALYZED gets every track among
    them without analysis data (left out when there are none)."""
    root = key(plan["root"]).rstrip("/") + "/"
    want = {n: ids for n, ids in members.items() if n != NOT_ANALYZED and ids}
    for p in plan["playlists"]:
        own = [index[key(f)].ID for f in p["files"]]
        if written is None:
            mine = [c for c in members.get(p["name"], []) if c in by_id and c not in own
                    and not key(by_id[c].FolderPath or "").startswith(root)]
        else:
            ours = set(written.get(p["name"], own))
            mine = [c for c in members.get(p["name"], []) if c in by_id and c not in own and c not in ours]
        want[p["name"]] = own + list(dict.fromkeys(mine))
    todo = [cid for cid in dict.fromkeys(c for ids in want.values() for c in ids)
            if cid in by_id and not by_id[cid].AnalysisDataPath]
    if todo:
        want[NOT_ANALYZED] = todo
    return want


def main(plan_path, apply, db_path=None):
    if get_rekordbox_pid():
        raise SystemExit("rekordbox is running - close it and run this again")
    with open(plan_path, encoding="utf-8") as fh:
        plan = json.load(fh)
    db = MasterDatabase(db_path)
    share = str(db.share_directory)
    index, by_id = {}, {}
    for c in db.get_content():
        index.setdefault(key(c.FolderPath or ""), c)
        by_id[c.ID] = c

    files = list(dict.fromkeys(f for p in plan["playlists"] for f in p["files"]))
    missing = [f for f in files if not os.path.exists(f)]
    if missing:
        raise SystemExit(f"{len(missing)} files in the plan don't exist, e.g. {missing[:3]}")
    new = [f for f in files if key(f) not in index]
    # changed: any MP3 under the library root, in a plan playlist or not (full mixes of
    # split mixes are in the collection too); renamed: a gone entry there with the
    # same YouTube link + start time as a new file
    root = key(plan["root"]).rstrip("/") + "/"
    changed, gone = [], defaultdict(list)
    for k, c in index.items():
        if not k.startswith(root):
            continue
        if not os.path.exists(c.FolderPath):
            gone[youtube_link(c.Commnt)].append(c)
        elif k.endswith(".mp3") and c.FileSize != os.path.getsize(c.FolderPath):
            changed.append(c.FolderPath)
    gone.pop(None, None)
    tags, moved = {}, {}
    if gone:
        for f in new:
            tags[f] = read_tags(f)
            rows = gone.get(youtube_link(tags[f]["Commnt"]), [])
            if len(rows) == 1:
                moved[f] = rows.pop()
        new = [f for f in new if f not in moved]
    orphaned = sum(len(rows) for rows in gone.values())

    folder = next((p for p in db.get_playlist(ParentID="root", Attribute=1)
                   if p.Name == plan["folder"]), None)
    lists = {p.Name: p for p in db.get_playlist(ParentID=folder.ID, Attribute=0)} if folder else {}
    members = {name: song_ids(db, pl) for name, pl in lists.items()}
    print(f"  {len(files)} tracks in {len(plan['playlists'])} playlists: {len(new)} new to rekordbox, "
          f"{len(moved)} renamed, {len(changed)} changed since rekordbox read them")
    new_set = set(new)
    for p in plan["playlists"]:
        n_new = sum(f in new_set for f in p["files"])
        state = "exists" if p["name"] in lists else "new playlist"
        gb = sum(os.path.getsize(f) for f in p["files"]) / 1e9
        print(f"    {p['name'][:44]:44} {len(p['files']):5} tracks {gb:6.1f} GB  {n_new:5} new  ({state})")
    if orphaned:
        print(f"  {orphaned} rekordbox entries under {plan['root']} point at files that are gone "
              "(dups / skips removed by mixarchive?) - they show as missing in rekordbox")
    written = load_written(plan)
    if not (new or moved or changed) and target(plan, index, by_id, members, written) == {
            n: ids for n, ids in members.items() if ids}:
        print("  rekordbox is up to date")
        if apply:
            save_written(plan, index)
        return
    if not apply:
        print("  dry run - nothing written")
        return

    print(f"  backup -> {backup(db, plan['backup_dir'], list(moved.values()))}")
    ids = Ids(db)
    names = Names(db, ids)
    device, link = db.get_device().first(), db.get_menu_items(Name="TRACK").one().rb_local_usn
    for i, f in enumerate(new, 1):
        c = add_track(db, ids, f, device, link)
        apply_tags(db, names, share, c, tags.get(f) or read_tags(f))
        index[key(f)] = by_id[c.ID] = c
        if i % 500 == 0:
            print(f"    added {i}/{len(new)}")
    for f, c in moved.items():
        db.update_content_path(c, f, save=True, check_path=True, commit=False)
        index[key(f)] = c
    relength = []
    for f in changed + list(moved):
        c, t = index[key(f)], tags.get(f) or read_tags(f)
        if c.Length and abs(c.Length - t["Length"]) > 1:
            relength.append(f)
        apply_tags(db, names, share, c, t)
        c.FileSize = os.path.getsize(f)

    if folder is None:
        folder = db.create_playlist_folder(plan["folder"])
    want = target(plan, index, by_id, members, written)
    if relength:
        want[REANALYZE] = list(dict.fromkeys(want.get(REANALYZE, []) + [index[key(f)].ID for f in relength]))
    for name, cids in want.items():
        if name not in lists:
            lists[name] = db.create_playlist(name, parent=folder,
                                             seq=1 if name == NOT_ANALYZED else None)
        add, rem = sync_songs(db, lists[name], cids)
        if (add or rem) and name != NOT_ANALYZED:
            print(f"    {name[:44]:44} +{add} -{rem}")
    if NOT_ANALYZED in lists and NOT_ANALYZED not in want:
        db.delete_playlist(lists[NOT_ANALYZED])
    db.commit()
    save_written(plan, index)
    created = ", ".join(f"{n} {k}s" for k, n in names.created.items()) or "none"
    print(f"  done: {len(new)} tracks added, {len(moved)} renames followed, {len(changed)} "
          f"refreshed; new names: {created}")
    print(f"  '{plan['folder']} / {NOT_ANALYZED}': {len(want.get(NOT_ANALYZED, []))} tracks "
          "to analyze in rekordbox")
    if relength:
        print(f"  '{plan['folder']} / {REANALYZE}': {len(relength)} tracks whose audio changed - analyze "
              "them again in rekordbox, then delete that playlist")
    db.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # pyrekordbox warns about every playlist rekordbox itself left out of
    # masterPlaylists6.xml (dozens here); that is rekordbox's normal state
    logging.getLogger("pyrekordbox").setLevel(logging.ERROR)
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(args) != 1:
        raise SystemExit(__doc__)
    db = next((x[5:] for x in sys.argv if x.startswith("--db=")), None)
    main(args[0], "--apply" in sys.argv, db)
