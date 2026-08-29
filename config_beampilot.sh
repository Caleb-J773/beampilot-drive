# this is a CONFIG FILE
# launch_beampilot will source this file

# marks this as a simulator bridge (same convention openpilot/tools/sim's own
# launch_openpilot.sh uses for the MetaDrive/CARLA bridges). Relaxes real-hardware
# checks that don't meaningfully apply here: low-disk-space and high-memory
# offroad alerts (selfdrived.py), and process-lag/sensor-timing leniency.
export SIMULATION="1"

# fake car
# if you change this, you have to fix beamngd
# and possibly beamcamd too as camera positions
# and CAN formats differ from car to car
export FINGERPRINT="HONDA_CIVIC_2022"
export SKIP_FW_QUERY="1"

# ---------------------------------------------------------------------------
# Driving limits. Stock openpilot follows EU/ISO passenger-comfort guidelines,
# which in a sim are usually the reason it won't take a corner or get up to
# speed. Every value below defaults to the stock number if unset, so commenting
# a line out restores unmodified openpilot behavior.
#
# LATERAL (turning). The binding one is lateral accel: max curvature is
# MAX_LAT_ACCEL / v^2, so stock 3.0 allows only a ~300m radius at 67mph.
#   3.0 = stock/comfort   5.0 = spirited   8.0+ = approaching real tire grip
export BEAMPILOT_MAX_LAT_ACCEL="5.0"    # m/s^2
export BEAMPILOT_MAX_LAT_JERK="8.0"    # m/s^3, how fast it may change curvature
# export BEAMPILOT_MAX_CURVATURE="0.2"  # 1/m, geometric cap; only binds below ~11mph
#
# Raising the two above is not the whole story, and the parts that were missing
# are why they used to do less than they looked like they did:
#   - long_mpc bounded its own solution with opendbc's UNSCALED accel limit, and
#     the planner takes min(mpc, cruise), so ACCEL_SCALE was mostly ignored.
#   - the combined lateral+longitudinal envelope was unscaled, so raising the
#     lateral limit left NOTHING for the throttle mid-corner (at 20 m/s, 2 m/s^2
#     of cornering used the entire budget).
#   - ExcessiveActuationCheck soft-disables on MEASURED actuation against
#     hardcoded stock trip points, so past those the car does not corner harder,
#     it hands back control.
# All three now scale together; see openpilot/common/beampilot_limits.py.
#
# How much headroom the excessive-actuation net keeps above what is allowed.
# Stock is 2x. Lower it to tighten the net; it can never drop below stock.
# export BEAMPILOT_ACTUATION_MARGIN="2.0"

# --- Slowing down for corners (EXPERIMENTAL) --------------------------------
# Off by default. This adds a planning layer stock openpilot does not have, so
# it is worth comparing against not having it rather than assuming it is
# better: drive the same road with it on and off. It is also the only feature
# here that changes longitudinal behaviour on its own, with no feed from the
# mod involved.
# Stock openpilot holds the set speed through a bend. It caps ACCELERATION once
# already cornering, but nothing ever brakes for a corner ahead. That is fine on
# a real car with a wide camera and a driver watching; here the model's view is
# a 25.70 degree narrow one, so a corner arrives late and thin, and the raised
# lateral limits above mean the car will carry a speed into a bend it then
# cannot hold. The result is running wide at the entry.
#
# This reads the curvature the model has already predicted along its own path,
# works out the fastest speed that keeps lateral acceleration at the target
# below, and asks for the deceleration that gets there by the time the corner
# arrives. It is OFFERED to the planner, not imposed: a lead car or the cruise
# setpoint still win when they are more restrictive.
export BEAMPILOT_CURVE_SLOWDOWN="0"

# The lateral acceleration to aim for in a corner. Defaults to 0.7x the hard
# lateral limit -- arriving at exactly the limit leaves the steering saturated
# for the whole bend with nothing in reserve for a mid-corner correction.
# Raise it to corner faster, lower it to be gentler.
# export BEAMPILOT_CURVE_LAT_ACCEL="2.1"

