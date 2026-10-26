"""yt-dlp wrappers. Uses the Windows cert store (no-certifi) and UTF-8 output."""
import glob
import os
import re
import subprocess
import time

from .util import settings

# Opus 251 is full-band (~20 kHz); AAC 140 is low-passed at ~16 kHz.
AUDIO_FORMAT = "251/140/bestaudio"
# Some uploads (all of DFA's) answer 403 on every direct stream unless the request
# carries a GVS PO token. The bgutil provider (tools/bgutil-ytdlp-pot-provider, run as a
# localhost server) mints them for the mweb client, which then gets Opus 251. The HLS
# copy of AAC 140 is the last resort: same audio as 140, 16 kHz low-pass.
POT_CLIENT = "mweb"
HLS_FORMAT = "234/233"
POT_HOME = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tools", "bgutil-ytdlp-pot-provider", "server")
POT_URL = "http://127.0.0.1:4416/ping"
_pot_proc = None


def pot_server():
    """Make sure the PO token provider is answering on localhost:4416 (start it if
    installed and not running; it stops when mixarchive exits). Returns True if up."""
    global _pot_proc
    import atexit
    import urllib.request

    def up():
        try:
            with urllib.request.urlopen(POT_URL, timeout=2):
                return True
        except OSError:
            return False

    if up():
        return True
    modules = os.path.join(POT_HOME, "node_modules")
    if _pot_proc or not os.path.isdir(modules):
        return False
    deno = os.path.join(os.path.dirname(settings()["ytdlp"]), "deno.exe")
    # --allow-sys=ca + DENO_TLS_CA_STORE: the provider's HTTP client (axios) uses the Windows
    # certificate store, so a TLS-inspecting proxy whose root CA is only there still works
    _pot_proc = subprocess.Popen(
        [deno, "run", "--allow-env", "--allow-net", "--allow-ffi=.", "--allow-read=.",
         "--allow-sys=ca", os.path.join("..", "src", "main.ts")],
        cwd=modules, env=dict(os.environ, DENO_TLS_CA_STORE="system"),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    atexit.register(_pot_proc.terminate)
    for _ in range(30):
        if up():
            return True
        time.sleep(1)
    return False


def ffmpeg_location(s):
    """yt-dlp's --ffmpeg-location for a full ffmpeg path; none when ffmpeg is just on PATH."""
    d = os.path.dirname(s["ffmpeg"])
    return ["--ffmpeg-location", d] if d else []


def _run(args, check=False, log=None):
    """log: a file path to stream yt-dlp's output to (long batch runs)."""
    s = settings()
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env["PATH"] = os.path.dirname(s["ytdlp"]) + os.pathsep + env.get("PATH", "")  # deno
    cmd = [s["ytdlp"], "--compat-options", "no-certifi"] + args
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            return subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env,
                                  stdin=subprocess.DEVNULL)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    if check and r.returncode:
        raise RuntimeError(f"yt-dlp failed ({r.returncode}): {r.stderr.strip()[-400:]}")
    return r


def _rows(out, keys):
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != len(keys):
            continue
        row = dict(zip(keys, parts))
        try:
            row["duration"] = float(row["duration"])
        except (KeyError, ValueError):
            row["duration"] = None
        rows.append(row)
    return rows


def search(query, n=8):
    r = _run(["--flat-playlist", "--print",
              "%(id)s\t%(duration)s\t%(channel)s\t%(title)s", f"ytsearch{n}:{query}"])
    return _rows(r.stdout, ["id", "duration", "channel", "title"])


def playlist(url):
    r = _run(["--flat-playlist", "--print",
              "%(playlist_index)s\t%(id)s\t%(duration)s\t%(title)s", url], check=True)
    return _rows(r.stdout, ["index", "id", "duration", "title"])


def videos(ids):
    """[{index, id, duration, channel, title}] for single videos, in order. Unavailable
    ones come back with duration None (resolve skips them)."""
    r = _run(["--ignore-errors", "--print", "%(id)s\t%(duration)s\t%(channel)s\t%(title)s", "--", *ids])
    got = {row["id"]: row for row in _rows(r.stdout, ["id", "duration", "channel", "title"])}
    return [{**(got.get(v) or {"id": v, "duration": None, "channel": "", "title": "[Unavailable video]"}),
             "index": n} for n, v in enumerate(ids, 1)]


def list_title(url):
    """A channel's name ('Mood Hut') or a playlist's title, from its first entry."""
    r = _run(["--flat-playlist", "--playlist-items", "1", "--print",
              "%(playlist_title)s\t%(playlist_channel)s", url], check=True)
    title, _, channel = (r.stdout.strip().splitlines() or [""])[0].partition("\t")
    if kind_of(url)[0] == "channel" and channel not in ("", "NA"):
        return channel
    return re.sub(r" - (Videos|Home)$", "", title) if title != "NA" else ""


