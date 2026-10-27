"""mixarchive: scrape, archive and split long mixes into tagged tracks.

Pipeline per collection (each step is resumable and skips finished work):
  resolve     find each mix's YouTube upload (playlists/channels, or search + duration
              match for local files). Channel videos are sorted into mixes, single
              tracks (-> editable _mixarchive/tracks.csv, parsed from the titles) and
              skips (trailers, too short/long, already archived elsewhere)
  tracklists  write editable _mixarchive/tracklists/<id>.txt from chapters /
              description / comments (existing files are kept - edit them freely);
              fill singles' years from their metadata once fetched
  fetch       download the best original audio (Opus 251, else AAC 140) into the
              originals folder; superseded local files move to <root>/_old 128k
  art         square covers (thumbnail - a release's sleeve is cropped out of it -
              or per-track video-frame crops)
  split       cut + tag tracks (MP3 320 / FLAC) into <root>/split/<format>/<mix>/;
              singles go whole into <root>/split/<format>/<label>/
  modmatch    (opt-in) match tracks to Modland modules -> _mixarchive/modules.csv
  modrender   (opt-in) render matched modules -> <root>/split/MOD renders/<mix>/

Around it: `add` turns YouTube links into collections (videos: youtube-picks), `update`
runs every collection then `rbsync` (tracks + playlists into rekordbox), `files` checks /
moves the files behind rekordbox's collection, `merge` merges one rekordbox playlist into
another, and `gui` does all of it in the browser.
"""
import argparse
import csv
import glob
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

from mutagen import File as MFile

from . import art, modland, singles, tracklist, yt
from .collection import Collection, known_video_ids, library, library_root, open_collection, register
from .split import (FORMATS, TAGGERS, export, gain_db, get_cover, measured, mix_folder,
                    plan, read_image, split_mix, track_name, write_manifest)
from .util import AUDIO_EXTS, load_json, mmss, norm, rekordbox_running, safe_name, slug

PICKS = "youtube-picks"      # where `add` puts single videos given no --to

RANK = {"chapters": 4, "description": 3, "track lengths": 3, "comment": 2}
RENDERABLE = ("auto", "auto-alt", "manual")


def _info(c, vid, force=False):
    """Cached info.json (with comments) for a video, fetched on first use."""
    path = c.info_path(vid)
    if force or not os.path.exists(path):
        yt.fetch_info(vid, c.p("info"))
    return load_json(path, {})


def _upload(info):
    u = info.get("upload_date") or ""
    return f"{u[:4]}-{u[4:6]}-{u[6:]}" if u else ""


def _score(info):
    src, timed, untimed = tracklist.best_from_info(info)
    abr = max((f.get("abr") or 0 for f in info.get("formats", [])
               if f.get("format_id") == "251"), default=0)
    return (RANK.get(src, 1 if untimed else 0), len(timed), abr), src


def _only(a):
    return [o.lower() for o in (getattr(a, "only", None) or "").split(",") if o]


def _selected(c, a):
    """Mixes (sources of kind 'mix'; older collections have no kind) matching --only."""
    only = _only(a)
    if getattr(a, "kind", None) == "track":
        return
    for s in c.sources:
        hay = f'{s.get("key", "")} {s.get("video_id", "")} {s.get("title", "")}'.lower()
        if s.get("kind", "mix") == "mix" and (not only or any(o in hay for o in only)):
            yield s


def _singles(c, a, rows=None, statuses=singles.EXPORT):
    """tracks.csv rows matching --only, in a stable order."""
    only = _only(a)
    rows = singles.read(c.p("tracks.csv")) if rows is None else rows
    if getattr(a, "kind", None) == "mix":
        return
    for k in sorted(rows, key=lambda k: (rows[k].get("upload") or "", k)):
        r = rows[k]
        hay = f'{r["video_id"]} {r["yt_title"]} {r["artist"]} {r["title"]}'.lower()
        if r["status"] in statuses and (not only or any(o in hay for o in only)):
            yield r


def _classify(c, e, skip_re, elsewhere):
    """('track' | 'mix' | 'skip', reason) for a playlist entry."""
    cfg, dur, title = c.config, e["duration"], e.get("title") or ""
    if dur is None or title in ("[Deleted video]", "[Private video]"):
        return "skip", "unavailable"
    if e["id"] in elsewhere:
        return "skip", f"archived in {elsewhere[e['id']]}"
    if dur < cfg["min_sec"]:
        return "skip", "too short"
    if dur > cfg["max_sec"]:
        return "skip", "too long (livestream?)"
    m = skip_re.search(title) if skip_re else None
    if m:
        return "skip", f"title: {m.group(0).strip()}"
    return ("mix" if dur >= cfg["mix_min_sec"] else "track"), ""


def _single_row(c, e):
    p = singles.parse_title(e["title"], c.config["label"], e.get("channel") or c.name)
    return {"video_id": e["id"], "part": 1, "start": 0, "end": "",
            "status": "ok" if p["artist"] else "check", "artist": p["artist"],
            "title": p["title"], "album": p["album"], "year": p["year"],
            "year_src": "title" if p["year"] else "", "label": c.config["label"],
            "catalog": p["catalog"], "duration": e["duration"], "upload": "",
            "yt_title": e["title"], "note": p["note"], "_kind": p["kind"]}


def _mix_meta(c, s, hdr):
    info = c.info(s["video_id"])
    return {"video_id": s["video_id"], "album": hdr.get("album") or info.get("title") or s["key"],
            "album_artist": hdr.get("album_artist") or c.config["album_artist"],
            "genre": hdr.get("genre") or c.config["genre"], "upload": _upload(info),
            "year": hdr.get("year", "")}


# ---------------------------------------------------------------- commands

def cmd_init(a):
    c = Collection(a.root)
    cfg = c.config
    if not os.path.exists(c.p("collection.json")):
        cfg["originals_dir"] = "originals"     # new collections keep the root tidy
    cfg["name"] = a.name
    if a.playlist:
        cfg["source"] = {"playlists": a.playlist}
    elif a.local:
        cfg["source"] = {"local_dir": os.path.abspath(a.local), "exclude": a.exclude or ""}
    for key in ("genre", "album_artist", "label"):
        if getattr(a, key) is not None:
            cfg[key] = getattr(a, key)
    if a.no_upload_year:
        cfg["year_from_upload"] = False
    if a.formats:
        cfg["formats"] = a.formats.split(",")
    if a.no_mains:
        cfg["mains"] = False
    if a.modrender:
        cfg["modrender"] = True
    if a.frames_art:
        cfg["art"] = {"mode": "frames", "offset": 28, "crops": {"default": [0, 0, 720, 720]}}
    c.save()
    register(a.name, c.root)
    print(f"collection '{a.name}' at {c.root}")


def cmd_list(a):
    for name, root in library().items():
        print(f"{name:20} {root}")