# export BEAMPILOT_CURVE_MIN_SPEED_MS="5.0"     # never slow below this for curvature
# export BEAMPILOT_CURVE_ENABLE_SPEED_MS="6.0"  # below this, do not bother
# export BEAMPILOT_CURVE_LOOKAHEAD_S="6.0"      # how far ahead to look
# export BEAMPILOT_CURVE_JERK="4.0"             # how fast the request may change
# export BEAMPILOT_CURVE_MIN_PLAN_S="1.5"       # shortest travel time to plan a change over
#
# LONGITUDINAL (accel/braking), as a multiplier on the stock envelope.
export BEAMPILOT_ACCEL_SCALE="2.0"    # 1.0 = stock (1.6 m/s^2 from a stop)
export BEAMPILOT_DECEL_SCALE="1.5"    # 1.0 = stock (-1.2 m/s^2 cruise braking)
# ---------------------------------------------------------------------------

# When camera calibration happens.
#   instant -- start already "calibrated" at a level zero pose. Usable
#              immediately; the right choice for the ETK series, where the
#              camera really is roughly where the seed claims.
#   live    -- converge from real driving instead. openpilot will NOT engage
#              until it has, which needs minutes of sustained straight driving
#              above 15mph (MIN_SPEED_FILTER / INPUTS_NEEDED in calibrationd).
#              Slower to get going, but more honest on a car whose camera pose
#              genuinely isn't level -- there a zero seed is a wrong answer
#              rather than a head start, and shows up as steering bias.
# calibrationd keeps refining either way; this only sets the starting point.
export BEAMPILOT_CALIBRATION="instant"

# Camera capture. Priority: CAM_REGION > CAM_WINDOW > CAM_MONITOR.
# CAM_WINDOW tracks the BeamNG window by name/class and follows it as it moves
# or resizes, so the game can be windowed and you keep a monitor free for the
# openpilot UI. Needs X11 and xdotool; falls back to monitor capture without
# them. Windows whose class looks like a terminal/editor/browser are ignored,
# so a terminal with "beamng" in the title isn't captured by mistake.
# How the screen is read.
#   auto   (default) X11 grab on an X11 session, desktop portal on Wayland.
#   portal ask the compositor for a ScreenCast stream over PipeWire. REQUIRED on
#          Wayland: Wayland forbids clients from reading the screen, so an X11
#          grab there returns nothing and every frame comes out solid green (an
#          all-zero NV12 buffer is RGB(0,135,0); real black is Y=16/U=V=128).
#          Works on X11 too, and is worth considering there: you pick the window
#          from a share dialog once (remembered afterwards via a restore token),
#          which skips window detection entirely, and it measures smoother --
#          50.00ms mean / 51.3ms max versus 49.96 / 66.8 for the X11 grab,
#          because the frame is already in memory rather than the loop blocking
#          on the X server. Needs gst-launch-1.0 with the pipewire plugin and a
#          working xdg-desktop-portal.
#   x11    force the classic grab.
export BEAMPILOT_CAPTURE_BACKEND="portal"

# What to do when the captured rectangle is not the shape of openpilot's frame.
# openpilot's is 1928x1208 = 1.5960; a full-screen 16:9 window is 1.7778, so the
# picture is squeezed ~11% horizontally. Vertically it is fine (the mod renders
# a 25.70 deg VERTICAL field, matching openpilot's road camera) but horizontally
# it spans 44.15 deg where the intrinsics claim 40.01 -- so everything reads as
# ~11% closer to the centre of the lane than it really is.
#   crop    (default) trim the sides first. What is left is exactly 40.01 deg.
#   stretch the old behaviour, kept so the change can be A/B'd.
# Better than either: size the BeamNG window to 1928x1208. Then the aspect is
# exact, nothing is cropped and nothing is resampled. It fits inside 1440p.
# On the portal backend this needs GStreamer's aspectratiocrop element
# (gst-plugins-good); without it beamcamd says so and falls back to stretch.
export BEAMPILOT_CAM_ASPECT="crop"

