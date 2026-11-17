import os
import struct
import tempfile
import time
import unittest

from mixarchive import rbmonitor as m

Cue, T = m.Cue, m.T


def write(path, data=b"x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    if mtime:
        os.utime(path, (mtime, mtime))


def anlz(sections):
    """An analysis file: PMAI header, then the given (fourcc, header bytes after the 12-byte
    section header, body) sections."""
    body = b""
    for fourcc, head, rest in sections:
        lh = 12 + len(head)
        body += fourcc + struct.pack(">II", lh, lh + len(rest)) + head + rest
    return b"PMAI" + struct.pack(">II", 28, 28 + len(body)) + bytes(16) + body


def pcpt(hot, ms, loop_end=None):
    e = bytearray(56)
    e[0:4] = b"PCPT"
    struct.pack_into(">III", e, 4, 28, 56, hot)
    e[28] = 2 if loop_end else 1
    struct.pack_into(">II", e, 32, ms, loop_end or 0xFFFFFFFF)
    return bytes(e)


class Cues(unittest.TestCase):
    def test_same_cues_within_slack(self):
        self.assertEqual(m.cue_diff([Cue(0, 1000, False), Cue(1, 5000, False)],
                                    [Cue(1, 5004, False), Cue(0, 995, False)]), ([], []))

    def test_differences(self):
        stick = [Cue(1, 5000, False), Cue(0, 300, False), Cue(2, 9000, True)]
        rb = [Cue(1, 5000, False), Cue(0, 661, False), Cue(2, 9000, False)]
        only_stick, only_rb = m.cue_diff(stick, rb)
        self.assertEqual(only_stick, [Cue(0, 300, False), Cue(2, 9000, True)])
        self.assertEqual(only_rb, [Cue(0, 661, False), Cue(2, 9000, False)])

    def test_cue_text(self):
        self.assertEqual(m.cue_text(Cue(0, 61500, False)), "memory 1:01.5")
        self.assertEqual(m.cue_text(Cue(4, 2000, True)), "D 0:02.0 loop")

    def test_cue_sections_skips_the_waveform(self):
        cues = struct.pack(">IHHI", 1, 0, 2, 0)     # list type 1 (hot cues), count 2
        data = anlz([(b"PPTH", b"", bytes(20)), (b"PWV3", b"", bytes(60000)),
                     (b"PCOB", cues, pcpt(1, 1234) + pcpt(3, 64000, 66000)), (b"PWV4", b"", bytes(500))])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ANLZ0000.DAT")
            write(path, data)
            small = m.cue_sections(path)
        self.assertLess(len(small), 300)
        from rekfix import anlz as rk
        got = sorted((c.hot, c.start_ms, c.is_loop) for c in rk.read_cues(small, "DAT"))
        self.assertEqual(got, [(1, 1234, False), (3, 64000, True)])

    def test_cue_sections_not_an_analysis_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.DAT")
            write(path, b"RIFF" + bytes(40))
            self.assertIsNone(m.cue_sections(path))
            self.assertIsNone(m.cue_sections(os.path.join(d, "missing.DAT")))


class Matching(unittest.TestCase):
    def test_tracks(self):
        coll = {"1": T("1", r"D:\a\Song.mp3", 1000, "Song", "A"),
                "2": T("2", r"D:\b\Other.mp3", 2000, "Other", "B"),
                "3": T("3", r"D:\c\Dup.mp3", 3000, "Dup", "C"),
                "4": T("4", r"D:\c\Dup-1.mp3", 3000, "Dup", "C")}
        stick = {10: T(10, "/Contents/A/Song.mp3", 1000, "song", "A"),       # size + title (any case)
                 11: T(11, "/Contents/B/Renamed.mp3", 2000, "Retitled", "B"),  # size alone: unique both sides
                 12: T(12, "/Contents/C/Dup-1.mp3", 3000, "Dup", "C"),         # two candidates: the file name
                 13: T(13, "/Contents/D/New.mp3", 4000, "New", "D")}           # rekordbox doesn't have it
        self.assertEqual(m.match_tracks(stick, coll), {10: "1", 11: "2", 12: "4"})

    def test_size_alone_needs_a_unique_size(self):
        coll = {"1": T("1", "a.mp3", 1000, "A", ""), "2": T("2", "b.mp3", 1000, "B", "")}
        stick = {5: T(5, "/x.mp3", 1000, "X", "")}
        self.assertEqual(m.match_tracks(stick, coll), {})

    def test_playlists(self):
        stick = {"DEEWEE": [], "HOUSE": [], "Old": [], "x / Same": [], "y / Same": []}
        rb = {"mixarchive / DEEWEE": [], "HOUSE": [], "Gone": [], "a / Same": [], "b / Same": []}
        pairs, only_stick, only_rb = m.pair_lists(stick, rb)
        self.assertEqual(sorted(pairs), [("DEEWEE", "mixarchive / DEEWEE"), ("HOUSE", "HOUSE")])
        self.assertEqual(sorted(only_stick), ["Old", "x / Same", "y / Same"])   # "Same" is ambiguous
        self.assertEqual(sorted(only_rb), ["Gone", "a / Same", "b / Same"])

    def test_list_diff(self):
        self.assertEqual(m.list_diff(["a", "b", "b", "c"], ["c", "b", "d"]), (["a", "b"], ["d"]))


class Histories(unittest.TestCase):
    def test_saved_partly_or_not(self):
        keys = ["1|a", "2|b", "3|c"]
        self.assertEqual(m.history_status(keys, [["1|a", "2|b", "3|c"]]), (3, 3))
        self.assertEqual(m.history_status(keys, [["1|a"], ["1|a", "2|b"]]), (2, 3))   # snapshot made mid-night
        self.assertEqual(m.history_status(keys, [["9|z", "2|b"]]), (0, 3))             # same name, other night
        self.assertEqual(m.history_status(keys, []), (0, 3))


class Backups(unittest.TestCase):
    def test_tree_and_diff(self):
        old = time.time() - 3600
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "lib"), os.path.join(d, "backup")
            write(os.path.join(src, "a", "same.mp3"), b"1234", old)
            write(os.path.join(dst, "a", "same.mp3"), b"1234", old)
            write(os.path.join(src, "a", "new.mp3"), b"12")
            write(os.path.join(src, "a", "changed.mp3"), b"12345", old + 600)
            write(os.path.join(dst, "a", "changed.mp3"), b"1234", old)
            write(os.path.join(dst, "gone.mp3"), b"1")
            write(os.path.join(src, "WAV", "x.wav"), b"1")       # left out, top level only
            write(os.path.join(src, "a", "WAV", "y.mp3"), b"1", old)
            write(os.path.join(dst, "a", "WAV", "y.mp3"), b"1", old)
            s, t = m.tree(src, ["wav"]), m.tree(dst, ["wav"])
            missing, changed, extra = m.tree_diff(s, t)
        self.assertEqual(len(s), 4)
        self.assertEqual([v[0] for v in missing], [os.path.join("a", "new.mp3")])
        self.assertEqual([v[0] for v in changed], [os.path.join("a", "changed.mp3")])
        self.assertEqual([v[0] for v in extra], ["gone.mp3"])

    def test_flac_copies(self):
        old = time.time() - 3600
        with tempfile.TemporaryDirectory() as d:
            wav, flac = os.path.join(d, "WAV"), os.path.join(d, "FLAC")
            for name in ("ok.wav", "ok2.aif", "stale.wav", "none.wav", "float.wav", "clash.wav", "clash.aif"):
                write(os.path.join(wav, "set", name), b"1", old)
            write(os.path.join(wav, "set", "notes.txt"), b"1", old)
            write(os.path.join(flac, "set", "ok.flac"), b"1")
            write(os.path.join(flac, "set", "ok2.flac"), b"1")
            write(os.path.join(flac, "set", "stale.flac"), b"1", old - 600)
            write(os.path.join(flac, "set", "float.wav"), b"1")                 # copied as it is
            write(os.path.join(flac, "set", "clash.flac"), b"1")
            write(os.path.join(flac, "set", "clash.aif.flac"), b"1")
            todo = m.flac_copies(m.tree(wav), m.tree(flac))
        self.assertEqual(sorted(os.path.basename(v[0]) for v in todo), ["none.wav", "stale.wav"])

    def test_check_backup(self):
        old = time.time() - 3600
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "Library"), os.path.join(d, "backup", "Library")
            write(os.path.join(src, "mixes", "a.mp3"), b"1234", old)
            write(os.path.join(dst, "mixes", "a.mp3"), b"1234", old)
            write(os.path.join(src, "mixes", "b.mp3"), b"12345678")
            write(os.path.join(src, "WAV", "set", "c.wav"), b"1", old)
            p = m.check_backup({"name": "test", "from": src, "to": dst, "skip": ["WAV"],
                                "flac": {"WAV": "WAV - FLAC backup"}}, None, {})
            codes = {f.code: f for f in p.findings}
            self.assertEqual(codes["backup.behind"].level, "warn")
            self.assertEqual(codes["backup.behind"].items, [f"mixes: 1 new file, 0.0 GB"])
            self.assertEqual(codes["backup.flac"].level, "warn")
            self.assertEqual(p.facts, {"files": 2, "not_backed_up": 1, "changed_since": 0})
            self.assertEqual(p.level, "warn")
            gone = m.check_backup({"from": src, "to": os.path.join(d, "nowhere")}, None, {})
            self.assertEqual(gone.findings[0].code, "backup.target")


