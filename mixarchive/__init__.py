"""mixarchive - scrape, archive and split long mixes into tagged tracks."""
import sys

__version__ = "0.2.1"     # = pyproject.toml's; publish.yml checks both

# pip won't install mixarchive on an older Python (Requires-Python); this catches a clone run
# with one, e.g. a venv made before 0.2, with what to do instead of an error further in
if sys.version_info < (3, 11):
    raise SystemExit(
        f"mixarchive needs Python 3.11 or newer, and this is Python {sys.version.split()[0]}.\n"
        "Install a newer one (python.org; Windows: winget install Python.Python.3.14; macOS: "
        "brew install python@3.14), then run mixarchive with it: in a clone, make the venv again "
        "(py -3.14 -m venv venv, then pip install -e .); with pipx, "
        "pipx install --python 3.14 --fetch-python missing mixarchive fetches one for you.")