export BEAMPILOT_CAM_WINDOW="beamng"
# export BEAMPILOT_CAM_MONITOR="1"
# export BEAMPILOT_CAM_REGION="0,0,1920,1080"

# Your BeamNG vehicle's steering lock, in degrees. Per-vehicle: hold full lock
# and read steering_wheel_deg in tools/beampilot_monitor.py to find yours.
# Too low makes openpilot oversteer, too high makes it run wide.
export BEAMPILOT_STEER_LOCK_DEG="500"

# Lock-to-lock sweep time. Lower is snappier but twitchier.
export BEAMPILOT_STEER_SWEEP_SECONDS="0.15"

# Report the real gear to openpilot. car_events.py raises wrongGear/reverseGear
# off it, which is what stops openpilot engaging while the car rolls backwards
# -- correct for driving, wrong if reversing under openpilot is the point
# (arcade mode, or messing about in a car park). 0 pins the gear to drive,
# which is what the bridge did before it read the real one.
export BEAMPILOT_REPORT_GEAR="1"

# Cruise keys, single letters. Check BeamNG's settings/inputmaps/keyboard.json
# for conflicts before rebinding -- a key bound on both sides does both things.
export BEAMPILOT_KEY_SET="i"
export BEAMPILOT_KEY_RESUME="o"
export BEAMPILOT_KEY_CANCEL="u"
export BEAMPILOT_CRUISE_STEP_MPH="1.0"

# How closely it follows / how late it brakes for the car ahead.
#   0 = aggressive (1.25s gap, brakes latest)   1 = standard (1.45s)
#   2 = relaxed    (1.75s gap, brakes earliest)
export BEAMPILOT_PERSONALITY="0"

# Stop inter-process communication hiccups from blocking engagement and
# disengaging mid-drive. Unlike the purely visual alerts, commIssue is a REAL
# signal -- it means a process stalled or is publishing at the wrong rate, and
# with this set openpilot will keep driving on stale data (e.g. if modeld dies
# it steers on the last model output it got). The underlying event is still
# logged, so check the terminal, and prefer fixing rates over hiding this:
#   uv run python tools/beampilot_monitor.py
# Set to 0 to restore stock behavior.
export BEAMPILOT_IGNORE_COMM_ISSUE="1"

# Commit a lane change from the blinker alone. Stock openpilot also requires a
# nudge on the steering wheel, which can't happen here -- there's no physical
# wheel, and beamngd reports user_torque=0 always, so steeringPressed is never
# true and a signalled lane change would arm and then stall forever.
# Signal with , (left) and . (right) while engaged above 20mph.
export BEAMPILOT_AUTO_LANE_CHANGE="1"

# --- BSM: blind spot monitoring -------------------------------------------
# The BeamNG mod watches every other vehicle in the scene (through mapmgr, the
# same data BeamNG's own AI uses) and reports whether one is sitting in either
# blind spot. openpilot then does what it has always done with a blind spot:
# refuses to start a lane change into that side, and shows "Car Detected in
# Blindspot". Especially worth having with AUTO_LANE_CHANGE above, which
# otherwise commits on the blinker with nothing watching.
#
# Needs the mod reinstalled if you are upgrading from a build before this
# existed -- an old mod simply never sets the bits, and BSM stays silent.
export BEAMPILOT_BSM="1"

# Cancel a lane change ALREADY UNDER WAY if the target lane fills up. Stock
# openpilot only checks the blind spot on the way in and never looks again --
# fine on a real car where the driver had to nudge the wheel and is watching
# the mirror, but here AUTO_LANE_CHANGE commits on the blinker alone, so
# nothing is watching the target lane once the move starts. On a cancel it
# holds the blinker armed and re-commits by itself as soon as the lane clears.
export BEAMPILOT_LANE_CHANGE_ABORT="1"
# How late into the move a cancel may still fire, in seconds. Past roughly the
# halfway point the car is mostly in the new lane and swerving back is its own
# hazard, so beyond this it commits and finishes. Set it very high (say 30) to
# allow a cancel at any point.
# export BEAMPILOT_LANE_CHANGE_ABORT_S="2.0"