class Runs(unittest.TestCase):
    def test_changes(self):
        before = {"rekordbox": {"name": "rekordbox", "kind": "collection", "facts": {"tracks": 10, "missing_files": 1}},
                  "stick:A": {"name": "Stick E:", "kind": "stick", "facts": {"deck_histories": ["HISTORY 001"]}},
                  "stick:B": {"name": "Stick F:", "kind": "stick", "facts": {}}}
        after = {"rekordbox": {"name": "rekordbox", "kind": "collection", "facts": {"tracks": 12, "missing_files": 1}},
                 "stick:A": {"name": "Stick E:", "kind": "stick",
                             "facts": {"deck_histories": ["HISTORY 001", "HISTORY 002"]}},
                 "backup:x": {"name": "Backup", "kind": "backup", "facts": {}}}
        self.assertEqual(m.changes(before, after), [
            "rekordbox: tracks 10 -> 12 (+2)",
            "Stick E:: new deck histories: HISTORY 002",
            "Backup: new since the last check",
            "Stick F:: not plugged in this time"])

    def test_render(self):
        report = {"at": "2026-01-02T03:04:05", "took": 3.2, "previous": None, "changes": [], "places": [
            {"id": "x", "kind": "backup", "name": "Backup", "summary": "2 files", "level": "warn", "facts": {},
             "findings": [{"code": "backup.behind", "level": "warn", "text": "7 files not in the backup yet",
                           "items": [f"f{i}" for i in range(7)], "total": 9},
                          {"code": "backup.extra", "level": "info", "text": "1 file", "items": ["old"]}]}]}
        out = m.render(report)
        self.assertIn(" ! Backup - 2 files", out)
        self.assertIn("        f4", out)
        self.assertNotIn("        f5", out)
        self.assertIn("... 4 more: mixarchive monitor --list backup.behind", out)
        self.assertNotIn("        old", out)
        self.assertIn("        old", m.render(report, list_code="backup.extra"))


if __name__ == "__main__":
    unittest.main()
