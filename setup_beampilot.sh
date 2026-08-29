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
  # Both mods are required. beampilot_bridge does telemetry and control;
  # openpilot_cam provides the rigidly-mounted, FOV-matched camera that
  # beampilot.lua selects with core_camera.setByName(0, 'openpilot', false).
  # Without openpilot_cam that call silently does nothing and beamcamd ends up
  # capturing whatever camera the player last used.
  for mod in beampilot_bridge openpilot_cam; do
    ln -sfn "$DIR/tools/beamng_mod/$mod" "$MODS_DIR/$mod"
    echo "  $mod -> $MODS_DIR/$mod"
  done
  echo "mods symlinked (edits in the repo apply live; reload with Ctrl+L in-game)"
else
  echo "BeamNG.drive user folder not found at $BEAMNG_USERDIR (run BeamNG.drive at least once, then re-run this script to install the mods)"
fi

echo "beampilot setup complete"
