import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest import mock

from mixarchive import skins

PLEDIT = """[Text]
Normal=#00FF00
Current=#FFFFFF   ; the playing track
NormalBG=#000000
SelectedBG=#0000C6
Font=Arial
mbFG=#nonsense
"""
VISCOLOR = "\n".join(f"{i * 10},{i * 5},{255 - i * 10}, // colour {i}" for i in range(24))


def wsz(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


SKIN = {"Neon Amp/Main.BMP": b"BM main", "Neon Amp/TITLEBAR.bmp": b"BM title", "Neon Amp/numbers.bmp": b"BM nums",
        "Neon Amp/pledit.txt": PLEDIT, "Neon Amp/viscolor.txt": VISCOLOR, "Neon Amp/readme.txt": "Made by someone.",
        "../escape.bmp": b"BM nope", "Neon Amp/setup.exe": b"MZ nope"}


class Skins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(skins, "SKINS_DIR", os.path.join(self.tmp.name, "skins"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_config_files(self):
        self.assertEqual(skins.parse_pledit(PLEDIT), {"normal": "#00ff00", "current": "#ffffff", "normalbg": "#000000",
                                                     "selectedbg": "#0000c6", "font": "Arial"})
        vis = skins.parse_viscolor(VISCOLOR)
        self.assertEqual(len(vis), 24)
        self.assertEqual(vis[1], "#0a05f5")

    def test_import_takes_only_flat_known_files(self):
        info = skins.import_bytes(wsz(SKIN), "Neon Amp.wsz", {"url": "https://archive.org/details/x"})
        self.assertTrue(info["id"].startswith("neon-amp-"))
        self.assertEqual(info["bitmaps"], ["main", "titlebar", "numbers"])
        self.assertEqual(info["colors"]["normal"], "#00ff00")
        self.assertEqual(info["readme"], "Made by someone.")
        folder = os.path.join(skins.SKINS_DIR, info["id"])
        self.assertEqual(sorted(os.listdir(folder)), ["escape.bmp", "main.bmp", "numbers.bmp", "pledit.txt",
                                                      "readme.txt", "skin.json", "titlebar.bmp", "viscolor.txt"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "escape.bmp")))
        self.assertEqual(skins.import_bytes(wsz(SKIN), "Neon Amp.wsz")["id"], info["id"])    # same file, same skin
        self.assertEqual([s["id"] for s in skins.list_skins()], [info["id"]])

    def test_not_a_classic_skin(self):
        with self.assertRaises(skins.SkinError) as e:
            skins.import_bytes(wsz({"skin.xml": "<xml/>", "scripts/main.maki": b"\x00"}), "modern.wal")
        self.assertIn("modern", str(e.exception))
        with self.assertRaises(skins.SkinError):
            skins.import_bytes(b"not a zip", "x.wsz")
        with self.assertRaises(skins.SkinError):
            skins.import_bytes(wsz({"pledit.txt": PLEDIT}), "no main.wsz")

    def test_size_limits(self):
        with mock.patch.object(skins, "MAX_TOTAL", 10):
            with self.assertRaises(skins.SkinError):
                skins.import_bytes(wsz(SKIN), "big.wsz")
        with mock.patch.object(skins, "MAX_FILE", 5):                  # oversized files are left out
            with self.assertRaises(skins.SkinError):                   # ... main.bmp too, so: no skin
                skins.import_bytes(wsz(SKIN), "big.wsz")

    def test_assets_and_removal(self):
        info = skins.import_bytes(wsz(SKIN), "Neon Amp.wsz")
        self.assertTrue(skins.asset(info["id"], "MAIN.bmp").endswith("main.bmp"))
        for bad in ((info["id"], "../skin.json"), (info["id"], "readme.txt"), (info["id"], "escape.bmp"),
                    ("../" + info["id"], "main.bmp"), (info["id"], "eqmain.bmp")):
            self.assertIsNone(skins.asset(*bad), bad)
        with self.assertRaises(skins.SkinError):
            skins.remove("../etc")
        skins.remove(info["id"])
        self.assertEqual(skins.list_skins(), [])

    def test_search_and_fetch(self):
        calls = []

        def fake_get(url, limit):
            calls.append(url)
            if "advancedsearch" in url:
                return json.dumps({"response": {"numFound": 2, "docs": [
                    {"identifier": "winampskins_Neon", "title": "Winamp Skin: Neon.wsz"},
                    {"identifier": "bad id/../x", "title": "x"}]}}).encode()
            if "/metadata/" in url:
                return json.dumps({"metadata": {"title": "Winamp Skin: Neon.wsz"},
                                   "files": [{"name": "Neon.png"}, {"name": "Neon.wsz"}]}).encode()
            return wsz(SKIN)
        with mock.patch.object(skins, "_get", side_effect=fake_get):
            r = skins.search("neon (glow)*")
            self.assertEqual(r["items"], [{"identifier": "winampskins_Neon", "title": "Neon.wsz",
                                           "thumb": "https://archive.org/services/img/winampskins_Neon"}])
            self.assertIn("title%3A%28neon+glow%29", calls[0])
            info = skins.fetch("winampskins_Neon")
        self.assertEqual(info["source"]["url"], "https://archive.org/details/winampskins_Neon")
        self.assertTrue(calls[-1].endswith("/download/winampskins_Neon/Neon.wsz"))
        with self.assertRaises(skins.SkinError):
            skins.fetch("../etc/passwd")


if __name__ == "__main__":
    unittest.main()
