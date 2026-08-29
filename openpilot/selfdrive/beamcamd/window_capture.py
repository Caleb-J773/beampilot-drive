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
import json
import os
import re
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
    return True, ("Wayland session detected -- capture goes through xdg-desktop-portal and" +
                  " PipeWire (BEAMPILOT_CAPTURE_BACKEND=auto selects it), which prompts once for" +
                  " a window or monitor. Forcing the x11 backend here would produce solid green" +
                  " frames, since an X11 grab cannot read a Wayland screen.")
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


# --------------------------------------------------------------------------
# Wayland / KWin
#
# Everything above is X11 and stays that way: X11 window coordinates are the
# only ones valid as an mss capture region, because mss grabs the X root
# window. Nothing below is consulted on the capture path -- it runs only after
# the X11 search has already come up empty, to explain why.
#
# That distinction is the whole point. On a Wayland session BeamNG normally
# still runs through XWayland (Proton/Wine defaults to Wine's X11 driver), so
# it is an ordinary X11 client, xdotool finds it, and everything above works
# unchanged. When that fails, the useful question is "is the game a native
# Wayland surface, or not running at all?" -- and only the compositor can
# answer. KWin answers in its own logical coordinate space, which does not map
# onto the X root, so it is good for telling the user what is wrong and useless
# as a capture rectangle. Saying so beats silently capturing the wrong thing.
# --------------------------------------------------------------------------

KWIN_DBUS_SERVICE = "org.kde.KWin"

# Emitted by the probe script so its output can be picked out of the journal,
# which is the only way a KWin script can hand data back to the outside world.
_KWIN_MARKER_PREFIX = "BEAMPILOT_WIN"

# This runs inside the compositor, so it has to be defensive: the window-list
# accessor was renamed between Plasma 5 (clientList) and Plasma 6 (windowList),
# and one uncaught exception would abort the whole enumeration.
_KWIN_PROBE_JS = """
(function () {
  var M = "%%MARKER%%";
  function emit(s) {
    // console.info and print reach the journal on different Plasma builds and
    // under different logging rules; emit on both and let the reader dedupe.
    try { console.info(M + " " + s); } catch (e) {}
    try { print(M + " " + s); } catch (e) {}
  }
  var list = [];
  try {
    if (typeof workspace.windowList === "function") list = workspace.windowList();
    else if (typeof workspace.clientList === "function") list = workspace.clientList();
    else if (workspace.stackingOrder) list = workspace.stackingOrder;
  } catch (e) { emit('{"probe_error":"cannot enumerate windows"}'); }
  for (var i = 0; i < list.length; i++) {
    try {
      var w = list[i];
      var g = w.frameGeometry || w.geometry || {};
      emit(JSON.stringify({
        id: String(w.internalId || i),
        caption: String(w.caption || ""),
        cls: String(w.resourceClass || ""),
        rname: String(w.resourceName || ""),
        pid: (w.pid === undefined || w.pid === null) ? -1 : w.pid,
        x: Math.round(g.x || 0), y: Math.round(g.y || 0),
        width: Math.round(g.width || 0), height: Math.round(g.height || 0),
        normal: !!w.normalWindow, minimized: !!w.minimized
      }));
    } catch (e) {}
  }
  emit('{"probe_done":true}');
})();
"""


def have_kwin() -> bool:
  """Is a KWin instance reachable on the session bus?"""
  if shutil.which("dbus-send") is None:
    return False
  out = _run(["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus",
              "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames"], timeout=6.0)
  return KWIN_DBUS_SERVICE in out


