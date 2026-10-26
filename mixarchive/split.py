"""Cut a full mix into tracks and tag them (MP3 ID3v2.3 / FLAC Vorbis / M4A atoms)."""
import base64
import csv
import os
import re
import subprocess

from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2,
                         TPUB, TRCK, WOAS, ID3NoHeaderError)
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus

from .tracklist import split_label
from .util import mmss, safe_name, settings, year_tag

FORMATS = {   # name -> (output folder, extension, ffmpeg codec args, final filters)
    "mp3": ("MP3 320", ".mp3", ["-c:a", "libmp3lame", "-b:a", "320k", "-write_xing", "1"], []),
    "flac": ("FLAC", ".flac", ["-c:a", "flac", "-compression_level", "8"],
             ["aresample=osf=s16:dither_method=triangular"]),
}
DEFAULT_NORMALIZE = {"target_lufs": -14.0, "ceiling_dbtp": -1.0, "scope": "track"}


def measure(src, start=None, end=None):
    """EBU R128 integrated loudness (LUFS) and true peak (dBTP) of a span."""
    cmd = [settings()["ffmpeg"], "-nostdin", "-hide_banner", "-nostats"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-to", f"{end:.3f}"]
    cmd += ["-i", src, "-map", "0:a", "-af", "ebur128=peak=true", "-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True, errors="replace").stderr
    summary = err[err.rfind("Summary:"):]          # ebur128 is ~6x faster than loudnorm
    lufs = re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", summary)
    peak = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
    if not lufs or not peak:                       # empty span: no gain rather than a crash
        return float("-inf"), float("-inf")
    return float(lufs.group(1)), float(peak.group(1))


def gain_db(meas, norm):
    """Linear gain toward the LUFS target, capped by the true-peak ceiling (no limiting)."""
    if not norm or not meas or meas[0] < -70:
        return 0.0
    lufs, tp = meas
    return round(min(norm["target_lufs"] - lufs, norm["ceiling_dbtp"] - tp), 2)


_MEASURED = {}


def measured(src, start=None, end=None):
    key = (src, start, end)
    if key not in _MEASURED:
        _MEASURED[key] = measure(src, start, end)
    return _MEASURED[key]


def mix_folder(album):
    return re.sub(r"#(\d)$", r"#0\1", safe_name(album))


def get_cover(src):
    """(mime, data) of the cover embedded in an .opus/.m4a/.mp3/.flac, or None."""
    low = src.lower()
    try:
        if low.endswith(".m4a"):
            covr = (MP4(src).tags or {}).get("covr")
            if covr:
                png = covr[0].imageformat == MP4Cover.FORMAT_PNG
                return ("image/png" if png else "image/jpeg", bytes(covr[0]))
        elif low.endswith(".opus"):
            blocks = (OggOpus(src).tags or {}).get("metadata_block_picture")
            if blocks:
                pic = Picture(base64.b64decode(blocks[0]))
                return (pic.mime, pic.data)
        elif low.endswith(".flac"):
            pics = FLAC(src).pictures
            if pics:
                return (pics[0].mime, pics[0].data)
        else:
            pics = ID3(src).getall("APIC")
            if pics:
                return (pics[0].mime, pics[0].data)
    except (ID3NoHeaderError, KeyError, ValueError):
        pass
    return None


def read_image(path):
    if path and os.path.exists(path):
        with open(path, "rb") as fh:
            return ("image/png" if path.lower().endswith(".png") else "image/jpeg", fh.read())
    return None


def tag_mp3(dst, t, cover):
    tags = ID3()
    tags.add(TIT2(encoding=3, text=t["title"]))
    if t.get("artist"):
        tags.add(TPE1(encoding=3, text=t["artist"]))
    tags.add(TALB(encoding=3, text=t["album"]))
    if t.get("album_artist"):
        tags.add(TPE2(encoding=3, text=t["album_artist"]))
    if t.get("n"):
        tags.add(TRCK(encoding=3, text=f'{t["n"]}/{t["total"]}'))
    if t.get("genre"):
        tags.add(TCON(encoding=3, text=t["genre"]))
    if t.get("year"):
        tags.add(TDRC(encoding=3, text=t["year"]))
    if t.get("label"):
        tags.add(TPUB(encoding=3, text=t["label"]))      # rekordbox "Label"
    tags.add(COMM(encoding=3, lang="eng", desc="", text=t["comment"]))
    if t.get("tracklist"):
        tags.add(COMM(encoding=3, lang="eng", desc="Tracklist", text=t["tracklist"]))
    if t.get("url"):
        tags.add(WOAS(url=t["url"]))
    if cover:
        tags.add(APIC(encoding=3, mime=cover[0], type=3, desc="Cover", data=cover[1]))
    tags.save(dst, v2_version=3)


def tag_flac(dst, t, cover):
    f = FLAC(dst)
    f.delete()
    f.clear_pictures()
    f["TITLE"] = t["title"]
    f["ALBUM"] = t["album"]
    for key, tag in (("artist", "ARTIST"), ("album_artist", "ALBUMARTIST"),
                     ("genre", "GENRE"), ("year", "DATE"), ("label", "LABEL"),
                     ("tracklist", "TRACKLIST"), ("url", "URL")):
        if t.get(key):
            f[tag] = t[key]
    if t.get("n"):
        f["TRACKNUMBER"], f["TRACKTOTAL"] = str(t["n"]), str(t["total"])
    f["COMMENT"] = t["comment"]
    if cover:
        pic = Picture()
        pic.type, pic.mime, pic.desc, pic.data = 3, cover[0], "Cover", cover[1]
        f.add_picture(pic)
    f.save()


TAGGERS = {".mp3": tag_mp3, ".flac": tag_flac}


def export(src, dst, fmt, start=None, end=None, gain=0.0):
    cmd = [settings()["ffmpeg"], "-nostdin", "-hide_banner", "-v", "error", "-y"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-to", f"{end:.3f}"]
    cmd += ["-i", src, "-map", "0:a", "-map_metadata", "-1", "-map_chapters", "-1"]
    filters = ([f"volume={gain:+.2f}dB"] if gain else []) + FORMATS[fmt][3]
    if filters:
        cmd += ["-af", ",".join(filters)]
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    subprocess.run(cmd + FORMATS[fmt][2] + [dst], check=True)


def plan(mix, timed):
    """Tracks for one mix: [{n, total, start, end, artist, title, year, year_shown}]."""
    merged = []                 # two tracks logged at the same second (a blend): one split
    for start, label in timed:
        if merged and int(start) == int(merged[-1][0]):
            a1, t1, y1, l1 = split_label(merged[-1][1])
            a2, t2, y2, l2 = split_label(label)
            label = f"{a1} / {a2} - {t1} / {t2}" if a1 and a2 else f"{merged[-1][1]} / {label}"
            merged[-1] = (merged[-1][0], label + (f" {{{y1 or y2}}}" if y1 or y2 else "")
                          + (f" {{label: {l1 or l2}}}" if l1 or l2 else ""))
        else:
            merged.append((start, label))
    timed = merged
    out, total = [], len(timed)
    for i, (start, label) in enumerate(timed):
        artist, title, shown, rec = split_label(label)
        out.append({"n": i + 1, "total": total, "start": float(start),
                    "end": float(timed[i + 1][0]) if i + 1 < total else None,
                    "artist": artist, "title": title, "year_shown": shown,
                    "year": year_tag(shown), "label": rec})
    return out


def track_name(t, ext, suffix=""):
    base = f'{t["n"]:02d} - {t["artist"]} - {t["title"]}' if t["artist"] \
        else f'{t["n"]:02d} - {t["title"]}'
    return safe_name(base + suffix) + ext


def split_mix(mix, tracks, src, out_root, fmt, art_dir=None, mains=False,
              dry_run=False, log=print, norm=None):
    """mix: {video_id, album, album_artist, genre, upload, year}. Returns manifest rows.
    norm: {target_lufs, ceiling_dbtp, scope: track|mix} or None for no normalizing."""
    folder_name, ext = FORMATS[fmt][0], FORMATS[fmt][1]
    base = os.path.join(out_root, folder_name)
    folder = mix_folder(mix["album"])
    src_cover = get_cover(src)
    vid = mix["video_id"]
    rows, lines = [], []
    mix_meas = measured(src) if norm and not dry_run and (mains or norm.get("scope") == "mix") else None
    for t in tracks:
        span = mmss(t["start"])
        lines.append(f'{span} {t["artist"]} - {t["title"]}' if t["artist"]
                     else f'{span} {t["title"]}')
        dst = os.path.join(base, folder, track_name(t, ext))
        url = f"https://youtu.be/{vid}?t={int(t['start'])}"
        note = f'{mix.get("album_artist") or ""} - {mix["album"]} @ {span} - {url}'.lstrip(" -")
        if t["year_shown"] and t["year_shown"] != t["year"]:
            note += f' - year shown: {t["year_shown"]}'
        if mix.get("upload"):
            note += f' - uploaded {mix["upload"]}'
        row = {"mix": mix["album"], "video_id": vid, "track": t["n"],
               "artist": t["artist"], "title": t["title"], "year": t["year"],
               "year_shown": t["year_shown"], "start": t["start"],
               "end": "" if t["end"] is None else t["end"],
               "file": os.path.relpath(dst, base), "url": url}
        rows.append(row)
        if dry_run:
            continue
        meas = None
        if norm:
            meas = mix_meas if norm.get("scope") == "mix" else measured(src, t["start"], t["end"])
            row.update(lufs=meas[0], true_peak=meas[1], gain_db=gain_db(meas, norm))
        export(src, dst, fmt, t["start"], t["end"], gain_db(meas, norm))
        cover = (read_image(os.path.join(art_dir or "", f'{vid}_{t["n"]:02d}.jpg'))
                 or read_image(os.path.join(art_dir or "", f"{vid}_main.jpg")) or src_cover)
        TAGGERS[ext](dst, {**t, "album": mix["album"], "album_artist": mix.get("album_artist"),
                           "genre": mix.get("genre"), "comment": note, "url": url}, cover)
    log(f'   {len(tracks)} tracks -> {folder_name}\\{folder}')
    if mains and not dry_run:
        dst = os.path.join(base, "_full mixes", folder + ext)
        export(src, dst, fmt, gain=gain_db(mix_meas, norm))
        url = f"https://youtu.be/{vid}"
        TAGGERS[ext](dst, {
            "title": mix["album"], "artist": mix.get("album_artist") or "",
            "album": mix["album"], "album_artist": mix.get("album_artist"),
            "genre": mix.get("genre"), "year": mix.get("year") or (mix.get("upload") or "")[:4],
            "comment": url + (f' - uploaded {mix["upload"]}' if mix.get("upload") else ""),
            "tracklist": "\n".join(lines), "url": url},
            read_image(os.path.join(art_dir or "", f"{vid}_main.jpg")) or src_cover)
        log(f'   full mix -> {folder_name}\\_full mixes\\{folder}{ext}')
    return rows


def write_manifest(out_root, fmt, rows, drop_ids=()):
    """Rewrite <out>/<format>/tracklist.csv, replacing rows for the videos in `rows` and
    removing `drop_ids`. Old rows without a video id are matched by mix name."""
    path = os.path.join(out_root, FORMATS[fmt][0], "tracklist.csv")
    done_ids, done_mixes = {r["video_id"] for r in rows} | set(drop_ids), {r["mix"] for r in rows}
    keep = []
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as fh:
            keep = [r for r in csv.DictReader(fh) if r.get("video_id") not in done_ids
                    and (r.get("video_id") or r.get("mix") not in done_mixes)]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = ["mix", "video_id", "track", "artist", "title", "year", "year_shown",
              "start", "end", "file", "url", "lufs", "true_peak", "gain_db"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(keep + rows)