def cmd_resolve(a):
    c = open_collection(a.collection)
    cfg = c.config["source"]
    known = {s["key"]: s for s in c.sources}
    if c.playlists() or c.videos():
        elsewhere = known_video_ids(c.root) if c.config.get("skip_known") else {}
        skip_re = re.compile(c.config["skip_titles"], re.I) if c.config.get("skip_titles") else None
        rows = singles.read(c.p("tracks.csv"))
        offset, counts = 0, {}
        lists = [yt.playlist(url) for url in c.playlists()]
        picks = [v for v in c.videos() if v not in known or a.force]
        if picks:                        # single videos added with `add`: numbered after the lists
            pos = {v: n for n, v in enumerate(c.videos(), 1)}
            lists.append([{**e, "index": pos[e["id"]]} for e in yt.videos(picks)])
        for entries in lists:
            for e in entries:
                if e["id"] in known and not a.force:
                    continue
                kind, why = _classify(c, e, skip_re, elsewhere)
                counts[kind] = counts.get(kind, 0) + 1
                entry = {"key": e["id"], "index": offset + int(e["index"]), "video_id": e["id"],
                         "title": e["title"], "duration": e["duration"], "kind": kind}
                if why:
                    entry["skip"] = why
                if kind == "mix":
                    try:
                        info = _info(c, e["id"], a.force)
                    except RuntimeError as err:     # listed, but removed / blocked here
                        entry.update(kind="skip", skip=f"unavailable: {str(err)[-80:]}")
                        known[e["id"]] = entry
                        print(f'  skip {e["id"]}  unavailable  {e["title"]}')
                        continue
                    entry.update({"title": info.get("title"), "channel": info.get("channel"),
                                  "duration": info.get("duration"), "upload": _upload(info),
                                  "tracklist_source": _score(info)[1]})
                    print(f'  mix  {e["id"]}  [{entry["tracklist_source"]}]  {info.get("title")}')
                elif (kind == "track" or (why and e["duration"] and e["duration"] < c.config["mix_min_sec"])) \
                        and (rows.get((e["id"], 1), {}).get("status") != "manual"):
                    row = _single_row(c, e)               # skipped short ones stay listed
                    if why:
                        row["status"], row["note"] = "skip", why
                    rows[(e["id"], 1)] = row
                known[e["id"]] = entry
            offset += len(entries)
        singles.mark_dups(list(rows.values()))
        if rows:
            singles.write(c.p("tracks.csv"), rows)
        st = {}
        for r in rows.values():
            st[r["status"]] = st.get(r["status"], 0) + 1
        print(f"  new: {counts}   tracks.csv: {st}")
    elif "local_dir" in cfg and not os.path.isdir(cfg["local_dir"]):
        print(f"  {cfg['local_dir']} is gone - no new local files to resolve")
    elif "local_dir" in cfg:
        ex = re.compile(cfg["exclude"], re.I) if cfg.get("exclude") else None
        for f in sorted(os.listdir(cfg["local_dir"])):
            path = os.path.join(cfg["local_dir"], f)
            if not os.path.isfile(path) or not f.lower().endswith(AUDIO_EXTS) or (ex and ex.search(f)):
                continue
            if f in known and not a.force:
                continue
            dur = MFile(path).info.length
            stem = os.path.splitext(f)[0]
            cands = [r for r in yt.search(stem) if r["duration"] and abs(r["duration"] - dur) <= 3]
            entry = {"key": f, "local_file": f, "local_duration": round(dur, 1), "video_id": None}
            scored = []
            for r in cands:
                info = _info(c, r["id"], a.force)
                score, src = _score(info)
                scored.append((score, r, src, info))
            scored.sort(key=lambda x: x[0], reverse=True)
            if scored:
                _, r, src, info = scored[0]
                entry.update({"video_id": r["id"], "title": info.get("title"),
                              "channel": info.get("channel"), "duration": info.get("duration"),
                              "upload": _upload(info), "tracklist_source": src,
                              "alternates": [{"id": x[1]["id"], "channel": x[1]["channel"],
                                              "tracklist_source": x[2]} for x in scored[1:]]})
            known[f] = entry
            print(f'  {f[:58]:58} -> {entry["video_id"] or "NO MATCH"}  '
                  f'[{entry.get("tracklist_source", "-")}]  {entry.get("channel", "")}')
    for pick in a.pick or []:
        key, _, vid = pick.partition("=")
        hits = [s for s in known.values() if key.lower() in s["key"].lower()]
        if len(hits) != 1:
            raise SystemExit(f"--pick {pick}: matches {len(hits)} mixes")
        info = _info(c, vid)
        hits[0].update({"video_id": vid, "title": info.get("title"), "channel": info.get("channel"),
                        "duration": info.get("duration"), "upload": _upload(info),
                        "tracklist_source": _score(info)[1], "picked": True})
        print(f"  picked {vid} for {hits[0]['key']}")
    c.sources = sorted(known.values(), key=lambda s: (s.get("index") or 0, s["key"].lower()))
    c.save()


def silence_gaps(path, noise_db=-50, min_sec=0.4):
    """Track starts (s) after each silent gap, ignoring silence at the very start/end."""
    import subprocess
    from .util import settings
    err = subprocess.run([settings()["ffmpeg"], "-nostdin", "-hide_banner", "-nostats", "-i", path, "-map", "0:a",
                          "-af", f"silencedetect=noise={noise_db}dB:d={min_sec}", "-f", "null", "-"],
                         capture_output=True, text=True, errors="replace").stderr
    dur = MFile(path).info.length
    gaps = [(float(e), float(d)) for e, d in
            re.findall(r"silence_end: ([\d.]+) \| silence_duration: ([\d.]+)", err)
            if 5 < float(e) < dur - 5]
    # quiet moments next to the real gap show up as a cluster: keep the longest silence
    merged = []
    for end, length in gaps:
        if merged and end - merged[-1][0] < 30:
            if length > merged[-1][1]:
                merged[-1] = (end, length)
        else:
            merged.append((end, length))
    return [round(e) for e, _ in merged]


def _guess(stem):
    year = (re.findall(r"\((19\d\d|20\d\d)\)", stem) or [""])[-1]
    artist = stem.split(" - ", 1)[0].strip() if " - " in stem else ""
    return artist, year


