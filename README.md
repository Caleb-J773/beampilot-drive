<div align="center">

# beampilot-drive

**openpilot, driving a car in BeamNG.drive**

[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux-informational.svg)](#requirements)
[![Game](https://img.shields.io/badge/BeamNG.drive-Steam-orange.svg)](https://www.beamng.com/game/)
[![No BeamNG.tech](https://img.shields.io/badge/BeamNG.tech-not%20required-success.svg)](#why-no-beamngtech)
[![Based on openpilot](https://img.shields.io/badge/based%20on-commaai%2Fopenpilot-blueviolet.svg)](https://github.com/commaai/openpilot)

</div>

---

A fork of [openpilot](https://github.com/commaai/openpilot) that swaps the camera and CAN bus for
a screen capture of BeamNG.drive and a small Lua mod. The driving stack is untouched — the same
`modeld`, `controlsd`, `plannerd` and `locationd` that run on comma hardware run here, and they
have no idea they're looking at a video game.

It works with the normal Steam version. No BeamNG.tech, no `beamngpy`.

**It steers, holds a set speed, brakes for traffic, and changes lanes when you signal.** It is
also not polished: the steering geometry is calibrated for a Honda Civic rather than whatever car
you spawn, there's no real wide-angle camera, and it will drive into things if you let it.

## Contents

- [Requirements](#requirements) · [Dependencies](#dependencies)
- [Install](#install)
- [Running it](#running-it)
- [Configuration](#configuration)
  - [Blind spot monitoring](#blind-spot-monitoring-bsm) · [Ground-truth radar](#ground-truth-radar)
  - [Camera capture](#camera-capture) · [Aspect ratio](#aspect-ratio) · [Wayland](#wayland)
- [How it works](#how-it-works)
- [Knowledge base](#knowledge-base) — the non-obvious parts, in detail
- [Troubleshooting](#troubleshooting)
- [Known problems](#known-problems)
- [Differences from ko6lvm/beampilot](#differences-from-ko6lvmbeampilot)
- [Development](#development)

---

## Requirements

| | |
|---|---|
| **OS** | Linux. openpilot is Linux-only; BeamNG.drive ships a native Linux build, so no Proton. |
| **GPU** | Anything [tinygrad](https://tinygrad.org) supports (NVIDIA or AMD). It renders the game *and* runs the driving model simultaneously. |
| **VRAM** | 4 GB standard model, 8 GB for chestnut-class. |
| **RAM** | 16 GB standard, 32 GB chestnut. |
| **Groups** | Your user needs to be in `input` (keyboard reads, and `/dev/uinput` for joystick mode). |
| **Display** | X11. Wayland works via XWayland in most cases — see [Wayland](#wayland). |
| **Optional** | `xdotool`, for capturing the BeamNG window rather than a whole monitor. |

> [!NOTE]
> These are conservative rather than measured floors. The GPU is the real constraint: it's
> shared between rendering and inference, and a weaker card shows up as dropped camera frames
> before anything else breaks.

### Dependencies

`setup_beampilot.sh` installs everything below automatically. This list is here so you can see
what it's going to touch, or install by hand.

<details>
<summary><b>System packages</b> — installed by openpilot's own <code>tools/setup_dependencies.sh</code></summary>

| Distro | Command |
|---|---|
| Debian/Ubuntu | `sudo apt-get install -y ca-certificates build-essential curl libcurl4-openssl-dev locales git xclip wl-clipboard` |
| Arch | `sudo pacman -S --needed base-devel ca-certificates curl git` |
| Fedora/RHEL | `sudo dnf install -y ca-certificates gcc gcc-c++ make curl libcurl-devel glibc-langpack-en git` |
| openSUSE | `sudo zypper install ca-certificates gcc gcc-c++ make curl libcurl-devel glibc-locale git` |
| Alpine | `sudo apk add ca-certificates build-base curl curl-dev musl-locales git` |
| Void | `sudo xbps-install -Syu base-devel ca-certificates curl git libcurl-devel glibc-locales` |

</details>

<details>
<summary><b>Extra system packages</b> — beampilot-specific, optional but recommended</summary>

| Package | Needed for | Without it |
|---|---|---|
| `xdotool` | Finding and tracking the BeamNG window (X11 backend) | Falls back to whole-monitor capture |
| `xprop` (`x11-utils` on apt, `xorg-xprop` on Arch) | Filtering out non-game windows by class | Window matching is less reliable |
| `gst-launch-1.0` + the PipeWire plugin | The `portal` capture backend | **Wayland cannot capture at all**; frames come out green |
| A desktop portal for your compositor | The `portal` capture backend | Same |

The portal backend needs, by distro:

| Distro | Command |
|---|---|
| Debian/Ubuntu | `sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-pipewire xdg-desktop-portal-kde` (or `-gnome`, `-wlr`) |
| Arch | `sudo pacman -S gstreamer gst-plugins-base gst-plugin-pipewire xdg-desktop-portal-kde` |
| Fedora | `sudo dnf install gstreamer1 gstreamer1-plugins-base gstreamer1-plugin-pipewire xdg-desktop-portal-kde` |

Pick the portal matching your desktop: `-kde` on KDE, `-gnome` on GNOME, `-wlr` on wlroots
compositors (Sway, Hyprland).

`tools/beampilot_setup.py` detects your package manager and offers to install these with the
right package names for your distro.

</details>

<details>
<summary><b>Python packages</b> — managed by <code>uv</code>, from <code>pyproject.toml</code></summary>

**beampilot-specific:**

| Package | Used by | For |
|---|---|---|
| `mss` | `beamcamd` | Screen capture |
| `opencv-python-headless` | `beamcamd` | BGRA→NV12 conversion (~48× faster than numpy) |
| `evdev` | `beamngd` | Reading cruise keys while BeamNG has focus |
| `python-uinput` | `beamngd` | Virtual wheel, only for `BEAMPILOT_CONTROL_MODE=joystick` |

**Inherited from openpilot:** `numpy`, `pycapnp`, `scons`, `requests`, `tqdm`, `sounddevice`,
`pyzmq`, `setproctitle`, `zstandard`, `jeepney`, `PyJWT[crypto]`, `websocket_client`, `inputs`,
`sentry-sdk`, plus comma's vendored native builds (`capnproto`, `acados`, `ffmpeg`, `zstd`,
`zeromq`, `json11`, `git-lfs`, `raylib`, `gcc-arm-none-eabi`).

**Submodules** (cloned with `--recurse-submodules`): `msgq`, `opendbc`, `panda`, `rednose`,
`teleoprtc`, `tinygrad`.

</details>

<details>
<summary><b>Toolchain</b></summary>

| | |
|---|---|
| [`uv`](https://docs.astral.sh/uv/) | Manages Python 3.12 and the venv. Install: `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `git` + `git-lfs` | LFS pulls the model weights. The vendored `comma-deps-git-lfs` covers this. |
| A C/C++ toolchain | `scons` builds openpilot's native parts. Covered by `build-essential`/`base-devel`. |

</details>

## Install

```bash
git clone --recurse-submodules https://github.com/Caleb-J773/beampilot-drive.git
cd beampilot-drive
uv run python tools/beampilot_setup.py
```

The setup tool checks your system before touching anything and tells you what's missing and how
to fix it. It's read-only until you confirm, and it offers to install the mod and run the build
for you.

<details>
<summary>What it checks</summary>

| | |
|---|---|
| **OS and Python** | Linux, and whether Python 3.12 is available (uv fetches it otherwise). |
| **GPU** | Detected via `nvidia-smi` and `lspci`, with the right `USE_NV`/`USE_AMD` to set. |
| **Display server** | X11 vs Wayland, whether capture will work, and whether `xdotool` is present. |
| **Permissions** | `input` group membership, with the `usermod` line if you're missing it. |
| **BeamNG** | Finds the game and your userfolder by parsing Steam's `libraryfolders.vdf`, so installs on secondary drives and flatpak Steam are found rather than guessed at. |
| **Capture target** | Lists the BeamNG windows it can see, so you can confirm it will grab the right one. |

</details>

If you'd rather do it by hand, `./setup_beampilot.sh` still does the build and mod symlink on its
own.

> [!IMPORTANT]
> Launch BeamNG.drive at least once **before** running setup, so the userfolder exists. If you
> install the game later, just run setup again.

### Configuring

There's a terminal UI if you'd rather not edit shell variables by hand:

```bash
uv run python tools/beampilot_tui.py
```

<details>
<summary>What the TUI does</summary>

Groups all 49 settings under Hardware / Car / Driving limits / Controls / Blind spot / Radar /
Camera / Alerts / Bridge, with an explanation of each and what the stock openpilot value is.
Detects your GPUs via `nvidia-smi` and `lspci` and only offers backends this machine has.
Settings that differ from their default are highlighted; ones with consequences (like
`BEAMPILOT_IGNORE_COMM_ISSUE`) warn.

`r` runs setup, `L` launches, `m` opens the monitor, `s` saves, `q` quits.

`config_beampilot.sh` remains the source of truth. The TUI rewrites only the `export` lines it
manages and preserves everything around them, including trailing inline comments. Editing the
file by hand works fine and round-trips correctly.

</details>

## Running it

Start BeamNG, spawn a car, then:

```bash
source .venv/bin/activate.fish   # bash/zsh: source .venv/bin/activate
./launch_beampilot.sh
```

Startup takes a while, which is plenty of time to tab back into the game. Once it's up, get above
~20 mph and press `i`. (Set `BEAMPILOT_LAUNCH_DELAY=5` if you'd rather have a countdown first.)

<div align="center">

| Key | Action |
|:---:|---|
| <kbd>i</kbd> | Set speed and engage (nudges speed down if already engaged) |
| <kbd>o</kbd> | Resume / speed up |
| <kbd>u</kbd> | Cancel |
| <kbd>,</kbd> <kbd>.</kbd> | Turn signals — signal while engaged to change lanes |

</div>

> [!TIP]
> Keys are read straight from the keyboard device via `evdev`, because BeamNG holds window focus
> while you drive. They're `i`/`o`/`u` rather than something memorable because the obvious keys
> are already taken by BeamNG (<kbd>c</kbd> cycles the camera). Rebind them with
> `BEAMPILOT_KEY_SET` / `_RESUME` / `_CANCEL`, but check
> `settings/inputmaps/keyboard.json` in the BeamNG install first — a key bound on both sides
> does both things.

### Watching what it's doing

```bash
uv run python tools/beampilot_monitor.py
```

Live message rates for every channel, what openpilot believes the car is doing, what it's
commanding, what the model predicts, and whether the turn limits are actually binding. This is
the first thing to run when something looks wrong.

`tools/beampilot_diag.py` is a one-shot version that also checks the raw UDP link from the mod.

## Configuration

Everything lives in `config_beampilot.sh`. **Every beampilot setting falls back to the stock
openpilot value when unset**, so deleting a line gives you unmodified behaviour rather than a
crash.

### Hardware

| Setting | Default | |
|---|---|---|
| `USE_NV` / `USE_AMD` | `USE_NV=1` | GPU backend. Set exactly one. |
| `BEAMPILOT_GPU_INDEX` | auto-detected | Which GPU tinygrad uses. See below. |
| `CHESTNUT` | `0` | Larger model. Needs 8 GB VRAM. |
| `BIG` | `1` | Window resolution: `1` = 2160×1080 (comma 3/3X), `0` = 536×240 (comma 4). Not a scale knob. |
| `SCALE` | unset | Multiplies the above. `0.6` ≈ 1296×648. |

#### GPU selection

tinygrad's backends are **not** CUDA or ROCm — they drive the card through raw ioctls, and each
supports only a limited range of hardware:

| Backend | Requires |
|---|---|
| `NV` | Compute capability **≥ 8.0** (Ampere or newer) |
| `AMD` | **gfx942, gfx950, or gfx11xx/gfx12xx**, plus membership of the `render` group for `/dev/kfd` |

An unsupported card is not merely slow — it cannot run the model at all, and the failure is
obscure: `modeld` dies during model load with a bare `StopIteration` deep inside
`tinygrad/runtime/ops_nv.py`, which surfaces only as
`{"event": "process_not_running", "not_running": "{'modeld'}"}`.

This bites on **mixed-GPU machines**, where tinygrad defaults to index 0 and index 0 may be the
card it can't use. On the development machine a GTX 1660 SUPER (Turing, 7.5) enumerates ahead of
an RTX 3060 (Ampere, 8.6), so the default was always wrong.

`config_beampilot.sh` therefore detects the first usable card and sets tinygrad's
`DEV=":<index>+NV"` (the index goes *before* the `+`). It detects rather than hardcodes because
enumeration follows the PCI bus and shifts when cards are reseated. Override with
`BEAMPILOT_GPU_INDEX`. `tools/beampilot_setup.py` prints each card with a verdict.

### Driving limits

Stock openpilot follows EU/ISO passenger-comfort guidelines. In a simulator those are usually
the reason it won't take a corner.

| Setting | Stock | |
|---|---|---|
| `BEAMPILOT_MAX_LAT_ACCEL` | `3.0` m/s² | Turning. Max curvature is `accel / v²` — stock allows only a ~300 m radius at 67 mph. |
| `BEAMPILOT_MAX_LAT_JERK` | `5.0` m/s³ | How fast curvature may change. **The usual cause of weaving if raised too far.** |
| `BEAMPILOT_MAX_CURVATURE` | `0.2` 1/m | Geometric cap. Only binds below ~11 mph. |
| `BEAMPILOT_ACCEL_SCALE` | `1.0` | Multiplier on the acceleration envelope. |
| `BEAMPILOT_DECEL_SCALE` | `1.0` | Same, braking. |
| `BEAMPILOT_PERSONALITY` | `1` | Follow distance: `0` aggressive (1.25 s), `1` standard (1.45 s), `2` relaxed (1.75 s). |

<details>
<summary>What raising these actually does (and doesn't)</summary>

These are *permission* limits. `clip_curvature()` in `drive_helpers.py` clamps the model's
requested curvature before it reaches the lateral controller — raising the ceiling stops the
clipping, it doesn't make the model want to turn harder.

The effect of the shipped values, in turn radius:

| Speed | Stock (3.0) | Configured (5.0) |
|---|---|---|
| 30 mph | 60 m | 36 m |
| 50 mph | 166 m | 100 m |
| 67 mph | 299 m | 179 m |

Whether this matters depends on whether the limit is *binding*. The monitor prints
`BINDING — raising the limit helps` or `not binding — limit is NOT the constraint` so you can
tell rather than guess. If it's not binding and the car still runs wide, the cause is the
[steering geometry mismatch](#steering-geometry-mismatch), not the limit.

</details>

### Vehicle

| Setting | Default | |
|---|---|---|
| `BEAMPILOT_STEER_LOCK_DEG` | `510` | Your BeamNG car's steering lock. **Per-vehicle.** |
| `BEAMPILOT_CALIBRATION` | `instant` | `instant` starts already calibrated at a level pose. `live` converges from real driving first, and won't engage until it has. |
| `BEAMPILOT_STEER_SWEEP_SECONDS` | `0.15` | Lock-to-lock sweep time. Lower is snappier and twitchier. |
| `FINGERPRINT` | `HONDA_CIVIC_2022` | The car openpilot thinks it's driving. |

> [!WARNING]
> Changing `FINGERPRINT` breaks `beamngd`, which hand-packs Honda Bosch CAN messages
> (`ENGINE_DATA`, `WHEEL_SPEEDS`, `POWERTRAIN_DATA`, `ACC_HUD`…) against
> `honda_bosch_radarless_generated.dbc`. Another car needs all of that rewritten against its own
> DBC, plus rework of the cruise-engagement path.

To find your steering lock: hold full lock in-game and read `steering_wheel_deg` in the monitor.
Too low makes openpilot oversteer, too high makes it run wide.

### Controls and alerts

| Setting | Default | |
|---|---|---|
| `BEAMPILOT_KEY_SET` / `_RESUME` / `_CANCEL` | `i` / `o` / `u` | Cruise keys, single letters. |
| `BEAMPILOT_CRUISE_STEP_MPH` | `1.0` | Per-tap speed change. |
| `BEAMPILOT_AUTO_LANE_CHANGE` | `1` | Signal alone commits a lane change. Required here. |
| `BEAMPILOT_BSM` | `1` | Blind spot monitoring. See [below](#blind-spot-monitoring-bsm). |
| `BEAMPILOT_LANE_CHANGE_ABORT` | `1` | Cancel a lane change in progress if the target lane fills up. |
| `BEAMPILOT_SIGNAL_AUTO_CANCEL` | `1` | Switch the blinker off once a lane change finishes. |
| `BEAMPILOT_RADAR` | `1` | Ground-truth lead detection. See [below](#ground-truth-radar). |
| `BEAMPILOT_CONTROL_MODE` | `lua` | `lua` or `joystick`. |
| `BEAMPILOT_CAM_ASPECT` | `crop` | `crop` or `stretch`. See [Aspect ratio](#aspect-ratio). |
| `BEAMPILOT_CAM_WINDOW` | `beamng` | Track the game window by name/class. See [Camera capture](#camera-capture). |
| `BEAMPILOT_CAM_MONITOR` | `1` | Whole-monitor fallback. |
| `BEAMPILOT_CAM_REGION` | unset | `left,top,width,height`, a fixed rectangle. |
| `BEAMPILOT_CAM_RATE_HZ` | `20` | Capture rate. `modeld` is driven by these frames; raising it only helps if the GPU can keep up with the game *and* the model. |
| `BEAMPILOT_CAM_RETRACK_S` | `2.0` | How often a tracked window's geometry is re-read. |
| `BEAMPILOT_VIPC_BUFFERS` | `20` | VisionIPC ring depth per stream. |
| `BEAMPILOT_IGNORE_COMM_ISSUE` | `0` | See warning below. |
| `BLOCK` | `,soundd` | Processes not to start. `soundd` mutes alert chimes. |

> [!CAUTION]
> `BEAMPILOT_IGNORE_COMM_ISSUE` is **behavioural, not cosmetic**. Unlike the alerts suppressed
> under `SIMULATION`, this one changes what openpilot *does*: `commIssue` is registered as both
> `NO_ENTRY` and `SOFT_DISABLE`, so suppressing it means openpilot keeps steering on stale model
> output if `modeld` stalls or dies, instead of handing back control. The event is still logged.

### Blind spot monitoring (BSM)

The mod checks both blind spots against every vehicle in the scene and reports them as
`carState.leftBlindspot` / `rightBlindspot`. openpilot then:

- **won't start a lane change** into an occupied side. Stock behaviour; nothing had ever set
  those two fields before.
- **cancels one already in progress** if the lane fills up mid-move. Not stock: upstream checks
  the blind spot once on the way in and never looks again. Fine on a real car, where the driver
  nudged the wheel to commit and is watching the mirror. Here the blinker alone commits it.

A cancel drops the desire immediately and goes back to *armed*, not off, so the change resumes
by itself once the lane clears. Turning the blinker off ends it. Past
`BEAMPILOT_LANE_CHANGE_ABORT_S` the car is mostly across already and it finishes instead of
swerving back.

Detection is geometric, not visual: an oriented box on each flank tested against each vehicle's
own bounding box, so a long trailer alongside counts even though its centre is metres away. The
zone is measured off your own car, so it fits whatever you spawn. Defaults follow SAE J2802.

```
                 frontM (1.5 m behind the front bumper)
                    |
      +-------------v--------------------+   <- outerM (3.6 m out from the flank)
      |                                  |
[==== your car ====]                     |
      |                                  |
      +----------------------------------+   <- innerM (0.2 m out from the flank)
                                         ^
                        rearM (4.0 m behind the rear bumper)
```

#### What you see

| | |
|---|---|
| **Mirror lamps** | Amber chevrons at the edges of the road view. Steady when a car is there, flashing while you're signalling into it. Nothing is drawn when both sides are clear. |
| **"Car Detected in Blindspot"** | openpilot's own alert, now reachable. |
| **"Lane Change Cancelled"** | 3 s, when one already under way is aborted. Kept distinct because *blocked* reads as "it never started". |

The chime is muted — `soundd` is in `BLOCK`. Remove it if you want the sound.

| Setting | Default | |
|---|---|---|
| `BEAMPILOT_BSM` | `1` | Master switch. |
| `BEAMPILOT_BSM_INDICATOR` | `1` | The mirror lamps. `0` still gates lane changes, just shows nothing. |
| `BEAMPILOT_LANE_CHANGE_ABORT` | `1` | Cancel a change already in progress. |
| `BEAMPILOT_LANE_CHANGE_ABORT_S` | `2.0` | How late a cancel may still fire. High = any time. |
| `BEAMPILOT_SIGNAL_AUTO_CANCEL` | `1` | Drop the blinker once a change finishes. |
| `BEAMPILOT_BSM_APPROACHING` | `1` | Also count a car closing fast enough to be there mid-move. |
| `BEAMPILOT_BSM_APPROACH_S` / `_MAX_M` | `2.0` / `20.0` | How far ahead to project it, and the cap. |
| `BEAMPILOT_BSM_FRONT_M` / `_REAR_M` | `1.5` / `4.0` | Zone edges, from your bumpers. |
| `BEAMPILOT_BSM_INNER_M` / `_WIDTH_M` | `0.2` / `3.6` | Zone edges, out from your flank. |
| `BEAMPILOT_BSM_HEIGHT_M` | `2.0` | Half-height, so an overpass isn't "beside" you. |
| `BEAMPILOT_BSM_MIN_SPEED_MS` | `1.4` | Below this, report nothing. |
| `BEAMPILOT_BSM_RANGE_M` | `60.0` | Distance pre-filter. |
| `BEAMPILOT_BSM_HOLD_S` | `0.4` | Stops a car on the zone edge strobing the warning. |
| `BEAMPILOT_BSM_IGNORE_TOUCHING` | `1` | Skip vehicles touching you (a towed trailer). |
| `BEAMPILOT_BSM_RATE_HZ` | `20` | Detection rate in the mod. |
| `BEAMPILOT_BSM_PORT` | `49154` | `beamngd` → `card`, loopback. |
| `BEAMPILOT_BSM_DEBUG` | `0` | Log every state change. |

If it never lights up, the usual cause is a **stale mod**: BSM arrived after the first release,
and a mod installed as a *copy* rather than a symlink doesn't follow `git pull`.
`tools/beampilot_setup.py` says so by name.

> [!NOTE]
> This is simulator ground truth, not perception. It sees through walls and fog. That's the
> trade: a working lane-change gate, not a faithful radar model.

### Ground-truth radar

`HONDA_CIVIC_2022` is radarless, so opendbc hands `radard` an **empty** `RadarData` at 20 Hz and
lead detection runs on the camera alone — the camera that's fed the wrong intrinsics (see
[No wide-angle camera](#no-wide-angle-camera)). Distance to the car in front is exactly what that
gets wrong.

The mod emits real radar points on the same scan as BSM, straight to `card.py`, which fills them
into the `RadarData` it was already publishing. Everything downstream is stock `radard`.

| Setting | Default | |
|---|---|---|
| `BEAMPILOT_RADAR` | `1` | Master switch. |
| `BEAMPILOT_RADAR_LEADS` | `1` | Let a track be the lead with no camera confirmation. See below. |
| `BEAMPILOT_RADAR_LEAD_HALF_WIDTH_M` | `1.8` | How far off the predicted path still counts as in-lane. |
| `BEAMPILOT_RADAR_RANGE_M` | `150` | About as far as real radar reaches. |
| `BEAMPILOT_RADAR_HALF_WIDTH_M` / `_SPREAD` | `4.5` / `0.12` | Beam width at the bumper, and per metre of range. |
| `BEAMPILOT_RADAR_MAX_TRACKS` | `12` | Nearest first; the rest dropped. Wire format caps at 24. |
| `BEAMPILOT_RADAR_RATE_HZ` | `20` | `DT_MDL` — `radard` runs at the model's rate. |
| `BEAMPILOT_RADAR_PORT` | `49155` | Mod → `card`, loopback. |
| `BEAMPILOT_RADAR_DEBUG` | `0` | Log the nearest track every scan. |

**About `BEAMPILOT_RADAR_LEADS`.** Stock `radard` ignores radar unless the camera already reports
a lead. That's right on a real car — radar has false positives and braking for one the camera
can't see is how you get phantom braking. These points can't be false positives.

Leave the gate in and ground truth only ever *refines* a lead the model already found, which does
nothing for the case that actually hurts: the model missing one. The risk is picking a car in the
next lane on a bend, so the in-lane test is measured against the model's predicted path rather
than straight ahead. Set to `0` for stock fusion.

The **lead car** line in `tools/beampilot_monitor.py` shows whether the lead came from ground
truth or the camera.

### Vehicles

<table>
<tr>
<td width="50%" valign="top">

#### 🟢 Recommended

**ETK 800 · ETK I · ETK K**

The camera framing was tuned against these. The hood sits where the model
expects it, and the view isn't clipped.

Start here if you want it to just work.

</td>
<td width="50%" valign="top">

#### 🟡 Works, with caveats

**Bastion · SBR4 · Sunburst**

Drive fine, but show less of the hood and the view clips slightly — the
camera offsets are fixed, not per-vehicle.

Expect to tune before these feel as good.

</td>
</tr>
</table>

Anything else is untested rather than known-broken. If you try one, the framing is the first
thing to check.

**Per-vehicle tuning**, in rough order of impact:

| What | Where | Symptom if wrong |
|---|---|---|
| Steering lock | `BEAMPILOT_STEER_LOCK_DEG` | Oversteers (too low) or runs wide (too high) |
| Camera height | `offUp` in `openpilot_cam` | Misjudges distance — the model assumes ~1.22 m |
| Camera fore/aft | `offFwd` | Too much or too little hood in frame |
| Lateral offset | `offRight` | Tracks consistently to one side of the lane |

The camera mod lives at
`tools/beamng_mod/openpilot_cam/lua/ge/extensions/core/cameraModes/openpilot.lua`. Edits apply
live — reload with <kbd>Ctrl</kbd>+<kbd>L</kbd> in game.

> [!NOTE]
> **Outside the ETK series the framing is off.** Other cars show less of the hood and the view is
> clipped slightly, because `openpilot_cam`'s offsets are fixed rather than per-vehicle. It still
> drives, but the model has less of the visual context it was trained on. If a car behaves badly,
> that framing is the first thing to suspect — nudge `offUp`/`offFwd` in
> `tools/beamng_mod/openpilot_cam/lua/ge/extensions/core/cameraModes/openpilot.lua`.

The camera also sits noticeably **off-centre** on some cars. The mod places it relative to
`veh:getPosition()`, which returns the vehicle's jbeam reference node — and that node isn't
reliably on the centreline, so where the camera lands varies per vehicle. A real comma three is
mounted near the rear-view mirror, and the model was trained from roughly there.

It drives surprisingly well anyway. Lane positioning is learned from the whole scene rather than
from the camera sitting at one exact spot, so a lateral offset mostly shifts where the car sits in
the lane rather than breaking it outright. Worth knowing before you spend an evening chasing it:
it's a real imperfection, but usually not the one causing your problem. `offRight` in the camera
mod corrects it if a particular car tracks consistently to one side.

Each vehicle also has its own steering lock, so set `BEAMPILOT_STEER_LOCK_DEG` when you switch
cars (hold full lock and read `steering_wheel_deg` in the monitor).

<details>
<summary><b>Using BeamNG's dashboard camera instead</b></summary>

The stock dashboard view can work as a capture source, but it's a downgrade:

- **Heavy motion blur.** You'll almost certainly want to turn it off in BeamNG's graphics
  settings — the driving model was trained on sharp frames, and blur during exactly the moments
  that matter (turning, braking) is the worst possible time to lose detail.
- **It generally performs worse** than `openpilot_cam`. That camera exists because openpilot's
  model assumes a rigidly-mounted lens with fixed intrinsics; every comfort feature of a normal
  in-game camera — head bob, look-ahead yaw, horizon stabilisation, FOV smoothing — violates
  that assumption. `openpilot_cam` deliberately has none of them and matches openpilot's
  road-camera vertical FOV of 25.70°.

If you do use it, note that `beampilot.lua` auto-selects `openpilot` at spawn, so you'd have to
remove that call or switch cameras manually after it runs.

</details>

### Camera capture

First, how the screen is read at all:

| `BEAMPILOT_CAPTURE_BACKEND` | |
|---|---|
| `auto` (default) | X11 grab on an X11 session, desktop portal on Wayland. |
| `portal` | Ask the compositor for a ScreenCast stream over PipeWire. Required on Wayland; also works, and measures smoother, on X11. See [Wayland](#wayland). |
| `x11` | Force the classic X11 grab. On Wayland this yields all-green frames. |

Under the X11 backend, `beamcamd` picks a capture region in this order:

1. **`BEAMPILOT_CAM_REGION`** — a fixed `left,top,width,height` rectangle. Overrides everything.
2. **`BEAMPILOT_CAM_WINDOW`** — track the BeamNG window. It follows the window as you move or
   resize it (re-checked every 2 seconds), so the game can be windowed and you keep another
   monitor free for the openpilot UI and the monitor tool. Needs X11 and `xdotool`.
3. **`BEAMPILOT_CAM_MONITOR`** — grab a whole monitor. The fallback when no window is found.

Window tracking never hard-fails: if the game isn't running or `xdotool` is missing, it says so
and falls back to monitor capture.

<details>
<summary>Why finding the window is harder than it looks</summary>

Three strategies, in order, because none is reliable alone:

**By process.** The honest approach — find BeamNG's PID, ask X which windows it owns. Except
BeamNG runs inside Steam's pressure-vessel container, so the PID in `_NET_WM_PID` frequently
doesn't match what `pgrep` sees. On the development machine this returns nothing at all.

**By `WM_CLASS`.** Stable across window titles and locales, so it's tried before titles.

**By title, filtered.** The fallback, and the one that needs care. A bare search for `beamng`
matched two `gnome-terminal` windows during testing — terminal tabs named after the project
directory. Capturing a terminal instead of the game is a genuinely confusing thing to debug, so
title matches are rejected if the window class looks like a terminal, editor, browser or chat
app, or if the window is smaller than 640×480.

`tools/beampilot_setup.py` prints every candidate it finds, with the strategy that found it, so
you can confirm it's about to grab the right thing.

</details>

### Aspect ratio

`beamcamd` resizes whatever it captures straight to **1928×1208** (aspect **1.596**). A
full-screen 16:9 window is **1.778**, so the picture arrives squeezed ~11% horizontally.

Vertically it's fine: the mod renders a 25.70° **vertical** field, matching openpilot's road
camera. Horizontally a 16:9 window spans **44.15°** while the intrinsics claim **40.01°**, so
everything reads as ~11% closer to the centre of the lane than it is.

This depends on the window's *shape*, not its size — 1440p is exactly as affected as 1080p:

| window | aspect | horizontal field | error |
|---|---|---|---|
| 1920×1080 (16:9) | 1.778 | 44.15° | **+11.4%** |
| 2560×1440 (16:9) | 1.778 | 44.15° | **+11.4%** |
| 3440×1440 (21:9) | 2.389 | 57.18° | **+49.7%** |
| 2560×1600 (16:10) | 1.600 | 40.10° | +0.2% |
| **1928×1208** | 1.596 | 40.01° | **0.0%** |

`BEAMPILOT_CAM_ASPECT=crop` (default) trims the sides first, leaving exactly 40.01°. `stretch` is
the old behaviour, kept so you can compare. On the portal backend the trim happens in the
GStreamer pipeline via `aspectratiocrop`; if that element isn't installed, `beamcamd` says so and
falls back to `stretch`.

> [!TIP]
> **Best option: size the BeamNG window to 1928×1208.** Exact aspect, nothing cropped, nothing
> resampled. Fits inside a 1440p screen.

A window *narrower* than 1.596 can't be fixed this way — cropping top and bottom would cut the
vertical field instead. `beamcamd` leaves those alone and tells you how much wider to go.

### Wayland

**Wayland is supported**, through `xdg-desktop-portal` and PipeWire. Wayland deliberately forbids
a client from reading the screen, so there is no equivalent of an X11 grab: the only supported
route is to ask the compositor, which prompts you to pick a window or monitor and then streams it.

Set the backend and go:

```bash
export BEAMPILOT_CAPTURE_BACKEND="portal"
```

`auto` (the default) already does this for you — it picks `portal` on a Wayland session and keeps
X11 sessions on the X11 grab they have always used.

**You get a share dialog once.** It appears at `beamcamd` startup, a few seconds into
`launch_beampilot.sh`. The choice is remembered afterwards via a portal restore token, so later
runs are silent. Two things worth knowing:

- **Start BeamNG before the stack**, or its window won't be in the picker. Or just pick the
  *monitor* it's fullscreen on — monitors are always listed, and the result is identical.
- If BeamNG is fullscreen and holding focus, the dialog can end up behind it. Alt-tab to reach it.

To choose a different source later, delete the token and relaunch:

```bash
rm ~/.local/state/beampilot/screencast_restore_token
```

Needs `gst-launch-1.0` with the PipeWire plugin, and a desktop portal for your compositor
(`xdg-desktop-portal-kde` on KDE, `-gnome` on GNOME, `-wlr` on wlroots). See
[Dependencies](#dependencies).

> [!TIP]
> **All-green frames mean the capture produced no data at all.** That is not a colour-conversion
> bug: an untouched NV12 buffer is `Y=0, U=V=0`, which decodes to RGB(0,135,0). Real black would
> be `Y=16, U=V=128`. So a green picture is the signature of an X11 grab returning nothing —
> exactly what happens when the X11 backend is used on Wayland. Switch to `portal`. `beamcamd`
> now detects a uniform frame and says this in the log rather than leaving you to guess.

#### The portal backend on X11

It works on X11 too, and it's worth considering there:

- **Smoother.** Measured over 300 frames: 50.00 ms mean / 51.28 ms max, against 49.96 / 66.81 for
  the X11 grab. The frame is already in memory from a reader thread instead of the capture loop
  blocking on the X server.
- **No window detection at all.** You pick the source from a dialog, so the whole
  `xdotool`/`WM_CLASS`/title-matching problem below simply doesn't apply, and neither does
  "it's capturing the wrong window".
- GStreamer does the colour conversion and scaling, so `beamcamd` skips both.

It is not the X11 default because it needs `gst-launch-1.0`, a working portal, and one dialog
click, none of which the X11 grab requires.

#### XWayland

BeamNG is normally an X11 client even on Wayland (Proton/Wine defaults to Wine's X11 driver), so
the X11 backend may appear to find the window. Finding it and capturing it are different things:
window detection queries the X server, while capture has to read pixels the compositor owns.
A found window with green frames is precisely this case.

If detection itself fails, `python -m openpilot.selfdrive.beamcamd.window_capture` prints a full
report and names the actual cause — game not running, `xdotool` missing, no `DISPLAY`, or a native
Wayland window that X11 cannot see. On KDE it asks KWin directly to tell those apart.

## How it works

```mermaid
flowchart TB
    subgraph GAME["🎮 BeamNG.drive &nbsp;·&nbsp; unmodified game"]
        direction TB
        PHYS["Vehicle physics<br/><i>electrics.values</i>"]
        LUA["<b>beampilot.lua</b><br/>protocol mod<br/><i>runs every physics tick</i>"]
        PHYS -.->|"read"| LUA
        LUA -.->|"input.event FILTER_DIRECT"| PHYS
    end

    subgraph OURS["🔧 beampilot &nbsp;·&nbsp; the only code we wrote"]
        direction TB
        CAM["<b>beamcamd</b><br/>mss capture → NV12<br/><i>20 Hz</i>"]
        BNG["<b>beamngd</b><br/>telemetry in, control out<br/><i>100 Hz</i>"]
        FAKE["Honda Bosch CAN<br/>synthesiser<br/><i>+ IMU / GPS / panda</i>"]
        BNG --> FAKE
    end

    subgraph OP["🚗 openpilot &nbsp;·&nbsp; stock, unmodified"]
        direction TB
        CARD["card<br/><i>decodes CAN → carState</i>"]
        MODELD["<b>modeld</b><br/><i>driving model</i>"]
        LOC["locationd · calibrationd<br/><i>pose &amp; extrinsics</i>"]
        PLAN["plannerd<br/><i>path → plan</i>"]
        CTRL["<b>controlsd</b><br/><i>clip_curvature + lat control</i>"]
        SELF["selfdrived<br/><i>engagement &amp; alerts</i>"]

        CARD --> SELF
        MODELD --> PLAN
        MODELD --> LOC
        LOC --> PLAN
        PLAN --> CTRL
        SELF --> CTRL
    end

    SCREEN(["🖥️ X11 window"])

    GAME ==>|"rendered frames"| SCREEN
    SCREEN ==>|"grab region"| CAM
    LUA ==>|"<b>UDP 49152</b><br/>speed · steering angle · pos<br/>vel · accel · gyro · gear<br/>blind spot L/R"| BNG
    CAM ==>|"VisionIPC<br/><i>wide + narrow road</i>"| MODELD
    FAKE ==>|"can · accelerometer<br/>gyroscope · gpsLocationExternal<br/>pandaStates"| CARD
    BNG -->|"<b>UDP 49154</b><br/>blind spot → carState"| CARD
    LUA ==>|"<b>UDP 49155</b><br/>radar points → radarTracks"| CARD
    CARD -->|"carState"| MODELD
    CTRL ==>|"<b>controlsState</b><br/>desiredCurvature (1/m)"| BNG
    BNG ==>|"<b>UDP 49153</b> JSON<br/>steering −1‥1 · throttle · brake"| LUA

    KEYS(["⌨️ keyboard<br/><i>evdev</i>"])
    KEYS -->|"i / o / u<br/>cruise buttons"| BNG

    classDef gameStyle fill:#2d1b3d,stroke:#a855f7,stroke-width:2px,color:#f3e8ff
    classDef oursStyle fill:#134e4a,stroke:#2dd4bf,stroke-width:2px,color:#ccfbf1
    classDef opStyle fill:#1e293b,stroke:#60a5fa,stroke-width:2px,color:#dbeafe
    classDef ioStyle fill:#422006,stroke:#f59e0b,stroke-width:2px,color:#fef3c7

    class PHYS,LUA gameStyle
    class CAM,BNG,FAKE oursStyle
    class CARD,MODELD,LOC,PLAN,CTRL,SELF opStyle
    class SCREEN,KEYS ioStyle

    %% subgraph containers -- explicit so they don't fall back to the theme's
    %% pale default, which clashes with the dark node fills in both GitHub modes
    style GAME fill:#1a0f24,stroke:#a855f7,stroke-width:2px,color:#f3e8ff
    style OURS fill:#0c2926,stroke:#2dd4bf,stroke-width:2px,color:#ccfbf1
    style OP fill:#111827,stroke:#60a5fa,stroke-width:2px,color:#dbeafe
```

**Reading it:** thick arrows are the two closed loops — vision going one way, control coming
back. `beamcamd` grabs the game window and publishes it as camera frames. `modeld` predicts a
path. `plannerd` and `controlsd` turn that into a desired curvature. `beamngd` converts curvature
into a steering angle using opendbc's vehicle model, scales it to BeamNG's `−1‥1` input range,
and sends it over UDP. The Lua mod applies it with `input.event()` — the same function BeamNG's
own AI driver uses.

CAN only flows one direction. `beamngd` fabricates Honda Civic CAN frames from BeamNG telemetry
so openpilot believes it's plugged into a real car. Nothing is ever sent back to the game over
CAN — the return path is that JSON packet on 49153.

<details>
<summary><b>What happens to a single steering command, end to end</b></summary>

```mermaid
flowchart LR
    A["<b>modelV2</b><br/>action.desiredCurvature<br/><i>1/m, from pixels</i>"]
    B["<b>clip_curvature</b><br/>lat accel · jerk · max<br/><i>drive_helpers.py</i>"]
    C["<b>lateral control</b><br/>torque for a real EPS<br/><i>latcontrol_torque</i>"]
    X(["not used here —<br/>no torque-controlled rack"])
    D["<b>VehicleModel</b><br/>get_steer_from_curvature<br/><i>+ understeer comp.</i>"]
    E["<b>normalise</b><br/>÷ STEER_LOCK_DEG<br/><i>→ −1‥1</i>"]
    F["<b>rate limit</b><br/>toward target<br/><i>not integrated</i>"]
    G["<b>UDP → Lua</b><br/>input.event<br/><i>FILTER_DIRECT</i>"]

    A --> B
    B -.->|"stock path"| C -.-> X
    B ==>|"what beamngd uses"| D ==> E ==> F ==> G

    classDef op fill:#1e293b,stroke:#60a5fa,color:#dbeafe
    classDef ours fill:#134e4a,stroke:#2dd4bf,color:#ccfbf1
    classDef dead fill:#292524,stroke:#78716c,color:#d6d3d1
    class A,B,C op
    class D,E,F,G ours
    class X dead
```

Two details worth knowing:

`clip_curvature` **clips** — openpilot does not command past its limit and merely warn. If the
model wants a tighter turn than the lateral-accel budget allows, the excess is discarded and the
car runs wide. That's why raising `BEAMPILOT_MAX_LAT_ACCEL` changes behaviour, and why the
monitor's BINDING line matters.

The rate limiter chases a **target position**, it does not integrate a velocity. An earlier
version did the latter (`position += torque × gain × dt`), which has no equilibrium: any
sustained torque marches to full lock forever. Stacked on openpilot's own PID — which already
integrates error — that's two integrators in series and a textbook growing oscillation.

</details>

<details>
<summary><b>Where every message comes from</b></summary>

| Message | Rate | Published by | Built from |
|---|---|---|---|
| `can` | 100 Hz | `beamngd` | BeamNG telemetry → Honda Bosch frames |
| `accelerometer` / `gyroscope` | 100 Hz | `beamngd` | `sensors.ffiSensors`, angular velocity |
| `gpsLocationExternal` | 10 Hz | `beamngd` | world position, projected to fake lat/lon |
| `pandaStates` | 10 Hz | `beamngd` | mirrors real `carParams.safetyConfigs` |
| `wideRoadCameraState` / `narrowRoadCameraState` | 20 Hz | `beamcamd` | the same captured frame (see [wide camera](#no-wide-angle-camera)) |
| `driverStateV2` / `driverMonitoringState` | ~20 Hz | `beamngd` | faked — driver monitoring is removed |
| `carState` | 100 Hz | `card` *(stock)* | decodes our fake CAN |
| `modelV2` | 20 Hz | `modeld` *(stock)* | the captured frames |
| `controlsState` | 100 Hz | `controlsd` *(stock)* | the plan |

</details>

### Why no BeamNG.tech

BeamNG runs any `.lua` file dropped into a mod's `lua/vehicle/protocols/` folder on every
physics tick — the mechanism behind the stock OutGauge and MotionSim protocols. That gives
direct access to `electrics.values.*` for telemetry and `input.event()` for control, with no
in-game binding and no fake OS input device. `beamngpy` (and therefore the paid research
licence) is only needed for things this doesn't do.

<details>
<summary>The virtual joystick alternative</summary>

`BEAMPILOT_CONTROL_MODE=joystick` creates a `uinput` virtual wheel ("BeamPilot Virtual Wheel")
and drives its axes instead of talking to the mod's control socket. BeamNG reads it like any
USB wheel, which means a one-time bind of Steering/Accelerate/Brake in Options → Controls.

It exists mostly because [jackz314's ETS2/ATS bridge](https://github.com/jackz314/openpilot/tree/master/tools/truck_sim)
has to work that way — ETS2 has no scripting hook at all. BeamNG does, so Lua injection is the
default and the more direct path. Requires `input` group membership for `/dev/uinput`.

</details>

---

## Knowledge base

The parts that took the longest to work out, written down so they don't have to be rediscovered.
`CLAUDE.md` has more.

<details>
<summary><b>Why blind spot state travels over a socket instead of the fake CAN</b></summary>

Everything else `beamngd` tells openpilot goes in as CAN: it synthesises Honda Bosch frames and
`card` decodes them into `carState`, exactly as on a real car. Blind spot is the one exception,
and it is not for want of trying.

The simulated car is a `HONDA_CIVIC_2022`. On a real one, BSM arrives on B-CAN and openpilot
reads it through a **second DBC** — `honda_crv_ex_2017_body_generated`, with `BSM_STATUS_LEFT`
and `BSM_STATUS_RIGHT` — enabled by `CP.enableBsm`, which `honda/interface.py` sets by
fingerprinting message `0x12f8bfa7`. The Civic 2022's DBC dict has no `Bus.body` entry at all,
so no body parser is ever built and those two messages have nowhere to be decoded.

Adding one means editing `opendbc/car/honda/values.py`. `opendbc` here is a **git submodule**
pointing at `commaai/opendbc`: any change there cannot be committed to this repo, so anyone
cloning beampilot would silently not get it, and BSM would work on exactly one machine.

So `beamngd` sends the two flags to `card` over loopback UDP — the same plain-socket pattern the
mod link already uses — and `card.state_update()` overlays them onto the `CarState` it publishes.
Five bytes a tick. Downstream nothing knows the difference: `desire_helper.py` and
`selfdrived.py` see ordinary `carState.leftBlindspot` / `rightBlindspot`.

Two details that matter more than they look:

- **The flags ride in spare `dashLights` bits**, not in new telemetry struct fields.
  `parse_telemetry()` rejects any packet whose length is not an exact match, so growing the
  struct would turn "your mod is old, no BSM" into "your mod is old, *no telemetry at all*".
- **A stale feed fails to "clear", not to "blocked".** If `beamngd` stops sending, the receiver
  times out after 0.5 s and reports clear. A latched warning would block every lane change for
  the rest of the drive with nothing on screen to explain why.

</details>

<details>
<summary><b>The steering chain, step by step</b></summary>

1. `modeld` publishes `modelV2.action.desiredCurvature` — where it wants to go, in 1/m.
2. `controlsd` runs it through `clip_curvature()`, which enforces the lateral accel, jerk and
   max-curvature limits. **This clips the value** — openpilot does not command past its limit
   and merely warn.
3. The clipped curvature goes to the lateral controller, which produces `actuators.torque`.
4. `beamngd.send_control()` takes `controlsState.desiredCurvature` and calls
   `VehicleModel.get_steer_from_curvature(curvature, speed, 0.0)`. This is opendbc's own inverse
   of `calc_curvature()` and includes the speed-dependent understeer compensation
   (`curvature_factor`) that a flat `angle = curvature × wheelbase × steerRatio` formula misses.
   Real tyres need *more* wheel angle for the same curvature the faster you go, which is why a
   single fixed gain works at one speed and not another.
5. Radians → degrees → divided by `BEAMPILOT_STEER_LOCK_DEG` → normalised to −1…1.
6. Rate-limited toward that target (not integrated as a velocity — see below), then sent as JSON
   over UDP.
7. The Lua mod calls `input.event("steering", v, FILTER_DIRECT)`.

**Why rate-limit toward a target instead of integrating.** An earlier version treated torque as
a *velocity* (`position += torque × gain × dt`). That has no equilibrium: any sustained non-zero
torque marches the position to full lock forever, because nothing pulls it back. Stacked on
openpilot's own PID — which already integrates error into torque — that's two integrators in
series, a textbook growing oscillation. Treating the command as a *target position* and
rate-limiting the approach gives a bounded equilibrium.

</details>

<details>
<summary><b>Two independent steering limits, and why only one counts</b></summary>

`_check_saturation()` in `latcontrol.py`:

```python
if (saturated or curvature_limited) and CS.vEgo > self.sat_check_min_speed \
   and not steer_limited_by_safety and not CS.steeringPressed:
```

1. **Planning/comfort limit** — `clip_curvature`, the EU/ISO lateral accel cap. This is what
   `BEAMPILOT_MAX_LAT_ACCEL` raises. It *does* count toward the saturation warning.
2. **Safety/actuation limit** — `steer_limited_by_safety`, the car's torque and rate limits in
   `carcontroller.py` plus panda safety. This explicitly does **not** count, because that's the
   car's firmware refusing, not openpilot being conservative. Untouched here, and it should stay
   that way — it's the layer that stops a software bug yanking the wheel.

</details>

<details>
<summary><b>Message rates matter more than they look</b></summary>

`commIssue` is `NO_ENTRY` + `SOFT_DISABLE` — wrong publish rates block engagement *and*
disengage mid-drive. They are not cosmetic log spam.

`SimulatedSensors` emits multi-sample bursts (5× IMU, 10× GPS) sized for a ~20 Hz caller.
`beamngd` ticks at 100 Hz. Measured before this was fixed:

| Channel | Was | Expected | Now |
|---|---|---|---|
| `gpsLocationExternal` | **1000 Hz** | 10 Hz | 9.4 Hz |
| `accelerometer` / `gyroscope` | 500 Hz | 104 Hz | 100 Hz |
| `pandaStates` | 2 Hz | 10 Hz | 10 Hz |
| `can` | 100 Hz | 100 Hz | 100 Hz |

The 100× GPS overshoot was real CPU load — a thousand capnp messages a second — and it starved
the fixed-size (512 checkpoint) EKF rewind buffer `locationd` needs for the backdated
`cameraOdometry` observations.

</details>

<details>
<summary><b>Engagement: the pcmCruise deadlock</b></summary>

`HONDA_CIVIC_2022` without alpha-long has `pcmCruise=True`. opendbc's Honda `carstate.py` sets
`cruiseState.enabled` straight from the `ACC_STATUS` CAN signal, and openpilot only engages
*after* seeing that go true — it expects the car's own ACC to switch on by itself, like a real
Honda's cruise button does.

So `ACC_STATUS` has to be driven directly from the cruise button (`self.acc_engaged` in
`beamngd`), never from `selfdriveState.active`. Sourcing it from `active` is circular: nothing
is ever first to flip `cruiseState.enabled`, and openpilot can never engage no matter how many
times you press the button.

Separately, `AlphaLongitudinalEnabled` must be set *before* the stack starts, or
`openpilotLongitudinalControl` stays false and openpilot steers while waiting for an ACC that
doesn't exist.

</details>

<details>
<summary><b>Bosch vs non-Bosch cruise speed</b></summary>

This car is Bosch, which takes a completely different branch in `carstate.py`:

```python
if self.CP.flags & HondaFlags.BOSCH:
    acc_hud = cp_cam.vl["ACC_HUD"] if BOSCH_RADARLESS else cp.vl["ACC_HUD"]
    ret.cruiseState.speed = ... acc_hud["CRUISE_SPEED"] ...
else:
    ret.cruiseState.speed = cp.vl["CRUISE"]["CRUISE_SPEED_PCM"] * CV.KPH_TO_MS
```

The set speed comes from `ACC_HUD.CRUISE_SPEED` **on the camera bus**. `CRUISE_SPEED_PCM` is
only read for non-Bosch Hondas — populating it does nothing here.

</details>

<details>
<summary><b>Don't echo openpilot's own pedal back at it</b></summary>

`electrics.values.throttle` reports the vehicle's current pedal state. While engaged, that *is*
openpilot's own `input.event` command coming back around. Reporting it as the driver's pedal
sets `CS.gasPressed`, which trips the rising-edge `pedalPressed` check in `selfdrived.py` and
disengages the instant openpilot tries to accelerate.

While engaged there's no separate user pedal input to report anyway — `pollControl()` calls
`input.event(FILTER_DIRECT)` every tick, overwriting whatever the player's keys set.

</details>

<details>
<summary><b>Camera timestamps</b></summary>

Frames must carry `time.monotonic_ns()`, not a synthetic `frame_id × dt` counter.
`locationd._validate_timestamp` compares camera time against the Kalman filter's elapsed time
and rejects fake values on every single frame, forever, silently blocking
`deviceMotion.inputsOK`.

This same bug exists unfixed in openpilot's own `system/camerad/webcam/camerad.py` — it just
never runs there, since that driver is disabled.

</details>

<details>
<summary><b>Lua gotchas</b></summary>

- **Declare locals above every function that references them.** Otherwise Lua silently creates
  an implicit global instead of closing over the variable, and state resets mysteriously.
- **`releaseControl()` must only fire on the disengage edge.** `beamngd` sends a control packet
  every tick regardless of engagement, so "not engaged" is the constant common case. Zeroing
  `input.event` every not-engaged tick fights — and beats — the player's own WASD input.
- **Bind the control socket lazily.** `init()` runs once per spawned vehicle, including traffic
  AI, so binding there makes every car in the scene race for the same port. Bind from
  `pollControl()`, which only the player-seated vehicle reaches.

</details>

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Won't engage at all | Below 20 mph, or `cruiseState.enabled` never went true | Check `carState` in the monitor |
| Engages then immediately disengages | `gasPressed` / `brakePressed` stuck true, or `commIssue` | Monitor → check the alert text |
| Steers but runs wide on corners | Steering lock wrong, or limits binding | Set `BEAMPILOT_STEER_LOCK_DEG`; check the BINDING line |
| Weaving / oscillation | `BEAMPILOT_MAX_LAT_JERK` too high | Lower it toward 5.0 first |
| Doesn't accelerate | `openpilotLongitudinalControl` false | Confirm `AlphaLongitudinalEnabled` was set at launch |
| Brakes too early for traffic | Follow distance | `BEAMPILOT_PERSONALITY=0` |
| Lane change arms but never commits | Needs the steering nudge | `BEAMPILOT_AUTO_LANE_CHANGE=1` |
| Bursts of `observation too old` | `modeld` behind, GPU contention | Lower BeamNG's graphics settings |
| `Address already in use` on 49152 | A previous `beamngd` still running | `pkill -f beamngd.py` |
| Nothing publishing at all | Stack not running | `tools/beampilot_diag.py` |
| Steering suddenly odd for no clear reason | Stale state somewhere in the stack | **Restart openpilot** (Ctrl+C, relaunch). BeamNG can keep running. |
| **Camera frames are solid green** | The capture returned no data — almost always the X11 backend on Wayland | `BEAMPILOT_CAPTURE_BACKEND=portal` — see [Wayland](#wayland) |
| Camera frames are black | Capturing a blank region or an idle source | Check the region/monitor, or which source the portal is sharing |
| It's capturing the wrong window | Ambiguous title match | Run `tools/beampilot_setup.py` to list candidates; pin it with `BEAMPILOT_CAM_REGION`, or use the portal backend and pick it from the dialog |
| Model sees nothing / drives blind | Capturing the wrong monitor | Set `BEAMPILOT_CAM_WINDOW=beamng`, or fix `BEAMPILOT_CAM_MONITOR` |
| "no BeamNG window found" | Several unrelated causes | `python -m openpilot.selfdrive.beamcamd.window_capture` names the actual one |
| The portal keeps asking which window to share | The restore token isn't being saved | Check `~/.local/state/beampilot/` is writable |
| Want to change the shared source | The choice is remembered | `rm ~/.local/state/beampilot/screencast_restore_token`, relaunch |
| `modeld` not running, no other error | tinygrad picked a GPU it can't drive | See [GPU selection](#gpu-selection) |
| `beamcamd` exits on an X11 protocol error | Capture rectangle outside the screen | Fixed — regions are clamped. If it recurs, report the region it logs |

> [!TIP]
> **If the driving or steering goes strange for no obvious reason, restart openpilot before
> investigating.** Leave BeamNG running — it's the openpilot side that accumulates state
> (calibration, the learned torque parameters, the localisation filter), and a fresh start clears
> it. This genuinely fixes a surprising share of "it was fine yesterday" problems, and it's much
> quicker than debugging.

> [!TIP]
> Almost every other question here is answered by `tools/beampilot_monitor.py`. Rates tell you
> what's alive; the `carState` block tells you what openpilot believes; the BINDING line tells you
> whether a limit is the constraint. Guessing is slower.

## Known problems

### No wide-angle camera

openpilot expects two road cameras: a narrow one and a roughly 120° wide one. `beamcamd` has
only the single game view, so it publishes **the same frame to both streams**. `modeld` computes
`has_wide_camera = use_extra_client or main_wide_camera`, which is `True` here, so it applies
`dc.wide_road.intrinsics` — a wide-lens calibration — to an image that doesn't have that field
of view.

The model therefore misjudges how far objects and lane lines sit laterally. **This is worst in
turns**, where the real wide camera is what sees around the corner.

> [!WARNING]
> **Keep Experimental mode off.** Its end-to-end longitudinal policy leans much harder on
> wide-camera scene understanding and behaves noticeably worse here.

Proper fix: a second BeamNG camera at a genuinely wide FOV published separately to
`VISION_STREAM_WIDE_ROAD`. The existing `openpilot_cam` mod shows how to register a
rigidly-mounted FOV-matched camera; this needs a second one plus a second capture region.

### Steering geometry mismatch

openpilot computes a steering angle from the **Civic's** steer ratio (15.38) and wheelbase
(2.700 m). Your BeamNG car has neither. Only the steering lock has been measured and made
configurable, so commanded curvature and achieved curvature still don't match and the car runs
wide. The real fix is calibrating the mapping against measured yaw rate, which the telemetry
already carries.

### Smaller things

- `beamcamd` drops ~6% of frames.

**Already fixed**, listed because the symptoms are worth recognising:

- **`vCruise` reporting a nonsense set speed.** It never did — `vCruise` is in km/h and the
  monitor was treating it as m/s, printing 30 m/s as "241 mph".
- **Driver monitoring at 33 Hz instead of 20.** A `now - last > interval` gate quantises to the
  caller's tick; at 100 Hz a 50 ms gate fires every 60 ms (16.7 Hz), and halving the interval to
  compensate overshot to 33 Hz. Now a phase accumulator: 20.0 Hz exactly.
- **Gear, parking brake and steering rate never reaching openpilot.** The mod had been sending
  the first two all along, nothing read them, and nothing produced a steering rate at all. So
  `wrongGear`/`reverseGear` could never fire and openpilot would engage in reverse.
- **Roll hardcoded to zero** in the curvature-to-steering conversion, under a comment saying
  BeamNG didn't send it. It had been sending it the whole time.
- **Capture aspect ratio** — see [Aspect ratio](#aspect-ratio).

## Safety

> [!CAUTION]
> **Simulation only.** Driver monitoring (`dmonitoringmodeld`/`dmonitoringd`) is removed and
> several safety alerts are suppressed. Disabling driver monitoring in a real vehicle is
> dangerous and violates comma's requirements. Do not put this in a car.

## Differences from [ko6lvm/beampilot](https://github.com/ko6lvm/beampilot)

Ryan's beampilot established the architecture this builds on: the process list, the
`beamngd`/`beamcamd` split, the config and launch scripts, and the plan for a BeamNG bridge.
Both daemons were stubs at that point, marked *"currently inop"* — `beamngd` published zeroed
CAN and `beamcamd` published a black frame.

About **1,650 lines across 22 files**.

<details>
<summary><b>Rewritten or new</b></summary>

- **`beamngd`** — parses live telemetry, synthesises Honda CAN, reads cruise buttons over
  `evdev`, converts desired curvature to BeamNG steering via `VehicleModel`.
- **`beamcamd`** — real capture pipeline (mss + OpenCV NV12). Meets its 20 Hz target; was
  managing 15.3 Hz before optimisation.
- **The BeamNG mods** — `beampilot_bridge` (telemetry and control) is new; `openpilot_cam` (the
  rigid, FOV-matched camera) is now bundled in the repo and installed automatically, rather than
  being a separate thing you had to already have.
- **`abeamngd.py`** — deleted; depended on the unavailable `beamngpy`.
- **Window-tracking capture** — `window_capture.py` finds the game window instead of grabbing a
  whole monitor, and follows it as it moves.
- **Tooling** — `beampilot_setup.py` (guided install and system check), `beampilot_tui.py`
  (config), `beampilot_monitor.py` and `beampilot_diag.py` (diagnostics).

</details>

<details>
<summary><b>Bugs found and fixed</b></summary>

- **`calibrationd` never converged.** It needs minutes of sustained straight highway driving
  before it trusts its extrinsics; until then the model's road frame is wrong, which reads as
  steering bias rather than an error. Now seeded at launch.
- **Message rates were badly off** — GPS at 1000 Hz against an expected 10, IMU at 5×,
  `pandaStates` 5× too slow.
- **Cruise speed always zero** — wrong CAN message for a Bosch car.
- **No longitudinal control at all** — `AlphaLongitudinalEnabled` unset, so `pcmCruise=True`.
- **Throttle feedback loop** — openpilot's own pedal command echoed back as driver input,
  disengaging it the instant it accelerated.
- **Synthetic camera timestamps** — `locationd` rejected every frame.
- **Controls Mismatch** — `pandaStates` hardcoded safety flags instead of mirroring
  `carParams.safetyConfigs`.

</details>

### Process changes vs. stock openpilot

**Removed** (hardware-specific or pointless on a desktop): `camerad`, `webcamerad`, `sensord`,
`pandad`, `_pandad`, `micd`, `dmonitoringmodeld`, `dmonitoringd`, `updated`, `qcomgpsd`,
`ubloxd`, `pigeond`, `modem`

**Added:** `beamngd` (telemetry, fake sensors, control — 100 Hz) and `beamcamd` (camera — 20 Hz)

Everything else is stock openpilot, with the exception of a handful of small, env-gated patches
— `desire_helper.py` (auto lane change), `drive_helpers.py` and `longitudinal_planner.py` (the
driving limits), `selfdrived.py` (`commIssue`) and `card.py` (the blind spot overlay). Each
defaults to upstream behaviour when its variable is unset; `grep -rn BEAMPILOT_ openpilot/`
lists every one.

## Development

`CLAUDE.md` carries the architecture notes and the full gotcha list. Read it before touching
`beamngd`, `beamcamd` or the Lua mod.

Mod edits under `tools/beamng_mod/` apply live — reload with <kbd>Ctrl</kbd>+<kbd>L</kbd> in game.

```bash
uv run ruff check .
```

> [!NOTE]
> Pushes from this fork need `git push --no-verify`. openpilot's inherited `.lfsconfig` points
> LFS at comma's own GitLab over SSH, which nobody else can authenticate to. Nothing here is
> LFS-tracked, so skipping the hook is lossless — the LFS *fetch* URL stays public, so clones
> still pull openpilot's model weights normally.

## Credits

Built on [**openpilot**](https://github.com/commaai/openpilot) by comma.ai.
Forked from [**ko6lvm/beampilot**](https://github.com/ko6lvm/beampilot).
[**jackz314/openpilot**](https://github.com/jackz314/openpilot)'s ETS2 bridge was useful prior art.

MIT, inherited from openpilot. See [LICENSE](LICENSE).
