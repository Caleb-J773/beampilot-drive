"""Find the BeamNG.drive window on X11 and turn it into a capture region.

Capturing a fixed monitor works only when BeamNG is fullscreen on that exact
display. Tracking the window instead means a windowed, moved or resized game
still gets captured correctly, and a second monitor can be used for the
openpilot UI and the monitor tool.

Detection is deliberately layered, because none of the obvious approaches is
reliable on its own:

  1. by process   - the honest way, but BeamNG runs inside Steam's
                    pressure-vessel container, so the PID X reports in
                    _NET_WM_PID frequently doesn't match the PID pgrep sees.
                    Works when it works; often finds nothing.
  2. by WM_CLASS  - stable across window titles and locales.
  3. by title     - last resort, and the one that needs filtering: a naive
                    search for "beamng" happily matches a terminal tab or an
                    editor with the project open, and capturing those instead
                    of the game is a confusing failure to debug. Windows whose
                    class looks like a terminal/editor/browser are excluded,
                    as are implausibly small ones.

Everything shells out to xdotool rather than depending on python-xlib, since
xdotool is already present on essentially any X11 desktop and adds no Python
dependency.
"""
import os
import shutil
import subprocess

# Window classes that are never the game, but frequently match a title search
# because the project directory or a chat window has "beamng" in its name.
EXCLUDED_CLASS_HINTS = (
  "terminal", "kitty", "alacritty", "konsole", "xterm", "wezterm", "ghostty", "tilix",
  "code", "codium", "sublime", "gedit", "kate", "emacs", "jetbrains", "pycharm",
  "firefox", "chrome", "chromium", "brave", "navigator",
  "discord", "slack", "telegram", "obs", "nautilus", "thunar", "dolphin",
)

# Anything smaller than this is a dialog, tooltip or launcher, not the game.
MIN_GAME_WIDTH = 640
MIN_GAME_HEIGHT = 480


def _run(argv: list[str], timeout: float = 5.0) -> str:
  try:
    out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return out.stdout if out.returncode == 0 else ""
  except (subprocess.SubprocessError, OSError):
    return ""


def have_xdotool() -> bool:
  return shutil.which("xdotool") is not None


def session_type() -> str:
  """'x11', 'wayland', or 'unknown'."""
  xdg = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
  if xdg in ("x11", "wayland"):
    return xdg
  if os.environ.get("WAYLAND_DISPLAY"):
    return "wayland"
  if os.environ.get("DISPLAY"):
    return "x11"
  return "unknown"


def capture_support() -> tuple[bool, str]:
  """(usable, explanation) for screen capture in this session.

  BeamNG.drive is an X11 client, so under a Wayland session it runs through
  XWayland and both xdotool and mss can usually still see it -- "usually",
  because whether an XWayland window's pixels are readable from the X root
  depends on the compositor. Capturing *native* Wayland surfaces is a different
  problem entirely: it needs the xdg-desktop-portal ScreenCast API and a
  PipeWire stream, which this does not implement.

  So rather than claim Wayland support, report honestly and let the caller try
  anyway -- on most Wayland setups the XWayland path does work for this game.
  """
  session = session_type()
  if not os.environ.get("DISPLAY"):
    return False, ("no DISPLAY set -- capture needs an X server. Under Wayland, XWayland" +
                   " normally provides one; check that XWayland is running.")
  if session == "wayland":
    return True, ("Wayland session detected. BeamNG is an X11 client so it runs under XWayland," +
                  " which usually captures fine -- but this depends on your compositor, and" +
                  " native Wayland capture (PipeWire portal) is not implemented. If frames come" +
                  " out black, log into an X11/Xorg session.")
  if session == "x11":
    return True, "X11 session -- fully supported."
  return True, f"unknown session type ({session!r}); attempting X11 capture."


def window_geometry(win_id: str) -> dict | None:
  """{'left','top','width','height'} for a window id, or None."""
  out = _run(["xdotool", "getwindowgeometry", "--shell", win_id])
  if not out:
    return None
  vals: dict[str, int] = {}
  for line in out.splitlines():
    if "=" not in line:
      continue
    k, _, v = line.partition("=")
    try:
      vals[k.strip()] = int(v.strip())
    except ValueError:
      pass
  if not {"X", "Y", "WIDTH", "HEIGHT"} <= vals.keys():
    return None
  return {"left": vals["X"], "top": vals["Y"], "width": vals["WIDTH"], "height": vals["HEIGHT"]}


def window_name(win_id: str) -> str:
  return _run(["xdotool", "getwindowname", win_id]).strip()


def window_class(win_id: str) -> str:
  out = _run(["xprop", "-id", win_id, "WM_CLASS"])
  return out.strip().lower()


def _plausible(win_id: str, check_class: bool) -> dict | None:
  geo = window_geometry(win_id)
  if geo is None:
    return None
  if geo["width"] < MIN_GAME_WIDTH or geo["height"] < MIN_GAME_HEIGHT:
    return None
  if check_class:
    cls = window_class(win_id)
    if any(hint in cls for hint in EXCLUDED_CLASS_HINTS):
      return None
  return geo


def candidates(match: str = "beamng") -> list[dict]:
  """Every plausible game window, best guess first.

  Each entry: {'id', 'name', 'class', 'left', 'top', 'width', 'height', 'how'}.
  Used by the TUI to let a human pick when detection is ambiguous.
  """
  if not have_xdotool():
    return []

  found: dict[str, dict] = {}

  def add(win_id: str, how: str, check_class: bool):
    win_id = win_id.strip()
    if not win_id or win_id in found:
      return
    geo = _plausible(win_id, check_class)
    if geo is None:
      return
    found[win_id] = {
      "id": win_id, "name": window_name(win_id), "class": window_class(win_id),
      "how": how, **geo,
    }

  # 1. by process
  pids = _run(["pgrep", "-f", "BeamNG.drive"]).split()
  for pid in pids:
    for win_id in _run(["xdotool", "search", "--pid", pid]).split():
      add(win_id, "process", check_class=False)

  # 2. by WM_CLASS
  for win_id in _run(["xdotool", "search", "--class", match]).split():
    add(win_id, "class", check_class=False)

  # 3. by title, filtered
  for win_id in _run(["xdotool", "search", "--name", match]).split():
    add(win_id, "title", check_class=True)

  order = {"process": 0, "class": 1, "title": 2}
  return sorted(found.values(),
                key=lambda w: (order.get(w["how"], 9), -(w["width"] * w["height"])))


def find_window(match: str = "beamng") -> dict | None:
  """Best guess at the game window, or None."""
  found = candidates(match)
  return found[0] if found else None