def cmd_tracklists(a):
    c = open_collection(a.collection)
    years = {}
    if a.years:
        with open(a.years, encoding="utf-8-sig") as fh:
            years = {(r["video_id"], int(r["track"])): r["year_shown"] for r in csv.DictReader(fh)}
    for s in _selected(c, a):
        vid = s.get("video_id")
        path = vid and c.tracklist_path(vid)
        if not vid or (os.path.exists(path) and not a.force):
            continue
        info = c.info(vid)
        src, timed, untimed = tracklist.best_from_info(info)
        if not timed:
            # a sibling upload (e.g. CD3 of a box set) may list every CD's track lengths
            texts = []
            for p in glob.glob(c.p("info", "*.info.json")):
                other = load_json(p, {})
                texts += [other.get("description")] + [x.get("text") for x in other.get("comments") or []]
            lengths = tracklist.from_durations(texts, info.get("duration") or 0, expect=untimed)
            if lengths:
                src, timed, untimed = "track lengths (sibling upload)", tracklist._tidy_timed(lengths), []
        if not timed and untimed and c.original(s) and os.path.exists(c.original(s)):
            # unmixed CDs: exactly one silent gap between consecutive tracks
            gaps = silence_gaps(c.original(s))
            if len(gaps) == len(untimed) - 1:
                src, timed, untimed = "silence gaps", list(zip([0] + gaps, untimed)), []
        if timed and timed[0][0] <= 15:
            timed[0] = (0, timed[0][1])
        timed = [(sec, f"{label} {{{years[(vid, n)]}}}" if (vid, n) in years else label)
                 for n, (sec, label) in enumerate(timed, 1)]
        stem = os.path.splitext(s.get("local_file") or "")[0]
        artist, year = _guess(stem) if stem else ("", "")
        status = "ok" if timed and not untimed else ("needs timestamps" if untimed else "no tracklist")
        header = {"video": vid, "url": f"https://youtu.be/{vid}", "source": src,
                  "status": status, "album": stem or info.get("title"),
                  "album_artist": c.config["album_artist"] or artist, "year": year,
                  "original": s.get("local_file", "")}
        if os.path.exists(path):        # --force refreshes tracks but keeps hand-edited metadata
            old = tracklist.read(path)[0]
            header.update({k: old[k] for k in ("album", "album_artist", "year", "genre") if k in old})
        tracklist.write(path, header, timed, untimed)
        print(f"  {status:16} {len(timed) or len(untimed):3} lines  [{src}]  {stem or info.get('title')}")
    _enrich_singles(c, a)


def _enrich_singles(c, a):
    """After fetch: upload dates, years (YouTube music data / (P) line / upload year) and
    multi-track videos ('Ice / Water World' with chapters) split into parts."""
    rows = singles.read(c.p("tracks.csv"))
    changed = 0
    for r in list(_singles(c, a, rows, statuses=("ok", "check"))):
        info = c.info(r["video_id"])
        if not info:
            continue
        before = dict(r)
        r["upload"] = r["upload"] or _upload(info)
        if not r["year"]:
            r["year"], r["year_src"] = singles.year_from_info(info, c.config["year_from_upload"])
        parts = singles.multi_track(info) if (r["video_id"], 2) not in rows else []
        if len(parts) > 1 and int(r["part"]) == 1:
            for n, (start, end, name) in enumerate(parts, 1):
                p = singles.parse_title(name, c.config["label"], c.name)
                rows[(r["video_id"], n)] = {
                    **r, "part": n, "start": start, "end": end if n < len(parts) else "",
                    "artist": p["artist"] or r["artist"], "title": p["title"],
                    "note": f"part {n}/{len(parts)} of: {r['yt_title']}"}
            print(f"  {len(parts)} tracks in one video: {r['yt_title']}")
        changed += r != before or len(parts) > 1
    if changed:
        singles.write(c.p("tracks.csv"), rows)
        print(f"  tracks.csv: {changed} singles updated from their metadata")


def cmd_fetch(a):
    c = open_collection(a.collection)
    old_dir = os.path.join(c.root, c.config["old_dir"])
    failed = []
    for s in _selected(c, a):
        vid = s.get("video_id")
        if not vid or (c.original(s) and os.path.exists(c.original(s)) and not a.force):
            continue
        hdr = tracklist.read(c.tracklist_path(vid))[0] if os.path.exists(c.tracklist_path(vid)) else {}
        name = safe_name(hdr.get("album") or s.get("title") or vid)
        wanted = {norm(name), norm(s.get("title"))}
        os.makedirs(c.originals, exist_ok=True)
        existing = [f for f in os.listdir(c.originals) if f.lower().endswith((".opus", ".m4a"))
                    and norm(os.path.splitext(f)[0]) in wanted]
        if existing and not a.force:           # already downloaded (e.g. by hand): adopt it
            s["original"] = os.path.relpath(os.path.join(c.originals, existing[0]), c.root)
            print(f"  adopted {existing[0]}")
        else:
            print(f"  downloading {vid} -> {name}")
            try:
                s["original"] = os.path.relpath(yt.download_audio(vid, os.path.join(c.originals, name)),
                                                c.root)
            except RuntimeError as e:          # one bad download shouldn't stop the batch
                failed.append(name)
                print(f"  !! {name}: {e}")
                continue
        local = s.get("local_file") and os.path.join(c.config["source"].get("local_dir", ""), s["local_file"])
        if local and os.path.exists(local):
            os.makedirs(old_dir, exist_ok=True)
            shutil.move(local, os.path.join(old_dir, s["local_file"]))
        c.save()
    if failed:
        print(f"  {len(failed)} download(s) failed - re-run fetch to retry: {', '.join(failed)}")
    have = c.single_originals()
    todo = sorted({r["video_id"] for r in _singles(c, a)} - set(have))
    if todo:
        print(f"  downloading {len(todo)} singles into {c.originals} (log: {c.p('fetch.log')})")
        yt.download_batch(todo, c.originals, c.p("info"), c.p("fetch.log"))
        missing = [v for v in todo if v not in c.single_originals()]
        if missing and yt.pot_server():   # 403s: again via mweb with a PO token (Opus)
            print(f"  {len(missing)} failed - retrying with a PO token ({yt.POT_CLIENT} client)")
            yt.download_batch(missing, c.originals, c.p("info"), c.p("fetch.log"),
                              client=yt.POT_CLIENT)
            missing = [v for v in missing if v not in c.single_originals()]
        if missing:                       # still blocked: the HLS copy of AAC 140
            print(f"  {len(missing)} blocked - trying their HLS (AAC 128k) streams")
            yt.download_batch(missing, c.originals, c.p("info"), c.p("fetch.log"), fmt=yt.HLS_FORMAT)
            missing = [v for v in missing if v not in c.single_originals()]
        print(f"  got {len(todo) - len(missing)} of {len(todo)} singles"
              + (f"; {len(missing)} failed - re-run fetch to retry" if missing else ""))


def cmd_art(a):
    c = open_collection(a.collection)
    mode = c.config["art"].get("mode", "thumbnail")
    review = []
    for s in _selected(c, a):
        vid, orig = s.get("video_id"), c.original(s)
        if not vid or not orig or not os.path.exists(orig):
            continue
        main = c.p("art", f"{vid}_main.jpg")
        if a.force or not os.path.exists(main):
            cover = get_cover(orig)
            if cover:
                art.square_from_bytes(cover[1], main)
        if mode != "frames" or not os.path.exists(c.tracklist_path(vid)):
            continue
        _, timed, _ = tracklist.read(c.tracklist_path(vid))
        crops = c.config["art"].get("crops", {})
        box = crops.get(vid) or crops.get(str(s.get("index"))) or crops.get("default")
        todo = [(n, sec) for n, (sec, _) in enumerate(timed, 1)
                if a.force or not os.path.exists(c.p("art", f"{vid}_{n:02d}.jpg"))]
        if todo:
            for (n, _), frame in zip(todo, art.grab_frames(vid, todo, c.config["art"].get("offset", 28),
                                                             c.p("frames"), vid)):
                if frame:
                    art.crop_frame(frame, box, c.p("art", f"{vid}_{n:02d}.jpg"))
        review += [(f"{s.get('index') or ''}_{n:02d}", c.p("art", f"{vid}_{n:02d}.jpg"))
                   for n in range(1, len(timed) + 1) if os.path.exists(c.p("art", f"{vid}_{n:02d}.jpg"))]
    for i in range(0, len(review), 60):
        art.contact_sheet(review[i:i + 60], c.p("review", f"art_{i // 60 + 1}.jpg"))
    have = c.single_originals()
    todo = {r["video_id"] for r in _singles(c, a) if r["video_id"] in have
            and (a.force or not os.path.exists(c.p("art", f'{r["video_id"]}_main.jpg')))}

    def cover(vid):
        data = get_cover(have[vid])
        if not data:                    # thumbnail wasn't embedded: fetch the one in the metadata
            data = art.fetch_image(c.info(vid).get("thumbnail"))
        return art.square_from_bytes(data[1], c.p("art", f"{vid}_main.jpg")) if data else "none"

    with ThreadPoolExecutor(max_workers=8) as pool:
        modes = list(pool.map(cover, sorted(todo)))
    if todo:
        print(f"  singles: {modes.count('cover')} sleeve crops, {modes.count('letterbox')} letterboxed, "
              f"{modes.count('none')} without a thumbnail")
    print(f"  art ready{'; review sheets in ' + c.p('review') if review else ''}")


