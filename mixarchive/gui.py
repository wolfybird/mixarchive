"""Browser GUI: `mixarchive gui` serves gui.html on 127.0.0.1 and opens it.

The GUI runs the CLI's own commands (one at a time, output streamed into the page) and
reads collection state straight from the collection folders, so the two never disagree.
Only this machine can reach the server, and every request must carry the random token
from the address it opens with (another web page can't drive it).
"""
import glob
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import rbxml, singles
from .collection import Collection, library, library_root
from .util import APP_DIR, rekordbox_running

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui.html")
COMMANDS = {"add", "update", "run", "resolve", "tracklists", "fetch", "art", "split", "status",
            "verify", "rbsync", "files", "merge", "list"}
REKORDBOX_EXE = r"C:\Program Files\Pioneer\rekordbox *\rekordbox.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Job:
    """The one command running (or last run), with its output."""

    def __init__(self, on_done):
        self.proc, self.args, self.lines, self.code = None, None, [], None
        self.started = self.ended = None
        self.lock, self.on_done = threading.Lock(), on_done

    @property
    def running(self):
        return self.proc is not None and self.code is None

    def start(self, args):
        with self.lock:
            if self.running:
                return False
            self.args, self.lines, self.code = args, [], None
            self.started, self.ended = time.time(), None
            self.proc = subprocess.Popen(
                [sys.executable, "-I", "-m", "mixarchive", *args], cwd=APP_DIR,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"))
            threading.Thread(target=self._pump, daemon=True).start()
            return True

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\r\n").split("\r")[-1])
        self.code = self.proc.wait()
        self.ended = time.time()
        self.on_done(self.args)

    def stop(self):
        if self.running:      # the whole tree: yt-dlp / ffmpeg / rbvenv children too
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                           capture_output=True, creationflags=NO_WINDOW)

    def state(self, since=0):
        return {"args": self.args, "running": self.running, "code": self.code,
                "started": self.started, "ended": self.ended,
                "total": len(self.lines), "lines": self.lines[since:]}


