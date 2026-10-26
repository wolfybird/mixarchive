"""Artwork: square covers for rekordbox/CDJ (600x600 baseline JPEG).

- mix cover: the video thumbnail letterboxed onto a square (nothing cropped)
- per-track cover (opt-in, e.g. off1k): a square crop of a video frame grabbed
  a few seconds into each track, where an on-screen info box shows the track
- MOD-render covers: the track cover with a corner badge
"""
import io
import os
import subprocess

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import yt
from .util import settings

SIZE = 600


def _save(im, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.convert("RGB").resize((SIZE, SIZE), Image.LANCZOS).save(
        path, quality=90, progressive=False)


def _run(density, min_density, gap=4):
    """(start, end) of the longest stretch of dense columns/rows (small gaps bridged)."""
    best, start, last = None, None, None
    for i, d in enumerate(density + [0.0] * (gap + 1)):
        if d >= min_density:
            if start is None or i - last > gap:
                start = i
            last = i
            if best is None or last - start > best[1] - best[0]:
                best = (start, last + 1)
    return best


def _dense_box(mask):
    """Bounding box of the main object in a content mask: the longest run of columns
    that are well filled, then of rows within it. A caption or logo in the backdrop
    is thin, so it doesn't stretch the box the way a plain getbbox() would."""
    w, h = mask.size
    px = mask.load()
    cols = [sum(1 for y in range(h) if px[x, y]) / h for x in range(w)]
    xs = _run(cols, 0.15)
    if not xs:
        return None
    rows = [sum(1 for x in range(*xs) if px[x, y]) / (xs[1] - xs[0]) for y in range(h)]
    ys = _run(rows, 0.15)
    return (xs[0], ys[0], xs[1], ys[1]) if ys else None


def _content_box(im, tol=28):
    """Bounding box inside flat borders (letterbox/pillarbox bars, or a plain backdrop
    around a record sleeve), or None when the picture runs to the edges."""
    small = im.resize((320, max(1, round(320 * im.height / im.width))))
    w, h = small.size
    corners = [small.getpixel(xy) for xy in ((1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2))]
    bg = sorted(corners, key=sum)[1]                 # robust to one odd corner
    if sum(1 for c in corners if max(abs(a - b) for a, b in zip(c, bg)) <= tol) < 3:
        return None
    diff = ImageChops.difference(small, Image.new("RGB", small.size, bg)).convert("L")
    mask = diff.point(lambda v: 255 if v > tol else 0)
    box = _dense_box(mask)
    if not box or (box[2] - box[0]) * (box[3] - box[1]) < 0.15 * w * h:
        return None
    if box[2] - box[0] >= 0.97 * w and box[3] - box[1] >= 0.97 * h:
        return None
    sx, sy = im.width / w, im.height / h
    ix, iy = (box[2] - box[0]) * 0.01, (box[3] - box[1]) * 0.01   # no sliver of backdrop
    return (round((box[0] + ix) * sx), round((box[1] + iy) * sy),
            round((box[2] - ix) * sx), round((box[3] - iy) * sy))


def square_from_bytes(data, path):
    """Square cover from a thumbnail. Flat borders are trimmed; a roughly square
    remainder is the release's artwork and fills the square, anything else (a video
    frame) is letterboxed whole. Returns 'cover' or 'letterbox'."""
    th = Image.open(io.BytesIO(data)).convert("RGB")
    box = _content_box(th)
    if box:
        inner = th.crop(box)
        if 0.85 <= inner.width / inner.height <= 1.18:
            side = min(inner.size)
            l, t = (inner.width - side) // 2, (inner.height - side) // 2
            _save(inner.crop((l, t, l + side, t + side)), path)
            return "cover"
        th = inner
    side = max(th.size)
    sq = Image.new("RGB", (side, side), "black")
    sq.paste(th, ((side - th.width) // 2, (side - th.height) // 2))
    _save(sq, path)
    return "letterbox"


def fetch_image(url):
    """(mime, bytes) of an image URL (e.g. a video's thumbnail), or None."""
    import urllib.request
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return (r.headers.get_content_type(), r.read())
    except OSError:
        return None


def crop_frame(frame_path, box, path):
    _save(Image.open(frame_path).convert("RGB").resize((1280, 720)).crop(tuple(box)), path)


def grab_frames(video_id, items, offset, dest_dir, prefix):
    """items: [(track no, start sec)] -> one 720p frame `offset` s into each track,
    saved as <dest>/<prefix>_<NN>.jpg. Returns paths (None where grabbing failed)."""
    url = yt.stream_url(video_id)
    out = []
    for n, start in items:
        p = os.path.join(dest_dir, f"{prefix}_{n:02d}.jpg")
        os.makedirs(dest_dir, exist_ok=True)
        subprocess.run([settings()["ffmpeg"], "-hide_banner", "-v", "error", "-y",
                        "-ss", str(int(start) + offset), "-i", url, "-frames:v", "1",
                        "-q:v", "2", p], check=False, stdin=subprocess.DEVNULL)
        out.append(p if os.path.exists(p) else None)
    return out


def badge(src_path, dst_path, text="MOD RENDER"):
    im = Image.open(src_path).convert("RGB").resize((SIZE, SIZE))
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 40)
    except OSError:
        font = ImageFont.load_default()
    w = d.textlength(text, font=font)
    d.rectangle([SIZE - w - 36, SIZE - 68, SIZE - 8, SIZE - 8], fill=(230, 40, 120))
    d.text((SIZE - w - 22, SIZE - 64), text, fill="white", font=font)
    _save(im, dst_path)


def contact_sheet(items, path, cols=10, cell=160):
    """items: [(label, image path)] -> one review sheet."""
    font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 16)
    rows = max(1, (len(items) + cols - 1) // cols)
    sheet = Image.new("RGB", (cols * cell, rows * cell), "gray")
    for i, (label, p) in enumerate(items):
        im = Image.open(p).convert("RGB").resize((cell - 4, cell - 4))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 8 + 9 * len(label), 20], fill="black")
        d.text((3, 1), label, fill="yellow", font=font)
        sheet.paste(im, ((i % cols) * cell + 2, (i // cols) * cell + 2))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sheet.save(path, quality=88)
