#!/usr/bin/env python3
"""Setup and configuration front-end for beampilot.

Edits config_beampilot.sh in place -- that file stays the single source of
truth, so everything here can also be done by hand in a text editor, and a
config edited by hand shows up correctly the next time this runs.

  uv run python tools/beampilot_tui.py

Arrow keys / j / k to move, Enter or space to change a setting, s to save,
r to run setup, L to launch, m to open the monitor, q to quit.
"""
import curses
import os
import re
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO, "config_beampilot.sh")


@dataclass
class Setting:
  key: str
  label: str
  help: str
  # What the CODE does when this variable is unset -- NOT what the shipped
  # config recommends. The two are different for several settings, and getting
  # it wrong makes this screen lie: a key absent from the config is displayed
  # as this value, so a wrong one shows a feature as on when it runs off.
  # test_beampilot_tui.py checks every one of these against the env_bool/
  # env_float/env_int/env_str call it comes from, so they cannot drift again.
  default: str
  # choices: cycle through fixed values. None means free text entry.
  choices: list[str] | None = None
  # values that are numbers we nudge with left/right rather than cycle
  numeric: bool = False
  step: float = 0.5
  section: str = ""
  # warn shown in red when the value is not the default
  warn: str = ""
  # for the few defaults the code derives from another setting
  derive: "Callable[[dict[str, str]], str] | None" = None
  # what to SHOW for each stored value, when the stored value is a bare number
  # that means something. A plain 0/1 toggle gets on/off automatically.
  value_labels: dict[str, str] | None = None

  def default_for(self, values: dict[str, str]) -> str:
    return self.derive(values) if self.derive else self.default

  def is_default(self, value: str, values: dict[str, str]) -> bool:
    """Numeric settings compare by value, so 4 and 4.0 are the same setting."""
    other = self.default_for(values)
    if value == other:
      return True
    if self.numeric:
      try:
        return float(value) == float(other)
      except ValueError:
        return False
    return False


@dataclass
class Section:
  name: str
  blurb: str
  settings: list[Setting] = field(default_factory=list)


def _as_float(raw: str | None, fallback: float) -> float:
  try:
    return float(raw)  # type: ignore[arg-type]
  except (TypeError, ValueError):
    return fallback


def nvidia_compute_caps() -> list[tuple[str, float]]:
  """[(index, compute capability)] in nvidia-smi order."""
  caps = []
  if not shutil.which("nvidia-smi"):
    return caps
  try:
    out = subprocess.run(["nvidia-smi", "--query-gpu=index,compute_cap", "--format=csv,noheader"],
                         capture_output=True, text=True, timeout=5)
    if out.returncode == 0:
      for line in out.stdout.strip().splitlines():
        idx, _, cap = line.partition(",")
        try:
          caps.append((idx.strip(), float(cap)))
        except ValueError:
          continue
  except (subprocess.SubprocessError, OSError):
    pass
  return caps


def gpu_options() -> list[str]:
  """Detected first, so the list reflects what this machine can actually use.

  Four tinygrad backends, and the difference between them is not speed, it is
  which cards they will open at all:

    nv    tinygrad's own NVIDIA driver, raw ioctls. Ampere or newer ONLY -- it
          implements the Ampere command classes and nothing older, so a Turing
          card dies at model load with a bare StopIteration.
    amd   the same for AMD: gfx942, gfx950, gfx11xx, gfx12xx.
    cuda  NVIDIA's own libcuda/nvrtc. Every NVIDIA card back to Maxwell,
          tensor cores included. The right answer for a pre-Ampere card.
    cl    OpenCL. Anything with an ICD, any vendor. No tensor core support in
          tinygrad, which costs little on the small model and a lot on chestnut.
  """
  caps = nvidia_compute_caps()
  opts = []
  if caps:
    if any(c >= 8.0 for _, c in caps):
      opts.append("nvidia")
  amd = False
  try:
    lspci = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
    amd = lspci.returncode == 0 and bool(re.search(r"VGA.*\b(AMD|ATI|Radeon)\b", lspci.stdout, re.I))
  except (subprocess.SubprocessError, OSError):
    pass
  if amd:
    opts.append("amd")
  if caps:
    opts.append("cuda")
  if caps or amd or os.path.isdir("/etc/OpenCL/vendors"):
    opts.append("opencl")
  return opts or ["nvidia", "amd", "cuda", "opencl"]


def gpu_list() -> list[str]:
  """Per-device names, indexed the way tinygrad's DEV=NV:n selects them."""
  names = []
  try:
    out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                         capture_output=True, text=True, timeout=5)
    if out.returncode == 0:
      names += [line.strip() for line in out.stdout.strip().splitlines() if line.strip()]
  except (subprocess.SubprocessError, OSError):
    pass
  try:
    lspci = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
    if lspci.returncode == 0:
      for line in lspci.stdout.splitlines():
        if re.search(r"VGA.*\b(AMD|ATI|Radeon)\b", line, re.I):
          names.append(line.split(":", 2)[-1].strip()[:48])
  except (subprocess.SubprocessError, OSError):
    pass
  return names


def gpu_detail() -> str:
  names = gpu_list()
  if not names:
    return "none detected"
  return "; ".join(f"[{i}] {n}" for i, n in enumerate(names))


