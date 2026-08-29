#!/usr/bin/env python3
"""Guided installer and environment check for beampilot.

Nothing here assumes anything about the machine it runs on: Steam libraries are
read from libraryfolders.vdf rather than guessed, the display server is probed
rather than assumed, and every check reports what it found and what to do about
it instead of failing silently.

  uv run python tools/beampilot_setup.py

Read-only until you explicitly choose to install the mod or run the build.
"""
import os
import re
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Both are required. beampilot_bridge carries telemetry and control;
# openpilot_cam is the rigidly-mounted, FOV-matched camera that beampilot.lua
# selects by name at spawn.
MODS = ("beampilot_bridge", "openpilot_cam")

GREEN, RED, YELLOW, BLUE, DIM, BOLD, RESET = (
  "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[1m", "\033[0m")

OK, WARN, FAIL, INFO = f"{GREEN}✓{RESET}", f"{YELLOW}!{RESET}", f"{RED}✗{RESET}", f"{BLUE}·{RESET}"


def hr(title=""):
  cols = shutil.get_terminal_size((80, 24)).columns
  if title:
    print(f"\n{BOLD}{title}{RESET}")
    print(DIM + "─" * min(cols, 78) + RESET)
  else:
    print(DIM + "─" * min(cols, 78) + RESET)


def run(argv, timeout=10):
  try:
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()
  except FileNotFoundError:
    return 127, "", "not found"
  except (subprocess.SubprocessError, OSError) as e:
    return 1, "", str(e)


def ask(prompt, default=True):
  suffix = "[Y/n]" if default else "[y/N]"
  try:
    raw = input(f"{BOLD}{prompt}{RESET} {suffix} ").strip().lower()
  except (EOFError, KeyboardInterrupt):
    print()
    return False
  if not raw:
    return default
  return raw.startswith("y")


# --------------------------------------------------------------------------
# Steam / BeamNG discovery
# --------------------------------------------------------------------------

def steam_roots() -> list[str]:
  """Every plausible Steam root, including flatpak and the older ~/.steam path."""
  home = os.path.expanduser("~")
  candidates = [
    os.path.join(home, ".local/share/Steam"),
    os.path.join(home, ".steam/steam"),
    os.path.join(home, ".steam/root"),
    os.path.join(home, ".var/app/com.valvesoftware.Steam/data/Steam"),
    "/usr/local/games/Steam",
  ]
  return [p for p in candidates if os.path.isdir(p)]


def steam_libraries() -> list[str]:
  """Parse libraryfolders.vdf so games on other drives are found too."""
  libs: list[str] = []
  for root in steam_roots():
    libs.append(root)
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    if not os.path.exists(vdf):
      continue
    try:
      with open(vdf, encoding="utf-8", errors="ignore") as f:
        for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
          p = m.group(1)
          if os.path.isdir(p):
            libs.append(p)
    except OSError:
      pass
  seen, out = set(), []
  for lib in libs:
    real = os.path.realpath(lib)
    if real not in seen:
      seen.add(real)
      out.append(lib)
  return out


def find_beamng_install() -> str | None:
  for lib in steam_libraries():
    path = os.path.join(lib, "steamapps", "common", "BeamNG.drive")
    if os.path.isdir(path):
      return path
  return None


def find_beamng_userdir() -> str | None:
  """The userfolder holds mods/. Its version subdirectory changes per release."""
  home = os.path.expanduser("~")
  bases = [
    os.path.join(home, ".local/share/BeamNG/BeamNG.drive"),
    os.path.join(home, ".var/app/com.valvesoftware.Steam/data/BeamNG/BeamNG.drive"),
  ]
  for base in bases:
    if not os.path.isdir(base):
      continue
    current = os.path.join(base, "current")
    if os.path.isdir(current):
      return current
    # fall back to the highest-numbered version directory
    versions = []
    for name in os.listdir(base):
      full = os.path.join(base, name)
      if os.path.isdir(full) and re.match(r"^\d+\.\d+", name):
        versions.append((name, full))
    if versions:
      return sorted(versions)[-1][1]
  return None


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def check_os():
  hr("System")
  if sys.platform != "linux":
    print(f"  {FAIL} {sys.platform} -- openpilot is Linux only. This will not work.")
    return False
  distro = ""
  try:
    with open("/etc/os-release") as f:
      for line in f:
        if line.startswith("PRETTY_NAME="):
          distro = line.split("=", 1)[1].strip().strip('"')
  except OSError:
    pass
  print(f"  {OK} Linux{f' ({distro})' if distro else ''}")

  v = sys.version_info
  if (v.major, v.minor) == (3, 12):
    print(f"  {OK} Python {v.major}.{v.minor}.{v.micro}")
  else:
    print(f"  {WARN} Python {v.major}.{v.minor} -- openpilot targets 3.12; uv will fetch it")
  return True


