set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"
cd "$DIR"

echo "Setting up beampilot dependencies and environment..."

if [ -f "$DIR/tools/op.sh" ]; then
  # this installs everything pretty much
  # and runs everything too, like uv lock, etc
  "$DIR/tools/op.sh" setup
else
  # tools is installed in stock OP too, not only beampilot
  echo "tools/op.sh not found (included in repo)"
  exit 1
fi

BEAMNG_USERDIR="$HOME/.local/share/BeamNG/BeamNG.drive/current"
if [ -d "$BEAMNG_USERDIR" ]; then
  MODS_DIR="$BEAMNG_USERDIR/mods/unpacked"
  mkdir -p "$MODS_DIR"
  ln -sfn "$DIR/tools/beamng_mod/beampilot_bridge" "$MODS_DIR/beampilot_bridge"
  echo "beampilot_bridge mod symlinked into $MODS_DIR (edits in the repo apply live; reload with Ctrl+L in-game)"
else
  echo "BeamNG.drive user folder not found at $BEAMNG_USERDIR (run BeamNG.drive at least once, then re-run this script to install the beampilot_bridge mod)"
fi

echo "beampilot setup complete"