def build_sections() -> list[Section]:
  gpus = gpu_options()
  devices = gpu_list()
  device_choices = [str(i) for i in range(max(len(devices), 1))]
  caps = dict(nvidia_compute_caps())
  device_help = ("Which physical GPU runs the model, if you have more than one: "
                 + (", ".join(f"[{i}] {n}" + (f" (compute {caps[str(i)]:g})" if str(i) in caps else "")
                              for i, n in enumerate(devices)) if devices else "none detected")
                 + ". Same numbering as nvidia-smi for every backend. Blank auto-detects the"
                 + " first card the chosen backend can actually drive."
                 + " BUILD-time -- re-run setup after changing.")
  return [
    Section("Hardware", "What this machine has, and which model to run.", [
      Setting("BEAMPILOT_GPU", "GPU backend",
              "Which tinygrad backend runs the driving model. nvidia and amd are tinygrad's own"
              + " drivers -- fastest, but nvidia needs compute 8.0 (Ampere) or newer and amd needs"
              + " gfx11xx/gfx12xx/gfx942/gfx950; an older card does not run slowly, it dies at"
              + " model load. cuda is the compatibility option for a pre-Ampere NVIDIA card"
              + " (works back to Maxwell, tensor cores and all). opencl is the widest net, any"
              + " vendor, but tinygrad's OpenCL has no tensor cores -- fine for the standard"
              + " model, costly for chestnut. Only backends this machine can use are offered."
              + " BUILD-time -- re-run setup after changing.",
              gpus[0], choices=gpus,
              warn="build-time setting -- rebuild for this to take effect"),
      Setting("BEAMPILOT_GPU_INDEX", "GPU device", device_help, "", choices=[""] + device_choices,
              value_labels={"": "auto"},
              warn="build-time setting -- rebuild for this to take effect"),
      Setting("CHESTNUT", "Chestnut model",
              "Larger, better-driving model. Needs 8GB+ VRAM (standard needs 4GB).",
              "0", choices=["0", "1"]),
      Setting("BIG", "Window size",
              "1 = comma 3/3X window (2160x1080). 0 = comma 4 (536x240) -- tiny on a desktop."
              + " Window scale below resizes whichever of the two you pick.",
              "1", choices=["1", "0"],
              value_labels={"1": "2160x1080", "0": "536x240"}),
      Setting("SCALE", "Window scale",
              "Multiplies the base size BIG selects. Blank fits the window to your smallest monitor in"
              + " EITHER direction -- it shrinks BIG=1 to fit a 1080p screen and grows BIG=0 up from its"
              + " 536x240 postage stamp. Set a number to override.",
              "", numeric=True, step=0.1),
    ]),
    Section("Car", "The car openpilot believes it is driving.", [
      Setting("BEAMPILOT_REPORT_GEAR", "Report the real gear",
              "openpilot raises wrongGear/reverseGear off this, which is what stops it engaging while the"
              + " car rolls backwards. Turn it off if reversing under openpilot is the point -- arcade"
              + " mode, or messing about in a car park. Off pins the gear to drive.",
              "1", choices=["1", "0"]),
      Setting("FINGERPRINT", "Fingerprint",
              "BEAMPILOT is our own opendbc platform: the same Honda Bosch CAN beamngd packs,"
              + " without a Civic's steering rack, weight distribution and engagement speeds"
              + " coming with it. Its geometry is a placeholder -- the mod measures the real"
              + " vehicle. HONDA_CIVIC_2022 is the old behaviour and is still fully supported;"
              + " every engagement-critical value is identical between the two. Anything else"
              + " needs beamngd rewritten.",
              "BEAMPILOT", choices=["BEAMPILOT", "HONDA_CIVIC_2022"],
              warn="beamngd packs Honda Bosch CAN; another car needs code changes"),
      Setting("BEAMPILOT_BEAMNG_GEOMETRY", "Measure the real vehicle",
              "The mod measures the vehicle BeamNG actually spawned -- wheelbase, weight"
              + " distribution, mass, yaw inertia, steering lock, and the rack ratio -- and sends"
              + " them over, so openpilot steers the car in front of you instead of a Honda."
              + " THE setting for a car that understeers through corners when raising the lateral"
              + " limit changed nothing. Needs the mod reinstalled (./setup_beampilot.sh); with an"
              + " old mod nothing arrives and it falls back to the Civic on its own."
              + " Off restores exactly the pre-measurement behaviour.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_LIVE_STEER_PARAMS", "Follow the live estimate",
              "paramsd estimates the steering ratio and tyre stiffness from how the car actually"
              + " responds, and stock openpilot feeds that back every tick. When the mod has"
              + " measured a ratio, that one is pinned instead and only the tyre stiffness is"
              + " followed -- geometry beats inference. Off freezes both at their static values.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_STEER_RATIO", "Steering ratio",
              "Degrees of steering wheel per degree of road wheel. Blank is right for almost"
              + " everyone: the mod measures it once from the real wheel angle against the real"
              + " road wheel angle, remembers it per vehicle and per rack, and until then"
              + " estimates it from the car's steering lock. Setting it here pins ONE value for"
              + " every vehicle and ignores all of that -- the Civic's is 15.38. To correct a"
              + " single car instead, edit the remembered value in the cache file below.",
              "", numeric=True, step=0.5),
      Setting("BEAMPILOT_SEED_STEER_RATIO", "Estimate the ratio from the lock",
              "Before a vehicle has ever been measured, work its steering ratio out from its"
              + " steering lock instead of using the Civic's 15.38. The lock is known the instant"
              + " the car spawns and ranges from 270 to 900 degrees across BeamNG's vehicles,"
              + " while the road wheel angle at full lock is set by the suspension and barely"
              + " varies -- so the ratio is very nearly proportional to it. Off falls back to the"
              + " fingerprint's value.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_STEER_RATIO_CACHE", "Remembered ratios",
              "Where measured steering ratios are saved, keyed by vehicle AND steering lock"
              + " (BeamNG's racks are parts, not car properties). Plain JSON, meant to be edited:"
              + " if one car measured badly, fix its number here rather than pinning every car.",
              "~/.config/beampilot/steer_ratios.json"),
      Setting("BEAMPILOT_WHEELBASE_M", "Wheelbase (m)",
              "Front axle to rear axle. Blank means measured. Set it to pin one; the Civic's is 2.70."
              + " Much less important than the steering ratio -- it mostly affects the"
              + " speed-dependent understeer term.",
              "", numeric=True, step=0.05),
      Setting("BEAMPILOT_CENTER_TO_FRONT_M", "Centre of gravity to front axle (m)",
              "Where the weight sits between the axles, which is what makes a car understeer or"
              + " oversteer. Blank means measured from every node's mass. Civic 1.08.",
              "", numeric=True, step=0.05),
      Setting("BEAMPILOT_MASS_KG", "Mass (kg)",
              "Blank means measured. Civic 1462.",
              "", numeric=True, step=25.0),
      Setting("BEAMPILOT_ROTATIONAL_INERTIA", "Yaw inertia (kg m2)",
              "How hard the car is to rotate about its vertical axis. Blank means measured, which"
              + " is a real integral over the body -- openpilot's own value is extrapolated from a"
              + " Civic's mass and wheelbase. Civic 2500.",
              "", numeric=True, step=100.0),
      Setting("BEAMPILOT_STEER_LOCK_DEG", "Steering lock (deg)",
              "Centre to full lock: the divisor that turns a steering wheel angle into BeamNG's"
              + " -1..1 input. Too low oversteers, too high runs wide. Leave it alone and the mod"
              + " reads the real one out of the spawned vehicle's jbeam, which is exact and"
              + " per-vehicle; 510 is only the fallback for when nothing reports one. Change it"
              + " here to pin one value for every car -- beamngd then warns at startup if it is"
              + " more than 10% off what the car actually has.",
              "510.0", numeric=True, step=10.0),
      Setting("BEAMPILOT_CALIBRATION", "Calibration",
              "instant = start already calibrated at a level pose, usable right away. "
              + "live = converge from real driving first; won't engage until it has.",
              "instant", choices=["instant", "live"]),
    ]),
    Section("Driving limits", "Stock openpilot follows EU/ISO comfort limits. These are often why it won't corner.", [
      Setting("BEAMPILOT_MAX_LAT_ACCEL", "Lateral accel (m/s2)",
              "Turning. Max curvature is accel/v^2 -- stock 3.0 allows only a ~300m radius at 67mph."
              + " The shipped config raises this to 5.0.",
              "3.0", numeric=True, step=0.5),
      Setting("BEAMPILOT_MAX_LAT_JERK", "Lateral jerk (m/s3)",
              "How fast curvature may change. The most likely cause of weaving if raised too far."
              + " The shipped config raises this to 8.0.",
              "5.0", numeric=True, step=0.5),
      Setting("BEAMPILOT_ACCEL_SCALE", "Accel scale",
              "Multiplier on the acceleration envelope. 1.0 is stock; the shipped config uses 2.0.",
              "1.0", numeric=True, step=0.25),
      Setting("BEAMPILOT_DECEL_SCALE", "Decel scale",
              "Same for braking. 1.0 is stock; the shipped config uses 1.5.",
              "1.0", numeric=True, step=0.25),
      Setting("BEAMPILOT_PERSONALITY", "Following distance",
              "0 aggressive (1.25s, brakes latest) / 1 standard (1.45s) / 2 relaxed (1.75s).",
              "0", choices=["0", "1", "2"],
              value_labels={"0": "aggressive", "1": "standard", "2": "relaxed"}),
      Setting("BEAMPILOT_STEER_SWEEP_SECONDS", "Steering response (s)",
              "Lock-to-lock sweep time. Lower is snappier but twitchier.",
              "0.15", numeric=True, step=0.05),
      Setting("BEAMPILOT_CURVE_SLOWDOWN", "Slow for corners (test)",
              "Stock openpilot holds the set speed through a bend and only caps acceleration once"
              + " already in it. With a narrow camera the model sees a corner late, so the car carries"
              + " too much speed in and runs wide. This brakes for it beforehand, using the curvature"
              + " the model has already predicted. OFF by default -- it is a planning layer stock"
              + " openpilot does not have, so drive the same road both ways and compare.",
              "0", choices=["0", "1"],
              warn="experimental: adds longitudinal planning openpilot does not normally do"),
      Setting("BEAMPILOT_CURVE_LAT_ACCEL", "Cornering accel (m/s2)",
              "What lateral acceleration to aim for in a corner, which sets the speed it slows to."
              + " Defaults to 0.7x the hard lateral limit so there is something in reserve mid-corner."
              + " Higher corners faster.",
              "2.1", numeric=True, step=0.1,
              derive=lambda v: f"{round(0.7 * _as_float(v.get('BEAMPILOT_MAX_LAT_ACCEL'), 3.0), 3):g}"),
      Setting("BEAMPILOT_ACTUATION_MARGIN", "Actuation headroom",
              "How much room the excessive-actuation check keeps above what the limits above allow."
              + " It soft-disables on MEASURED actuation, so left at stock it becomes a ceiling on the"
              + " limits rather than a net under them -- raise the lateral limit past it and the car"
              + " hands back control mid-corner instead of cornering harder. Never drops below stock.",
              "2.0", numeric=True, step=0.25),
      Setting("BEAMPILOT_MAX_CURVATURE", "Max curvature (1/m)",
              "Hard geometric cap on turn tightness. Only binds below ~11mph; 0.2 = a 5m radius.",
              "0.2", numeric=True, step=0.05),
    ]),
    Section("Controls", "Keys are read from the keyboard directly, since BeamNG holds focus.", [
      Setting("BEAMPILOT_KEY_SET", "Set / engage key", "Check BeamNG's own bindings before rebinding.", "i"),
      Setting("BEAMPILOT_KEY_RESUME", "Resume / speed up key", "", "o"),
      Setting("BEAMPILOT_KEY_CANCEL", "Cancel key", "", "u"),
      Setting("BEAMPILOT_CRUISE_STEP_MPH", "Speed step (mph)", "Per-tap speed change.", "1.0", numeric=True, step=1.0),
      Setting("BEAMPILOT_AUTO_LANE_CHANGE", "Auto lane change",
              "Signal alone commits the change. Required here -- there is no wheel to nudge, so with"
              + " this off (the code default) a signalled lane change never commits. The shipped config"
              + " turns it on.",
              "0", choices=["1", "0"]),
      Setting("BEAMPILOT_CONTROL_MODE", "Control mode",
              "lua injects into the game directly. joystick needs axes bound in BeamNG's Options > Controls.",
              "lua", choices=["lua", "joystick"]),
    ]),
    Section("Blind spot", "The mod sees every vehicle in the scene, so openpilot can refuse to move into one.", [
      Setting("BEAMPILOT_BSM", "Blind spot monitoring",
              "Detected in the BeamNG mod, so it works off the simulator's own traffic rather than the camera."
              + " Blocks a signalled lane change and shows \"Car Detected in Blindspot\"."
              + " Needs the mod reinstalled if you are upgrading from an older build.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_LANE_CHANGE_ABORT", "Cancel lane change",
              "Abort a lane change already under way if the target lane fills up. Stock openpilot only"
              + " checks the blind spot on the way in and never looks again. Holds the blinker armed and"
              + " re-commits by itself once the lane clears.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_LANE_CHANGE_ABORT_S", "Cancel window (s)",
              "How late into the move a cancel may still fire. Past the halfway point the car is mostly"
              + " in the new lane and swerving back is its own hazard. Set very high to allow it any time.",
              "2.0", numeric=True, step=0.5),
      Setting("BEAMPILOT_SIGNAL_AUTO_CANCEL", "Cancel signal after a change",
              "Switch the blinker off once a lane change finishes. Nothing in the game cancels an"
              + " indicator that was never physically stalked. A change cancelled by the blind spot"
              + " deliberately keeps its signal on, which is what lets it resume.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_BSM_APPROACHING", "Warn on closing traffic",
              "Also count a car that is not beside you yet but is closing fast enough to be there mid-manoeuvre.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_BSM_WIDTH_M", "Zone width (m)",
              "How far out from your flank the zone reaches. 3.6 is about one lane; raise it for wide lanes.",
              "3.6", numeric=True, step=0.2),
      Setting("BEAMPILOT_BSM_REAR_M", "Zone rear reach (m)",
              "How far past your rear bumper the zone extends.",
              "4.0", numeric=True, step=0.5),
      Setting("BEAMPILOT_BSM_INDICATOR", "Mirror lamps",
              "Amber chevrons at the edges of the road view. Off by default because the radar markers"
              + " already occupy that space. The blind spot still gates lane changes and still raises"
              + " its alert either way. Worth turning on if you run with the radar off.",
              "0", choices=["0", "1"]),
      Setting("BEAMPILOT_BSM_DEBUG", "Log state changes",
              "Prints every blind spot change to the beamngd terminal and the BeamNG console. For tuning the zone.",
              "0", choices=["0", "1"]),
    ]),
    Section("Radar", "Where the car in front actually is, from the simulator rather than from the camera.", [
      Setting("BEAMPILOT_RADAR", "Ground-truth radar",
              "Reports nearby traffic as radar points. This car is radarless, so with this off openpilot"
              + " finds the lead with the camera alone -- the same camera that is fed the wrong intrinsics,"
              + " and distance to the car in front is exactly what that gets wrong. OFF by default: it is"
              + " the simulator's object list, not a sensor. Needs the mod reinstalled if you are"
              + " upgrading from an older build.",
              "0", choices=["0", "1"]),
      Setting("BEAMPILOT_RADAR_LEADS", "Radar can find a lead alone",
              "Let a track become the lead with no confirmation from the camera. Off (default) means radar"
              + " only refines a lead the camera already found -- accurate distance, same timing. ON hands"
              + " openpilot leads the camera never saw, so it starts managing distance much earlier, which"
              + " from the seat reads as braking absurdly early for cars still a long way off.",
              "0", choices=["0", "1"]),
      Setting("BEAMPILOT_RADAR_ONCOMING", "Report oncoming traffic",
              "Off by default. An approaching car is not a lead, and on a narrow road the in-path test is"
              + " quite capable of picking one -- which is a hard-braking event for a car that was going to"
              + " pass on the other side anyway. A STATIONARY car facing you is still reported: that is a"
              + " breakdown in your lane.",
              "0", choices=["0", "1"]),
      Setting("BEAMPILOT_RADAR_OCCLUSION", "Require line of sight",
              "Drop anything hidden behind a hill or a building. Static geometry only, so a car does not"
              + " hide the car behind it -- real radar sees under and around one. This is the big one for"
              + " realism: without it the radar reads straight through terrain.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_RADAR_NOISE_M", "Range noise (m)",
              "Real radar is not exact, and openpilot's Kalman filter is built expecting it not to be."
              + " 0 gives you the simulator's exact answer.",
              "0.12", numeric=True, step=0.05),
      Setting("BEAMPILOT_RADAR_LEAD_HALF_WIDTH_M", "In-lane width (m)",
              "How far off the model's predicted path a track may sit and still count as being in your"
              + " lane. Measured against the path, so it follows a bend. Wider risks braking for the"
              + " next lane over; narrower risks missing your own lead on a curve.",
              "1.8", numeric=True, step=0.2),
      Setting("BEAMPILOT_RADAR_RANGE_M", "Radar range (m)",
              "Deliberately shorter than real radar reaches. The camera would never have seen a lead at"
              + " 150 m, so handing openpilot one changes when it starts managing distance -- which reads"
              + " as braking absurdly early.",
              "110", numeric=True, step=10.0),
      Setting("BEAMPILOT_RADAR_HALF_WIDTH_M", "Beam half-width (m)",
              "How wide the beam is at your bumper, before it spreads. Narrow enough not to fill the track"
              + " list with the next carriageway; wide enough to hold your own lane round a bend.",
              "3.0", numeric=True, step=0.5),
      Setting("BEAMPILOT_RADAR_MAX_TRACKS", "Max tracks",
              "Nearest first; anything past this is dropped. Hard ceiling of 24 in the wire format.",
              "12", numeric=True, step=1.0),
      Setting("BEAMPILOT_RADAR_INDICATOR", "Show tracks on screen",
              "A cyan diamond on the road under every radar track, ringed on whichever one radard picked"
              + " as the lead. Projected through the same calibration as the model's path. This is how you"
              + " tell 'no traffic' apart from 'the feed is dead' -- a missing lead chevron cannot.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_RADAR_DEBUG", "Log the nearest track",
              "Prints the closest track to the BeamNG console every scan. Noisy in traffic; for checking"
              + " the mod sees what you think it does.",
              "0", choices=["0", "1"]),
    ]),
    Section("Camera", "beamcamd captures the BeamNG window off your desktop.", [
      Setting("BEAMPILOT_CAPTURE_BACKEND", "Capture backend",
              "auto picks X11 grabbing on X11 and the desktop portal on Wayland. portal asks you to"
              + " pick the window once (a share dialog, remembered afterwards) and streams it over"
              + " PipeWire -- required on Wayland, and on X11 it is smoother and skips window"
              + " detection entirely, at the cost of needing gst-launch-1.0 and a desktop portal."
              + " x11 forces the classic grab; on Wayland that yields all-green frames.",
              "auto", choices=["auto", "x11", "portal"]),
      Setting("BEAMPILOT_CAM_ASPECT", "Aspect handling",
              "openpilot's frame is 1.596 wide; a full-screen 16:9 window is 1.778, so the picture gets"
              + " squeezed ~11% horizontally and everything reads as closer to the centre of the lane"
              + " than it is. crop trims the sides first, leaving exactly the 40.01 deg the model's"
              + " intrinsics assume. stretch is the old behaviour. Better than either: size the BeamNG"
              + " window 1928x1208, then nothing is cropped OR resampled.",
              "crop", choices=["crop", "stretch"]),
      Setting("BEAMPILOT_CAM_WINDOW", "Track window",
              "Match text for the BeamNG window; it follows the window as it moves or resizes. Blank = capture a whole monitor. Needs X11 and xdotool.",
              "beamng"),
      Setting("BEAMPILOT_CAM_MONITOR", "Monitor",
              "Fallback when no window is tracked or found. 1 is the first monitor.", "1", choices=["1", "2", "3", "0"]),
      Setting("BEAMPILOT_CAM_REGION", "Capture region",
              "left,top,width,height. Overrides both of the above with a fixed rectangle.", ""),
    ]),
    Section("Alerts", "What openpilot complains about. See the README before turning these off.", [
      Setting("BEAMPILOT_IGNORE_COMM_ISSUE", "Ignore comm issues",
              "Timing hiccups stop disengaging. Behavioural, not cosmetic -- it will drive on stale data if a process stalls.",
              "1", choices=["1", "0"],
              warn="openpilot will keep driving if modeld stalls"),
      Setting("BLOCK", "Blocked processes", "Comma-separated. soundd mutes the alert chimes.", ",soundd"),
    ]),
    Section("Bridge", "How beamngd talks to the game. Defaults are fine unless something conflicts.", [
      Setting("BEAMPILOT_TICK_HZ", "Bridge rate (Hz)",
              "How often beamngd runs. 100 matches the CAN rate openpilot expects.",
              "100", numeric=True, step=10.0),
      Setting("BEAMPILOT_TELEMETRY_PORT", "Telemetry port",
              "Mod -> beamngd. Must match TELEMETRY_PORT in the Lua mod; vehicle Lua can't read this.",
              "49152", numeric=True, step=1.0,
              warn="change beampilot.lua to match, or telemetry stops"),
      Setting("BEAMPILOT_CONTROL_PORT", "Control port",
              "beamngd -> mod. Must also match the Lua mod.",
              "49153", numeric=True, step=1.0,
              warn="change beampilot.lua to match, or control stops"),
      Setting("BEAMPILOT_BEAMNG_ADDRESS", "BeamNG address",
              "Where the game is. Only change this if BeamNG runs on another machine.",
              "127.0.0.1"),
      Setting("BEAMPILOT_LAUNCH_DELAY", "Launch countdown (s)",
              "Seconds to wait before starting, to tab into the game. 0 = start immediately.",
              "0", numeric=True, step=1.0),
    ]),
  ]