def check_display():
  hr("Display server")
  sys.path.insert(0, REPO)
  try:
    from openpilot.selfdrive.beamcamd.window_capture import capture_support, have_xdotool, session_type
  except ImportError:
    print(f"  {WARN} could not import window_capture (run the build first)")
    return True

  session = session_type()
  usable, explanation = capture_support()
  mark = OK if (usable and session == "x11") else (WARN if usable else FAIL)
  print(f"  {mark} session: {session}")
  for line in _wrap(explanation, 72):
    print(f"      {DIM}{line}{RESET}")

  if have_xdotool():
    print(f"  {OK} xdotool present -- window tracking available")
  else:
    print(f"  {WARN} xdotool missing -- window tracking unavailable, falling back to monitor capture")
    print(f"      {DIM}install it for per-window capture:{RESET}")
    print(f"      {DIM}  apt install xdotool   |   pacman -S xdotool   |   dnf install xdotool{RESET}")

  if session == "wayland":
    # Detection and capture are both X11-only. Under Wayland that is fine as
    # long as BeamNG is an XWayland client (the normal case for Proton/Wine),
    # so say plainly what has to be true rather than just flagging "wayland".
    try:
      from openpilot.selfdrive.beamcamd.window_capture import have_kwin
      kwin = have_kwin()
    except ImportError:
      kwin = False
    if os.environ.get("DISPLAY"):
      print(f"  {OK} XWayland present (DISPLAY={os.environ['DISPLAY']})"
            + " -- BeamNG should be visible to both tracking and capture")
    else:
      print(f"  {FAIL} DISPLAY is unset, so there is no XWayland server")
      print(f"      {DIM}BeamNG runs as an X11 client; without XWayland it cannot be"
            + f" found or captured.{RESET}")
    if kwin:
      print(f"  {INFO} KWin detected -- if no window is found, setup can ask the compositor")
      print(f"      {DIM}whether BeamNG is a native Wayland window (which X11 capture"
            + f" cannot read).{RESET}")
    print(f"      {DIM}Full report any time:"
          + f" python -m openpilot.selfdrive.beamcamd.window_capture{RESET}")
  return usable


def _wrap(text, width):
  words, line, out = text.split(), "", []
  for w in words:
    if len(line) + len(w) + 1 > width:
      out.append(line)
      line = w
    else:
      line = f"{line} {w}".strip()
  if line:
    out.append(line)
  return out


def amd_gfx_targets():
  """[(tinygrad index, gfx_target_version)] for each KFD GPU node.

  Mirrors ops_amd.py's own enumeration: only nodes with a nonzero gpu_id count,
  in numeric node order, so the index matches what DEV=":N+AMD" selects.
  """
  topo = "/sys/devices/virtual/kfd/kfd/topology/nodes"
  out = []
  try:
    nodes = sorted(os.listdir(topo), key=lambda n: int(n) if n.isdigit() else 1 << 30)
  except OSError:
    return out
  idx = -1
  for node in nodes:
    try:
      with open(os.path.join(topo, node, "gpu_id")) as fh:
        if fh.read().strip() in ("", "0"):
          continue
      idx += 1
      with open(os.path.join(topo, node, "properties")) as fh:
        for line in fh:
          if line.startswith("gfx_target_version"):
            out.append((idx, int(line.split()[1])))
            break
    except (OSError, ValueError):
      continue
  return out


