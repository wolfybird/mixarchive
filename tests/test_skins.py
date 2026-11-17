import io
import json
import os
import struct
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


def bmp(w, h):            # just enough of each format for its size to be read
    return b"BM" + b"\0" * 12 + struct.pack("<Iii", 40, w, h) + b"\0" * 28


def png(w, h):
    return b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + struct.pack(">II", w, h) + b"\0" * 8


def gif(w, h):
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\0" * 8


def jpeg(w, h):
    return b"\xff\xd8\xff\xe0\x00\x04ab\xff\xc0\x00\x11\x08" + struct.pack(">HH", h, w) + b"\0" * 12


WMS = """<!-- a comment <VIEW backgroundImage="comment.bmp"> -->
<THEME title="Glow" author="Some Studio" copyright="(c) 2002">
<VIEW backgroundColor="none" titleBar="false" onclick="jscript:if (a > b) x();">
  <SUBVIEW backgroundImage="Main_Body.PNG" transparencyColor="#FF00FF" clippingColor="magenta"/>
  <SUBVIEW backgroundImage="vid_screen.bmp"/>
  <SUBVIEW backgroundImage='main_body_down.png'/>
  <SUBVIEW backgroundImage="jscript:pick()"/>
  <TEXT foregroundColor="#CED2D9" fontFace="tahoma, verdana" hoverForegroundColor="#00DBFF"/>
  <TEXT foregroundColor=#CED2D9 />
  <PLAYLIST backgroundColor="#142B51" foregroundColor="wmpprop:x.color"/>
</VIEW>
<VIEW id="previewView" backgroundImage="preview.png"/>
</THEME>"""
WMZ = {"Glow/glow.wms": WMS.encode("utf-16"), "Glow/Main_Body.PNG": png(462, 301), "Glow/vid_screen.bmp": bmp(800, 600),
       "Glow/main_body_down.png": png(462, 301), "Glow/preview.png": png(449, 284), "Glow/script.js": "x()",
       "Glow/Read Me.txt": "Thanks for downloading."}

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
        self.assertTrue(skins.asset(info["id"], "escape.bmp").endswith("escape.bmp"))  # flattened: its own file
        for bad in ((info["id"], "../skin.json"), (info["id"], "skin.json"), (info["id"], "readme.txt"),
                    ("../" + info["id"], "main.bmp"), (info["id"], "eqmain.bmp"), (info["id"], "..\\main.bmp")):
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
                                           "thumb": "https://archive.org/services/img/winampskins_Neon",
                                           "kind": "winamp"}])
            self.assertIn("title%3A%28neon+glow%29", calls[0])
            info = skins.fetch("winampskins_Neon")
        self.assertEqual(info["source"]["url"], "https://archive.org/details/winampskins_Neon")
        self.assertTrue(calls[-1].endswith("/download/winampskins_Neon/Neon.wsz"))
        with self.assertRaises(skins.SkinError):
            skins.fetch("../etc/passwd")


