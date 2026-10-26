"""rekordbox XML (DJ_PLAYLISTS) export: every collection's tracks as one playlist each,
for File > Import in rekordbox (rekordbox xml view > Import Playlist). Tracks already in
the rekordbox collection (same file path) are matched rather than added twice.

A playlist holds a collection's splits (mix tracks + singles); a mix that has no timed
tracklist contributes its full-mix file instead, so syncing playlists to a USB drive
doesn't carry a whole mix alongside its own tracks.
"""
import csv
import datetime
import os
import urllib.parse
from xml.sax.saxutils import quoteattr

from mutagen.id3 import ID3
from mutagen.mp3 import MP3

from .split import FORMATS


def location(path):
    return "file://localhost/" + urllib.parse.quote(os.path.abspath(path).replace("\\", "/"),
                                                    safe="/:")


def _text(tags, key):
    frame = tags.get(key)
    return str(frame.text[0]) if frame and frame.text else ""


def track_attrs(path):
    """rekordbox TRACK attributes from an MP3's own tags (as written by split)."""
    tags, info = ID3(path), MP3(path).info
    comm = next((f.text[0] for f in tags.getall("COMM") if not f.desc and f.text), "")
    trck = _text(tags, "TRCK").split("/")[0]
    return {"Name": _text(tags, "TIT2"), "Artist": _text(tags, "TPE1"),
            "Album": _text(tags, "TALB"), "Genre": _text(tags, "TCON"),
            "Label": _text(tags, "TPUB"), "Year": _text(tags, "TDRC")[:4],
            "TrackNumber": trck if trck.isdigit() else "0", "Comments": comm[:250],
            "Kind": "MP3 File", "Size": str(os.path.getsize(path)),
            "TotalTime": str(int(round(info.length))), "BitRate": str(info.bitrate // 1000),
            "SampleRate": str(info.sample_rate), "Location": location(path)}


def collection_files(c, fmt="mp3"):
    """[(playlist name, [file paths])] for one collection: splits in manifest order, plus
    full mixes of mixes without splits, plus MOD renders as their own playlist."""
    base = os.path.join(c.out_root, FORMATS[fmt][0])
    manifest = os.path.join(base, "tracklist.csv")
    files, split_mixes = [], set()
    if os.path.exists(manifest):
        with open(manifest, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                p = os.path.join(base, r["file"])
                if os.path.exists(p):
                    files.append(p)
                    split_mixes.add(r["mix"].lower())
    mains = os.path.join(base, "_full mixes")
    if os.path.isdir(mains):
        from .split import mix_folder
        split_folders = {mix_folder(m).lower() for m in split_mixes}
        for f in sorted(os.listdir(mains)):
            if f.lower().endswith(FORMATS[fmt][1]) and os.path.splitext(f)[0].lower() not in split_folders:
                files.append(os.path.join(mains, f))
    out = [(c.config.get("playlist") or c.config.get("label") or c.name, files)]
    renders = os.path.join(c.out_root, "MOD renders")
    if os.path.isdir(renders):
        mods = [os.path.join(d, f) for d, _, fs in sorted(os.walk(renders)) for f in sorted(fs)
                if f.lower().endswith(".mp3")]
        if mods:
            out.append((f"{out[0][0]} (MOD renders)", mods))
    return out


def write(path, playlists, folder="mixarchive"):
    """playlists: [(name, [mp3 paths])] -> rekordbox XML at `path`. Returns track count."""
    ids, rows = {}, []
    for _, files in playlists:
        for f in files:
            key = os.path.normcase(os.path.abspath(f))
            if key not in ids:
                ids[key] = len(ids) + 1
                rows.append((ids[key], track_attrs(f)))
    today = datetime.date.today().isoformat()
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<DJ_PLAYLISTS Version="1.0.0">',
             '  <PRODUCT Name="mixarchive" Version="0.1.0" Company=""/>',
             f'  <COLLECTION Entries="{len(rows)}">']
    for tid, a in rows:
        attrs = " ".join(f"{k}={quoteattr(v)}" for k, v in
                         [("TrackID", str(tid))] + list(a.items()) + [("DateAdded", today)])
        lines.append(f"    <TRACK {attrs}/>")
    lines += ["  </COLLECTION>", "  <PLAYLISTS>", '    <NODE Type="0" Name="ROOT" Count="1">',
              f'      <NODE Type="0" Name={quoteattr(folder)} Count="{len(playlists)}">']
    for name, files in playlists:
        lines.append(f'        <NODE Name={quoteattr(name)} Type="1" KeyType="0" Entries="{len(files)}">')
        lines += [f'          <TRACK Key="{ids[os.path.normcase(os.path.abspath(f))]}"/>' for f in files]
        lines.append("        </NODE>")
    lines += ["      </NODE>", "    </NODE>", "  </PLAYLISTS>", "</DJ_PLAYLISTS>"]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(rows)
