"""A collection = an output root holding originals + splits, with its state in
<root>/_mixarchive/ (collection.json, sources.json, info/, tracklists/, art/, ...)."""
import os
import re

from .util import STATE, load_json, safe_name, save_json, settings

DEFAULT_CONFIG = {
    # {"playlists": [url, ...], "videos": [id, ...]} (playlists or channel /videos pages, and
    # single videos added with `add`; old: {"playlist": url}) or {"local_dir": dir, "exclude": regex}
    "source": {},
    "genre": "",
    "album_artist": "",        # fixed album artist (e.g. "off1k"); "" = per mix, from tracklist
    "label": "",               # record label of a label channel (rekordbox "Label" on singles)
    "originals_dir": "",       # originals subfolder ("" = the root, as in older collections)
    "singles_folder": "",      # singles' folder + album ("" = label / album artist / name)
    "singles_album_artist": "label",   # or "artist": each single's own artist (mixed picks)
    # sorting a channel's videos: shorter than mix_min_sec = a single track (tracks.csv),
    # longer = a mix (tracklists); outside min/max or a matching title = skipped
    "mix_min_sec": 900,
    "min_sec": 60,
    "max_sec": 43200,
    "skip_titles": r"\b(trailer|teaser|announcement|infomercial|preview|snippet|interview|"
                   r"behind the scenes|documentary|short film|unboxing|introducing)\b|#shorts",
    "skip_known": True,        # skip videos another collection already archives
    "year_from_upload": True,  # singles without a stated year get their upload year
    "formats": ["mp3"],        # any of: mp3, flac
    "mains": True,             # also write each whole mix to <format>/_full mixes
    # rekordbox playlist (rbsync / rbxml): list each mix by its full mix, not its splits;
    # and {mix: playlist} for mixes that get a playlist of their own (splits + full mix)
    "full_mixes": False,
    "mix_playlists": {},
    "art": {"mode": "thumbnail"},  # or {"mode": "frames", "offset": 28, "crops": {...}}
    "modrender": False,        # opt-in: Modland + libopenmpt renders (tracker music)
    # linear gain to a loudness target, capped by a true-peak ceiling (no limiter);
    # scope "track" = per split, "mix" = one gain per mix; null = don't normalize
    "normalize": {"target_lufs": -14.0, "ceiling_dbtp": -1.0, "scope": "track"},
    "old_dir": "_old 128k",    # where superseded local files are moved
}


class Collection:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.state = os.path.join(self.root, STATE)
        self.config = {**DEFAULT_CONFIG, **load_json(self.p("collection.json"), {})}
        self.sources = load_json(self.p("sources.json"), [])

    # paths
    def p(self, *parts):
        return os.path.join(self.state, *parts)

    @property
    def name(self):
        return self.config.get("name") or os.path.basename(self.root)

    @property
    def out_root(self):
        return os.path.join(self.root, "split")

    def info_path(self, vid):
        return self.p("info", f"{vid}.info.json")

    def tracklist_path(self, vid):
        return self.p("tracklists", f"{vid}.txt")

    def info(self, vid):
        return load_json(self.info_path(vid), {})

    def original(self, src):
        return os.path.join(self.root, src["original"]) if src.get("original") else None

    @property
    def originals(self):
        return os.path.join(self.root, self.config.get("originals_dir") or "")

    @property
    def singles_folder(self):
        return safe_name(self.config.get("singles_folder") or self.config.get("label")
                         or self.config.get("album_artist") or self.name)

    def single_originals(self):
        """{video id: path} of downloaded singles ('<title> [<id>].opus')."""
        out = {}
        if os.path.isdir(self.originals):
            for f in os.listdir(self.originals):
                m = re.search(r"\[([\w-]{11})\]\.(opus|m4a|webm|ogg|mp3)$", f)
                if m:
                    out[m.group(1)] = os.path.join(self.originals, f)
        return out

    def playlists(self):
        src = self.config["source"]
        return list(src.get("playlists") or []) + ([src["playlist"]] if src.get("playlist") else [])

    def videos(self):
        return list(self.config["source"].get("videos") or [])

    def save(self):
        save_json(self.p("collection.json"), self.config)
        save_json(self.p("sources.json"), self.sources)


def library():
    return load_json(settings()["library"], {})


def library_root():
    """The folder holding every collection (new ones go here)."""
    roots = list(library().values())
    try:
        return os.path.commonpath(roots) if len(roots) > 1 else os.path.dirname(roots[0])
    except (ValueError, IndexError):    # none yet, or on several drives: the first one's
        return os.path.dirname(roots[0]) if roots else os.getcwd()


def register(name, root):
    lib = library()
    lib[name] = os.path.abspath(root)
    save_json(settings()["library"], lib)


def known_video_ids(exclude_root=None):
    """{video id: collection} for every upload other collections archive (incl. alternates)."""
    out = {}
    for name, root in library().items():
        if exclude_root and os.path.abspath(root) == os.path.abspath(exclude_root):
            continue
        for s in load_json(os.path.join(root, STATE, "sources.json"), []):
            if s.get("video_id") and s.get("kind") != "skip":
                out[s["video_id"]] = name
            for alt in s.get("alternates") or []:
                out.setdefault(alt["id"], name)
    return out


def open_collection(name_or_root):
    lib = library()
    root = lib.get(name_or_root, name_or_root)
    if not os.path.isdir(os.path.join(root, "_mixarchive")):
        raise SystemExit(f"unknown collection: {name_or_root} (known: {', '.join(lib) or 'none'})")
    return Collection(root)