# A settings line is `export KEY=value` at column 0 and nothing else on it.
#
# The indentation matters. config_beampilot.sh is a shell script as well as a
# settings file, and the backend-selection block it ends with is full of lines
# like `export USE_AMD=1; unset USE_NV` and `export DEV=":${IDX}+NV"`. Matching
# those as settings meant reading a value of `1; unset USE_AMD` -- and, on the
# next save, REWRITING the branch as `export USE_AMD=""` and destroying the
# logic. Every real setting in the file sits at column 0; every line of shell
# is inside a block and indented.
SETTING_LINE = re.compile(r'^export\s+([A-Z_][A-Z0-9_]*)=([^;]*)$')


def read_config() -> dict[str, str]:
  """Parse `export KEY="value"` lines. Commented-out lines are treated as unset."""
  values: dict[str, str] = {}
  if not os.path.exists(CONFIG):
    return values
  with open(CONFIG) as f:
    for line in f:
      m = SETTING_LINE.match(line.rstrip("\n"))
      if not m:
        continue
      key, raw = m.group(1), m.group(2).strip()
      raw = raw.split('#')[0].strip()
      if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        raw = raw[1:-1]
      elif raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        raw = raw[1:-1]
      # ${BLOCK},soundd -> ,soundd  (keep it readable in the UI)
      raw = raw.replace('${BLOCK}', '').replace('$BLOCK', '')
      values[key] = raw
  return values


