#!/bin/zsh
# Builds "DVD Ripper GUI.app" into /Applications so it shows up in Launchpad,
# Spotlight and the Dock with the custom icon.
#
# The app is a thin launcher. The real code stays in this folder, so editing
# server.py or ui.html changes the app with no rebuild - same arrangement as
# make_app.sh and the terminal version.
#
# Run:  zsh make_gui_app.sh
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="/Applications/DVD Ripper GUI.app"

if [ ! -f "$HERE/server.py" ]; then
  echo "server.py not found next to this script."
  exit 1
fi

if [ ! -w /Applications ]; then
  echo "/Applications is not writable by this account."
  echo "Change APP at the top of this script to \$HOME/Applications instead."
  exit 1
fi

echo "Building $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>DVD Ripper GUI</string>
  <key>CFBundleDisplayName</key><string>DVD Ripper GUI</string>
  <key>CFBundleIdentifier</key><string>local.dvdripper.gui</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>DVDRipperGUI</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# __SRC__ is replaced below so the launcher holds a real absolute path.
cat > "$APP/Contents/MacOS/DVDRipperGUI" << 'LAUNCH'
#!/bin/zsh
# Starts the local server and opens the page. Quitting this app (Dock menu ->
# Quit, or Cmd-Q) stops the server, because the server is this process.
SRC="__SRC__"
cd "$SRC" || exit 1

# Homebrew python lives outside the sparse PATH a Finder launch inherits.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  osascript -e 'display alert "Python 3 not found" message "Install it with: brew install python"'
  exit 1
fi

exec "$PY" "$SRC/server.py"
LAUNCH

sed -i '' "s|__SRC__|$HERE|" "$APP/Contents/MacOS/DVDRipperGUI"
chmod +x "$APP/Contents/MacOS/DVDRipperGUI"

if ! zsh -n "$APP/Contents/MacOS/DVDRipperGUI" 2>/dev/null; then
  echo "ERROR: the generated launcher has a syntax problem."
  exit 1
fi
echo "Launcher syntax OK"

if [ -f "$HERE/icon.png" ]; then
  echo "Making icon from icon.png"
  TMPD="$(mktemp -d)"
  ICONSET="$TMPD/icon.iconset"
  mkdir -p "$ICONSET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$HERE/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    d=$((s*2))
    sips -z $d $d "$HERE/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns"
  rm -rf "$TMPD"
  echo "Icon installed"
else
  echo "No icon.png found - using the default app icon."
fi

xattr -cr "$APP" 2>/dev/null || true
touch "$APP"

echo
echo "Done: $APP"
echo "Code stays in: $HERE"
echo
echo "Open it with:  open \"$APP\""
echo "If the Dock shows a stale icon:  killall Dock"