def cmd_split(a):
    c = open_collection(a.collection)
    fmts = a.formats.split(",") if a.formats else c.config["formats"]
    rows = {f: [] for f in fmts}
    jobs = []
    for s in _selected(c, a):
        vid, orig = s.get("video_id"), c.original(s)
        label = s.get("title") or s["key"]
        if not vid or not orig or not os.path.exists(orig):
            print(f"  -- no original yet: {label}")
            continue
        if not os.path.exists(c.tracklist_path(vid)):
            print(f"  -- no tracklist: {label}")
            continue
        hdr, timed, untimed = tracklist.read(c.tracklist_path(vid))
        mix = _mix_meta(c, s, hdr)
        tracks = plan(mix, timed) if timed and not untimed else []
        print(f"== {mix['album']}" + ("" if tracks else "   (no timed tracklist: full mix only)"))
        for fmt in fmts:
            folder = os.path.join(c.out_root, FORMATS[fmt][0], mix_folder(mix["album"]))
            if tracks and not a.dry_run and os.path.isdir(folder):
                for old in glob.glob(os.path.join(glob.escape(folder), "*" + FORMATS[fmt][1])):
                    os.remove(old)       # tracklist may have changed: no stale names
            jobs.append((fmt, mix, tracks, orig))
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:   # each job runs ffmpeg processes
        futures = [(fmt, pool.submit(split_mix, mix, tracks, orig, c.out_root, fmt,
                                     art_dir=c.p("art"), mains=c.config["mains"],
                                     dry_run=a.dry_run, norm=c.config.get("normalize")))
                   for fmt, mix, tracks, orig in jobs]
        for fmt, fut in futures:
            rows[fmt] += fut.result()
    if not a.dry_run:
        for fmt, r in rows.items():
            if r:
                write_manifest(c.out_root, fmt, r)
        for fmt in fmts:
            _split_singles(c, a, fmt)


def _split_singles(c, a, fmt):
    """Export each single whole (or each part of a multi-track video): one gain to the
    loudness target, tags, cover. Files already made from the same row are kept."""
    table = singles.read(c.p("tracks.csv"))
    if not table:
        return
    base = os.path.join(c.out_root, FORMATS[fmt][0])
    folder, ext = c.singles_folder, FORMATS[fmt][1]
    old = {}
    manifest = os.path.join(base, "tracklist.csv")
    if os.path.exists(manifest):
        with open(manifest, encoding="utf-8-sig") as fh:
            old = {(r["video_id"], int(r["track"] or 1)): r for r in csv.DictReader(fh)
                   if r["mix"] == folder}
    have, norm_cfg = c.single_originals(), c.config.get("normalize")
    names = singles.file_names(list(_singles(c, argparse.Namespace(only=None), table)), ext)
    rows, drop, jobs = [], set(), []
    for k, r in table.items():           # rows no longer exported: remove their files
        if r["status"] not in singles.EXPORT and k in old:
            f = os.path.join(base, old[k]["file"])
            if os.path.exists(f):
                os.remove(f)
            drop.add(r["video_id"])
    for r in _singles(c, a, table):
        vid, part = r["video_id"], int(r["part"] or 1)
        if vid not in have:
            continue
        start, end = float(r["start"] or 0), (float(r["end"]) if r["end"] else None)
        rel = os.path.join(folder, names[(vid, part)])
        url = f"https://youtu.be/{vid}" + (f"?t={int(start)}" if start else "")
        prev = old.get((vid, part))
        if prev and prev["file"] != rel and os.path.exists(os.path.join(base, prev["file"])):
            os.remove(os.path.join(base, prev["file"]))      # renamed in tracks.csv
        row = {"mix": folder, "video_id": vid, "track": part, "artist": r["artist"],
               "title": r["title"], "year": r["year"], "year_shown": "", "start": start,
               "end": "" if end is None else end, "file": rel, "url": url}
        rows.append(row)
        if prev and prev["file"] == rel and os.path.exists(os.path.join(base, rel)) and not a.force:
            row.update({k: prev.get(k, "") for k in ("lufs", "true_peak", "gain_db")})
            continue
        jobs.append((r, row, have[vid], start, end, url))

    def export_one(job):
        r, row, src, start, end, url = job
        meas = measured(src, start or None, end) if norm_cfg else None
        gain = gain_db(meas, norm_cfg)
        if meas:
            row.update(lufs=meas[0], true_peak=meas[1], gain_db=gain)
        dst = os.path.join(base, row["file"])
        export(src, dst, fmt, start or None, end, gain)
        label = r["label"] or c.config["label"]
        note = " - ".join(x for x in (label, r["yt_title"], url) if x)
        if r["catalog"]:
            note += f' - cat. {r["catalog"]}'
        if r["upload"]:
            note += f' - uploaded {r["upload"]}' + (" (year from upload date)" if r["year_src"] == "upload" else "")
        by_artist = c.config.get("singles_album_artist") == "artist"
        TAGGERS[ext](dst, {"title": r["title"], "artist": r["artist"],
                           "album": r["album"] or folder,
                           "album_artist": c.config["album_artist"] or label
                           or (r["artist"] if by_artist else "") or folder,
                           "genre": c.config["genre"], "year": r["year"], "label": label,
                           "comment": note, "url": url},
                     read_image(c.p("art", f"{r['video_id']}_main.jpg")) or get_cover(src))
        return row

    print(f"  singles: {len(jobs)} to export, {len(rows) - len(jobs)} already done -> "
          f"{FORMATS[fmt][0]}\\{folder}")
    with ThreadPoolExecutor(max_workers=a.jobs) as pool:
        for i, _ in enumerate(pool.map(export_one, jobs), 1):
            if i % 100 == 0:
                print(f"   {i}/{len(jobs)}")
    if rows or drop:
        write_manifest(c.out_root, fmt, rows, drop_ids=drop)


