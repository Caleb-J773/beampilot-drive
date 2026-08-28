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