class RekordboxStatus:
    """`files status --json` (about 5 s), refreshed in the background on request."""

    def __init__(self):
        self.data, self.error, self.at, self.loading = None, None, None, False
        self.lock = threading.Lock()

    def refresh(self):
        with self.lock:
            if self.loading:
                return
            self.loading = True
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            r = subprocess.run([sys.executable, "-I", "-m", "mixarchive", "files", "status", "--json"],
                               cwd=APP_DIR, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", creationflags=NO_WINDOW)
            line = (r.stdout.strip().splitlines() or [""])[-1]
            if r.returncode or not line.startswith("{"):
                raise RuntimeError((r.stderr or r.stdout).strip()[-400:] or "no output")
            self.data, self.error = json.loads(line), None
        except Exception as e:          # shown in the page; the rest of the GUI still works
            self.error = str(e)
        self.at, self.loading = time.time(), False

    def state(self):
        return {"status": self.data, "error": self.error, "at": self.at, "loading": self.loading}


def collection_info(name, root):
    c = Collection(root)
    src = c.config["source"]
    if "local_dir" in src:
        kind, links = "local folder", [src["local_dir"]]
    else:
        links = c.playlists() + [f"https://youtu.be/{v}" for v in c.videos()]
        kind = ("label" if c.config.get("label") else "picks" if not c.playlists()
                else "playlist" if all("list=" in u for u in c.playlists()) else "channel")
    mixes = [s for s in c.sources if s.get("kind", "mix") == "mix"]
    rows = singles.read(c.p("tracks.csv"))
    playlists = []
    for pl, files in rbxml.collection_files(c):
        playlists.append({"name": pl, "tracks": len(files), "bytes": sum(os.path.getsize(f) for f in files)})
    return {"name": name, "root": root, "kind": kind, "label": c.config.get("label", ""),
            "genre": c.config.get("genre", ""), "links": links,
            "mixes": len(mixes),
            "mixes_fetched": sum(1 for s in mixes if c.original(s) and os.path.exists(c.original(s))),
            "skipped": sum(1 for s in c.sources if s.get("kind") == "skip"),
            "singles": dict(Counter(r["status"] for r in rows.values())),
            "playlists": playlists}


def overview():
    out = []
    for name, root in library().items():
        try:
            out.append(collection_info(name, root))
        except Exception as e:      # a half-made collection shouldn't hide the others
            out.append({"name": name, "root": root, "error": str(e)})
    return {"collections": out, "library_root": library_root(), "rekordbox_running": rekordbox_running()}


def journals():
    backups = os.path.join(os.path.dirname(library_root()), "_rekordbox backups")
    out = []
    for j in sorted(glob.glob(os.path.join(glob.escape(backups), "* files-*", "journal.csv")), reverse=True):
        with open(j, encoding="utf-8") as fh:
            n = max(0, sum(1 for _ in fh) - 1)
        out.append({"path": j, "name": os.path.basename(os.path.dirname(j)), "changes": n})
    return out


def open_rekordbox():
    exes = sorted(glob.glob(REKORDBOX_EXE))
    if not exes:
        raise RuntimeError("rekordbox.exe not found under C:\\Program Files\\Pioneer")
    subprocess.Popen([exes[-1]], close_fds=True)


def serve(port=8765, open_browser=True):
    token = secrets.token_urlsafe(18)
    rb = RekordboxStatus()
    job = Job(on_done=lambda args: rb.refresh() if args and args[0] in ("rbsync", "files", "update", "merge") else None)
    rb.refresh()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self, url):
            # DNS rebinding: only our own host names; CSRF: only requests with the token
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return False
            given = (parse_qs(url.query).get("t") or [""])[0] if url.path == "/" else self.headers.get("X-Token", "")
            return secrets.compare_digest(given, token)

        def do_GET(self):
            url = urlparse(self.path)
            if not self._allowed(url):
                return self._send(403, {"error": "forbidden"})
            q = parse_qs(url.query)
            if url.path == "/":
                with open(PAGE, "rb") as fh:
                    return self._send(200, fh.read(), "text/html")
            if url.path == "/api/overview":
                return self._send(200, {**overview(), "job": job.state(len(job.lines))})
            if url.path == "/api/rekordbox":
                if q.get("refresh"):
                    rb.refresh()
                return self._send(200, rb.state())
            if url.path == "/api/job":
                return self._send(200, job.state(int((q.get("from") or ["0"])[0])))
            if url.path == "/api/journals":
                return self._send(200, journals())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            if not self._allowed(url):
                return self._send(403, {"error": "forbidden"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if url.path == "/api/run":
                args = body.get("args")
                if not (isinstance(args, list) and args and all(isinstance(x, str) for x in args)
                        and args[0] in COMMANDS):
                    return self._send(400, {"error": "not a mixarchive command"})
                if not job.start(args):
                    return self._send(409, {"error": "another command is still running"})
                return self._send(200, job.state())
            if url.path == "/api/stop":
                job.stop()
                return self._send(200, job.state(len(job.lines)))
            if url.path == "/api/open":
                try:
                    if body.get("what") == "rekordbox":
                        open_rekordbox()
                    elif body.get("what") == "folder" and body.get("name") in library():
                        os.startfile(library()[body["name"]])
                    else:
                        return self._send(400, {"error": "can't open that"})
                except Exception as e:
                    return self._send(500, {"error": str(e)})
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    class Server(ThreadingHTTPServer):
        # on Windows SO_REUSEADDR lets a second GUI bind a port the first still listens on,
        # and requests then reach the first one (which rejects the new token)
        allow_reuse_address = os.name != "nt"

    for p in range(port, port + 20):
        try:
            server = Server(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    else:
        raise SystemExit(f"no free port between {port} and {port + 19}")
    address = f"http://127.0.0.1:{port}/?t={token}"
    print(f"mixarchive GUI: {address}\n(keep this window open while you use it; Ctrl+C stops it)", flush=True)
    if open_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        job.stop()