TUI_BLOCK_MARKER = "# --- added by tools/beampilot_tui.py ---"

# Settings whose "unset" behaviour is decided by launch_beampilot.sh rather than
# by a default in the Python. BEAMPILOT_GPU_INDEX is the one that matters: unset
# means the launcher picks a card by compute capability, which is not the same
# thing as picking card 0, so dropping an explicit 0 would quietly change which
# GPU runs the model. These are written out even when they match.
NEVER_PRUNE = {
  "BEAMPILOT_GPU_INDEX", "BEAMPILOT_CAM_MONITOR", "BEAMPILOT_CAM_WINDOW",
  "BEAMPILOT_CAM_REGION", "BEAMPILOT_CAPTURE_BACKEND", "BEAMPILOT_CALIBRATION",
  "BEAMPILOT_IGNORE_COMM_ISSUE", "BEAMPILOT_PERSONALITY", "BEAMPILOT_LAUNCH_DELAY",
  "BIG", "SCALE", "CHESTNUT", "FINGERPRINT", "BLOCK", "USE_NV", "USE_AMD",
  "BEAMPILOT_BACKEND",
}


def write_config(values: dict[str, str], defaults: dict[str, str] | None = None) -> None:
  """Rewrite only the export lines we manage, preserving comments and layout.

  A key left at its default is deliberately NOT written. This screen used to
  materialise every setting it knew about into the file at whatever the default
  was that day, and those lines then outranked the code forever: change a
  default in the source and the config still pinned the old one, while this
  screen -- reading the same file -- cheerfully showed the stale value as
  current. Unset means "whatever the code does", and it has to stay unset to
  keep meaning that.

  Lines already in the hand-written part of the file are left in place even
  when they match the default; they carry the comments that explain them. Only
  the blocks this tool appended are pruned back.
  """
  defaults = defaults or {}
  lines: list[str] = []
  if os.path.exists(CONFIG):
    with open(CONFIG) as f:
      lines = f.readlines()

  seen: set[str] = set()
  out: list[str] = []
  in_tui_block = False
  for line in lines:
    if line.strip().startswith(TUI_BLOCK_MARKER):
      in_tui_block = True
      out.append(line)
      continue
    m = SETTING_LINE.match(line.rstrip("\n"))
    if not m:
      # a non-blank, non-export line ends an appended block
      if line.strip() and not line.strip().startswith("#"):
        in_tui_block = False
      out.append(line)
      continue
    indent, key, rest = "", m.group(1), m.group(2)
    if key not in values:
      out.append(line)
      continue
    seen.add(key)
    val = values[key]
    if in_tui_block and key not in NEVER_PRUNE and key in defaults and val == defaults[key]:
      # back to the default: drop it rather than pinning today's number
      continue
    # Preserve any trailing inline comment -- those carry the units and stock
    # values, and silently eating them would make the file worse every save.
    comment = ""
    hash_idx = rest.find('#')
    if hash_idx != -1:
      comment = "    " + rest[hash_idx:].rstrip()
    if val == "":
      # keep the key discoverable rather than deleting it outright
      out.append(f'{indent}# export {key}=""{comment}\n')
    elif key == "BLOCK":
      out.append(f'{indent}export BLOCK="${{BLOCK}}{val}"{comment}\n')
    else:
      out.append(f'{indent}export {key}="{val}"{comment}\n')

  missing = {k: v for k, v in values.items()
             if k not in seen and v != "" and (k in NEVER_PRUNE or v != defaults.get(k))}
  if missing:
    out.append(f"\n{TUI_BLOCK_MARKER}\n")
    for k, v in missing.items():
      if k == "BLOCK":
        out.append(f'export BLOCK="${{BLOCK}}{v}"\n')
      else:
        out.append(f'export {k}="{v}"\n')

  out = _drop_empty_blocks(out)
  with open(CONFIG, "w") as f:
    f.writelines(out)