def cmd_modmatch(a):
    c = open_collection(a.collection)
    index = modland.prepare(modland.load_index())
    rows = modland.read_csv(c.p("modules.csv"))
    for s in _selected(c, a):
        vid = s.get("video_id")
        if not vid or not os.path.exists(c.tracklist_path(vid)):
            continue
        _, timed, _ = tracklist.read(c.tracklist_path(vid))
        for n, (_, label) in enumerate(timed, 1):
            old = rows.get((vid, n))
            if old and (not a.force or old["status"] in ("manual", "skip")):
                continue            # hand-set rows survive --force
            artist, title, _, _ = tracklist.split_label(label)
            status, best, alts = modland.match(artist, title, index)
            rows[(vid, n)] = {"video_id": vid, "track": n, "artist": artist, "title": title,
                              "status": status, "modland_path": best[4] if best else "",
                              "size": best[3] if best else "", "score": best[0] if best else "",
                              "alternates": " | ".join(x[4] for x in alts)}
    modland.write_csv(c.p("modules.csv"), rows)
    counts = {}
    for r in rows.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"  modules.csv: {counts}")
    for r in rows.values():
        if r["status"] == "check":
            print(f'   check: {r["artist"]} - {r["title"]}  ->  {r["modland_path"]}')


def cmd_modrender(a):
    c = open_collection(a.collection)
    rows = modland.read_csv(c.p("modules.csv"))
    fmts = a.formats.split(",") if a.formats else ["mp3"]
    report = []
    for s in _selected(c, a):
        vid = s.get("video_id")
        if not vid or not os.path.exists(c.tracklist_path(vid)):
            continue
        hdr, timed, _ = tracklist.read(c.tracklist_path(vid))
        mix = _mix_meta(c, s, hdr)
        mix_dur = c.info(vid).get("duration") or MFile(c.original(s)).info.length
        for t in plan(mix, timed):
            r = rows.get((vid, t["n"]))
            if not r or r["status"] not in RENDERABLE or not r["modland_path"]:
                continue
            # Same-named modules are often other songs or other versions, so check the
            # render's length against the mix's track; try alternates when it's off.
            target = (t["end"] or mix_dur) - t["start"]
            cands = [r["modland_path"]]
            if r["status"] != "manual":
                # only other versions by the same artist (e.g. "jungle love.edit.mod");
                # a same-titled song from another folder is a different track
                folder = r["modland_path"].split("/")[1:2]
                cands += [p for p in r.get("alternates", "").split(" | ")
                          if p and p.split("/")[1:2] == folder]
            flac, tried = None, []
            for path in cands:
                f = c.p("renders", path.replace("/", "__") + ".flac")
                if a.force or not os.path.exists(f):
                    modland.render(modland.download(path, c.p("modules")), f)
                dur = MFile(f).info.length
                tried.append(f"{path} {mmss(dur)}")
                if r["status"] == "manual" or abs(dur - target) <= max(20, 0.12 * target):
                    flac = f
                    if path != r["modland_path"]:
                        r.update(modland_path=path, status="auto-alt")
                    break
            if not flac:
                r["status"] = "mismatch"
                print(f'   mismatch {t["artist"]} - {t["title"]} (mix {mmss(target)}): {"; ".join(tried)}')
                continue
            badge = c.p("art", "mod", f'{vid}_{t["n"]:02d}.jpg')
            base_art = c.p("art", f'{vid}_{t["n"]:02d}.jpg')
            if not os.path.exists(base_art):
                base_art = c.p("art", f"{vid}_main.jpg")
            if os.path.exists(base_art) and (a.force or not os.path.exists(badge)):
                art.badge(base_art, badge)
            span = mmss(t["start"])
            norm = c.config.get("normalize")
            gain = gain_db(measured(flac), norm) if norm else 0.0
            for fmt in fmts:
                sub = "MOD renders" + ("" if fmt == "mp3" else f" ({fmt.upper()})")
                dst = os.path.join(c.out_root, sub, mix_folder(mix["album"]),
                                   track_name(t, FORMATS[fmt][1], " (MOD render)"))
                export(flac, dst, fmt, gain=gain)
                TAGGERS[FORMATS[fmt][1]](dst, {
                    **t, "title": f'{t["title"]} (MOD render)', "album": mix["album"],
                    "album_artist": mix["album_artist"], "genre": mix["genre"],
                    "comment": f'MOD render of Modland {r["modland_path"]} (libopenmpt: A1200 filter, '
                               f'50% stereo, 48 kHz) - A/B with {mix["album"]} @ {span}',
                    "url": f"https://youtu.be/{vid}?t={int(t['start'])}"}, read_image(badge))
            dur = MFile(flac).info.length
            report.append([mix["album"], t["n"], t["artist"], t["title"], r["modland_path"],
                           round(dur, 1), round(target, 1)])
            print(f'   rendered {t["artist"]} - {t["title"]}  ({mmss(dur)} vs mix {mmss(target)})')
    modland.write_csv(c.p("modules.csv"), rows)
    if report:
        path = os.path.join(c.out_root, "MOD renders", "renders.csv")
        keep = []
        if os.path.exists(path):
            done = {(x[0], str(x[1])) for x in report}
            with open(path, encoding="utf-8-sig") as fh:
                keep = [row for row in list(csv.reader(fh))[1:] if (row[0], row[1]) not in done]
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["mix", "track", "artist", "title", "modland_path", "render_sec", "mix_sec"])
            w.writerows(keep + report)


def cmd_verify(a):
    """Every manifest row: file decodes, length matches, tags + cover present; plus a
    loudness re-measure of a random sample against the normalize target."""
    import random
    import subprocess
    from mutagen.id3 import ID3
    from .split import measure
    from .util import settings
    c = open_collection(a.collection)
    norm = c.config.get("normalize")
    for fmt in c.config["formats"]:
        base = os.path.join(c.out_root, FORMATS[fmt][0])
        path = os.path.join(base, "tracklist.csv")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        problems = []

        def check(r):
            p = os.path.join(base, r["file"])
            if not os.path.exists(p):
                return [f"missing: {r['file']}"]
            out = []
            f = MFile(p)
            if r["end"] and abs(f.info.length - (float(r["end"]) - float(r["start"]))) > 0.1:
                out.append(f"length off: {r['file']}")
            if fmt == "mp3":
                t = ID3(p)
                if "TIT2" not in t or "TALB" not in t or not t.getall("APIC"):
                    out.append(f"tags/cover missing: {r['file']}")
            err = subprocess.run([settings()["ffmpeg"], "-nostdin", "-v", "error", "-i", p, "-f", "null", "-"],
                                 capture_output=True, text=True, errors="replace").stderr.strip()
            if err:
                out.append(f"decode: {r['file']}: {err[:120]}")
            return out

        with ThreadPoolExecutor(max_workers=a.jobs) as pool:
            for out in pool.map(check, rows):
                problems += out
        mains = glob.glob(os.path.join(glob.escape(base), "_full mixes", "*" + FORMATS[fmt][1]))
        print(f"  {FORMATS[fmt][0]}: {len(rows)} tracks, {len(mains)} full mixes, "
              f"{len(problems)} problems")
        for p in problems[:20]:
            print("    " + p)
        if norm and rows:
            print(f"  loudness re-measure (sample of {min(a.sample, len(rows))}):")
            for r in random.sample(rows, min(a.sample, len(rows))):
                lufs, tp = measure(os.path.join(base, r["file"]))
                capped = r.get("gain_db") and r.get("lufs") and \
                    float(r["gain_db"]) < norm["target_lufs"] - float(r["lufs"]) - 0.05
                ok = tp <= norm["ceiling_dbtp"] + 0.3 and (capped or abs(lufs - norm["target_lufs"]) <= 0.5)
                print(f"    {'ok ' if ok else 'BAD'} {lufs:6.1f} LUFS {tp:+5.1f} dBTP"
                      f"{' (peak-capped)' if capped else ''}  {r['file'][:70]}")