def check_gpu():
  hr("GPU")
  found = False
  usable_nv = None

  # tinygrad's NV backend is not CUDA -- it only implements Ampere and newer
  # command classes, so an older card is not "slower", it cannot run the model
  # at all (modeld dies with a bare StopIteration in ops_nv.py). On a mixed-GPU
  # machine tinygrad defaults to index 0, which may well be the unusable one,
  # so show per-card capability and which index will actually be picked.
  rc, out, _ = run(["nvidia-smi", "--query-gpu=index,name,memory.total,compute_cap",
                    "--format=csv,noheader"])
  if rc == 0 and out:
    found = True
    for line in out.splitlines():
      parts = [p.strip() for p in line.split(",")]
      if len(parts) < 4:
        print(f"  {OK} NVIDIA: {line.strip()}")
        continue
      idx, name, mem, cap = parts[0], parts[1], parts[2], parts[3]
      try:
        ok_cap = float(cap) >= 8.0
      except ValueError:
        ok_cap = False
      if ok_cap:
        if usable_nv is None:
          usable_nv = idx
        print(f"  {OK} NVIDIA {idx}: {name}, {mem} (compute {cap})")
      else:
        print(f"  {WARN} NVIDIA {idx}: {name}, {mem} (compute {cap})"
              + " -- too old for tinygrad, needs >= 8.0 (Ampere)")
    if usable_nv is not None:
      print(f"      {DIM}set USE_NV=1; config_beampilot.sh will select GPU {usable_nv}"
            + f" via DEV=\":{usable_nv}+NV\"{RESET}")
    else:
      print(f"  {FAIL} no NVIDIA GPU is new enough for tinygrad's NV backend")

  rc, out, _ = run(["lspci"])
  if rc == 0:
    for line in out.splitlines():
      if re.search(r"VGA.*\b(AMD|ATI|Radeon)\b", line, re.I):
        print(f"  {OK} AMD: {line.split(':', 2)[-1].strip()[:60]}")
        found = True

  # Same story for AMD: gfx942, gfx950 and gfx11xx/gfx12xx only.
  for idx, ver in amd_gfx_targets():
    target = (ver // 10000, (ver // 100) % 100, ver % 100)
    arch = f"gfx{target[0]}{target[1]:x}{target[2]:x}"
    if target in ((9, 4, 2), (9, 5, 0)) or target[0] in (11, 12):
      print(f"      {DIM}AMD {idx}: {arch} -- usable with USE_AMD=1"
            + f" (DEV=\":{idx}+AMD\"){RESET}")
    else:
      print(f"  {WARN} AMD {idx}: {arch} -- tinygrad's AMD backend does not support it"
            + " (needs gfx942, gfx950 or gfx11xx/gfx12xx)")

  if not found:
    print(f"  {WARN} no discrete GPU detected -- the driving model will be very slow on CPU")
    return False
  print(f"      {DIM}the GPU renders the game AND runs the model; that contention is normal{RESET}")
  return True


def detect_package_manager() -> tuple[str, list[str]] | None:
  """(name, install command prefix) for this distro, or None if unrecognised."""
  managers = [
    ("apt", ["sudo", "apt", "install", "-y"]),
    ("pacman", ["sudo", "pacman", "-S", "--needed", "--noconfirm"]),
    ("dnf", ["sudo", "dnf", "install", "-y"]),
    ("zypper", ["sudo", "zypper", "install", "-y"]),
    ("apk", ["sudo", "apk", "add"]),
    ("xbps-install", ["sudo", "xbps-install", "-y"]),
  ]
  for name, cmd in managers:
    if shutil.which(name):
      return name, cmd
  return None


# Package names differ per distro for the same tool.
SYSTEM_PACKAGES = {
  "xdotool": {"apt": "xdotool", "pacman": "xdotool", "dnf": "xdotool",
              "zypper": "xdotool", "apk": "xdotool", "xbps-install": "xdotool"},
  "xprop": {"apt": "x11-utils", "pacman": "xorg-xprop", "dnf": "xorg-x11-utils",
            "zypper": "xprop", "apk": "xprop", "xbps-install": "xprop"},
}


def check_system_deps():
  """Tools beampilot shells out to. Missing ones degrade features, not break them."""
  hr("System packages")
  pm = detect_package_manager()
  missing = [tool for tool in SYSTEM_PACKAGES if not shutil.which(tool)]

  for tool in SYSTEM_PACKAGES:
    if shutil.which(tool):
      print(f"  {OK} {tool}")
    else:
      print(f"  {WARN} {tool} missing -- window tracking falls back to whole-monitor capture")

  if not missing:
    return
  if pm is None:
    print(f"  {INFO} unknown package manager; install manually: {', '.join(missing)}")
    return

  name, cmd = pm
  pkgs = sorted({SYSTEM_PACKAGES[t].get(name, t) for t in missing})
  full = " ".join(cmd + pkgs)
  print(f"\n  detected {name}. To install: {BLUE}{full}{RESET}")
  if ask("Run that now? (needs sudo)", default=False):
    subprocess.run(cmd + pkgs, check=False)


def check_python_deps():
  """uv owns the Python side; just report whether the venv has been built."""
  hr("Python environment")
  if not shutil.which("uv"):
    print(f"  {FAIL} uv not found -- it manages the Python environment and the build")
    print(f"      {DIM}curl -LsSf https://astral.sh/uv/install.sh | sh{RESET}")
    return False
  print(f"  {OK} uv present")

  venv = os.path.join(REPO, ".venv")
  if os.path.isdir(venv):
    print(f"  {OK} .venv exists")
    missing = []
    for mod, why in (("mss", "screen capture"), ("cv2", "colour conversion"),
                     ("evdev", "cruise keys"), ("uinput", "joystick control mode")):
      rc, _, _ = run([os.path.join(venv, "bin", "python"), "-c", f"import {mod}"], timeout=30)
      if rc == 0:
        print(f"  {OK} {mod} ({why})")
      else:
        print(f"  {WARN} {mod} missing ({why})")
        missing.append(mod)
    if missing and ask("Sync Python dependencies now? (uv sync)", default=True):
      subprocess.run(["uv", "sync", "--all-extras"], cwd=REPO, check=False)
    return True

  print(f"  {INFO} .venv not built yet -- setup_beampilot.sh will create it")
  return True


def check_groups():
  hr("Permissions")
  ok = True
  try:
    import grp
    groups = [grp.getgrgid(g).gr_name for g in os.getgroups()]
  except (ImportError, KeyError, OSError):
    groups = []
  if "input" in groups:
    print(f"  {OK} in the 'input' group -- cruise keys will work")
  else:
    print(f"  {FAIL} NOT in the 'input' group -- cruise keys will not be readable")
    print(f"      {DIM}sudo usermod -aG input $USER   (then log out and back in){RESET}")
    ok = False
  if os.path.exists("/dev/uinput"):
    print(f"  {OK} /dev/uinput exists (needed only for joystick control mode)")
  else:
    print(f"  {INFO} /dev/uinput missing -- only matters for BEAMPILOT_CONTROL_MODE=joystick")
  return ok


def check_beamng():
  hr("BeamNG.drive")
  install = find_beamng_install()
  if install:
    print(f"  {OK} game: {install}")
  else:
    print(f"  {WARN} game not found in any Steam library")
    print(f"      {DIM}searched: {', '.join(steam_libraries()) or 'no Steam install found'}{RESET}")

  userdir = find_beamng_userdir()
  if userdir:
    print(f"  {OK} userfolder: {userdir}")
  else:
    print(f"  {FAIL} userfolder not found -- launch BeamNG.drive once, then re-run this")
    return None, None

  missing = []
  for name in MODS:
    mod = os.path.join(userdir, "mods", "unpacked", name)
    src = os.path.join(REPO, "tools", "beamng_mod", name)
    if os.path.islink(mod) and os.path.realpath(mod) == os.path.realpath(src):
      print(f"  {OK} {name} installed (symlink -> repo, edits apply live)")
    elif os.path.exists(mod):
      print(f"  {WARN} {name} exists but is not a link to this repo: {mod}")
    else:
      print(f"  {INFO} {name} not installed yet")
      missing.append(name)
  return userdir, missing


def install_mods(userdir, names):
  """Both mods are required: beampilot_bridge does telemetry and control,
  openpilot_cam provides the camera beampilot.lua selects by name. Without the
  latter, that selection silently does nothing and beamcamd captures whatever
  camera the player happened to be using."""
  for name in names:
    src = os.path.join(REPO, "tools", "beamng_mod", name)
    mod = os.path.join(userdir, "mods", "unpacked", name)
    if not os.path.isdir(src):
      print(f"  {FAIL} mod source missing at {src}")
      continue
    try:
      os.makedirs(os.path.dirname(mod), exist_ok=True)
      if os.path.islink(mod) or os.path.exists(mod):
        if not ask(f"{mod} already exists. Replace it?", default=False):
          continue
        if os.path.islink(mod) or os.path.isfile(mod):
          os.unlink(mod)
        else:
          shutil.rmtree(mod)
      os.symlink(src, mod)
      print(f"  {OK} linked {name} -> {src}")
    except OSError as e:
      print(f"  {FAIL} could not install {name}: {e}")
  print(f"      {DIM}edits under tools/beamng_mod/ now apply live (Ctrl+L in game){RESET}")


def detect_window():
  hr("Capture target")
  sys.path.insert(0, REPO)
  try:
    from openpilot.selfdrive.beamcamd.window_capture import candidates, explain_not_found
  except ImportError:
    print(f"  {WARN} window_capture unavailable")
    return
  found = candidates()
  if not found:
    print(f"  {INFO} no BeamNG window found right now.")
    # "no window found" is the single most-reported problem and has several
    # very different causes (game not running, xdotool missing, no DISPLAY, or
    # a native Wayland surface that X11 simply cannot see). Ask for the real
    # reason instead of printing the same guess every time.
    for line in _wrap(explain_not_found(), 72):
      print(f"      {DIM}{line}{RESET}")
    print(f"      {DIM}Start the game and spawn a vehicle, then re-run to pick a window.{RESET}")
    print(f"      {DIM}Without one, beamcamd captures a whole monitor (BEAMPILOT_CAM_MONITOR).{RESET}")
    return
  print(f"  found {len(found)} candidate window(s):")
  for i, w in enumerate(found, 1):
    print(f"    {i}. {w['width']}x{w['height']} at +{w['left']}+{w['top']}  "
          + f"{DIM}[{w['how']}]{RESET} {w['name'][:40]!r}")
  print(f"\n  {DIM}Set BEAMPILOT_CAM_WINDOW=beamng to track the window automatically,{RESET}")
  print(f"  {DIM}or BEAMPILOT_CAM_REGION=left,top,width,height to pin an exact rectangle.{RESET}")


def main():
  print(f"\n{BOLD}beampilot setup{RESET}")
  print(f"{DIM}Checks your system and installs the BeamNG mod. Read-only until you confirm.{RESET}")

  if not check_os():
    return 1
  check_gpu()
  check_system_deps()
  check_display()
  check_python_deps()
  perms_ok = check_groups()
  userdir, missing_mods = check_beamng()
  detect_window()

  hr("Next steps")
  if userdir and missing_mods:
    label = "mod" if len(missing_mods) == 1 else "mods"
    if ask(f"Install the BeamNG {label} ({', '.join(missing_mods)}) now?"):
      install_mods(userdir, missing_mods)

  if not perms_ok:
    print(f"  {WARN} fix the 'input' group before driving, or the cruise keys won't work")

  built = os.path.isdir(os.path.join(REPO, ".venv"))
  if not built:
    print(f"  {INFO} openpilot is not built yet.")
    if ask("Run ./setup_beampilot.sh now? (takes a while)", default=False):
      subprocess.run(["./setup_beampilot.sh"], cwd=REPO, check=False)
  else:
    print(f"  {OK} .venv exists -- openpilot appears built")

  print(f"""
  {BOLD}Then:{RESET}
    {BLUE}uv run python tools/beampilot_tui.py{RESET}   configure settings
    {BLUE}./launch_beampilot.sh{RESET}                   start openpilot
    {BLUE}uv run python tools/beampilot_monitor.py{RESET}  watch it (second terminal)

  {DIM}Engage with 'i' once you're above 20mph. See README.md.{RESET}
""")
  return 0


if __name__ == "__main__":
  try:
    sys.exit(main())
  except KeyboardInterrupt:
    print("\ncancelled")
    sys.exit(130)
