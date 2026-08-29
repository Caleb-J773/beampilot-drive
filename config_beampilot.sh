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
export BEAMPILOT_CAM_WINDOW="beamng"
# export BEAMPILOT_CAM_MONITOR="1"
# export BEAMPILOT_CAM_REGION="0,0,1920,1080"

# Your BeamNG vehicle's steering lock, in degrees. Per-vehicle: hold full lock
# and read steering_wheel_deg in tools/beampilot_monitor.py to find yours.
# Too low makes openpilot oversteer, too high makes it run wide.
export BEAMPILOT_STEER_LOCK_DEG="500"

# Lock-to-lock sweep time. Lower is snappier but twitchier.
export BEAMPILOT_STEER_SWEEP_SECONDS="0.15"

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

# fine-tune the actual on-screen window size (application.py's GuiApplication
# multiplies BIG's base resolution -- 2160x1080 -- by this). On PC, if unset,
# it auto-picks 1.0 unless your monitor is smaller than the base resolution.
# uncomment and adjust to taste, e.g. 0.6 for a ~1296x648 window:
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
export BEAMPILOT_CONTROL_MODE="lua"
export BEAMPILOT_CAM_MONITOR="1"
