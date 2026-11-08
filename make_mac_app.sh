#!/bin/sh
# Builds ~/Applications/mixarchive.app: a launcher that opens the GUI in its own window.
# It runs this folder's venv, so code changes need no rebuild. Rebuild if you move the folder.
# Needs this folder's venv with mixarchive in it: python3 -m venv venv && venv/bin/python -m pip install -e .
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PAGE="${1:-/hybrid}"        # start page: /hybrid, /radio, /panel, /music or /
APP="$HOME/Applications/mixarchive.app"

mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/MacOS/mixarchive" <<EOF
#!/bin/sh
# apps start with a bare PATH; the venv has yt-dlp and deno
export PATH="$HERE/venv/bin:/opt/homebrew/bin:/usr/local/bin:\$PATH"
cd "$HERE"
exec "$HERE/venv/bin/python" -I -m mixarchive app --page "$PAGE"
EOF
chmod +x "$APP/Contents/MacOS/mixarchive"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>mixarchive</string>
  <key>CFBundleDisplayName</key><string>mixarchive</string>
  <key>CFBundleIdentifier</key><string>local.mixarchive</string>
  <key>CFBundleExecutable</key><string>mixarchive</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF
echo "built $APP (start page $PAGE)"
