#!/usr/bin/env python3
"""Setup and configuration front-end for beampilot.

Edits config_beampilot.sh in place -- that file stays the single source of
truth, so everything here can also be done by hand in a text editor, and a
config edited by hand shows up correctly the next time this runs.

  uv run python tools/beampilot_tui.py

Arrow keys / j / k to move, Enter or space to change a setting, Tab or 1-9 to
change section, s to save, r to run setup, L to launch, m to open the monitor,
q to quit.
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
  device_help = ("Select the GPU used for model inference: "
                 + (", ".join(f"[{i}] {n}" + (f" (compute {caps[str(i)]:g})" if str(i) in caps else "")
                              for i, n in enumerate(devices)) if devices else "none detected")
                 + ". Indices match nvidia-smi. Auto selects the first compatible device."
                 + " Re-run setup after changing this build-time setting.")
  return [
    Section("Hardware", "Model, GPU, and interface display settings.", [
      Setting("BEAMPILOT_GPU", "GPU backend",
              "Select the tinygrad inference backend. nvidia requires an Ampere-or-newer NVIDIA"
              + " GPU; amd supports the listed modern AMD architectures. Use cuda for older"
              + " NVIDIA GPUs, or opencl for broader compatibility. Only detected-compatible"
              + " options are shown. Re-run setup after changing this build-time setting.",
              gpus[0], choices=gpus,
              warn="build-time setting -- rebuild for this to take effect"),
      Setting("BEAMPILOT_GPU_INDEX", "GPU device", device_help, "", choices=[""] + device_choices,
              value_labels={"": "auto"},
              warn="build-time setting -- rebuild for this to take effect"),
      Setting("CHESTNUT", "Chestnut model",
              "Use the larger Chestnut model. Requires at least 8 GB of VRAM; the standard model"
              + " requires about 4 GB.",
              "0", choices=["0", "1"]),
      Setting("BIG", "Window size",
              "Select the base interface resolution. Window scale applies after this selection.",
              "1", choices=["1", "0"],
              value_labels={"1": "2160x1080", "0": "536x240"}),
      Setting("SCALE", "Window scale",
              "Scale the selected base resolution. Leave blank to fit it automatically to the"
              + " smallest monitor; enter a number to set an explicit multiplier.",
              "", numeric=True, step=0.1),
    ]),
    Section("Car", "Vehicle identity, geometry, and steering calibration.", [
      Setting("BEAMPILOT_REPORT_GEAR", "Report the real gear",
              "Report BeamNG's current gear to openpilot. This prevents engagement in reverse."
              + " When disabled, the reported gear remains Drive.",
              "1", choices=["1", "0"]),
      Setting("FINGERPRINT", "Fingerprint",
              "BEAMPILOT is the native simulator platform and uses geometry reported by the mod."
              + " HONDA_CIVIC_2022 preserves the previous Civic-based behavior. Other fingerprints"
              + " are incompatible with the CAN messages generated by beamngd.",
              "BEAMPILOT", choices=["BEAMPILOT", "HONDA_CIVIC_2022"],
              warn="beamngd packs Honda Bosch CAN; another car needs code changes"),
      Setting("BEAMPILOT_BEAMNG_GEOMETRY", "Use measured geometry",
              "Use wheelbase, weight distribution, mass, yaw inertia, steering lock, and steering"
              + " ratio reported by the spawned BeamNG vehicle. If no geometry packet arrives,"
              + " openpilot falls back to fingerprint values. Re-run setup after updating the mod.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_LIVE_STEER_PARAMS", "Use live steering estimates",
              "Apply paramsd's live steering-ratio and tire-stiffness estimates. A ratio measured"
              + " from vehicle geometry takes priority; tire stiffness can still update. Disable"
              + " to keep both parameters at their static values.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_STEER_RATIO", "Steering ratio",
              "Steering-wheel degrees per road-wheel degree. Leave blank to measure and cache the"
              + " value per vehicle and steering rack. An explicit value overrides measurement for"
              + " every vehicle; the Civic reference value is 15.38.",
              "", numeric=True, step=0.5),
      Setting("BEAMPILOT_SEED_STEER_RATIO", "Estimate ratio from lock",
              "Estimate an unmeasured vehicle's steering ratio from its steering lock. Disable to"
              + " use the fingerprint's steering ratio until a measurement is available.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_STEER_CALIBRATE", "Auto-measure steering ratio",
              "Run one steering sweep when an uncached vehicle and rack are first detected. The"
              + " sweep takes about 2.5 seconds, only runs while stopped and disengaged, and aborts"
              + " if either condition changes. Disable to measure during normal driving instead.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_STEER_RATIO_CACHE", "Steering ratio cache",
              "JSON file containing measured ratios, keyed by vehicle and steering lock. Edit an"
              + " individual cache entry to correct one vehicle without setting a global override.",
              "~/.config/beampilot/steer_ratios.json"),
      Setting("BEAMPILOT_WHEELBASE_M", "Wheelbase (m)",
              "Distance between the front and rear axles. Leave blank to use measured geometry."
              + " The Civic reference value is 2.70 m.",
              "", numeric=True, step=0.05),
      Setting("BEAMPILOT_CENTER_TO_FRONT_M", "Centre of gravity to front axle (m)",
              "Distance from the center of gravity to the front axle. Leave blank to calculate it"
              + " from the vehicle's node masses. The Civic reference value is 1.08 m.",
              "", numeric=True, step=0.05),
      Setting("BEAMPILOT_MASS_KG", "Mass (kg)",
              "Vehicle mass. Leave blank to use the measured value. The Civic reference is 1462 kg.",
              "", numeric=True, step=25.0),
      Setting("BEAMPILOT_ROTATIONAL_INERTIA", "Yaw inertia (kg m2)",
              "Resistance to rotation about the vertical axis. Leave blank to calculate it from"
              + " the vehicle body. The Civic reference value is 2500 kg m2.",
              "", numeric=True, step=100.0),
      Setting("BEAMPILOT_STEER_LOCK_DEG", "Steering lock (deg)",
              "Steering-wheel angle from center to full lock, used to normalize BeamNG steering"
              + " input. The mod normally reports this per vehicle; 510 degrees is the fallback."
              + " An explicit value applies to every vehicle.",
              "510.0", numeric=True, step=10.0),
      Setting("BEAMPILOT_CALIBRATION", "Calibration",
              "instant seeds a valid level-pose calibration at startup. live requires calibration"
              + " to converge from driving before engagement.",
              "instant", choices=["instant", "live"]),
    ]),
    Section("Driving limits", "Acceleration, steering response, and curve-speed limits.", [
      Setting("BEAMPILOT_MAX_LAT_ACCEL", "Lateral accel (m/s2)",
              "Maximum lateral acceleration used to limit curvature at speed. Stock openpilot uses"
              + " 3.0 m/s2; the shipped configuration uses 5.0 m/s2.",
              "3.0", numeric=True, step=0.5),
      Setting("BEAMPILOT_MAX_LAT_JERK", "Lateral jerk (m/s3)",
              "Maximum rate of lateral-acceleration change. Higher values allow faster steering"
              + " transitions but can increase oscillation. Stock is 5.0 m/s3; shipped is 8.0.",
              "5.0", numeric=True, step=0.5),
      Setting("BEAMPILOT_ACCEL_SCALE", "Accel scale",
              "Multiplier for the longitudinal acceleration envelope. Stock is 1.0; the shipped"
              + " configuration uses 2.0.",
              "1.0", numeric=True, step=0.25),
      Setting("BEAMPILOT_DECEL_SCALE", "Decel scale",
              "Multiplier for the longitudinal braking envelope. Stock is 1.0; the shipped"
              + " configuration uses 1.5.",
              "1.0", numeric=True, step=0.25),
      Setting("BEAMPILOT_PERSONALITY", "Following distance",
              "Select the desired time gap: aggressive 1.25 s, standard 1.45 s, or relaxed 1.75 s.",
              "0", choices=["0", "1", "2"],
              value_labels={"0": "aggressive", "1": "standard", "2": "relaxed"}),
      Setting("BEAMPILOT_T_FOLLOW_SCALE", "Follow gap scale",
              "Multiplier on top of the following-distance time gap above. Stock is 1.0; lower"
              + " shrinks the gap further and delays the braking point. Braking distance still grows"
              + " with the square of speed regardless of this setting.",
              "1.0", numeric=True, step=0.1),
      Setting("BEAMPILOT_STEER_SWEEP_SECONDS", "Steering response (s)",
              "Time used to rate-limit a full steering sweep. Lower values respond faster and may"
              + " reduce stability.",
              "0.15", numeric=True, step=0.05),
      Setting("BEAMPILOT_CURVE_SLOWDOWN", "Curve slowdown (experimental)",
              "Reduce speed before a curve using curvature predicted by the model. Stock openpilot"
              + " does not apply this additional planning layer. Disabled by default.",
              "0", choices=["0", "1"],
              warn="experimental: adds longitudinal planning openpilot does not normally do"),
      Setting("BEAMPILOT_CURVE_LAT_ACCEL", "Cornering accel (m/s2)",
              "Target lateral acceleration used by curve slowdown to choose corner speed. The"
              + " default is 70% of the maximum lateral-acceleration limit.",
              "2.1", numeric=True, step=0.1,
              derive=lambda v: f"{round(0.7 * _as_float(v.get('BEAMPILOT_MAX_LAT_ACCEL'), 3.0), 3):g}"),
      Setting("BEAMPILOT_ACTUATION_MARGIN", "Actuation headroom",
              "Margin between configured driving limits and the excessive-actuation safety check."
              + " Increase it when raising the limits to prevent valid commanded actuation from"
              + " triggering a soft disable. It never reduces the stock threshold.",
              "2.0", numeric=True, step=0.25),
      Setting("BEAMPILOT_MAX_CURVATURE", "Max curvature (1/m)",
              "Geometric curvature limit. A value of 0.2 1/m corresponds to a 5 m turn radius and"
              + " normally applies only at low speed.",
              "0.2", numeric=True, step=0.05),
    ]),
    Section("Controls", "Keyboard bindings and control-input behavior.", [
      Setting("BEAMPILOT_KEY_SET", "Set / engage key",
              "Keyboard key used to set cruise speed and engage. Avoid conflicts with BeamNG bindings.", "i"),
      Setting("BEAMPILOT_KEY_RESUME", "Resume / speed up key",
              "Keyboard key used to resume or increase the cruise set speed.", "o"),
      Setting("BEAMPILOT_KEY_CANCEL", "Cancel key", "Keyboard key used to disengage control.", "u"),
      Setting("BEAMPILOT_CRUISE_STEP_MPH", "Speed step (mph)", "Per-tap speed change.", "1.0", numeric=True, step=1.0),
      Setting("BEAMPILOT_AUTO_LANE_CHANGE", "Auto lane change",
              "Start a lane change from the turn signal alone. This replaces openpilot's steering-"
              + "torque confirmation, which is unavailable without a physical steering wheel.",
              "0", choices=["1", "0"]),
      Setting("BEAMPILOT_CONTROL_MODE", "Control mode",
              "lua sends input directly through the BeamNG mod. joystick uses a virtual controller"
              + " and requires axis bindings in BeamNG.",
              "lua", choices=["lua", "joystick"]),
    ]),
    Section("Blind spot", "Lane-change blocking and blind-spot display settings.", [
      Setting("BEAMPILOT_BSM", "Blind spot monitoring",
              "Use BeamNG vehicle positions to detect traffic beside the vehicle. Detection blocks"
              + " lane changes and raises the blind-spot alert. Re-run setup after updating the mod.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_LANE_CHANGE_ABORT", "Cancel lane change",
              "Cancel an active lane change if a vehicle enters the target blind spot. The turn"
              + " signal remains active so the maneuver can restart after the lane clears.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_LANE_CHANGE_ABORT_S", "Cancel window (s)",
              "Maximum time after lane-change start during which a blind-spot detection can cancel"
              + " the maneuver. Later cancellation can create a sharper return to the original lane.",
              "2.0", numeric=True, step=0.5),
      Setting("BEAMPILOT_SIGNAL_AUTO_CANCEL", "Cancel signal after a change",
              "Turn off the signal after a completed lane change. A lane change cancelled by blind-"
              + "spot detection keeps the signal active so it can resume.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_BSM_APPROACHING", "Warn on closing traffic",
              "Include vehicles that are approaching the blind-spot zone fast enough to enter it"
              + " during a lane change.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_BSM_WIDTH_M", "Zone width (m)",
              "Lateral reach of each blind-spot zone. 3.6 m is approximately one lane width.",
              "3.6", numeric=True, step=0.2),
      Setting("BEAMPILOT_BSM_REAR_M", "Zone rear reach (m)",
              "How far past your rear bumper the zone extends.",
              "4.0", numeric=True, step=0.5),
      Setting("BEAMPILOT_BSM_INDICATOR", "Mirror lamps",
              "Show amber blind-spot chevrons at the edges of the road view. This affects display"
              + " only; lane-change blocking and alerts remain active when it is disabled.",
              "0", choices=["0", "1"]),
      Setting("BEAMPILOT_BSM_DEBUG", "Log state changes",
              "Log blind-spot state changes to the beamngd terminal and BeamNG console.",
              "0", choices=["0", "1"]),
    ]),
    Section("Radar", "Simulator-based traffic tracks and lead selection.", [
      Setting("BEAMPILOT_RADAR", "Ground-truth radar",
              "Publish nearby BeamNG vehicles as radar tracks. When disabled, openpilot uses camera-"
              + "based lead estimates only. Disabled by default because this uses simulator object"
              + " data rather than a simulated physical sensor. Re-run setup after updating the mod.",
              "0", choices=["0", "1"]),
      Setting("BEAMPILOT_RADAR_LEADS", "Allow radar-only leads",
              "Allow a radar track to become the lead without camera confirmation. When disabled,"
              + " radar only refines the distance and speed of a camera-detected lead. Enabling this"
              + " can cause earlier speed adjustments for distant traffic.",
              "0", choices=["0", "1"]),
      Setting("BEAMPILOT_RADAR_ONCOMING", "Report oncoming traffic",
              "Include moving oncoming vehicles in radar tracks. Disabled by default to prevent an"
              + " oncoming vehicle on a narrow road from being selected as the lead. Stationary"
              + " vehicles remain eligible regardless of heading.",
              "0", choices=["0", "1"]),
      Setting("BEAMPILOT_RADAR_OCCLUSION", "Require line of sight",
              "Exclude tracks blocked by terrain or buildings. The line-of-sight test uses static"
              + " geometry; vehicles do not occlude one another.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_RADAR_NOISE_M", "Range noise (m)",
              "Standard range variation added to tracks. Set to 0 for exact simulator distance.",
              "0.12", numeric=True, step=0.05),
      Setting("BEAMPILOT_RADAR_LEAD_HALF_WIDTH_M", "In-lane width (m)",
              "Maximum lateral distance from the predicted path for radar-only lead selection. A"
              + " wider value may include adjacent lanes; a narrower value may reject leads on curves.",
              "1.8", numeric=True, step=0.2),
      Setting("BEAMPILOT_RADAR_RANGE_M", "Radar range (m)",
              "Maximum distance at which vehicles are reported. Longer ranges can cause openpilot to"
              + " begin managing speed for traffic earlier.",
              "110", numeric=True, step=10.0),
      Setting("BEAMPILOT_RADAR_HALF_WIDTH_M", "Beam half-width (m)",
              "Half-width of the radar search area at the vehicle before angular spread is applied."
              + " Increase it for wide lanes or sharp curves.",
              "3.0", numeric=True, step=0.5),
      Setting("BEAMPILOT_RADAR_MAX_TRACKS", "Max tracks",
              "Maximum number of nearest tracks sent per scan. The protocol limit is 24.",
              "12", numeric=True, step=1.0),
      Setting("BEAMPILOT_RADAR_INDICATOR", "Show tracks on screen",
              "Draw a cyan road marker for each radar track and a ring around the selected lead."
              + " This can also confirm that radar data is reaching the interface.",
              "1", choices=["1", "0"]),
      Setting("BEAMPILOT_RADAR_DEBUG", "Log the nearest track",
              "Log the nearest track to the BeamNG console on every scan.",
              "0", choices=["0", "1"]),
    ]),
    Section("Camera", "Road-camera mode, placement, and screen capture.", [
      Setting("BEAMPILOT_CAMERA_MODE", "Road-camera mode",
              "narrow publishes a calibrated 25.70-degree narrowRoad stream. wide_crop renders a"
              + " calibrated 93.62-degree image, publishes it as wideRoad, and derives narrowRoad"
              + " from a centered crop. The crop has less detail than a separate narrow camera.",
              "narrow", choices=["narrow", "wide_crop"],
              warn="wide_crop is experimental and needs in-game validation",
              value_labels={"narrow": "narrow (default)",
                            "wide_crop": "wide + narrow crop (experimental)"}),
      Setting("BEAMPILOT_WIDE_CAMERA_PLACEMENT", "Wide-camera placement",
              "vehicle_front places the wide lens from the spawned vehicle's bounding box to avoid"
              + " interior or body clipping. legacy uses fixed camera offsets. This setting affects"
              + " wide_crop only; narrow retains its existing placement.",
              "vehicle_front", choices=["vehicle_front", "legacy"],
              value_labels={"vehicle_front": "adaptive per vehicle",
                            "legacy": "legacy fixed offsets"}),
      Setting("BEAMPILOT_WIDE_CAMERA_HEIGHT_M", "Wide camera height (m)",
              "Lens height above the bottom of the vehicle bounding box. The default 1.22 m matches"
              + " the approximate device height expected by the model.",
              "1.22", numeric=True, step=0.05),
      Setting("BEAMPILOT_WIDE_CAMERA_CLEARANCE_M", "Wide front clearance (m)",
              "Distance between the lens and the front of the vehicle bounding box. Increase this"
              + " if bodywork remains visible in the wide image.",
              "0.15", numeric=True, step=0.05),
      Setting("BEAMPILOT_CAMERA_COMMAND_PORT", "Camera command port",
              "Loopback UDP port used by the BeamNG camera tuner to ask beamngd to reset"
              + " openpilot's camera/extrinsics calibration.",
              "49157", numeric=True, step=1.0),
      Setting("BEAMPILOT_CAPTURE_BACKEND", "Capture backend",
              "auto selects X11 capture on X11 and portal/PipeWire capture on Wayland. portal"
              + " requires a screen-sharing selection and GStreamer. x11 forces direct X11 capture"
              + " and does not work on native Wayland surfaces.",
              "auto", choices=["auto", "x11", "portal"]),
      Setting("BEAMPILOT_CAM_ASPECT", "Aspect handling",
              "crop trims the sides to match openpilot's 1.596 frame aspect before resizing. stretch"
              + " resizes the complete capture and distorts a 16:9 image horizontally. A 1928x1208"
              + " BeamNG window matches the target aspect without cropping.",
              "crop", choices=["crop", "stretch"]),
      Setting("BEAMPILOT_CAM_WINDOW", "Track window",
              "Match text for the BeamNG window; it follows the window as it moves or resizes. Blank = capture a whole monitor. Needs X11 and xdotool.",
              "beamng"),
      Setting("BEAMPILOT_CAM_MONITOR", "Monitor",
              "Fallback when no window is tracked or found. 1 is the first monitor.", "1", choices=["1", "2", "3", "0"]),
      Setting("BEAMPILOT_CAM_REGION", "Capture region",
              "left,top,width,height. Overrides both of the above with a fixed rectangle.", ""),
    ]),
    Section("Alerts", "Alert handling and optional process suppression.", [
      Setting("BEAMPILOT_IGNORE_COMM_ISSUE", "Ignore comm issues",
              "Prevent communication-timing events from disengaging control. This can allow control"
              + " to continue with stale data if a process stalls.",
              "1", choices=["1", "0"],
              warn="openpilot will keep driving if modeld stalls"),
      Setting("BLOCK", "Blocked processes", "Comma-separated. soundd mutes the alert chimes.", ",soundd"),
    ]),
    Section("Bridge", "Bridge timing, network address, and UDP ports.", [
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
              "Delay before starting the stack. Use this to return focus to BeamNG; 0 starts"
              + " immediately.",
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
    # Hardware probing shells out. Cache this instead of doing it on every
    # repaint/key press.
    self.gpu_summary = gpu_detail()
    self.values = self.load()
    self.on_disk = dict(self.values)
    self.rows = self.flatten()
    self.cursor = next(i for i, r in enumerate(self.rows) if r[0] == "setting")
    self.scroll = 0
    self.status = ""
    self.dirty = False
    self.quit_armed = False

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

  def active_section(self) -> Section:
    return self.section_of(self.cursor) or self.sections[0]

  def active_section_index(self) -> int:
    return self.sections.index(self.active_section())

  def setting_indices(self, section: Section | None = None) -> list[int]:
    """Global row indices for one section's settings."""
    wanted = section or self.active_section()
    return [i for i, (kind, obj) in enumerate(self.rows)
            if kind == "setting" and obj in wanted.settings]

  def select_section(self, index: int):
    """Open a section at its first setting."""
    section = self.sections[index % len(self.sections)]
    indices = self.setting_indices(section)
    if indices:
      self.cursor = indices[0]
      self.scroll = 0

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
    # Up/down stays inside the visible section.  Crossing a category boundary
    # in the old 71-row list was easy to do accidentally and left the heading
    # off-screen; Tab and the numbered section shortcuts are explicit.
    indices = self.setting_indices()
    if not indices:
      return
    try:
      pos = indices.index(self.cursor)
    except ValueError:
      pos = 0
    self.cursor = indices[(pos + delta) % len(indices)]

  def move_page(self, delta: int):
    indices = self.setting_indices()
    if not indices:
      return
    pos = indices.index(self.cursor)
    self.cursor = indices[max(0, min(len(indices) - 1, pos + delta * 8))]

  def move_section(self, delta: int):
    self.select_section(self.active_section_index() + delta)

  def jump_home(self, end: bool = False):
    indices = self.setting_indices()
    if indices:
      self.cursor = indices[-1 if end else 0]

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
      if needle in obj.label.lower() or needle in obj.key.lower() or needle in obj.help.lower():
        self.cursor = idx
        self.scroll = 0
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

  def layout(self, w, origin=0):
    """Responsive setting columns inside ``origin..w``."""
    usable = max(1, w - origin - 2)
    value_w = 18 if usable >= 76 else 14
    source_w = 18 if usable >= 62 else 0
    reserved = value_w + (source_w + 2 if source_w else 0) + 6
    label_w = max(12, usable - reserved)
    lx = origin + 3
    vx = lx + label_w + 2
    nx = vx + value_w + 2 if source_w else None
    return label_w, value_w, source_w, lx, vx, nx

  def override_count(self, section: Section | None = None) -> int:
    settings = section.settings if section else [s for sec in self.sections for s in sec.settings]
    return sum(not s.is_default(self.values.get(s.key, self.defaults.get(s.key, s.default)), self.values)
               for s in settings)

  def source_label(self, setting: Setting) -> str:
    value = self.values.get(setting.key, self.defaults.get(setting.key, setting.default))
    if self.unsaved(setting.key):
      return "● unsaved"
    if not setting.is_default(value, self.values):
      return "custom"
    if setting.key in self.in_file:
      return "pinned default"
    return "code default"

  def draw_sidebar(self, width: int, footer_start: int):
    active = self.active_section_index()
    self.put(2, 1, "SECTIONS", curses.A_BOLD | curses.color_pair(4), width + 1)
    self.put(2, width - 7, "CUSTOM", curses.A_DIM, width + 1)
    self.put(3, 1, "─" * (width - 2), curses.A_DIM, width + 1)
    for i, section in enumerate(self.sections):
      y = 4 + i
      if y >= footer_start:
        break
      selected = i == active
      attr = curses.A_REVERSE | curses.A_BOLD if selected else curses.A_NORMAL
      self.put(y, 0, " " * width, attr, width + 1)
      marker = "▸" if selected else " "
      self.put(y, 1, f"{marker} {i + 1} {_fit(section.name, width - 10)}", attr, width + 1)
      custom = self.override_count(section)
      dirty = any(self.unsaved(s.key) for s in section.settings)
      badge = f"● {custom}" if dirty else (str(custom) if custom else "—")
      badge_attr = attr if selected else (curses.color_pair(3) if dirty else curses.A_DIM)
      self.put(y, width - len(badge) - 2, badge, badge_attr, width + 1)
    for y in range(1, footer_start):
      self.put(y, width, "│", curses.A_DIM, width + 2)

  def draw(self):
    self.stdscr.clear()
    h, w = self.stdscr.getmaxyx()
    title = " BEAMPILOT CONFIG "
    overridden = self.override_count()
    state = "\u25cf UNSAVED" if self.dirty else "saved"
    right = f" {state}  \u00b7  {overridden} custom  \u00b7  {os.path.relpath(CONFIG, REPO)} "
    self.put(0, 0, " " * (w - 1), curses.A_REVERSE)
    self.put(0, 0, title, curses.A_REVERSE | curses.A_BOLD)
    right_room = max(0, w - len(title) - 2)
    right = _fit(right, right_room)
    self.put(0, max(len(title), w - 1 - len(right)), right, curses.A_REVERSE)

    if h < 12 or w < 48:
      self.put(3, 2, "Terminal too small for the config editor.",
               curses.A_BOLD | curses.color_pair(3), w)
      self.put(5, 2, f"Current: {w}x{h}  \u00b7  minimum: 48x12", curses.A_DIM, w)
      self.put(h - 1, 0, " q quit ".ljust(max(0, w - 1)), curses.A_REVERSE, w)
      self.stdscr.refresh()
      return

    sidebar_w = 25 if w >= 104 and h >= 22 else 0
    main_x = sidebar_w + 1 if sidebar_w else 0
    footer_lines = 8 if h >= 21 else 6
    footer_start = h - footer_lines
    top = 5
    avail = max(1, footer_start - top)
    section = self.active_section()
    section_num = self.active_section_index() + 1
    indices = self.setting_indices(section)
    cursor_pos = indices.index(self.cursor)
    self.scroll = max(0, min(self.scroll, max(0, len(indices) - avail)))
    if cursor_pos < self.scroll:
      self.scroll = cursor_pos
    elif cursor_pos >= self.scroll + avail:
      self.scroll = cursor_pos - avail + 1
    visible = indices[self.scroll:self.scroll + avail]

    if sidebar_w:
      self.draw_sidebar(sidebar_w, footer_start)

    label_w, value_w, source_w, lx, vx, nx = self.layout(w, main_x)
    first = self.scroll + 1 if visible else 0
    progress = f"{first}-{self.scroll + len(visible)} / {len(indices)}"
    heading = f"{section_num}/{len(self.sections)}  {section.name.upper()}"
    self.put(1, main_x + 2,
             _fit(f"GPU: {self.gpu_summary}  \u00b7  defaults remain controlled by the code",
                  w - main_x - 4), curses.A_DIM, w)
    self.put(2, main_x + 2, heading, curses.A_BOLD | curses.color_pair(4), w)
    if w - main_x - len(heading) > len(progress) + 6:
      self.put(2, w - len(progress) - 2, progress, curses.A_DIM, w)
    self.put(3, main_x + 2, _fit(section.blurb, w - main_x - 4), curses.A_DIM, w)
    self.put(4, lx, "SETTING", curses.A_DIM | curses.A_BOLD, w)
    self.put(4, vx, "VALUE", curses.A_DIM | curses.A_BOLD, w)
    if nx:
      self.put(4, nx, "SOURCE", curses.A_DIM | curses.A_BOLD, w)

    row_width = max(1, w - main_x - 1)
    for y, idx in enumerate(visible, top):
      _, obj = self.rows[idx]
      val = self.values.get(obj.key, self.defaults.get(obj.key, obj.default))
      changed = not obj.is_default(val, self.values)
      sel = idx == self.cursor
      shown = display_value(obj, val)
      row_attr = curses.A_REVERSE if sel else curses.A_NORMAL
      if obj.warn and changed:
        vattr = curses.color_pair(3)
      elif shown == "on":
        vattr = curses.color_pair(4)
      elif shown == "off":
        vattr = curses.A_DIM
      elif changed:
        vattr = curses.color_pair(2)
      else:
        vattr = curses.color_pair(1)
      if sel:
        vattr = curses.A_REVERSE

      self.put(y, main_x, " " * row_width, row_attr, w)
      self.put(y, main_x + 1, "\u25b8" if sel else " ", row_attr | curses.A_BOLD, w)
      self.put(y, lx, _fit(obj.label, label_w).ljust(label_w), row_attr, w)
      self.put(y, vx, _fit(shown, value_w), vattr | curses.A_BOLD, w)
      if nx:
        source = self.source_label(obj)
        if source == "\u25cf unsaved":
          nattr = curses.color_pair(3)
        elif source == "custom":
          nattr = curses.color_pair(2)
        else:
          nattr = curses.A_DIM
        self.put(y, nx, _fit(source, source_w),
                 nattr | (curses.A_REVERSE if sel else 0), w)

    self.draw_footer(h, w, footer_start, main_x)
    self.stdscr.refresh()

  def draw_footer(self, h, w, start_y, origin=0):
    self.put(start_y, origin, "\u2500" * (w - origin - 1), curses.A_DIM, w)
    cur = self.current()
    if cur:
      val = self.values.get(cur.key, self.defaults.get(cur.key, cur.default))
      default = self.defaults.get(cur.key, cur.default)
      shown = display_value(cur, val)
      value_badge = f"[ {shown} ]"
      label_room = max(1, w - origin - len(value_badge) - 7)
      self.put(start_y + 1, origin + 2, _fit(cur.label, label_room), curses.A_BOLD, w)
      self.put(start_y + 1, w - len(value_badge) - 2, value_badge,
               curses.A_BOLD | curses.color_pair(2), w)

      help_lines = max(1, h - start_y - 5)
      wrapped = textwrap.wrap(cur.help or "No description for this setting.",
                              max(20, w - origin - 6))[:help_lines]
      for i, line in enumerate(wrapped):
        self.put(start_y + 2 + i, origin + 2, line, curses.A_DIM, w)

      meta_y = start_y + 2 + help_lines
      source = self.source_label(cur)
      detail = (f"{cur.key}  \u00b7  current {val or '\u2205'}  \u00b7  "
                + f"default {default or '\u2205'}  \u00b7  {source}")
      if source == "\u25cf unsaved" or (cur.warn and source == "custom"):
        attr = curses.color_pair(3)
      elif source == "custom":
        attr = curses.color_pair(2)
      else:
        attr = curses.A_DIM
      self.put(meta_y, origin + 2, _fit(detail, w - origin - 4), attr, w)

      info_y = min(h - 2, meta_y + 1)
      if self.status:
        info, info_attr = self.status, curses.A_BOLD | curses.color_pair(4)
      elif cur.warn and not cur.is_default(val, self.values):
        info, info_attr = "! " + cur.warn, curses.A_BOLD | curses.color_pair(3)
      else:
        action = "Enter cycles choices" if cur.choices else "Enter edits the value"
        info = (f"{action}  \u00b7  1-9 jump to section  \u00b7  / find  \u00b7  "
                + "r setup  \u00b7  L launch  \u00b7  m monitor")
        info_attr = curses.A_DIM
      self.put(info_y, origin + 2, _fit(info, w - origin - 4), info_attr, w)

    if w >= 90:
      keys = (" \u2191\u2193 move   \u2190\u2192 change   Enter edit   Tab section"
              + "   1-9 jump   / find   d default   s save   q quit ")
    elif w >= 65:
      keys = (" \u2191\u2193 move   \u2190\u2192 change   Enter edit   Tab section"
              + "   d default   s save   q quit ")
    else:
      keys = " \u2191\u2193 move   \u2190\u2192 change   Tab section   s save   q quit "
    self.put(h - 1, 0, " " * (w - 1), curses.A_REVERSE, w)
    self.put(h - 1, 0, _fit(keys, w - 1), curses.A_REVERSE, w)

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
        if self.dirty and not self.quit_armed:
          self.status = "unsaved changes -- s to save, or q again to discard"
          self.quit_armed = True
          continue
        return
      self.quit_armed = False
      if ord('1') <= ch <= ord('9'):
        self.select_section(ch - ord('1'))
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
