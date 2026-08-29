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
export BEAMPILOT_MAX_LAT_JERK="8.0"     # m/s^3, how fast it may change curvature
# export BEAMPILOT_MAX_CURVATURE="0.2"  # 1/m, geometric cap; only binds below ~11mph
#
# LONGITUDINAL (accel/braking), as a multiplier on the stock envelope.
export BEAMPILOT_ACCEL_SCALE="2.0"      # 1.0 = stock (1.6 m/s^2 from a stop)
export BEAMPILOT_DECEL_SCALE="1.5"      # 1.0 = stock (-1.2 m/s^2 cruise braking)
# ---------------------------------------------------------------------------

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

# Silence the audible alert chimes. manager.py:114 treats every comma-separated
# name in BLOCK as a process not to start; soundd is the one that plays alert
# sounds. Visual alerts still appear in the UI -- this only mutes audio, it does
# NOT suppress the underlying events or stop them from disengaging.
# Remove soundd from this list to get the chimes back.
export BLOCK="${BLOCK},soundd"
export HSA_ENABLE_DXG_DETECTION=1

# to use gpu, pick one, (AMD) or (NV)idia
# AMD iGPU compute needs the user in the `render` group (not set up here);
# using the RTX 3060 instead -- confirmed present and in host/nvidia mode.
# export USE_AMD=1
export USE_NV=1

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