def cmd_status(a):
    c = open_collection(a.collection)
    mods = modland.read_csv(c.p("modules.csv"))
    print(f"{c.name}: {c.root}")
    rows = singles.read(c.p("tracks.csv"))
    if rows:
        st = {}
        for r in rows.values():
            st[r["status"]] = st.get(r["status"], 0) + 1
        have = c.single_originals()
        todo = [r for r in rows.values() if r["status"] in singles.EXPORT]
        out = {}
        for fmt in c.config["formats"]:
            folder = os.path.join(c.out_root, FORMATS[fmt][0], c.singles_folder)
            out[fmt] = len(glob.glob(os.path.join(glob.escape(folder), "*" + FORMATS[fmt][1])))
        print(f"  singles: {st}  originals {sum(1 for r in todo if r['video_id'] in have)}/{len(todo)}  "
              + "  ".join(f"{f} {n}" for f, n in out.items()))
    skipped = [s for s in c.sources if s.get("kind") == "skip"]
    if skipped:
        print(f"  skipped videos: {len(skipped)} (see sources.json 'skip')")
    for s in c.sources:
        if s.get("kind", "mix") != "mix":
            continue
        vid = s.get("video_id")
        tl = vid and c.tracklist_path(vid)
        hdr, timed, untimed = tracklist.read(tl) if tl and os.path.exists(tl) else ({}, [], [])
        album = hdr.get("album") or s.get("title") or s["key"]
        orig = "orig" if c.original(s) and os.path.exists(c.original(s)) else "----"
        parts = []
        for fmt in c.config["formats"]:
            folder = os.path.join(c.out_root, FORMATS[fmt][0], mix_folder(album))
            parts.append(f"{fmt} {len(glob.glob(os.path.join(glob.escape(folder), '*' + FORMATS[fmt][1])))}")
        if c.config.get("modrender"):
            ok = sum(1 for (v, _), r in mods.items() if v == vid and r["status"] in RENDERABLE)
            parts.append(f"mod {ok}")
        tlinfo = f"{len(timed)} timed" + (f" + {len(untimed)} untimed" if untimed else "")
        print(f"  {album[:52]:52} {vid or 'NO VIDEO':12} {orig}  {tlinfo:20} {'  '.join(parts)}")


def cmd_rbxml(a):
    """rekordbox XML with one playlist per collection (see rbxml.py)."""
    from . import rbxml
    names = a.collections or list(library())
    playlists = []
    for n in names:
        playlists += rbxml.collection_files(open_collection(n))
    playlists = [(name, files) for name, files in playlists if files]
    out = a.out or os.path.join(os.path.commonpath([library()[n] for n in library()]), "rekordbox.xml")
    n = rbxml.write(out, playlists)
    total = 0
    for name, files in playlists:
        size = sum(os.path.getsize(f) for f in files)
        total += size
        print(f"  {name:40} {len(files):5} tracks  {size / 1e9:6.1f} GB")
    print(f"  {n} tracks in {len(playlists)} playlists ({total / 1e9:.1f} GB) -> {out}")


def cmd_rbsync(a):
    """Write the same playlists as rbxml straight into rekordbox's database (rbdb.py, run
    with rbvenv's Python): new tracks with tags + covers, one playlist per collection in a
    `mixarchive` folder, refreshed on every run. rekordbox must be closed."""
    import subprocess
    from . import rbxml
    from .util import APP_DIR, save_json, settings
    lib = library()
    playlists = []
    for n in a.collections or list(lib):
        playlists += rbxml.collection_files(open_collection(n))
    plan = {"folder": "mixarchive", "root": library_root(), "backup_dir": _rb_backups(),
            "state": os.path.join(APP_DIR, "cache", "rbsync_written.json"),
            "playlists": [{"name": name, "files": [os.path.abspath(f) for f in files]}
                          for name, files in playlists if files]}
    path = os.path.join(APP_DIR, "cache", "rbsync_plan.json")
    save_json(path, plan)
    cmd = [settings()["rbpython"], "-I", os.path.join(APP_DIR, "mixarchive", "rbdb.py"), path]
    r = subprocess.run(cmd + ([] if a.dry_run else ["--apply"]))
    if r.returncode:
        raise SystemExit(r.returncode)


def _rb_backups():
    return os.path.join(os.path.dirname(library_root()), "_rekordbox backups")


def cmd_files(a):
    """Check / relocate / move the files behind rekordbox's whole collection (rbfiles.py,
    run with rbvenv's Python like rbsync). `mixarchive files` alone prints its help."""
    import subprocess
    from .util import APP_DIR, settings
    s, root = settings(), library_root()
    cmd = [s["rbpython"], "-I", os.path.join(APP_DIR, "mixarchive", "rbfiles.py")] + (a.args or ["--help"])
    if a.args and a.args[0] not in ("-h", "--help"):
        cmd += [f"--backup-dir={_rb_backups()}", f"--ffmpeg={s['ffmpeg']}", f"--ffprobe={s['ffprobe']}",
                f"--mirror={s['usb_mirror']}", f"--cache={os.path.join(APP_DIR, 'cache', 'silence_scan.json')}",
                f"--area=mixarchive={root}", f"--area=Library={os.path.dirname(root)}"]
    r = subprocess.run(cmd)
    if r.returncode:
        raise SystemExit(r.returncode)


def cmd_merge(a):
    """Merge one rekordbox playlist into another (rbmerge.py, run with rbvenv's Python like
    rbsync): copies of the same recording are found by ear, the best stays (WAV first), the
    others' cues move onto it, and the merged playlist is deleted. rekordbox must be closed."""
    import subprocess
    from .util import APP_DIR, settings
    s = settings()
    cmd = [s["rbpython"], "-I", os.path.join(APP_DIR, "mixarchive", "rbmerge.py"), a.source, a.into,
           f"--backup-dir={_rb_backups()}", f"--ffmpeg={s['ffmpeg']}",
           f"--written={os.path.join(APP_DIR, 'cache', 'rbsync_written.json')}", f"--root={library_root()}"]
    r = subprocess.run(cmd + (["--apply"] if a.apply else []) + (["--keep-source"] if a.keep_source else []))
    if r.returncode:
        raise SystemExit(r.returncode)