# Also warn about a car that is not in the zone yet but is closing fast enough
# to be there mid-manoeuvre. 0 = only warn about what is beside you right now.
export BEAMPILOT_BSM_APPROACHING="1"
# How far ahead to project that closing car, in seconds.
# export BEAMPILOT_BSM_APPROACH_S="2.0"
# export BEAMPILOT_BSM_APPROACH_MAX_M="20.0"

# Zone geometry, in metres, all measured from your own vehicle's body so it
# adapts to whatever you spawn. Defaults roughly follow SAE J2802: the zone
# starts level with the driver's shoulder and ends a few metres past the rear
# bumper, one lane wide.
# export BEAMPILOT_BSM_FRONT_M="1.5"    # forward edge, behind the front bumper
# export BEAMPILOT_BSM_REAR_M="4.0"     # rear edge, behind the rear bumper
# export BEAMPILOT_BSM_INNER_M="0.2"    # inner edge, out from your flank
# export BEAMPILOT_BSM_WIDTH_M="3.6"    # outer edge, out from your flank
# export BEAMPILOT_BSM_HEIGHT_M="2.0"   # half-height; keeps overpasses out of it
# export BEAMPILOT_BSM_MIN_SPEED_MS="1.4"  # below this, report nothing
# export BEAMPILOT_BSM_RATE_HZ="20"     # detection rate inside the mod
# How long a side stays "occupied" after it stops being reported. Stops a car
# sitting exactly on the zone boundary from strobing the warning -- and, with
# the cancel above, from strobing the lane change itself.
# export BEAMPILOT_BSM_HOLD_S="0.4"
# export BEAMPILOT_BSM_PORT="49154"     # beamngd -> card, loopback only

# The on-screen blind spot indicator: amber chevrons at the edges of the road
# view, steady when a car is there and flashing when you are signalling into it.
# Off by default because the radar markers already occupy the road view and two
# overlays at once is worse than either. The blind spot still gates lane changes
# and still raises "Car Detected in Blindspot" either way. Turn it on if you run
# with BEAMPILOT_RADAR off, since nothing else then shows the blind spot.
export BEAMPILOT_BSM_INDICATOR="0"

# Log every blind spot state change to the BeamNG console and the beamngd
# terminal. Turn this on when tuning the zone.
# export BEAMPILOT_BSM_DEBUG="1"

# Switch the blinker off once a lane change has finished. Nothing in the game
# cancels an indicator that was never physically stalked -- BeamNG's own
# auto-cancel keys on the driver steering out of the turn, and openpilot's
# steering arrives through input.event, not the stalk. A change CANCELLED by
# the blind spot deliberately leaves the signal on, so it resumes by itself
# once the lane clears.
export BEAMPILOT_SIGNAL_AUTO_CANCEL="1"

# --- Ground-truth radar ----------------------------------------------------
# The mod reports nearby traffic as radar points, straight to card.py, and
# radard fuses them exactly as it would on a car with a radar.
#
# Why it matters: this car is HONDA_CIVIC_2022, which openpilot knows as
# BOSCH_RADARLESS -- opendbc hands radard an EMPTY RadarData at 20Hz and lead
# detection falls back entirely on the camera. The same camera that, per the
# README, is fed wide-lens intrinsics for an image that is not wide. How far
# away the car in front is happens to be exactly what that gets wrong.
export BEAMPILOT_RADAR="0"

# Let a ground-truth track become the lead on its own, with no confirmation
# from the camera. Stock openpilot refuses to, because a real radar returns
# false positives (bridges, signs) and braking for one the camera cannot see is
# how you get phantom braking. These points come from the simulator's object
# list, so there is no false positive to protect against -- and the case worth
# fixing is the model missing a lead entirely, which is the one the stock gate
# blocks. Set to 0 for stock fusion, where ground truth can only refine a lead
# the camera already found.
export BEAMPILOT_RADAR_LEADS="0"

