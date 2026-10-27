# mixarchive

Archive YouTube channels, playlists and long mixes (DJ sets, compilations, mixed CDs) as
tagged, level-matched, cover-art-equipped tracks, and put them straight into rekordbox as
playlists, ready to analyze and export to a USB stick for Pioneer DJ players.

New here? Start with [`docs/guide.html`](docs/guide.html): how the pieces fit together
from YouTube to the decks, setup, the day-to-day steps with screenshots, where the backups
are and how to undo things. This README is the reference.

> mixarchive is an independent project. It is not made, endorsed or supported by
> AlphaTheta or Pioneer DJ; rekordbox, CDJ and XDJ are trademarks of their owners. It
> writes to rekordbox's database only while rekordbox is closed, after saving a copy, but
> keep your own backups. Download and play only what you have the right to: YouTube's
> terms limit downloading, and playing music in public needs the licences that apply where
> you play.

Built and tested on Windows 10 with rekordbox 7.

## Day to day

```
mixarchive add https://www.youtube.com/@somelabel --label "Some Label" --genre House
mixarchive add https://youtu.be/VIDEOID https://youtu.be/OTHERID      # -> YouTube picks
mixarchive update            # every collection: new uploads -> download -> split -> rbsync
```

then in rekordbox: *mixarchive › Not analyzed yet*, Ctrl+A, right-click › Analyze Track;
then export the playlists you want to the USB stick. `update` leaves rekordbox alone while
it's open (it says so) - close it and run `mixarchive rbsync`.

`mixarchive` here means `mixarchive.bat` in the install folder (or
`venv\Scripts\python -I -m mixarchive`).

## GUI

`mixarchive gui` opens the same workflow in the browser (served on 127.0.0.1 only; the
address carries a random token, so other pages can't drive it - keep its window open while
you use it). Tabs: **Collections** (status, Update per collection or all, open folder),
**Add sources** (paste links; shows what each becomes), **rekordbox** (open/closed, tracks
left to analyze, mixarchive playlist sizes, Preview sync / Sync, Open rekordbox, Merge
playlists) and **Library files** (the `files` tool below). It runs the CLI's own commands
one at a time, with their output in the panel at the bottom; anything that writes to
rekordbox is disabled while rekordbox is open, and moves need a second click to confirm.

## Setup

1. Python 3.10+ for mixarchive itself:
   `py -3 -m venv venv` then `venv\Scripts\python -m pip install -e .`
2. A second environment for everything that touches rekordbox's database (`rbsync`,
   `files`, `merge`), because pyrekordbox's SQLCipher wheels need a newer Python:
   `py -3.14 -m venv rbvenv` then
   `rbvenv\Scripts\python -m pip install pyrekordbox==0.5.0 mutagen pillow`.
   `mixarchive\rbdb.py`, `rbfiles.py` and `rbmerge.py` run there and import nothing from the
   rest of the package.
3. ffmpeg, on PATH or named in `settings.json`.
4. Copy `settings.example.json` to `settings.json` and adjust it. Every key is optional:

   | key | default | |
   |---|---|---|
   | `ffmpeg`, `ffprobe` | `ffmpeg`, `ffprobe` (on PATH) | full paths if they aren't on PATH |
   | `ytdlp` | `venv\Scripts\yt-dlp.exe` | |
   | `rbpython` | `rbvenv\Scripts\python.exe` | the Python with pyrekordbox |
   | `usb_mirror` | none | a backup copy of your USB stick, if you keep one: `files` flags tracks rekordbox plays from inside it |
   | `openmpt123` | `tools\openmpt\openmpt123.exe` | only for MOD renders |
   | `library` | `library.json` | the list of collections (written by `add` / `init`) |