def _new_collection(url, kind, a):
    """A collection for a channel / playlist, named after it, in the library."""
    title = yt.list_title(url) or url
    name = a.name or slug(a.label or title)
    if name in library():
        raise SystemExit(f"there's already a collection '{name}' - add to it with --to {name}, "
                         "or pick another --name")
    root = a.root or os.path.join(library_root(), "labels" if a.label else "", safe_name(a.label or title))
    c = Collection(root)
    if os.path.exists(c.p("collection.json")):
        raise SystemExit(f"{root} already holds a collection")
    cfg = c.config
    cfg.update(name=name, originals_dir="originals", source={"playlists": [url]}, genre=a.genre or "")
    if a.label:
        cfg["label"] = a.label
    else:                       # rekordbox playlist named after the channel / playlist
        cfg["playlist"] = title
        if kind == "channel":   # a DJ's channel: their mixes (playlists: per mix, from tracklists)
            cfg["album_artist"] = title
    if a.album_artist is not None:
        cfg["album_artist"] = a.album_artist
    if a.no_upload_year:
        cfg["year_from_upload"] = False
    c.save()
    register(name, c.root)
    print(f"  new collection '{name}' ({kind}: {title}) at {c.root}")
    return name


def _picks(name=PICKS):
    """A collection of single videos (youtube-picks unless named): one rekordbox playlist,
    singles tagged with their own artist as album artist."""
    if name not in library():
        title = "YouTube picks" if name == PICKS else name
        c = Collection(os.path.join(library_root(), safe_name(title)))
        if os.path.exists(c.p("collection.json")):
            raise SystemExit(f"{c.root} already holds a collection")
        c.config.update(name=name, originals_dir="originals", source={"videos": []},
                        playlist=title, singles_folder=title, singles_album_artist="artist")
        c.save()
        register(name, c.root)
        print(f"  new collection '{name}' at {c.root}")
    return open_collection(name)


def cmd_add(a):
    """Add YouTube links: channels / playlists become new collections (or join --to),
    single videos go to youtube-picks (or --to). Short videos become single tracks,
    long ones mixes, as in any collection. --run downloads + splits them straight away."""
    parsed = [yt.kind_of(u) for u in a.urls]
    bad = [u for k, u in parsed if not k]
    if bad:
        raise SystemExit(f"not a YouTube channel, playlist or video link: {bad[0]}")
    lists = [(k, u) for k, u in parsed if k != "video"]
    vids = list(dict.fromkeys(v for k, v in parsed if k == "video"))
    touched = []
    if a.to:
        c = open_collection(a.to)
        src = c.config["source"]
        if "local_dir" in src:
            raise SystemExit(f"{c.name} is a local-folder collection; it can't take YouTube links")
        new_lists = [u for _, u in lists if u not in c.playlists()]
        if new_lists:
            src["playlists"] = c.playlists() + new_lists
            src.pop("playlist", None)
        new_vids = [v for v in vids if v not in c.videos()]
        if new_vids:
            src["videos"] = c.videos() + new_vids
        c.save()
        print(f"  {c.name}: +{len(new_lists)} channels/playlists, +{len(new_vids)} videos"
              + (f" ({len(lists) + len(vids) - len(new_lists) - len(new_vids)} already there)"
                 if len(new_lists) + len(new_vids) < len(lists) + len(vids) else ""))
        touched.append(c.name)
    else:
        if len(lists) > 1 and (a.name or a.label):
            raise SystemExit("--name / --label name one collection: add one channel or playlist at a "
                             "time, or put several in one with --to")
        for kind, url in lists:
            touched.append(_new_collection(url, kind, a))
        if vids:                 # --name with only videos: a picks collection of that name
            c = _picks(a.name if a.name and not lists else PICKS)
            new_vids = [v for v in vids if v not in c.videos()]
            c.config["source"]["videos"] = c.videos() + new_vids
            c.save()
            print(f"  {c.name}: +{len(new_vids)} videos" + (f" ({len(vids) - len(new_vids)} already there)"
                                                            if len(new_vids) < len(vids) else ""))
            touched.append(c.name)
    if a.run:
        for name in dict.fromkeys(touched):
            print(f"===== {name}")
            _pipeline(_run_args(name, a.jobs))
            cmd_status(argparse.Namespace(collection=name))
    else:
        print(f"  next: mixarchive update {' '.join(dict.fromkeys(touched))}")


def _run_args(name, jobs):
    return argparse.Namespace(collection=name, only=None, force=False, kind=None, pick=None,
                              formats=None, jobs=jobs, years=None, dry_run=False)


def cmd_update(a):
    """Every collection (or the ones named), end to end: new uploads on their channels and
    playlists and new picks are resolved, downloaded and split; then rbsync, unless
    rekordbox is open (it says so) or --no-rbsync."""
    names = a.collections or list(library())
    failed = []
    for n in names:
        print(f"===== {n}")
        try:
            _pipeline(_run_args(n, a.jobs))
        except (Exception, SystemExit) as e:      # one bad collection shouldn't stop the rest
            failed.append(n)
            print(f"  !! {n} stopped: {e}")
    if failed:
        print(f"  {len(failed)} collection(s) stopped early: {', '.join(failed)} - re-run update for them")
    if a.no_rbsync:
        return
    if rekordbox_running():
        print("  rekordbox is open: close it, then run `mixarchive rbsync` to add the new tracks")
        return
    print("===== rbsync")
    cmd_rbsync(argparse.Namespace(collections=[], dry_run=False))


def cmd_gui(a):
    from . import gui
    gui.serve(a.port, open_browser=not a.no_browser)


def cmd_move(a):
    """Move a collection to a new root (e.g. another drive). Every path inside a
    collection is relative, so this is copy -> check sizes -> re-register; the old
    copy is removed only with --delete-old (run `verify` on the new one first)."""
    lib = library()
    name = a.collection if a.collection in lib else open_collection(a.collection).name
    src, dst = os.path.abspath(lib.get(name, a.collection)), os.path.abspath(a.dest)
    if os.path.normcase(src) == os.path.normcase(dst):
        raise SystemExit(f"{name} is already at {dst}")
    files = [os.path.relpath(os.path.join(d, f), src) for d, _, fs in os.walk(src) for f in fs]
    todo = [f for f in files if not (os.path.exists(os.path.join(dst, f)) and
            os.path.getsize(os.path.join(dst, f)) == os.path.getsize(os.path.join(src, f)))]
    size = sum(os.path.getsize(os.path.join(src, f)) for f in todo)
    print(f"  {name}: copying {len(todo)} of {len(files)} files ({size / 1e9:.1f} GB) -> {dst}")
    for i, f in enumerate(todo, 1):
        os.makedirs(os.path.dirname(os.path.join(dst, f)), exist_ok=True)
        shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
        if i % 1000 == 0:
            print(f"   {i}/{len(todo)}")
    bad = [f for f in files if not os.path.exists(os.path.join(dst, f)) or
           os.path.getsize(os.path.join(dst, f)) != os.path.getsize(os.path.join(src, f))]
    if bad:
        raise SystemExit(f"  {len(bad)} files differ after copying, e.g. {bad[:3]} - old copy kept")
    register(name, dst)
    print(f"  {len(files)} files match; '{name}' now points to {dst}")
    if a.delete_old and os.path.isdir(src):
        def unlock(fn, path, _):
            os.chmod(path, 0o666)
            fn(path)
        shutil.rmtree(src, onerror=unlock)
        print(f"  removed {src}")


