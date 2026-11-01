#!/bin/sh
# macOS/Linux counterpart of mixarchive.bat
exec "$(dirname "$0")/venv/bin/python" -I -m mixarchive "$@"