def _kwin_run_script(source: str) -> bool:
  """Load, run and unload a KWin script. True if it appeared to run."""
  import tempfile
  import uuid
  name = f"beampilot-probe-{uuid.uuid4().hex[:8]}"
  path = None
  try:
    # KWin reads the file itself, so it must exist on disk, and loadScript
    # dedupes by path -- hence a fresh temp file per probe.
    with tempfile.NamedTemporaryFile("w", suffix=".js", prefix=f"{name}-", delete=False) as fh:
      fh.write(source)
      path = fh.name

    out = _run(["dbus-send", "--session", "--print-reply", f"--dest={KWIN_DBUS_SERVICE}",
                "/Scripting", "org.kde.kwin.Scripting.loadScript",
                f"string:{path}", f"string:{name}"], timeout=8.0)
    m = re.search(r"int32\s+(-?\d+)", out)
    if m is None or int(m.group(1)) < 0:
      return False
    script_id = m.group(1)

    # Plasma has used both object paths over the years; try each.
    for obj in (f"/Scripting/Script{script_id}", f"/{script_id}"):
      if _run(["dbus-send", "--session", "--print-reply", f"--dest={KWIN_DBUS_SERVICE}",
               obj, "org.kde.kwin.Script.run"], timeout=8.0):
        _run(["dbus-send", "--session", f"--dest={KWIN_DBUS_SERVICE}",
              obj, "org.kde.kwin.Script.stop"], timeout=6.0)
        return True
    return False
  except OSError:
    return False
  finally:
    _run(["dbus-send", "--session", f"--dest={KWIN_DBUS_SERVICE}", "/Scripting",
          "org.kde.kwin.Scripting.unloadScript", f"string:{name}"], timeout=6.0)
    if path is not None:
      try:
        os.unlink(path)
      except OSError:
        pass


def _journal_lines(marker: str, since: str) -> list[str]:
  """Lines containing `marker` logged since `since` (a journalctl timestamp)."""
  attempts = (
    ["journalctl", "--user", "-o", "cat", "--since", since, "_COMM=kwin_wayland"],
    ["journalctl", "--user", "-o", "cat", "--since", since, "_COMM=kwin_x11"],
    ["journalctl", "-o", "cat", "--since", since, "_COMM=kwin_wayland"],
    ["journalctl", "--user", "-o", "cat", "--since", since],
  )
  for argv in attempts:
    hits = [ln for ln in _run(argv, timeout=8.0).splitlines() if marker in ln]
    if hits:
      return hits
  return []