def _pipeline(a):
    # tracklists again after fetch: singles' years come from the downloaded metadata,
    # and unmixed CDs need the original for silence detection
    for step in (cmd_resolve, cmd_tracklists, cmd_fetch, cmd_tracklists, cmd_art, cmd_split):
        print(f"--- {step.__name__[4:]}")
        step(a)
    c = open_collection(a.collection)
    if c.config.get("modrender"):
        for step in (cmd_modmatch, cmd_modrender):
            print(f"--- {step.__name__[4:]}")
            step(a)


def cmd_run(a):
    _pipeline(a)
    cmd_status(a)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mixarchive", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create/update a collection")
    p.add_argument("name")
    p.add_argument("--root", required=True, help="collection folder (originals + split/)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--playlist", action="append",
                   help="YouTube playlist or channel URL (repeat for several)")
    g.add_argument("--local", help="folder of existing full-mix files to resolve on YouTube")
    p.add_argument("--exclude", help="regex of local filenames to skip")
    p.add_argument("--genre")
    p.add_argument("--album-artist", dest="album_artist")
    p.add_argument("--label", help="record label (label channels: rekordbox Label, singles folder)")
    p.add_argument("--no-upload-year", action="store_true",
                   help="archive labels: don't use upload years for singles without a stated year")
    p.add_argument("--formats", help="comma list: mp3,flac")
    p.add_argument("--no-mains", action="store_true")
    p.add_argument("--modrender", action="store_true", help="opt-in Modland renders")
    p.add_argument("--frames-art", action="store_true", help="per-track art from video frames")
    p.set_defaults(fn=cmd_init)

    sub.add_parser("list", help="list collections").set_defaults(fn=cmd_list)

    p = sub.add_parser("rbxml", help="rekordbox XML: one playlist per collection, for import")
    p.add_argument("collections", nargs="*", help="collection names (default: all)")
    p.add_argument("--out", help="XML path (default: rekordbox.xml beside the collections)")
    p.set_defaults(fn=cmd_rbxml)

    p = sub.add_parser("rbsync", help="add tracks + playlists straight to rekordbox (closed)")
    p.add_argument("collections", nargs="*", help="collection names (default: all)")
    p.add_argument("--dry-run", action="store_true", help="only report what would change")
    p.set_defaults(fn=cmd_rbsync)

    p = sub.add_parser("move", help="move a collection to a new root (copy, check, re-register)")
    p.add_argument("collection")
    p.add_argument("dest", help="new collection folder")
    p.add_argument("--delete-old", action="store_true",
                   help="remove the old copy once every file matches")
    p.set_defaults(fn=cmd_move)

    def coll(name, fn, help_, extra=()):
        p = sub.add_parser(name, help=help_)
        p.add_argument("collection")
        p.add_argument("--only", help="comma list of substrings (key/video id/title)")
        p.add_argument("--force", action="store_true", help="redo finished work")
        p.add_argument("--kind", choices=("track", "mix"),
                       help="only singles (tracks.csv) or only mixes")
        for flag, kw in extra:
            p.add_argument(flag, **kw)
        p.set_defaults(fn=fn)

    pick = ("--pick", {"action": "append", "help": "KEY_SUBSTRING=VIDEO_ID override"})
    fmts = ("--formats", {"help": "comma list: mp3,flac"})
    coll("resolve", cmd_resolve, "find YouTube sources", [pick])
    coll("tracklists", cmd_tracklists, "write editable tracklists",
         [("--years", {"help": "CSV video_id,track,year_shown merged as {year}"})])
    coll("fetch", cmd_fetch, "download best original audio")
    coll("art", cmd_art, "build square artwork")
    jobs = ("--jobs", {"type": int, "default": max(1, (os.cpu_count() or 2) // 2),
                       "help": "mixes encoded in parallel"})
    coll("split", cmd_split, "cut + tag tracks",
         [fmts, jobs, ("--dry-run", {"action": "store_true"})])
    coll("modmatch", cmd_modmatch, "match tracks to Modland (opt-in)")
    coll("modrender", cmd_modrender, "render matched modules (opt-in)", [fmts])
    coll("status", cmd_status, "show progress per mix")
    coll("verify", cmd_verify, "check every split + sample loudness",
         [jobs, ("--sample", {"type": int, "default": 12, "help": "tracks to re-measure"})])
    coll("run", cmd_run, "resolve > tracklists > fetch > art > split (> modmatch > modrender)",
         [pick, fmts, jobs, ("--years", {"help": "CSV of on-screen years"}),
          ("--dry-run", {"action": "store_true"})])

    p = sub.add_parser("add", help="add YouTube channels / playlists / videos to the library")
    p.add_argument("urls", nargs="+", help="channel, playlist or video links (or video ids)")
    p.add_argument("--to", help="an existing collection to add them to")
    p.add_argument("--name", help="name of the new collection (default: from the channel / playlist)")
    p.add_argument("--label", help="record label: a label collection in labels\\ (rekordbox Label)")
    p.add_argument("--genre")
    p.add_argument("--album-artist", dest="album_artist")
    p.add_argument("--root", help="folder of the new collection (default: in the library)")
    p.add_argument("--no-upload-year", action="store_true",
                   help="archive labels: don't use upload years for singles without a stated year")
    p.add_argument("--run", action="store_true", help="download + split them right away")
    p.add_argument(jobs[0], **jobs[1])
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("update", help="new uploads > download > split for every collection, then rbsync")
    p.add_argument("collections", nargs="*", help="collection names (default: all)")
    p.add_argument("--no-rbsync", action="store_true", help="leave rekordbox alone")
    p.add_argument(jobs[0], **jobs[1])
    p.set_defaults(fn=cmd_update)

    p = sub.add_parser("files", help="check / relocate / move the files behind rekordbox's collection",
                       add_help=False)
    p.add_argument("args", nargs=argparse.REMAINDER, help="status | audit | verify | relocate | move | undo")
    p.set_defaults(fn=cmd_files)

    p = sub.add_parser("merge", help="merge one rekordbox playlist into another (rekordbox closed)")
    p.add_argument("source", help='the playlist to merge in (deleted afterwards), e.g. "Some Label (old)"')
    p.add_argument("--into", required=True, help='the playlist that stays, e.g. "mixarchive / Some Label"')
    p.add_argument("--apply", action="store_true", help="write it (default: only report)")
    p.add_argument("--keep-source", action="store_true", help="don't delete the merged playlist")
    p.set_defaults(fn=cmd_merge)

    p = sub.add_parser("gui", help="open the GUI in the browser (local only)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="just print its address")
    p.set_defaults(fn=cmd_gui)

    for stream in (sys.stdout, sys.stderr):     # titles with emoji / math letters on cp1252
        stream.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["files"]:      # the rest (incl. --help) belongs to rbfiles.py
        return cmd_files(argparse.Namespace(args=argv[1:]))
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main(sys.argv[1:])
