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


def gpu_detail() -> str:
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
          names.append(line.split(":", 2)[-1].strip())
  except (subprocess.SubprocessError, OSError):
    pass
  return "; ".join(names) if names else "none detected"


def build_sections() -> list[Section]:
  gpus = gpu_options()
  return [
    Section("Hardware", "What this machine has, and which model to run.", [
      Setting("BEAMPILOT_GPU", "GPU backend",
              "Which GPU runs the driving model. It shares the card with BeamNG's rendering.",
              gpus[0], choices=gpus),
      Setting("CHESTNUT", "Chestnut model",
              "Larger, better-driving model. Needs 8GB+ VRAM (standard needs 4GB).",
              "0", choices=["0", "1"]),
      Setting("BIG", "Display size",
              "1 = comma 3/3X window (2160x1080). 0 = comma 4 (536x240) -- tiny on a desktop.",
              "1", choices=["1", "0"]),
      Setting("SCALE", "Window scale",
              "Multiplies the window size. 0.6 gives roughly 1296x648. Blank = automatic.",
              "", numeric=True, step=0.1),
    ]),
    Section("Car", "The car openpilot believes it is driving.", [
      Setting("FINGERPRINT", "Fingerprint",
              "Changing this breaks beamngd -- CAN layouts differ per car.",
              "HONDA_CIVIC_2022",
              warn="beamngd packs Honda Bosch CAN; another car needs code changes"),
      Setting("BEAMPILOT_STEER_LOCK_DEG", "Steering lock (deg)",
              "Your BeamNG vehicle's full lock. Measure it in the monitor. Too low oversteers, too high runs wide.",
              "510.0", numeric=True, step=10.0),
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
    Section("Camera", "beamcamd captures the BeamNG window off your desktop.", [
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
