#!/usr/bin/env bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"
source "$DIR/config_beampilot.sh"

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

# Hand LONGITUDINAL (speed) control to openpilot. Without this, honda's
# interface.py sets openpilotLongitudinalControl=False / pcmCruise=True, meaning
# openpilot steers but expects the CAR's own ACC to manage speed -- and BeamNG
# has no ACC, so nothing accelerates or holds speed at all. The MetaDrive/CARLA
# bridges set this same param for the same reason (tools/sim/bridge/common.py).
# Must be set before card.py reads it during fingerprinting, hence here.
python3 -c "from openpilot.common.params import Params; Params().put_bool('AlphaLongitudinalEnabled', True, block=True)"

echo ""
echo ">>> Switch to the BeamNG.drive window now -- starting in 5 seconds <<<"
for i in 5 4 3 2 1; do
  echo "  $i..."
  sleep 1
done

echo "Starting..."
exec ./launch_chffrplus.sh