# How far off the model's predicted path a track may sit and still count as
# being in our lane. Compared against the path, not against straight ahead, so
# it follows the road round a bend.
# export BEAMPILOT_RADAR_LEAD_HALF_WIDTH_M="1.8"

# How much of the cheating to give back. mapmgr knows where every vehicle is,
# through hills and buildings, with exact velocities -- that is not a radar, it
# is omniscience, and openpilot behaves unrealistically well on it.
# export BEAMPILOT_RADAR_ONCOMING="0"         # report oncoming traffic. Off by default:
#                                             # an approaching car is not a lead, and
#                                             # treating one as such is a hard-braking
#                                             # event for nothing. A STATIONARY car
#                                             # facing you IS still reported -- that is
#                                             # a breakdown in your lane.
# export BEAMPILOT_RADAR_OCCLUSION="1"        # drop anything with no line of sight
#                                             # (static geometry only, so a car does not
#                                             # hide the car behind it)
# export BEAMPILOT_RADAR_NOISE_M="0.12"       # range noise; real radar is not exact
# export BEAMPILOT_RADAR_NOISE_MS="0.06"      # range-rate noise
# export BEAMPILOT_RADAR_RANGE_M="110"        # shorter than real radar reaches, on
#                                             # purpose: the camera would never have
#                                             # seen a lead at 150m, so handing openpilot
#                                             # one makes it start managing distance far
#                                             # earlier than it otherwise would
# export BEAMPILOT_RADAR_HALF_WIDTH_M="3.0"   # beam half-width at the bumper...
# export BEAMPILOT_RADAR_SPREAD="0.07"        # ...growing per metre of range
# export BEAMPILOT_RADAR_MAX_TRACKS="12"
# export BEAMPILOT_RADAR_RATE_HZ="20"         # DT_MDL; radard runs at the model's rate
# export BEAMPILOT_RADAR_PORT="49155"         # mod -> card, loopback only
# export BEAMPILOT_RADAR_DEBUG="1"            # log the nearest track every scan

# Draw the radar's tracks on the road view: a cyan diamond under every track,
# ringed on whichever one radard picked as the lead. Projected through the same
# calibration the model's path uses, so the markers land where the model thinks
# the road is. Nothing is drawn when there are no tracks.
export BEAMPILOT_RADAR_INDICATOR="1"

# Processes not to start. manager.py:114 treats every comma-separated name here
# as blocked.
#
#   soundd          -- the alert chimes. Visual alerts still appear in the UI;
#                      this only mutes audio, it does NOT suppress the
#                      underlying events or stop them from disengaging.
#   uploader        -- uploads drive logs to comma's servers.
#   manage_athenad  -- athena, comma connect's remote access/telemetry channel.
#
# Those two are belt-and-suspenders, not the actual safeguard: manager.py:110
# already skips both whenever DongleId is None or UNREGISTERED_DONGLE_ID, and an
# unregistered install is always the latter. Listing them here means they stay
# off even if this device ever gets registered -- simulator footage of a fake
# Honda has no business near comma's training data.
#
# Also already inert, and so not listed: Sentry crash reporting (sentry.py bails
# unless the git origin contains "commaai" AND the device is registered AND it
# isn't a PC -- we fail all three) and device registration (no keypair, so it
# short-circuits to UNREGISTERED before making any request).
export BLOCK="${BLOCK},soundd,uploader,manage_athenad"
export HSA_ENABLE_DXG_DETECTION=1

# to use gpu, pick one, (AMD) or (NV)idia
# AMD iGPU compute needs the user in the `render` group (not set up here);
# using the RTX 3060 instead -- confirmed present and in host/nvidia mode.
# export USE_AMD=1
export USE_NV="1"