def kwin_windows() -> list[dict] | None:
  """Every window KWin knows about, or None if KWin could not be queried.

  None and [] mean different things. None is "no usable answer from the
  compositor" (not KDE, no dbus-send, script blocked, journal unreadable);
  [] is "KWin answered and has no windows". Only None should make a caller
  fall back to guessing.

  Geometry is KWin's logical coordinate space and is NOT a valid mss capture
  region -- see the section comment above.
  """
  if not have_kwin():
    return None

  import datetime
  import uuid
  marker = f"{_KWIN_MARKER_PREFIX}-{uuid.uuid4().hex[:8]}"
  # A few seconds of slack absorbs clock skew between us and journald.
  since = (datetime.datetime.now() - datetime.timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")

  if not _kwin_run_script(_KWIN_PROBE_JS.replace("%%MARKER%%", marker)):
    return None

  found: dict[str, dict] = {}
  saw_output = False
  for line in _journal_lines(marker, since):
    _, _, payload = line.partition(marker)
    try:
      obj = json.loads(payload.strip())
    except (ValueError, TypeError):
      continue
    saw_output = True
    if not isinstance(obj, dict) or "id" not in obj:
      continue  # the probe_done / probe_error sentinel
    # Both emit() channels log the same window; they're equal, last wins.
    found[obj["id"]] = obj

  if not saw_output:
    # The script ran but nothing reached the journal (logging rules, or no
    # journal access). "No windows" and "can't read" are indistinguishable
    # here, so report the honest answer: no usable data.
    return None
  return list(found.values())


def _kwin_haystack(w: dict) -> str:
  return f"{w.get('caption', '')} {w.get('cls', '')} {w.get('rname', '')}".lower()


def kwin_matches(match: str = "beamng") -> list[dict] | None:
  """KWin windows whose caption/class/name contain `match`, case-insensitively."""
  wins = kwin_windows()
  if wins is None:
    return None
  return [w for w in wins if match.lower() in _kwin_haystack(w)]


def explain_not_found(match: str = "beamng") -> str:
  """Why the X11 search found no game window, as something actionable.

  Only called after find_window() has already returned None, so it is free to
  take its time.
  """
  if not have_xdotool():
    return ("xdotool is not installed, so no window search was possible at all."
            + " Install it (Debian/Ubuntu: sudo apt install xdotool -- Arch: sudo pacman -S"
            + " xdotool -- Fedora: sudo dnf install xdotool), or set BEAMPILOT_CAM_REGION"
            + " / BEAMPILOT_CAM_MONITOR to capture a fixed rectangle instead.")

  if not os.environ.get("DISPLAY"):
    if session_type() == "wayland":
      return ("DISPLAY is not set, so there is no XWayland server to search."
              + " BeamNG runs as an X11 client under Wayland, so XWayland must be running and"
              + " DISPLAY exported (usually :0) for the game to be found or captured.")
    return "DISPLAY is not set, so no X server could be queried."

  hits = kwin_matches(match)
  if hits is None:
    if session_type() == "wayland":
      return (f"No X11 window matching {match!r}. This is a Wayland session and KWin could not"
              + " be queried, so whether the game is a native Wayland window is unconfirmed."
              + " If BeamNG is running, it is most likely a native Wayland surface, which X11"
              + " screen capture cannot read -- start it under XWayland, or use an X11 session.")
    return (f"No window matching {match!r} was found. Is BeamNG actually running?"
            + " Detection also skips windows smaller than"
            + f" {MIN_GAME_WIDTH}x{MIN_GAME_HEIGHT}.")

  if hits:
    w = hits[0]
    return (f"KWin reports a matching window ({w.get('caption', '')!r}, class"
            + f" {w.get('cls', '')!r}, {w.get('width')}x{w.get('height')}"
            + f" at +{w.get('x')}+{w.get('y')}) but xdotool cannot see it, so it is a NATIVE"
            + " WAYLAND window rather than an XWayland one, which X11 window tracking cannot"
            + " follow. This is not a problem to work around: set"
            + " BEAMPILOT_CAPTURE_BACKEND=portal (or leave it on auto) to capture through"
            + " xdg-desktop-portal and PipeWire, which streams the window the compositor owns"
            + " and needs no X11 tracking at all.")

  return (f"Neither xdotool nor KWin can see a window matching {match!r};"
          + " KWin answered, so the compositor genuinely has no such window. Is BeamNG running?")


def diagnose(match: str = "beamng") -> str:
  """Human-readable report of what detection can and cannot see.

  Meant to be pasted into a bug report:
  `python -m openpilot.selfdrive.beamcamd.window_capture`
  """
  lines = [
    "beampilot window detection diagnosis",
    "------------------------------------",
    f"session type      : {session_type()}",
    f"XDG_SESSION_TYPE  : {os.environ.get('XDG_SESSION_TYPE', '<unset>')}",
    f"WAYLAND_DISPLAY   : {os.environ.get('WAYLAND_DISPLAY', '<unset>')}",
    f"DISPLAY           : {os.environ.get('DISPLAY', '<unset>')}",
    f"xdotool present   : {have_xdotool()}",
    f"xprop present     : {shutil.which('xprop') is not None}",
    f"dbus-send present : {shutil.which('dbus-send') is not None}",
    f"KWin on the bus   : {have_kwin()}",
    "",
    f"capture support   : {capture_support()[1]}",
    "",
  ]

  found = candidates(match)
  lines.append(f"X11 windows matching {match!r} (these ARE capturable): {len(found)}")
  for w in found:
    lines.append(f"  [{w['how']:>7}] id={w['id']} {w['width']}x{w['height']}"
                 + f" at +{w['left']}+{w['top']}  {w['name']!r}")
  if not found:
    lines.append("  (none)")

  lines.append("")
  kw = kwin_windows()
  if kw is None:
    lines.append("KWin windows: not available (not KDE, or KWin could not be queried)")
  else:
    hits = [w for w in kw if match.lower() in _kwin_haystack(w)]
    lines.append(f"KWin windows: {len(kw)} total, {len(hits)} matching {match!r}"
                 + " (logical coords -- NOT valid capture regions)")
    for w in hits:
      lines.append(f"  id={w.get('id')} {w.get('width')}x{w.get('height')}"
                   + f" at +{w.get('x')}+{w.get('y')}  {w.get('caption')!r}"
                   + f" class={w.get('cls')!r}")

  if not found:
    lines += ["", "Why nothing was picked:", f"  {explain_not_found(match)}"]
  return "\n".join(lines)


if __name__ == "__main__":
  print(diagnose(os.environ.get("BEAMPILOT_CAM_WINDOW", "beamng").strip() or "beamng"))