def _drop_empty_blocks(lines: list[str]) -> list[str]:
  """Remove an appended block header that no longer has anything under it."""
  out: list[str] = []
  for i, line in enumerate(lines):
    if line.strip().startswith(TUI_BLOCK_MARKER):
      rest = lines[i + 1:]
      has_export = False
      for nxt in rest:
        if nxt.strip().startswith(TUI_BLOCK_MARKER):
          break
        if nxt.startswith("export "):
          has_export = True
          break
        if nxt.strip() and not nxt.strip().startswith("#"):
          break
      if not has_export:
        while out and out[-1].strip() == "":
          out.pop()
        continue
    out.append(line)
  return out


ON_OFF = ("0", "1")

# What the screen calls a backend, and what config_beampilot.sh and the build
# call it. Kept apart because "nvidia" and "amd" are the names people use for
# cards, while nv/amd/cuda/cl are tinygrad's backends -- and two of the four
# NVIDIA options are both "nvidia" to a person.
CHOICE_TO_BACKEND = {"nvidia": "nv", "amd": "amd", "cuda": "cuda", "opencl": "cl"}
BACKEND_TO_CHOICE = {v: k for k, v in CHOICE_TO_BACKEND.items()} | {"opencl": "opencl", "nv": "nvidia"}


def _fit(text: str, width: int) -> str:
  if width <= 1:
    return ""
  return text if len(text) <= width else text[:width - 1] + "\u2026"