5. The first collection decides where the library lives: give it `--root`, e.g.
   `mixarchive add https://www.youtube.com/@somelabel --label "Some Label" --root "D:\DJ\Library\mixarchive\Some Label"`.
   Later collections go next to it (label collections in a `labels\` folder beside it).

yt-dlp needs deno for YouTube's JavaScript (installed with the package). Some videos
answer 403 unless the request carries a PO token; see yt-dlp's
[PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide). If a bgutil
provider is installed in `tools\bgutil-ytdlp-pot-provider` (see that project's own
instructions), mixarchive starts it on 127.0.0.1 when a download needs one. Videos YouTube
age-restricts need a signed-in account; mixarchive never signs in, so they're left out.

## Where things live

All collections live under one library root, e.g.

```
D:\DJ\Library\mixarchive\         <- one folder per collection; labels\<Label>\ for labels
D:\DJ\Library\_rekordbox backups\ <- copies of rekordbox's database before each write
```

`library.json` maps collection names to their folders; `move NAME NEWROOT` relocates one
(copy, size check, re-register; `--delete-old`). Keep the library off the USB stick and out
of any backup copy of the stick: rekordbox's USB export re-files tracks as
`Contents\<Artist>\<Album>\`, and the next copy of the stick would bring them back as
second copies.

## Conventions

Every collection is one folder:

```
<collection>\
  originals\<title> [<id>].opus   best YouTube audio, never re-encoded
  _mixarchive\                    state (small, safe to sync)
    collection.json               settings (below)
    sources.json                  which YouTube upload each mix maps to (+ alternates)
    info\<id>.info.json           yt-dlp metadata incl. top comments
    tracklists\<id>.txt           EDITABLE tracklist per mix - the source of truth for splits
    tracks.csv                    EDITABLE singles list (label channels)
    art\                          square 600x600 covers (<id>_main.jpg, <id>_<NN>.jpg)
    modules.csv, modules\, renders\   (opt-in MOD renders)
  split\
    MP3 320\<Mix>\NN - Artist - Title.mp3      tracks
    MP3 320\_full mixes\<Mix>.mp3              whole mixes (tracklist in comments)
    MP3 320\tracklist.csv                      manifest incl. loudness + gain applied
    MOD renders\<Mix>\NN - Artist - Title (MOD render).mp3
```

- **Audio source**: YouTube Opus 251 (full band to ~20 kHz); AAC 140 (16 kHz low-pass) only
  when no Opus exists. YouTube serves nothing lossless.
- **Output**: MP3 320 CBR (plays on every CDJ/XDJ). Add `flac` to `formats` for 16-bit FLAC.
- **Normalization** (default on): each track gets one linear gain to **-14 LUFS**, capped so the
  true peak stays <= **-1 dBTP** - no limiter, dynamics untouched. Full mixes get one gain
  for the whole mix. YouTube decodes often peak above 0 dBFS, so this also stops clipping.
  Very dynamic tracks can land a few dB under -14 (peak-capped) rather than be squashed;
  rekordbox Auto Gain evens those out. `tracklist.csv` records measured LUFS / peak / gain.
- **Tags**: artist, title, album (= mix), album artist (DJ / channel), track n/total, genre,
  year, label (TPUB = rekordbox "Label"), comment with a YouTube link at the track's start time,
  square cover art.

## Label channels and playlists (singles + mixes)

`init NAME --root DIR --playlist URL [--playlist URL2] --label "Label" --genre G` takes
playlists and channel pages (`https://www.youtube.com/@channel/videos`). `resolve` sorts
every video:

- **single track** (shorter than `mix_min_sec`, 15 min) -> a row in `_mixarchive\tracks.csv`
- **mix** (longer) -> the usual tracklist file and split
- **skip**: unavailable, under 60 s, over 12 h (livestreams), a title matching `skip_titles`
  (trailer, teaser, preview, announcement, interview, ...), or a video another collection
  already archives (`skip_known`)

`tracks.csv` is the singles' source of truth (UTF-8 CSV, opens in Excel). Edit freely,
then re-run `split`: renamed rows are re-exported and the old file removed.

| column | |
|---|---|
| status | `ok`, `check` (no artist found), `dup` (same artist + title as a better upload: official audio > plain > video, then longest), `skip`; set `manual` to protect a hand-edited row from `resolve --force` |
| artist, title, album | parsed from the title: `Artist - Title`, `Artist 'Title'`, catalog numbers (`ABC039`, `- XYZ030`, `[LB007]`), `(Label, 1993)`, `[Official Audio]`, `\| Series` |
| year, year_src | `title` / `youtube` (music metadata) / `description` (the (P) line) / `upload` (fallback; off with `--no-upload-year` for archive labels whose uploads are decades after release) |
| part, start, end | a short video with real chapters becomes one row per track |

Singles export whole (one gain to -14 LUFS / -1 dBTP) to `split\MP3 320\<Label>\Artist - Title.mp3`
with album = label (or the series), rekordbox Label = label, and the YouTube title, link,
catalog number and upload date in the comment. Covers: when the thumbnail is a sleeve on a
flat backdrop or in black bars, the sleeve is cropped out; otherwise the frame is letterboxed.

`--kind track` / `--kind mix` limits any step to singles or mixes (e.g. fetch only singles).

## Adding sources

`add LINK...` takes channel pages (`youtube.com/@name`, `/channel/UC...`; any tab becomes
`/videos`), playlists (`playlist?list=`) and single videos (`youtu.be/ID`, `watch?v=`,
`shorts/`, or a bare id; a watch link inside a playlist counts as the video):

- a channel / playlist becomes a new collection named after it, in the library root.
  `--label L` makes it a label collection in `labels\L` (rekordbox Label = L); without one,
  a channel is a DJ's (album artist = the channel) and the rekordbox playlist is named
  after the channel / playlist. `--name`, `--genre`, `--album-artist`, `--root`,
  `--no-upload-year` as for `init`
- single videos go to the **youtube-picks** collection (`YouTube picks` in the library root,
  rekordbox playlist "YouTube picks"; created on first use). Short ones become singles
  (album artist = their own artist), long ones mixes with tracklists, as anywhere else.
  `--name X` with only videos makes / extends a picks collection called X instead
- `--to NAME` puts everything into an existing collection instead
- `--run` downloads + splits straight away; otherwise `update` does it later

Videos already archived in another collection are skipped as usual (`skip_known`).
Single videos live in `collection.json` as `source.videos`.

## Tracklist files

`_mixarchive\tracklists\<video id>.txt` - edit freely, then re-run `split`:

```
@album: Some DJ - Studio Mix (1996)
@album_artist: Some DJ
@year: 1996
00:00:00 First Artist - First Track {1995} {label: First Label}
00:07:51 Second Artist - Second Track (Some Remix) {1996} {label: Second Label}
--:--:-- Some Artist - Untimed Track     <- blocks splitting until given a time
```

Mixes with any `--:--:--` line get only a full-mix file until every line has a time.
`tracklists --force` refreshes the track lines but keeps your `@album/@album_artist/@year/@genre`.

Where tracklists come from, best first: YouTube chapters -> timestamps in the description ->
per-track **lengths** whose total matches the mix (CD listings, also borrowed from sibling
uploads in the same collection, +-15 s and >=50% track overlap) -> timestamped top comments ->
silent gaps (unmixed CDs: clustered silences within 30 s merge, longest wins; used only when
there are exactly N-1 gaps) -> untimed lists.

Downloads retry YouTube's intermittent 403s; a mix that still fails is skipped and listed so
the rest of the batch finishes - just re-run `fetch`. As a last resort a download falls back
to the HLS copy of AAC 128k (format 234: same audio as 140, 16 kHz low-pass).

## Commands

| command | does |
|---|---|
| `init NAME --root DIR (--playlist URL... \| --local DIR [--exclude RE]) [--genre G] [--album-artist A] [--label L] [--no-upload-year] [--modrender] [--frames-art]` | create a collection (`--playlist` repeats; channel `/videos` pages work) |
| `resolve NAME [--pick "substring=VIDEOID"]` | find each mix's upload (search + duration match for local files), pick the one with the best tracklist |
| `tracklists NAME [--years CSV] [--force]` | write the editable tracklists |
| `fetch NAME` | download originals; move old local files to `_old 128k` |
| `art NAME` | square covers (thumbnail letterboxed; `frames` mode crops each track's on-screen info box) |
| `split NAME [--only X] [--formats mp3,flac] [--jobs N]` | cut + normalize + tag |
| `modmatch NAME` / `modrender NAME` | opt-in tracker music: Modland match, libopenmpt render |
| `status NAME` | per-mix progress |
| `verify NAME [--sample N]` | every split decodes, has the right length, tags + cover; re-measures a sample's loudness |
| `run NAME` | everything above in order |
| `add LINK... [--to NAME] [--label L] [--genre G] [--name N] [--root DIR] [--run]` | add channels / playlists (new collections) or videos (youtube-picks) |
| `update [NAME...] [--no-rbsync]` | `run` for every collection (or those named), then `rbsync` if rekordbox is closed |
| `files status \| audit \| verify \| relocate \| move \| flac \| undo \| flaccopy \| silence ...` | the files behind rekordbox's whole collection (below) |
| `gui [--port N]` | the browser GUI |
| `list` | known collections |
| `move NAME NEWROOT [--delete-old]` | relocate a collection (copy, check sizes, re-register) |
| `rbsync [NAME...] [--dry-run]` | put tracks + playlists straight into rekordbox's database (rekordbox closed); default: all collections |
| `rbxml [NAME...] [--out PATH]` | the same playlists as a rekordbox XML file, for a manual import instead |
| `merge SOURCE --into TARGET [--apply] [--keep-source]` | merge one rekordbox playlist into another (below; rekordbox closed) |

All steps skip finished work; `--force` redoes it, `--only a,b` limits to matching mixes.

## Into rekordbox and onto the USB

Close rekordbox, then `mixarchive rbsync`. It writes into rekordbox's own database
(`%APPDATA%\Pioneer\rekordbox\master.db`, via pyrekordbox in `rbvenv`):

- a `mixarchive` playlist folder with one playlist per collection. Each holds the
  collection's splits (mix tracks + singles); a mix without a timed tracklist contributes
  its full-mix file instead, so a USB export never carries a mix alongside its own tracks.
  MOD renders get their own playlist. Two collection.json settings change that:
  `"full_mixes": true` lists every mix by its full mix instead (for when its splits live in
  playlists of your own), and `"mix_playlists": {"<mix>": "<playlist>"}` moves a mix to a
  playlist of its own, its splits then its full mix. Playlists are rewritten to match on every run, except
  that tracks you dragged into one yourself stay (after the collection's own, in your order;
  rbsync remembers what it put there in `cache\rbsync_written.json` and only ever removes
  those). Other playlists, including your own inside that folder, are never touched
- new files are added to the collection with their tags, label and cover
- files a re-split changed (size differs) get tags, cover and size refreshed, like Reload
  Tag; cues, beat grids and analysis stay
- files a tracklist / tracks.csv edit renamed are followed: the existing rekordbox entry
  (matched by the YouTube link + start time in its comment) is pointed at the new name,
  so its cues aren't lost to a fresh copy
- `mixarchive / Not analyzed yet` lists every synced track rekordbox hasn't analyzed. Open
  it, Ctrl+A, right-click > Analyze Track. It disappears once it's empty

`master.db` and `masterPlaylists6.xml` are copied to `_rekordbox backups\<date> rbsync\`
(next to the library root) before each write; the last 10 are kept. To undo a sync, close
rekordbox and copy them back. A run with nothing to do writes nothing. `--dry-run` shows
what would change. Entries under the library whose file mixarchive removed (a single
that became a dup or skip) are reported, not deleted; they show as missing in rekordbox.

`rbxml` remains for a manual route: it writes the same playlists as `rekordbox.xml` in the
library root for rekordbox xml > Import Playlist. Use one route or the other; importing the
XML after `rbsync` adds a second set of playlists.

Both print each playlist's size, for planning what fits on the USB stick.

rekordbox has no way to start an analysis from outside, so analyzing stays a click in
rekordbox. Closing rekordbox stops an analysis midway; the next `rbsync` shrinks *Not
analyzed yet* to what's left. When a re-split changes the audio behind a file rekordbox
already analyzed (same name, or a followed rename, but a different length), `rbsync` adds
it to *mixarchive › Re-analyze (audio changed)*: analyze those again, then delete that
playlist. Old split files that no longer exist show as missing in rekordbox - remove them
with *File › Display All Missing Files*.

mixarchive never writes to the USB stick; rekordbox's export does.

## Merging playlists (`merge`)

`mixarchive merge "Some Label (old)" --into "mixarchive / Some Label"` (or the rekordbox tab
of the GUI) merges one rekordbox playlist into another (rbmerge.py, in rbvenv like rbsync).
Name a playlist by its name, or by its path with ` / ` when the name is used twice.

- the target keeps its tracks in its order; the source's follow in theirs; a track listed
  twice is listed once
- copies of the same recording (same words in the name, similar length, then confirmed
  by ear: the first 4 minutes correlated) keep only the best copy: WAV / AIFF, then FLAC,
  then the highest bitrate. Tracks rbsync put in a mixarchive target always stay; a copy
  of yours stays next to them only if it's lossless or at least 320 kbps (a bought file),
  so lower-bitrate rips of what mixarchive has go
- the copies that go leave the playlist, not the collection (play histories keep them);
  their cues and hot cues are copied to the copy that stays, shifted by the measured
  offset (not where it already has one there, or the hot cue pad is taken)
- the source playlist is deleted (`--keep-source` keeps it). One of mixarchive's own
  playlists can't be the source: Sync would bring it back. Tracks merged into a mixarchive
  playlist stay there on later Syncs (after mixarchive's own)

Without `--apply` it only reports, with every copy it found and where its cues go. With
it, master.db and masterPlaylists6.xml are copied to `_rekordbox backups\<date> merge\` first
with `plan.csv` (every track's fate); to undo, close rekordbox and copy them back. The USB
stick isn't touched: export the merged playlist again and delete the old one from the stick.

## rekordbox's files (`files`)

`mixarchive files COMMAND` works on rekordbox's whole collection, not only mixarchive's
(rbfiles.py, in rbvenv like rbsync). Filters for every command: `--ext wav,aif,aiff`,
`--under DIR`, `--playlist NAME` (a folder includes what's below it), `--missing`.

| command | does |
|---|---|
| `status` | rekordbox open?, tracks, not analyzed, missing; mixarchive playlists; tracks / GB per location and format |
| `audit [--list] [--csv PATH]` | the same per location for the filtered tracks; `--list` prints each file + its playlists |
| `verify [--decode]` | each file exists with the size rekordbox recorded; `--decode` decodes them all (ffmpeg) |
| `relocate --search DIR...` | missing files: same name (+ size) found under DIR -> rekordbox points there |
| `move --to DIR [--layout keep\|flat\|playlist\|artist] [--copy]` | move (or copy) the files and repoint rekordbox; `playlist` files each under the smallest playlist holding it, `artist` under artist / album, `keep` keeps the folders below `--under` |
| `flac --to DIR [--layout ...]` | WAV / AIFF -> FLAC under DIR, rekordbox repointed (type FLAC, bitrate 0, new size, as rekordbox records FLACs). Each FLAC is decoded and compared sample for sample with its original first; tags + cover art come along, with rekordbox's title / artist / album / genre on top. Originals stay; 32-bit float files (FLAC can't hold them) are copied as they are. Older Pioneer players don't play FLAC: if you use them, keep WAV for rekordbox and use `flaccopy` for the backup instead |
| `undo JOURNAL.csv [--keep]` | reverse a relocate / move / flac; `--keep` points rekordbox back at the originals but leaves the copies / FLACs |
| `flaccopy --from DIR --to DIR` | keep `--to` a FLAC copy of the WAV / AIFF under `--from` (checked the same way; float files copied as they are; copies whose source is gone are deleted). Files only, rekordbox untouched - `backup_library.bat` runs it for `<library>\WAV` |
| `silence [--min-gap S] [--list] [--csv PATH] [--jobs N] [--cues --apply]` | silent gaps inside tracks, such as dropouts in a rip (below) |

`files silence` decodes each track (mono 8 kHz, ffmpeg, 8 at a time; about 4,000x realtime
on a desktop) and measures its level every 0.1 s over 0.4 s. A gap is a stretch of
`--min-gap` seconds (default 2) at least 30 dB under the track's typical level and under
-35 dBFS, between where the music first gets within 10 dB of typical and where it last is,
so quiet intros, fade-ins and endings don't count. Short blips inside a gap don't end it.
A gap is **dropout-like** when the level falls 20 dB or more into it, or half of it is digital
silence; otherwise it's a **quiet passage** (breakdowns, dub drops). Files over 15 minutes
(mixes, tapes) and acapellas are reported apart. Results are cached in
`cache\silence_scan.json`, so a re-run scans only new or changed files. With `--cues --apply`
(rekordbox closed, master.db backed up first) it puts a memory cue named "gap N s" at each
dropout-like gap at least 20 s from either end, so you see it on the deck; a gap that
already has a memory cue within 1 s is skipped, so a re-run adds nothing twice.

relocate / move / flac / undo only report until `--apply`, and write only with rekordbox
closed. Cues, beat grids and analysis stay (pyrekordbox's `update_content_path` also rewrites
the path inside the analysis files, and rbfiles updates the size + md5 rekordbox keeps of
each analysis file in `contentFile`). Before writing, master.db, masterPlaylists6.xml and the
touched tracks' analysis files go to `_rekordbox backups\<date> files-<command>\` with a
`journal.csv` of every change - the input for `undo`. If anything fails midway the files are
put back, the analysis files restored and the database left as it was. Moves on one drive
are instant renames; to another drive the files are copied, checked, and the originals
deleted only after the database is committed.

`status` flags tracks that play from inside a backup copy of the USB stick (`usb_mirror`
in settings): that folder is a copy of the stick, not a library.

## Backup

`backup_library.bat LIBRARY BACKUP`, e.g. `backup_library.bat "D:\DJ\Library" "B:\DJ Backup"`,
mirrors the library to `BACKUP\Library` (robocopy /MIR, so files you deleted from the
library go from the backup too) and, while rekordbox is closed, rekordbox's own database
and analysis files (`%APPDATA%\Pioneer\rekordbox`) to `BACKUP\rekordbox`. Log:
`cache\backup_library.log`.

If the library has a `WAV` folder (WAV / AIFF for rekordbox), the backup leaves it out and
carries `WAV - FLAC backup` instead: the same audio as FLAC at about half the size,
refreshed by `files flaccopy` at the start of every backup. To restore a WAV from it:
`ffmpeg -i "x.flac" -c:a pcm_s16le "x.wav"` - `pcm_s24le` for 24-bit files (ffprobe shows
`bits_per_raw_sample`); left to its default ffmpeg would write 24-bit audio as 16-bit.

## MOD renders (tracker collections, opt-in)

`modmatch` fuzzy-matches artist/title against the Modland index (cached monthly), with scene
aliases in `aliases.json` (e.g. `{"xsm": ["xtra spicy mike"]}`). `modules.csv` statuses:
`auto` (title + artist match), `check` (needs a look), `none`, and the hand-set
`manual` / `skip` (never overwritten). `modrender` renders with libopenmpt (A1200 filter
emulation, 50% stereo, 48 kHz; needs `openmpt123`) and **verifies each render's length
against the mix's track** (+-12% / 20 s), trying alternates and marking `mismatch` instead
of exporting a same-named different song.

## Licence

mixarchive's own code is under the MIT licence (see [LICENSE](LICENSE)). It installs and
uses other projects under their own licences, notably yt-dlp (Unlicense), pyrekordbox (MIT),
mutagen (GPL-2.0-or-later), Pillow (MIT-CMU), SQLAlchemy (MIT) and sqlcipher3 (zlib); none of
them is bundled here. Thanks to their authors, and to pyrekordbox in particular for making
rekordbox's database reachable.