# --- which GPU tinygrad uses --------------------------------------------
# tinygrad's NV and AMD backends are NOT CUDA/ROCm: they drive the card through
# raw ioctls, and each supports only a limited range of hardware. Picking the
# wrong card is not a graceful failure -- modeld dies during model load and
# manager reports {"event": "process_not_running", "not_running": "{'modeld'}"}
# with nothing else obviously wrong, so nothing drives.
#
#   NV  -- ops_nv.py's setup_usermode looks up BLACKWELL/AMPERE_CHANNEL_GPFIFO_A,
#          ADA/AMPERE_COMPUTE_B, etc. On anything older than Ampere those are
#          bare next() calls over an empty generator, so the failure surfaces as
#          an unexplained `StopIteration` deep in ops_nv.py. Needs compute
#          capability >= 8.0.
#   AMD -- ops_amd.py asserts the target is gfx942, gfx950, or gfx11xx/gfx12xx
#          ("Unsupported arch: gfxNNNN" otherwise), and needs the user in the
#          `render` group for /dev/kfd.
#
# This bites on any mixed-GPU machine. Here, a GTX 1660 SUPER (Turing, compute
# 7.5) enumerates as NVIDIA index 0 and the RTX 3060 (Ampere, 8.6) as index 1,
# so tinygrad's default of index 0 cannot work.
#
# So detect the first *usable* card rather than assuming index 0, and detect it
# rather than hardcoding, because enumeration follows the PCI bus and shifts
# when cards are reseated. tinygrad's syntax is DEV=":<index>+<BACKEND>" -- the
# index goes BEFORE the '+', so ":1+NV" means "NV restricted to physical GPU 1".
# The selected card then becomes index 0 inside tinygrad.
# Override either backend by exporting BEAMPILOT_GPU_INDEX before running this.

# First KFD node that tinygrad's AMD backend actually supports. The index must
# count only nodes with a nonzero gpu_id, in numeric node order, because that is
# exactly the list ops_amd.py builds (_is_usable_gpu + sorted) and indexes into
# -- the CPU node is node 0 and must not shift the numbering.
_bp_amd_gpu_index() {
  _topo=/sys/devices/virtual/kfd/kfd/topology/nodes
  [ -d "$_topo" ] || return 1
  _idx=-1
  for _n in $(ls "$_topo" 2>/dev/null | sort -n); do
    [ "$(cat "$_topo/$_n/gpu_id" 2>/dev/null || echo 0)" != "0" ] || continue
    _idx=$((_idx + 1))
    _ver=$(awk '/^gfx_target_version/{print $2; exit}' "$_topo/$_n/properties" 2>/dev/null)
    [ -n "$_ver" ] && [ "$_ver" -gt 0 ] 2>/dev/null || continue
    # gfx_target_version 100306 -> (10,3,6) -> gfx1036
    _maj=$((_ver / 10000)); _min=$(((_ver / 100) % 100)); _stp=$((_ver % 100))
    if [ "$_maj" -eq 11 ] || [ "$_maj" -eq 12 ] \
       || { [ "$_maj" -eq 9 ] && [ "$_min" -eq 4 ] && [ "$_stp" -eq 2 ]; } \
       || { [ "$_maj" -eq 9 ] && [ "$_min" -eq 5 ] && [ "$_stp" -eq 0 ]; }; then
      echo "$_idx"; return 0
    fi
  done
  return 1
}

