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
from dataclasses import dataclass, field

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO, "config_beampilot.sh")


@dataclass
class Setting:
  key: str
  label: str
  help: str
  default: str
  # choices: cycle through fixed values. None means free text entry.
  choices: list[str] | None = None
  # values that are numbers we nudge with left/right rather than cycle
  numeric: bool = False
  step: float = 0.5
  section: str = ""
  # warn shown in red when the value is not the default
  warn: str = ""


@dataclass
class Section:
  name: str
  blurb: str
  settings: list[Setting] = field(default_factory=list)


def gpu_options() -> list[str]:
  """Detected first, so the list reflects what this machine can actually use."""
  opts = []
  if shutil.which("nvidia-smi"):
    try:
      out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=5)
      if out.returncode == 0 and out.stdout.strip():
        opts.append("nvidia")
    except (subprocess.SubprocessError, OSError):
      pass
  try:
    lspci = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
    if lspci.returncode == 0 and re.search(r"VGA.*\b(AMD|ATI|Radeon)\b", lspci.stdout, re.I):
      opts.append("amd")
  except (subprocess.SubprocessError, OSError):
    pass
  return opts or ["nvidia", "amd"]


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
  device_help = ("Which physical GPU runs the model, if you have more than one: "
                 + (", ".join(f"[{i}] {n}" for i, n in enumerate(devices)) if devices else "none detected")
                 + ". BUILD-time -- re-run setup after changing.")
  return [
    Section("Hardware", "What this machine has, and which model to run.", [
      Setting("BEAMPILOT_GPU", "GPU backend",
              "Which GPU runs the driving model. It shares the card with BeamNG's rendering.",
              gpus[0], choices=gpus),
      Setting("BEAMPILOT_GPU_INDEX", "GPU device", device_help, "0", choices=device_choices,
              warn="build-time setting -- rebuild for this to take effect"),
      Setting("CHESTNUT", "Chestnut model",
              "Larger, better-driving model. Needs 8GB+ VRAM (standard needs 4GB).",
              "0", choices=["0", "1"]),
      Setting("BIG", "Display size",
              "1 = comma 3/3X window (2160x1080). 0 = comma 4 (536x240) -- tiny on a desktop.",
              "1", choices=["1", "0"]),
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
              "Changing this breaks beamngd -- CAN layouts differ per car.",
              "HONDA_CIVIC_2022",
              warn="beamngd packs Honda Bosch CAN; another car needs code changes"),
      Setting("BEAMPILOT_STEER_LOCK_DEG", "Steering lock (deg)",
              "Your BeamNG vehicle's full lock. Measure it in the monitor. Too low oversteers, too high runs wide.",
              "510.0", numeric=True, step=10.0),
      Setting("BEAMPILOT_CALIBRATION", "Calibration",
              "instant = start already calibrated at a level pose, usable right away. "
              + "live = converge from real driving first; won't engage until it has.",
              "instant", choices=["instant", "live"]),
    ]),
    Section("Driving limits", "Stock openpilot follows EU/ISO comfort limits. These are often why it won't corner.", [
      Setting("BEAMPILOT_MAX_LAT_ACCEL", "Lateral accel (m/s2)",
              "Turning. Max curvature is accel/v^2 -- stock 3.0 allows only a ~300m radius at 67mph.",
              "5.0", numeric=True, step=0.5),
      Setting("BEAMPILOT_MAX_LAT_JERK", "Lateral jerk (m/s3)",
              "How fast curvature may change. The most likely cause of weaving if raised too far.",
              "8.0", numeric=True, step=0.5),
      Setting("BEAMPILOT_ACCEL_SCALE", "Accel scale",
              "Multiplier on the acceleration envelope. 1.0 is stock.",
              "2.0", numeric=True, step=0.25),
      Setting("BEAMPILOT_DECEL_SCALE", "Decel scale",
              "Same for braking. 1.0 is stock.",
              "1.5", numeric=True, step=0.25),
      Setting("BEAMPILOT_PERSONALITY", "Following distance",
              "0 aggressive (1.25s, brakes latest) / 1 standard (1.45s) / 2 relaxed (1.75s).",
              "0", choices=["0", "1", "2"]),
      Setting("BEAMPILOT_STEER_SWEEP_SECONDS", "Steering response (s)",
              "Lock-to-lock sweep time. Lower is snappier but twitchier.",
              "0.15", numeric=True, step=0.05),
      Setting("BEAMPILOT_CURVE_SLOWDOWN", "Slow for corners (EXPERIMENTAL)",
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
              "2.1", numeric=True, step=0.1),
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
              "Signal alone commits the change. Required here -- there is no wheel to nudge.",
              "1", choices=["1", "0"]),
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


def read_config() -> dict[str, str]:
  """Parse `export KEY="value"` lines. Commented-out lines are treated as unset."""
  values: dict[str, str] = {}
  if not os.path.exists(CONFIG):
    return values
  with open(CONFIG) as f:
    for line in f:
      m = re.match(r'^\s*export\s+([A-Z_][A-Z0-9_]*)=(.*)$', line)
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


def write_config(values: dict[str, str]) -> None:
  """Rewrite only the export lines we manage, preserving comments and layout.

  Anything not already present is appended in a clearly marked block, so a
  hand-written config keeps its structure and its comments.
  """
  lines: list[str] = []
  if os.path.exists(CONFIG):
    with open(CONFIG) as f:
      lines = f.readlines()

  seen: set[str] = set()
  out: list[str] = []
  for line in lines:
    m = re.match(r'^(\s*)export\s+([A-Z_][A-Z0-9_]*)=(.*)$', line)
    if not m:
      out.append(line)
      continue
    indent, key, rest = m.group(1), m.group(2), m.group(3)
    if key not in values:
      out.append(line)
      continue
    seen.add(key)
    val = values[key]
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

  missing = {k: v for k, v in values.items() if k not in seen and v != ""}
  if missing:
    out.append("\n# --- added by tools/beampilot_tui.py ---\n")
    for k, v in missing.items():
      if k == "BLOCK":
        out.append(f'export BLOCK="${{BLOCK}}{v}"\n')
      else:
        out.append(f'export {k}="{v}"\n')

  with open(CONFIG, "w") as f:
    f.writelines(out)


class Tui:
  def __init__(self, stdscr):
    self.stdscr = stdscr
    self.sections = build_sections()
    self.values = self.load()
    self.rows = self.flatten()
    self.cursor = 0
    self.status = "arrow keys to move  ·  enter/space to change  ·  s to save"
    self.dirty = False

  def load(self) -> dict[str, str]:
    on_disk = read_config()
    values = {}
    for sec in self.sections:
      for s in sec.settings:
        if s.key == "BEAMPILOT_GPU":
          if on_disk.get("USE_NV") == "1":
            values[s.key] = "nvidia"
          elif on_disk.get("USE_AMD") == "1":
            values[s.key] = "amd"
          else:
            values[s.key] = s.default
        else:
          values[s.key] = on_disk.get(s.key, s.default)
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

  def move(self, delta: int):
    n = len(self.rows)
    for _ in range(n):
      self.cursor = (self.cursor + delta) % n
      if self.rows[self.cursor][0] == "setting":
        return

  def cycle(self, s: Setting, delta: int):
    val = self.values.get(s.key, s.default)
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
    self.dirty = True

  def edit_text(self, s: Setting):
    curses.echo()
    curses.curs_set(1)
    h, w = self.stdscr.getmaxyx()
    prompt = f"  {s.label} = "
    self.stdscr.move(h - 1, 0)
    self.stdscr.clrtoeol()
    self.stdscr.addstr(h - 1, 0, prompt, curses.A_BOLD)
    try:
      raw = self.stdscr.getstr(h - 1, len(prompt), 60).decode("utf-8", "ignore")
      self.values[s.key] = raw.strip()
      self.dirty = True
    except (curses.error, UnicodeDecodeError):
      pass
    curses.noecho()
    curses.curs_set(0)

  def save(self):
    to_write = dict(self.values)
    gpu = to_write.pop("BEAMPILOT_GPU", "nvidia")
    to_write["USE_NV"] = "1" if gpu == "nvidia" else ""
    to_write["USE_AMD"] = "1" if gpu == "amd" else ""
    try:
      write_config(to_write)
      self.dirty = False
      self.status = f"saved to {os.path.relpath(CONFIG, REPO)}"
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

  def draw(self):
    self.stdscr.erase()
    h, w = self.stdscr.getmaxyx()

    title = " beampilot setup "
    self.stdscr.attron(curses.A_REVERSE | curses.A_BOLD)
    self.stdscr.addstr(0, 0, title.ljust(w - 1)[:w - 1])
    self.stdscr.attroff(curses.A_REVERSE | curses.A_BOLD)

    detail = f" GPUs: {gpu_detail()}"
    self.stdscr.addstr(1, 0, detail[:w - 1], curses.A_DIM)

    top = 3
    avail = h - top - 5
    # keep the cursor in view
    start = 0
    if self.cursor > avail - 4:
      start = self.cursor - (avail - 4)

    y = top
    for idx in range(start, len(self.rows)):
      if y >= top + avail:
        break
      kind, obj = self.rows[idx]
      if kind == "section":
        if y > top:
          y += 1
        if y >= top + avail:
          break
        self.stdscr.addstr(y, 2, obj.name.upper()[:w - 4], curses.A_BOLD | curses.color_pair(4))
        y += 1
      else:
        val = self.values.get(obj.key, obj.default)
        shown = val if val != "" else "(unset)"
        sel = idx == self.cursor
        attr = curses.A_REVERSE if sel else curses.A_NORMAL
        label = f"  {obj.label:<26} "
        self.stdscr.addstr(y, 2, label[:w - 4], attr)
        vx = 2 + len(label)
        if vx < w - 2:
          changed = val != obj.default
          vattr = curses.color_pair(2) if changed else curses.color_pair(1)
          if obj.warn and changed:
            vattr = curses.color_pair(3)
          self.stdscr.addstr(y, vx, shown[:w - vx - 2], vattr | curses.A_BOLD)
        y += 1

    cur = self.current()
    hy = h - 4
    self.stdscr.hline(hy - 1, 0, curses.ACS_HLINE, w)
    if cur:
      help_text = cur.help or ""
      self.stdscr.addstr(hy, 2, help_text[:w - 4], curses.A_DIM)
      if cur.warn and self.values.get(cur.key) != cur.default:
        self.stdscr.addstr(hy + 1, 2, f"! {cur.warn}"[:w - 4], curses.color_pair(3))
      else:
        self.stdscr.addstr(hy + 1, 2, f"{cur.key}   (default: {cur.default or 'unset'})"[:w - 4], curses.A_DIM)

    keys = " enter/←→ change   s save   r setup   L launch   m monitor   q quit "
    mark = "  ● unsaved" if self.dirty else ""
    self.stdscr.attron(curses.A_REVERSE)
    self.stdscr.addstr(h - 1, 0, (keys + mark).ljust(w - 1)[:w - 1])
    self.stdscr.attroff(curses.A_REVERSE)
    if self.status:
      self.stdscr.addstr(hy + 2, 2, self.status[:w - 4], curses.A_DIM)
    self.stdscr.refresh()

  def loop(self):
    while True:
      self.draw()
      try:
        ch = self.stdscr.getch()
      except KeyboardInterrupt:
        return
      cur = self.current()
      if ch in (ord('q'), 27):
        if self.dirty:
          self.status = "unsaved changes -- press s to save, or q again to discard"
          self.dirty = False
          continue
        return
      elif ch in (curses.KEY_DOWN, ord('j')):
        self.move(1)
      elif ch in (curses.KEY_UP, ord('k')):
        self.move(-1)
      elif ch in (curses.KEY_RIGHT, ord('l')) and cur and (cur.choices or cur.numeric):
        self.cycle(cur, 1)
      elif ch in (curses.KEY_LEFT, ord('h')) and cur and (cur.choices or cur.numeric):
        self.cycle(cur, -1)
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
  curses.curs_set(0)
  curses.use_default_colors()
  curses.init_pair(1, curses.COLOR_WHITE, -1)
  curses.init_pair(2, curses.COLOR_CYAN, -1)
  curses.init_pair(3, curses.COLOR_YELLOW, -1)
  curses.init_pair(4, curses.COLOR_GREEN, -1)
  Tui(stdscr).loop()


if __name__ == "__main__":
  if not os.path.exists(CONFIG):
    print(f"config not found at {CONFIG}", file=sys.stderr)
    sys.exit(1)
  try:
    curses.wrapper(main)
  except KeyboardInterrupt:
    pass
