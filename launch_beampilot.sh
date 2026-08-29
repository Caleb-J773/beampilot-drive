#!/usr/bin/env bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"
source "$DIR/config_beampilot.sh"

# FINGERPRINT is BEAMPILOT by default, which lives in this repo rather than in
# the opendbc submodule (see tools/install_beampilot_car.py). A `git submodule
# update` silently reverts the two lines that register it, and the symptom is
# openpilot failing to start on a car it cannot find -- so repair it here rather
# than only at setup time. Silent and instant when nothing is wrong.
python3 "$DIR/tools/install_beampilot_car.py" --quiet || {
  echo "could not install the BEAMPILOT opendbc platform; falling back to HONDA_CIVIC_2022"
  export FINGERPRINT="HONDA_CIVIC_2022"
}

# Seeds CalibrationParams (validBlocks=20, rpyCalib=[0,0,0]) so calibrationd
# starts "calibrated" instead of requiring minutes of sustained 15+mph
# straight-line driving to converge on its own (see calibrationd.py's
# MIN_SPEED_FILTER/INPUTS_NEEDED gating) -- reasonable here since
# openpilot_cam is a rigidly-mounted, always-level virtual camera, not a
# real one whose pose is actually unknown at startup. Also sets
# HasAcceptedTerms/OpenpilotEnabledToggle so onboarding doesn't block
# engagement. This mutates only the one-off python subprocess's own
# environment, not this shell's -- FINGERPRINT stays whatever
# config_beampilot.sh already exported.
python3 -c "from openpilot.selfdrive.test.helpers import set_params_enabled; set_params_enabled()"

# BEAMPILOT_CALIBRATION controls what the above did to CalibrationParams.
#   instant (default) -- keep the seed: calibrationd starts "calibrated" with a
#                        level zero pose, usable from the first second.
#   live              -- clear it: calibrationd converges from real driving, and
#                        openpilot will NOT engage until it has (minutes of
#                        sustained straight driving above 15mph -- see
#                        MIN_SPEED_FILTER / INPUTS_NEEDED in calibrationd.py).
# Note calibrationd keeps refining either way; the seed only decides the
# starting point. "live" is more honest on a car whose camera pose genuinely
# isn't level, where a zero seed is a wrong answer rather than a head start.
if [ "${BEAMPILOT_CALIBRATION:-instant}" = "live" ]; then
  python3 -c "from openpilot.common.params import Params; Params().remove('CalibrationParams')"
  echo "calibration: live -- openpilot will not engage until calibrationd converges"
fi

# Hand LONGITUDINAL (speed) control to openpilot. Without this, honda's
# interface.py sets openpilotLongitudinalControl=False / pcmCruise=True, meaning
# openpilot steers but expects the CAR's own ACC to manage speed -- and BeamNG
# has no ACC, so nothing accelerates or holds speed at all. The MetaDrive/CARLA
# bridges set this same param for the same reason (tools/sim/bridge/common.py).
# Must be set before card.py reads it during fingerprinting, hence here.
python3 -c "from openpilot.common.params import Params; Params().put_bool('AlphaLongitudinalEnabled', True, block=True)"

# Following distance / how late it brakes for the car ahead. This is a stored
# param, not an env var, so it persists between runs -- set it explicitly here
# rather than leaving whatever a previous session left behind.
#   0 = aggressive (T_FOLLOW 1.25s, jerk factor 0.5 -- brakes latest)
#   1 = standard   (T_FOLLOW 1.45s)
#   2 = relaxed    (T_FOLLOW 1.75s -- brakes earliest)
# See get_T_FOLLOW/get_jerk_factor in longitudinal_mpc_lib/long_mpc.py.
python3 -c "
from openpilot.common.params import Params
import os
Params().put('LongitudinalPersonality', int(os.environ.get('BEAMPILOT_PERSONALITY', '1')), block=True)
"

# Optional countdown before handing off, to give you time to tab into BeamNG.
# Off by default: the build and process startup take long enough on their own
# that there's no window to miss. Set BEAMPILOT_LAUNCH_DELAY to a number of
# seconds if you want one.
delay="${BEAMPILOT_LAUNCH_DELAY:-0}"
if [ "$delay" -gt 0 ] 2>/dev/null; then
  echo ""
  echo ">>> Switch to the BeamNG.drive window now -- starting in ${delay}s <<<"
  while [ "$delay" -gt 0 ]; do
    echo "  $delay..."
    sleep 1
    delay=$((delay - 1))
  done
fi

echo "Starting..."
exec ./launch_chffrplus.sh