def display_value(s: Setting, raw: str) -> str:
  """What goes in the value column.

  A setting that is only ever 0 or 1 reads as on/off. The whole reason this
  screen exists is to answer "is that feature actually off", and a bare 1 in a
  column of other bare 1s does not answer it at a glance. BIG and the
  personality are 0/1 without being switches, so they carry their own labels.
  """
  if s.value_labels and raw in s.value_labels:
    return s.value_labels[raw]
  if not s.value_labels and s.choices and sorted(s.choices) == list(ON_OFF):
    return {"1": "on", "0": "off"}.get(raw, raw or "(unset)")
  return raw if raw != "" else "(unset)"


class Tui:
  def __init__(self, stdscr):
    self.stdscr = stdscr
    self.sections = build_sections()
    self.values = self.load()
    self.on_disk = dict(self.values)
    self.rows = self.flatten()
    self.cursor = next(i for i, r in enumerate(self.rows) if r[0] == "setting")
    self.scroll = 0
    self.status = ""
    self.dirty = False

  # ---------------------------------------------------------------- state --

  def load(self) -> dict[str, str]:
    on_disk = read_config()
    self.in_file = set(on_disk)
    # Resolved against the file, not against each other: BEAMPILOT_CURVE_LAT_ACCEL
    # defaults to 0.7x whatever the lateral limit is, so it has to see the
    # lateral limit the config actually sets.
    self.defaults = {s.key: s.default_for(on_disk)
                     for sec in self.sections for s in sec.settings}
    values = {}
    for sec in self.sections:
      for s in sec.settings:
        if s.key == "BEAMPILOT_GPU":
          # BEAMPILOT_BACKEND is the real setting; USE_NV/USE_AMD are the
          # upstream spelling and only consulted when it is absent, so a config
          # written before this existed still reads correctly.
          backend = (on_disk.get("BEAMPILOT_BACKEND") or "").strip().lower()
          if backend in BACKEND_TO_CHOICE:
            values[s.key] = BACKEND_TO_CHOICE[backend]
          elif on_disk.get("USE_NV") == "1":
            values[s.key] = "nvidia"
          elif on_disk.get("USE_AMD") == "1":
            values[s.key] = "amd"
          else:
            values[s.key] = self.defaults[s.key]
        else:
          values[s.key] = on_disk.get(s.key, self.defaults[s.key])
    return values

  def flatten(self):
    rows = []
    for sec in self.sections:
      rows.append(("section", sec))
      for s in sec.settings:
        rows.append(("setting", s))
    return rows

  def current(self) -> Setting | None:
    kind, obj = self.rows[self.cursor]
    return obj if kind == "setting" else None

  def section_of(self, idx: int) -> Section | None:
    for i in range(idx, -1, -1):
      kind, obj = self.rows[i]
      if kind == "section":
        return obj
    return None

  def refresh_derived(self):
    """A derived default follows the setting it is derived from, live."""
    for sec in self.sections:
      for s in sec.settings:
        if s.derive:
          was = self.defaults.get(s.key)
          now = s.derive(self.values)
          self.defaults[s.key] = now
          if self.values.get(s.key) == was:
            self.values[s.key] = now

  def unsaved(self, key: str) -> bool:
    return self.values.get(key) != self.on_disk.get(key)

  # ------------------------------------------------------------ movement --

  def move(self, delta: int):
    n = len(self.rows)
    for _ in range(n):
      self.cursor = (self.cursor + delta) % n
      if self.rows[self.cursor][0] == "setting":
        return

  def move_page(self, delta: int):
    for _ in range(8):
      nxt = self.cursor + delta
      if not 0 <= nxt < len(self.rows):
        break
      self.cursor = nxt
    if self.rows[self.cursor][0] != "setting":
      self.move(1 if delta > 0 else -1)

  def move_section(self, delta: int):
    n = len(self.rows)
    i = self.cursor
    for _ in range(n):
      i = (i + delta) % n
      if self.rows[i][0] == "section":
        self.cursor = i
        self.move(1)
        return

  def jump_home(self, end: bool = False):
    self.cursor = len(self.rows) - 1 if end else 0
    if self.rows[self.cursor][0] != "setting":
      self.move(-1 if end else 1)

  def find(self):
    needle = self.prompt("find: ")
    if not needle:
      return
    needle = needle.lower()
    n = len(self.rows)
    for step in range(1, n + 1):
      idx = (self.cursor + step) % n
      kind, obj = self.rows[idx]
      if kind != "setting":
        continue
      if needle in obj.label.lower() or needle in obj.key.lower():
        self.cursor = idx
        self.status = f"found {obj.key}"
        return
    self.status = f"nothing matches {needle!r}"

  # --------------------------------------------------------------- edits --

  def cycle(self, s: Setting, delta: int):
    val = self.values.get(s.key, self.defaults.get(s.key, s.default))
    if s.choices:
      try:
        i = s.choices.index(val)
      except ValueError:
        i = 0
      self.values[s.key] = s.choices[(i + delta) % len(s.choices)]
    elif s.numeric:
      try:
        cur = float(val) if val else 0.0
      except ValueError:
        cur = 0.0
      new = max(0.0, cur + delta * s.step)
      self.values[s.key] = f"{new:g}"
    else:
      return
    self.refresh_derived()
    self.dirty = True

  def reset(self, s: Setting):
    self.values[s.key] = self.defaults.get(s.key, s.default)
    self.refresh_derived()
    self.dirty = True
    self.status = f"{s.key} back to its default"

  def prompt(self, label: str, initial: str = "") -> str | None:
    """Read a line, with a hand-rolled editor rather than curses.getstr().

    getstr() does its own line editing, and its erase handling depends on the
    terminal's termios erase character agreeing with what curses expects.
    Backspace simply did nothing here. It also cannot edit text that was
    already on screen, so a pre-filled value could only be appended to -- to
    change one character of a key binding you had to retype the whole thing.

    Returns None if the edit was cancelled, so a caller can tell "escaped" from
    "deliberately cleared".
    """
    buf = list(initial)
    curses.noecho()
    curses.curs_set(1)
    self.stdscr.keypad(True)
    try:
      while True:
        h, w = self.stdscr.getmaxyx()
        room = max(4, w - len(label) - 2)
        text = "".join(buf)
        # Show the tail when the value is longer than the space for it, so the
        # cursor stays visible while typing.
        shown = text[-room:]
        self.stdscr.move(h - 1, 0)
        self.stdscr.clrtoeol()
        self.put(h - 1, 0, (label + shown).ljust(w - 1)[:w - 1], curses.A_REVERSE, w)
        self.stdscr.move(h - 1, min(len(label) + len(shown), w - 2))
        self.stdscr.refresh()

        try:
          ch = self.stdscr.getch()
        except KeyboardInterrupt:
          return None

        if ch in (10, 13, curses.KEY_ENTER):
          return "".join(buf).strip()
        if ch == 27:                       # escape: leave the value alone
          return None
        # Every spelling of backspace. Which one arrives depends on the
        # terminal and on whether keypad translation is on, so accept all of
        # them rather than betting on one.
        if ch in (curses.KEY_BACKSPACE, 127, 8):
          if buf:
            buf.pop()
          continue
        if ch == 21:                       # ctrl-u: clear the line
          buf.clear()
          continue
        if ch == 23:                       # ctrl-w: delete the last word
          while buf and buf[-1] == " ":
            buf.pop()
          while buf and buf[-1] != " ":
            buf.pop()
          continue
        if ch == curses.KEY_RESIZE:
          continue
        if 32 <= ch < 127:                 # printable ASCII
          buf.append(chr(ch))
    finally:
      curses.curs_set(0)

  def edit_text(self, s: Setting):
    raw = self.prompt(f" {s.label} = ", self.values.get(s.key, ""))
    if raw is None:
      return
    self.values[s.key] = raw
    self.refresh_derived()
    self.dirty = True

  def save(self):
    to_write = dict(self.values)
    gpu = to_write.pop("BEAMPILOT_GPU", "nvidia")
    to_write["BEAMPILOT_BACKEND"] = CHOICE_TO_BACKEND.get(gpu, "nv")
    # Kept in step because setup and the openpilot build still read them, and a
    # stale pair contradicting BEAMPILOT_BACKEND is a confusing way to end up
    # building for the wrong card. cuda and cl are NVIDIA-or-anything, so they
    # ride under USE_NV.
    to_write["USE_AMD"] = "1" if gpu == "amd" else ""
    to_write["USE_NV"] = "" if gpu == "amd" else "1"
    try:
      write_config(to_write, self.defaults)
      self.dirty = False
      # Reload so the screen shows what the file now says rather than what we
      # thought we were writing -- a default we deliberately left out has to
      # read back as the default, and this is what proves it did.
      self.values = self.load()
      self.on_disk = dict(self.values)
      n = sum(1 for k, v in self.values.items() if v != self.defaults.get(k))
      self.status = f"saved to {os.path.relpath(CONFIG, REPO)}  \u00b7  {n} setting(s) overriding a default"
    except OSError as e:
      self.status = f"could not save: {e}"

  def run_command(self, argv: list[str], label: str):
    curses.endwin()
    print(f"\n=== {label} ===\n", flush=True)
    try:
      subprocess.run(argv, cwd=REPO, check=False)
    except (OSError, subprocess.SubprocessError) as e:
      print(f"failed: {e}")
    input("\npress enter to return to the config screen ")
    self.stdscr.clear()
    curses.doupdate()

  # ---------------------------------------------------------------- draw --

  def put(self, y, x, text, attr=curses.A_NORMAL, w=None):
    """addstr that clips instead of raising at the edge of the screen."""
    if y < 0 or x < 0 or not text:
      return
    maxx = (w or self.stdscr.getmaxyx()[1]) - 1
    if x >= maxx:
      return
    try:
      self.stdscr.addstr(y, x, text[:maxx - x], attr)
    except curses.error:
      pass

  def layout(self, w):
    """Column x-positions, degrading as the terminal narrows."""
    label_w = 30 if w >= 92 else 24
    value_w = 16 if w >= 92 else 12
    lx = 3
    vx = lx + label_w + 1
    nx = vx + value_w + 2
    return label_w, value_w, lx, vx, nx if nx < w - 10 else None

  def draw(self):
    self.stdscr.clear()
    h, w = self.stdscr.getmaxyx()
    label_w, value_w, lx, vx, nx = self.layout(w)

    overridden = sum(1 for k, v in self.values.items() if v != self.defaults.get(k))
    title = " beampilot setup "
    right = f"{os.path.relpath(CONFIG, REPO)}  \u00b7  {overridden} overriding a default "
    if self.dirty:
      right = "\u25cf unsaved  \u00b7  " + right
    self.put(0, 0, " " * (w - 1), curses.A_REVERSE)
    self.put(0, 0, title, curses.A_REVERSE | curses.A_BOLD)
    self.put(0, max(len(title) + 1, w - 1 - len(right)), right, curses.A_REVERSE)
    self.put(1, 1, _fit(f"GPUs: {gpu_detail()}", w - 3), curses.A_DIM)

    top = 3
    foot = 6                      # rule + help (2) + detail + status + keys
    avail = max(1, h - top - foot)

    # Scroll by LINES, not by rows: a section header costs three of them (a
    # blank line, the heading and its rule), so counting rows walked the cursor
    # off the bottom of a long list and settings simply could not be seen.
    def cost(idx):
      return 3 if self.rows[idx][0] == "section" else 1

    if self.cursor < self.scroll:
      self.scroll = self.cursor
    while self.scroll < self.cursor and sum(cost(i) for i in range(self.scroll, self.cursor + 1)) > avail:
      self.scroll += 1
    while self.scroll > 0 and sum(cost(i) for i in range(self.scroll - 1, self.cursor + 1)) <= avail:
      self.scroll -= 1

    y = top
    last = self.scroll
    for idx in range(self.scroll, len(self.rows)):
      if y + cost(idx) > top + avail:
        break
      last = idx
      kind, obj = self.rows[idx]
      if kind == "section":
        if y > top:
          y += 1
        self.put(y, 2, obj.name.upper(), curses.A_BOLD | curses.color_pair(4), w)
        self.put(y, 2 + len(obj.name) + 3, _fit(obj.blurb, w - len(obj.name) - 8),
                 curses.A_DIM, w)
        y += 1
        self.put(y, 2, "\u2500" * max(0, w - 5), curses.A_DIM, w)
        y += 1
        continue

      val = self.values.get(obj.key, self.defaults.get(obj.key, obj.default))
      default = self.defaults.get(obj.key, obj.default)
      changed = not obj.is_default(val, self.values)
      sel = idx == self.cursor
      shown = display_value(obj, val)

      row_attr = curses.A_REVERSE if sel else curses.A_NORMAL
      # Colour says what the value IS; the right-hand column says where it came
      # from. Keeping those two separate is the point -- "off" and "at the
      # default" are different facts and used to be conflated into one colour.
      if shown == "off":
        vattr = curses.color_pair(5)
      elif shown == "on":
        vattr = curses.color_pair(4)
      elif changed:
        vattr = curses.color_pair(2)
      else:
        vattr = curses.color_pair(1)
      if sel:
        vattr = curses.A_REVERSE

      self.put(y, 0, " " * (w - 1), row_attr, w)
      self.put(y, 1, "\u25b8" if sel else " ", row_attr | curses.A_BOLD, w)
      self.put(y, lx, _fit(obj.label, label_w).ljust(label_w), row_attr, w)
      self.put(y, vx, _fit(shown, value_w), vattr | curses.A_BOLD, w)
      if nx:
        if not changed:
          note, nattr = "default", curses.A_DIM
        else:
          note = f"set \u00b7 default {_fit(display_value(obj, default) or 'unset', 14)}"
          nattr = curses.color_pair(3) if obj.warn else curses.color_pair(2)
        if self.unsaved(obj.key):
          note = "\u25cf " + note
        self.put(y, nx, _fit(note, w - nx - 2), nattr | (curses.A_REVERSE if sel else 0), w)
      y += 1

    # scroll hints, so a list longer than the window says so
    if self.scroll > 0:
      self.put(top - 1, w - 14, "\u25b2 more above", curses.A_DIM, w)
    if last < len(self.rows) - 1:
      self.put(top + avail, w - 14, "\u25bc more below", curses.A_DIM, w)

    self.draw_footer(h, w)
    self.stdscr.refresh()

  def draw_footer(self, h, w):
    hy = h - 5
    self.put(hy - 1, 0, "\u2500" * (w - 1), curses.A_DIM, w)
    cur = self.current()
    if cur:
      wrapped = textwrap.wrap(cur.help or "", max(20, w - 6))[:2]
      for i, line in enumerate(wrapped):
        self.put(hy + i, 2, line, curses.A_DIM, w)
      val = self.values.get(cur.key, "")
      default = self.defaults.get(cur.key, cur.default)
      if not cur.is_default(val, self.values):
        detail = (f"{cur.key}={val or '\u2205'}  \u00b7  overrides the default of "
                  + f"{default or 'unset'}  \u00b7  written to the config")
        attr = curses.color_pair(3) if cur.warn else curses.color_pair(2)
        if cur.warn:
          detail += f"  \u00b7  ! {cur.warn}"
      elif cur.key in self.in_file:
        detail = (f"{cur.key}={val or '\u2205'}  \u00b7  the default  \u00b7  "
                  + "spelled out in the config, which is harmless but pins it")
        attr = curses.A_DIM
      else:
        detail = (f"{cur.key}={val or '\u2205'}  \u00b7  the default  \u00b7  "
                  + "not in the config, so the code decides")
        attr = curses.A_DIM
      if self.unsaved(cur.key):
        detail = f"unsaved ({self.on_disk.get(cur.key) or '\u2205'} on disk)  \u00b7  " + detail
      self.put(hy + 2, 2, _fit(detail, w - 4), attr, w)

    keys = (" \u2191\u2193 move   \u2190\u2192/enter change   d default   tab section"
            + "   / find   s save   r setup   L launch   m monitor   q quit ")
    self.put(h - 1, 0, " " * (w - 1), curses.A_REVERSE, w)
    self.put(h - 1, 0, _fit(keys, w - 1), curses.A_REVERSE, w)
    if self.status:
      self.put(hy + 3, 2, _fit(self.status, w - 4), curses.A_BOLD | curses.color_pair(4), w)

  # ---------------------------------------------------------------- loop --

  def loop(self):
    while True:
      self.draw()
      try:
        ch = self.stdscr.getch()
      except KeyboardInterrupt:
        return
      cur = self.current()
      self.status = ""
      if ch in (ord('q'), 27):
        if self.dirty:
          self.status = "unsaved changes -- s to save, or q again to discard"
          self.dirty = False
          continue
        return
      elif ch in (curses.KEY_DOWN, ord('j')):
        self.move(1)
      elif ch in (curses.KEY_UP, ord('k')):
        self.move(-1)
      elif ch == curses.KEY_NPAGE:
        self.move_page(1)
      elif ch == curses.KEY_PPAGE:
        self.move_page(-1)
      elif ch in (curses.KEY_HOME, ord('g')):
        self.jump_home()
      elif ch in (curses.KEY_END, ord('G')):
        self.jump_home(end=True)
      elif ch == ord('\t'):
        self.move_section(1)
      elif ch == curses.KEY_BTAB:
        self.move_section(-1)
      elif ch == ord('/'):
        self.find()
      elif ch in (curses.KEY_RIGHT, ord('l')) and cur and (cur.choices or cur.numeric):
        self.cycle(cur, 1)
      elif ch in (curses.KEY_LEFT, ord('h')) and cur and (cur.choices or cur.numeric):
        self.cycle(cur, -1)
      elif ch == ord('d') and cur:
        self.reset(cur)
      elif ch in (10, 13, ord(' ')) and cur:
        if cur.choices:
          self.cycle(cur, 1)
        else:
          self.edit_text(cur)
      elif ch == ord('s'):
        self.save()
      elif ch == ord('r'):
        self.run_command(["./setup_beampilot.sh"], "running setup_beampilot.sh")
      elif ch == ord('L'):
        self.run_command(["./launch_beampilot.sh"], "launching beampilot")
      elif ch == ord('m'):
        self.run_command([sys.executable, "tools/beampilot_monitor.py"], "monitor")


def main(stdscr):
  # ncurses waits a full second after a bare ESC to see whether it is the start
  # of an escape sequence. That is the difference between "escape cancels" and
  # "escape appears to hang".
  try:
    curses.set_escdelay(25)
  except (AttributeError, curses.error):
    pass
  curses.curs_set(0)
  curses.use_default_colors()
  curses.init_pair(1, curses.COLOR_WHITE, -1)
  curses.init_pair(2, curses.COLOR_CYAN, -1)
  curses.init_pair(3, curses.COLOR_YELLOW, -1)
  curses.init_pair(4, curses.COLOR_GREEN, -1)
  curses.init_pair(5, curses.COLOR_RED, -1)
  Tui(stdscr).loop()


if __name__ == "__main__":
  if not os.path.exists(CONFIG):
    print(f"config not found at {CONFIG}", file=sys.stderr)
    sys.exit(1)
  try:
    curses.wrapper(main)
  except KeyboardInterrupt:
    pass
