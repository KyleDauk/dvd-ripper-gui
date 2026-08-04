#!/bin/zsh
# Sets the app up on a Mac. Run this after unzipping:
#
#   zsh install.sh
#
# The web interface needs only Python 3 and MakeMKV. The Homebrew tools are
# for the terminal program's DVD path and are optional here.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
FAIL=0

echo
echo "Checking what this Mac has."
echo

# ---- Python 3 (required) --------------------------------------------
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "  Python 3    MISSING  <- required"
  echo "              Install it with:  brew install python"
  echo "              or install Xcode Command Line Tools: xcode-select --install"
  FAIL=1
else
  echo "  Python 3    $($PY -V 2>&1)  at $PY"
fi

# ---- MakeMKV (required for this interface) ---------------------------
MK="/Applications/MakeMKV.app/Contents/MacOS/makemkvcon"
if [ ! -x "$MK" ]; then
  echo "  MakeMKV     MISSING  <- required"
  echo "              Download from https://www.makemkv.com and drag it to"
  echo "              /Applications, then run this again."
  FAIL=1
else
  echo "  MakeMKV     found"
fi

# ---- optical drive (warn only, it may just be unplugged) -------------
if command -v drutil >/dev/null 2>&1 && drutil status 2>/dev/null | grep -qi "Type:"; then
  echo "  Drive       detected"
else
  echo "  Drive       none detected right now (plug it in before ripping)"
fi

# ---- optional Homebrew tools ----------------------------------------
MISSING=""
for t in lsdvd dvdbackup ffmpeg ffprobe; do
  command -v "$t" >/dev/null 2>&1 || MISSING="$MISSING $t"
done
if [ -n "$MISSING" ]; then
  echo "  Optional   not installed:$MISSING"
  echo "              Only the terminal program's DVD path uses these."
  echo "              Add them any time with:  brew install$MISSING"
else
  echo "  Optional   all present"
fi

echo
if [ "$FAIL" = "1" ]; then
  echo "Install what is marked required above, then run this again."
  exit 1
fi

# ---- sanity check that the code actually imports ---------------------
if ! "$PY" -c "import sys; sys.path.insert(0,'$HERE'); import dvdrip, episodes" 2>/dev/null; then
  echo "The Python files failed to import. The download may be incomplete."
  exit 1
fi
echo "Code imports cleanly."

echo
echo "Building the app."
zsh "$HERE/make_gui_app.sh"

echo
echo "Setup done."
echo "Open the app, then use the 'Library folders' button to point it at"
echo "this Mac's TV and movie folders - those are stored per-machine."
