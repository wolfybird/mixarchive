import unittest

from mixarchive import util


class PyrekordboxHint(unittest.TestCase):
    def test_up_to_314_it_comes_with_mixarchive(self):
        self.assertTrue(util.pyrekordbox_hint((3, 14)).startswith("it comes with mixarchive; put it back with: "))

    def test_from_315_it_says_why_and_both_ways_out(self):
        hint = util.pyrekordbox_hint((3, 15))
        self.assertIn("on Python 3.15 mixarchive installs without it", hint)
        self.assertIn("pipx install --python 3.14 mixarchive", hint)
        self.assertIn('-m pip install "mixarchive[rekordbox]"', hint)
        self.assertIn("3.16", util.pyrekordbox_hint((3, 16)))


if __name__ == "__main__":
    unittest.main()