VIDEO = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/))([\w-]{11})")
CHANNEL = re.compile(r"^(https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[\w-]+|c/[^/?#]+|user/[^/?#]+))"
                     r"(/[a-z]*)?/?(?:[?#].*)?$")


def kind_of(url):
    """('video', id) | ('playlist', url) | ('channel', its /videos page) | (None, url).
    A watch link that is also in a playlist (&list=) counts as the video."""
    u = url.strip()
    if re.fullmatch(r"[\w-]{11}", u):
        return "video", u
    m = VIDEO.search(u)
    if m:
        return "video", m.group(1)
    if re.search(r"youtube\.com/playlist\?(?:.*&)?list=[\w-]+", u):
        return "playlist", "https://www.youtube.com/playlist?list=" + re.search(r"list=([\w-]+)", u).group(1)
    m = CHANNEL.match(u)
    if m:
        tab = m.group(2) if m.group(2) in ("/videos", "/streams") else "/videos"
        return "channel", m.group(1) + tab
    return None, u


def fetch_info(video_id, dest_dir, comments=True):
    """Write <dest_dir>/<id>.info.json (with top comments) and return its path."""
    os.makedirs(dest_dir, exist_ok=True)
    args = ["--skip-download", "--write-info-json", "-o",
            os.path.join(dest_dir, "%(id)s.%(ext)s")]
    if comments:
        args += ["--write-comments", "--extractor-args",
                 "youtube:max_comments=150,all,0;comment_sort=top"]
    _run(args + ["--", video_id], check=True)
    return os.path.join(dest_dir, f"{video_id}.info.json")


def download_audio(video_id, dest_noext, attempts=4):
    """Best original audio stream, remuxed without re-encoding (.opus or .m4a),
    with the thumbnail embedded. Returns the written path. YouTube intermittently
    answers 403 for a stream URL; a fresh try with the default client usually works
    (other clients often lack formats without PO tokens), so `tv` is the last resort."""
    s = settings()
    clients = [None, POT_CLIENT, None, "tv"]
    for i in range(attempts + 1):
        hls = i == attempts                     # last resort: the HLS copy of AAC 140
        client = None if hls else clients[i % len(clients)]
        if client == POT_CLIENT and not pot_server():
            client = None
        args = ["-f", HLS_FORMAT if hls else AUDIO_FORMAT, "-x", "--embed-thumbnail",
                "--convert-thumbnails", "jpg", *ffmpeg_location(s),
                "-o", dest_noext + ".%(ext)s"]
        if client:
            args += ["--extractor-args", f"youtube:player_client={client}"]
        r = _run(args + ["--", video_id])
        if r.returncode == 0:
            break
        if hls:
            raise RuntimeError(f"yt-dlp failed after {attempts + 1} tries: {r.stderr.strip()[-300:]}")
        time.sleep(5 * (i + 1))
    hits = [p for p in glob.glob(glob.escape(dest_noext) + ".*")
            if p.lower().endswith((".opus", ".m4a", ".mp3", ".ogg", ".webm"))]
    if not hits:
        raise RuntimeError(f"no audio written for {video_id}")
    return hits[0]


def download_batch(video_ids, dest_dir, info_dir, log, fmt=AUDIO_FORMAT, client=None):
    """Many single-track videos in one yt-dlp session: best original audio (no
    re-encode, thumbnail embedded) as <dest>/<title> [<id>].<ext>, plus
    <info_dir>/<id>.info.json. Unavailable videos are skipped (re-run to retry);
    the sleeps keep a long run under YouTube's rate limits."""
    s = settings()
    os.makedirs(dest_dir, exist_ok=True)
    todo = os.path.join(info_dir, "_batch.txt")
    os.makedirs(info_dir, exist_ok=True)
    with open(todo, "w", encoding="utf-8") as fh:
        fh.write("\n".join(video_ids) + "\n")
    args = ["-f", fmt, "-x", "--embed-thumbnail", "--convert-thumbnails", "jpg", *ffmpeg_location(s),
            "--write-info-json", "--no-write-comments", "--no-overwrites", "--ignore-errors",
            "--retries", "10", "--extractor-retries", "5", "--newline", "--no-progress",
            "--sleep-requests", "0.5", "--sleep-interval", "1", "--max-sleep-interval", "4",
            "-o", os.path.join(dest_dir, "%(title).120B [%(id)s].%(ext)s"),
            "-o", "infojson:" + os.path.join(info_dir, "%(id)s"),
            "-a", todo]
    if client:
        args += ["--extractor-args", f"youtube:player_client={client}"]
    return _run(args, log=log).returncode


def stream_url(video_id, fmt="bv*[height<=720][vcodec^=avc1]"):
    r = _run(["-g", "-f", fmt, "--", video_id], check=True)
    return r.stdout.strip().splitlines()[0]