if [ -z "${DEV}" ]; then
  if [ "${USE_NV}" = "1" ]; then
    if [ -z "${BEAMPILOT_GPU_INDEX}" ] && command -v nvidia-smi >/dev/null 2>&1; then
      # First GPU with compute capability >= 8.0. awk rather than sort, so the
      # lowest usable index wins and NVIDIA's own ordering is preserved.
      BEAMPILOT_GPU_INDEX="$(nvidia-smi --query-gpu=index,compute_cap --format=csv,noheader 2>/dev/null \
        | awk -F', *' '{ if ($2 + 0 >= 8.0) { print $1; exit } }')"
    fi
    if [ -n "${BEAMPILOT_GPU_INDEX}" ]; then
      export DEV=":${BEAMPILOT_GPU_INDEX}+NV"
      echo "[beampilot] tinygrad NV -> GPU ${BEAMPILOT_GPU_INDEX} (DEV=${DEV})"
    else
      echo "[beampilot] WARNING: no Ampere-or-newer NVIDIA GPU found (tinygrad's NV backend" \
           "needs compute capability >= 8.0). modeld will fail at model load with a" \
           "StopIteration in ops_nv.py. Set BEAMPILOT_GPU_INDEX to override." >&2
    fi
  elif [ "${USE_AMD}" = "1" ]; then
    [ -n "${BEAMPILOT_GPU_INDEX}" ] || BEAMPILOT_GPU_INDEX="$(_bp_amd_gpu_index)"
    if [ -n "${BEAMPILOT_GPU_INDEX}" ]; then
      export DEV=":${BEAMPILOT_GPU_INDEX}+AMD"
      echo "[beampilot] tinygrad AMD -> GPU ${BEAMPILOT_GPU_INDEX} (DEV=${DEV})"
    else
      echo "[beampilot] WARNING: no AMD GPU that tinygrad supports (needs gfx942, gfx950," \
           "or gfx11xx/gfx12xx). modeld will fail with 'Unsupported arch'." \
           "Set BEAMPILOT_GPU_INDEX to override, or USE_NV=1 for an NVIDIA card." >&2
    fi
    # /dev/kfd is render-group owned; without it every AMD device open fails.
    if ! id -nG 2>/dev/null | tr ' ' '\n' | grep -qx render; then
      echo "[beampilot] WARNING: $(id -un) is not in the 'render' group, so /dev/kfd is not" \
           "accessible and the AMD backend cannot open the GPU." \
           "Fix with: sudo usermod -aG render $(id -un)  (then log out and back in)" >&2
    fi
  fi
fi
unset -f _bp_amd_gpu_index 2>/dev/null || true

# tici (c3 big) vs mici (c4 small)
# do 1 for tici, 0 for mici
# NOTE: this isn't a UI scale knob -- it's a hard resolution switch in
# system/ui/lib/application.py: BIG=1 renders the window at 2160x1080 (comma
# 3X's screen), BIG=0 at literally 536x240 (comma 4's tiny embedded screen).
# On a desktop monitor, BIG=0 will look absurdly small. Use SCALE below to
# actually fine-tune the on-screen size instead.
export BIG="1"

# On-screen window size: a multiplier on the base resolution BIG selects
# (2160x1080 for BIG=1, 536x240 for BIG=0).
#
# Unset, blank, or "auto" fits the window to your SMALLEST monitor, in either
# direction -- it shrinks BIG=1 so it fits a 1080p screen, and it GROWS BIG=0
# up from its 536x240 postage stamp. The smallest monitor rather than the
# current one because the window manager decides where the window opens, and
# fitting the smallest means it fits wherever that turns out to be.
#
# Set a number to override: 0.6 for a ~1296x648 window from BIG=1, or 3.0 to
# blow BIG=0 up. A value that is not a number falls back to fitting rather
# than crashing the UI.
# export SCALE="0.6"

# chestnut class model selection
# chestnut is the eGPU line of models from comma
# for comma hardware; it can run on desktop with enough resources
# anyone without a strong dedicated dGPU should use non-chestnut
# see more about it in the readme or online at comma.ai in a blogpost somewhere
# NOTE: modeld/helpers.py's usbgpu_present() just returns CHESTNUT=="1" -- there's
# no real USB eGPU accessory detection here. Setting this to 1 forces the "big"
# model build to hardcode DEV=AMD (openpilot/selfdrive/modeld/SConscript's
# usbgpu_tg_flags), regardless of USE_NV/USE_AMD above. Leave this 0 unless you
# actually have a comma Chestnut USB eGPU plugged in.
export CHESTNUT="0"

# --- added by tools/beampilot_tui.py ---
export BEAMPILOT_CAM_MONITOR="1"

# --- added by tools/beampilot_tui.py ---
export BEAMPILOT_GPU_INDEX="0"
# export SCALE=""
export BEAMPILOT_LAUNCH_DELAY="0"