class WmpSkins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(skins, "SKINS_DIR", os.path.join(self.tmp.name, "skins"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_image_sizes(self):
        for data, size in ((bmp(275, -116), (275, 116)), (png(462, 301), (462, 301)), (gif(20, 10), (20, 10)),
                           (jpeg(640, 480), (640, 480)), (b"nonsense", (0, 0)), (b"BM", (0, 0))):
            self.assertEqual(tuple(skins.image_size(data)), size)

    def test_markup(self):
        files = {n.split("/")[-1].lower(): d for n, d in WMZ.items() if not n.endswith((".wms", ".js", ".txt"))}
        r = skins.parse_wms(WMS, files)
        self.assertEqual(r["theme"], {"title": "Glow", "author": "Some Studio", "copyright": "(c) 2002"})
        self.assertEqual(r["colors"], {"normal": "#ced2d9", "normalbg": "#142b51", "current": "#00dbff", "font": "tahoma"})
        # the biggest picture is a video pane and the next a pressed state: the main one is the body
        self.assertEqual(r["images"], {"main": "main_body.png", "preview": "preview.png",
                                       "keys": {"main_body.png": ["#ff00ff"]}})
        self.assertEqual(skins.parse_wms("no markup here")["images"]["main"], None)
        self.assertEqual(skins.parse_wms('<TEXT foregroundColor="red"><PLAYLIST backgroundColor="#abc">')["colors"],
                         {"normal": "#ff0000", "normalbg": "#aabbcc"})

    def test_import_keeps_pictures_never_scripts(self):
        info = skins.import_bytes(wsz(WMZ), "Glow.wmz", {"url": "https://archive.org/details/x"})
        self.assertEqual(info["kind"], "wmp")
        self.assertTrue(info["id"].startswith("glow-"))
        self.assertEqual(info["images"]["main"], "main_body.png")
        self.assertIn("Author: Some Studio", info["readme"])
        self.assertIn("Thanks for downloading.", info["readme"])
        folder = os.path.join(skins.SKINS_DIR, info["id"])
        self.assertEqual(sorted(os.listdir(folder)), ["glow.wms", "main_body.png", "main_body_down.png", "preview.png",
                                                      "read_me.txt", "skin.json", "vid_screen.bmp"])
        path = skins.asset(info["id"], "main_body.png")
        self.assertEqual(skins.asset_type(path), "image/png")
        self.assertIsNone(skins.asset(info["id"], "glow.wms"))
        self.assertIsNone(skins.asset(info["id"], "script.js"))

    def test_a_zip_around_one_skin_but_not_a_bundle(self):
        info = skins.import_bytes(wsz({"Glow skin/Glow.wmz": wsz(WMZ), "Glow skin/install.txt": "double-click it"}),
                                  "Glow.zip")
        self.assertEqual((info["kind"], info["name"]), ("wmp", "Glow"))
        with self.assertRaises(skins.SkinError) as e:
            skins.import_bytes(wsz({"a.wmz": wsz(WMZ), "b.wsz": wsz(SKIN)}), "pack.zip")
        self.assertIn("bundle of 2", str(e.exception))
        with self.assertRaises(skins.SkinError):        # one level only
            skins.import_bytes(wsz({"x.zip": wsz({"Glow.wmz": wsz(WMZ)})}), "deep.zip")

    def test_search_files_and_fetch(self):
        calls = []
        meta = {"metadata": {"title": "Windows Media Player Skins Collection"},
                "files": [{"name": "Glow.wmz", "size": "900"}, {"name": "Glow_thumb.jpg"},
                          {"name": "Huge.wmz", "size": str(skins.MAX_SKIN + 1)}, {"name": "Other Pack.zip", "size": "5"},
                          {"name": "sub/Deep.wmz", "size": "5"}, {"name": "Glow.png"}]}

        def fake_get(url, limit):
            calls.append(url)
            if "advancedsearch" in url:
                return json.dumps({"response": {"numFound": 1, "docs": [
                    {"identifier": "windowsmediaplayerskinscollection", "title": "Windows Media Player Skins"}]}}).encode()
            if "/metadata/" in url:
                return json.dumps(meta).encode()
            return wsz(WMZ)
        with mock.patch.object(skins, "_get", side_effect=fake_get):
            r = skins.search("-xbox NOT", kind="wmp")
            self.assertEqual(r["items"][0]["kind"], "wmp")
            self.assertIn("windows+media+player", calls[0])
            self.assertIn("AND+%28xbox+not%29", calls[0])         # words, never operators
            f = skins.files("windowsmediaplayerskinscollection")
            self.assertEqual([x["file"] for x in f["items"]], ["Glow.wmz", "Other Pack.zip"])
            self.assertEqual(f["too_big"], 1)
            self.assertEqual(f["items"][0]["thumb"],
                             "https://archive.org/download/windowsmediaplayerskinscollection/Glow_thumb.jpg")
            self.assertEqual(f["items"][1]["thumb"], "https://archive.org/services/img/windowsmediaplayerskinscollection")
            info = skins.fetch("windowsmediaplayerskinscollection", "Glow.wmz")
            self.assertEqual((info["name"], info["source"]["file"]), ("Glow", "Glow.wmz"))
            for bad in ("Huge.wmz", "sub/Deep.wmz", "Glow.png", "../x.wmz"):
                with self.assertRaises(skins.SkinError):
                    skins.fetch("windowsmediaplayerskinscollection", bad)



class DefaultSkin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(skins, "SKINS_DIR", os.path.join(self.tmp.name, "skins"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_bundled_added_once_and_removing_it_sticks(self):
        first = skins.default_skin()
        self.assertEqual(first["kind"], "winamp")
        self.assertEqual(first["source"]["archive"], skins.DEFAULT_SOURCE["archive"])
        self.assertIn("by johnnyg0", first["readme"])                      # its credits come with it
        self.assertEqual(len(first["bitmaps"]), 18)
        self.assertEqual(skins.default_skin()["id"], first["id"])          # not added twice
        self.assertEqual([s["id"] for s in skins.list_skins()], [first["id"]])
        skins.remove(first["id"])
        self.assertIsNone(skins.default_skin())                            # removed stays removed
        self.assertEqual(skins.list_skins(), [])

    def test_a_copy_from_the_internet_archive_is_used(self):
        mine = skins.import_bytes(wsz(SKIN), "v2_technics_white_resting_by_johnnyg0.wsz",
                                  {"archive": skins.DEFAULT_SOURCE["archive"]})
        self.assertEqual(skins.default_skin()["id"], mine["id"])
        self.assertEqual(len(skins.list_skins()), 1)

if __name__ == "__main__":
    unittest.main()
