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

# The BEAMPILOT opendbc platform. opendbc is a submodule pointing at
# commaai/opendbc, which we cannot push to, so the platform lives in this repo
# (tools/opendbc_beampilot_car) and gets installed into the submodule here --
# the same trade the BeamNG mods below make. Idempotent, and re-applies the two
# small edits to comma.ai's files that a `git submodule update` reverts.
python3 "$DIR/tools/install_beampilot_car.py"

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
    src="$DIR/tools/beamng_mod/$mod"
    dst="$MODS_DIR/$mod"
    # `ln -sfn SRC DIR` does NOT replace a real directory -- it creates
    # DIR/basename(SRC) inside it. (-n only helps when DIR is already a symlink
    # to a directory.) So a mod that was ever COPIED here silently stays a copy,
    # with a stray nested symlink inside it, and never follows a git pull again.
    # The symptom is a feature that is simply never there: no error, no log.
    if [ -L "$dst" ]; then
      :                       # already a link; ln -sfn replaces it correctly
    elif [ -e "$dst" ]; then
      backup="$dst.replaced-$(date +%Y%m%d-%H%M%S)"
      mv "$dst" "$backup"
      echo "  $mod was a copy, not a link -- moved to $(basename "$backup")"
    fi
    ln -sfn "$src" "$dst"
    echo "  $mod -> $dst"
  done
  echo "mods symlinked (edits in the repo apply live; reload with Ctrl+L in-game)"
else
  echo "BeamNG.drive user folder not found at $BEAMNG_USERDIR (run BeamNG.drive at least once, then re-run this script to install the mods)"
fi

echo "beampilot setup complete"
