"""Wayland screen capture via xdg-desktop-portal ScreenCast + PipeWire.

X11 capture (mss) reads pixels straight out of the X server. Wayland has no
equivalent: a client cannot read the screen, by design. The only supported route
is to ask the compositor through xdg-desktop-portal, which prompts the user to
pick a window or monitor and then hands back a PipeWire stream.

This is why "BeamNG shows up but every frame is green": under Wayland an X11
grab of the root returns nothing, the NV12 buffer is never written, and an
all-zero NV12 buffer decodes to RGB(0,135,0) -- green, not black, because
Y=0/U=0/V=0 is not the encoding of black (that is Y=16, U=V=128). A green
picture is the signature of a capture that produced no data at all.

Two processes' worth of plumbing, kept deliberately dependency-light:

  * the portal handshake speaks D-Bus directly through jeepney (already a
    dependency, and it supports the file-descriptor passing this needs), rather
    than pulling in PyGObject;
  * the PipeWire stream is consumed by a `gst-launch-1.0` subprocess that
    converts and scales to exactly the model's NV12 resolution and writes raw
    frames to a pipe. GStreamer's pipewiresrc already handles format
    negotiation, so this avoids binding libpipewire from Python.

The portal prompts on first use. `persist_mode=2` plus the returned restore
token means later runs reuse the same selection without a dialog.
"""
import os
import shutil
import subprocess
import threading
import time

CURSOR_HIDDEN, CURSOR_EMBEDDED = 1, 2
SOURCE_MONITOR, SOURCE_WINDOW = 1, 2

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"

# Where the restore token is kept so the portal only prompts once. Not in the
# repo: it is per-user, per-machine state.
def restore_token_path() -> str:
  base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
  return os.path.join(base, "beampilot", "screencast_restore_token")


def _unwrap(value):
  """jeepney returns variants as (signature, value); unwrap recursively."""
  if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
    return _unwrap(value[1])
  if isinstance(value, dict):
    return {k: _unwrap(v) for k, v in value.items()}
  if isinstance(value, list):
    return [_unwrap(v) for v in value]
  return value


class PortalError(RuntimeError):
  pass


class PortalScreenCast:
  """One ScreenCast session: handshake, then a PipeWire node id and fd."""

  def __init__(self, cursor_mode: int = CURSOR_HIDDEN,
               source_types: int = SOURCE_MONITOR | SOURCE_WINDOW,
               use_restore_token: bool = True):
    self.cursor_mode = cursor_mode
    self.source_types = source_types
    self.use_restore_token = use_restore_token
    self.conn = None
    self.session_handle = None
    self.node_id = None
    self.fd = None
    self.stream_size = None

  # -- D-Bus helpers -------------------------------------------------------
  def _request(self, method: str, signature: str, body: tuple, token: str):
    """Call a portal method and block for its async Response signal.

    The Request object path is predictable from our bus name and the token, so
    the match rule can be installed BEFORE the call -- otherwise a fast portal
    can answer before we are listening.
    """
    from jeepney import DBusAddress, MatchRule, new_method_call
    from jeepney.io.blocking import Proxy
    from jeepney.bus_messages import message_bus

    sender = self.conn.unique_name.lstrip(":").replace(".", "_")
    req_path = f"{PORTAL_PATH}/request/{sender}/{token}"

    rule = MatchRule(type="signal", interface=REQUEST_IFACE,
                     member="Response", path=req_path)
    Proxy(message_bus, self.conn).AddMatch(rule)

    with self.conn.filter(rule) as queue:
      addr = DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS, interface=SCREENCAST_IFACE)
      self.conn.send_and_get_reply(new_method_call(addr, method, signature, body))
      # The portal may legitimately take as long as the user takes to click.
      reply = self.conn.recv_until_filtered(queue, timeout=300)

    code, results = reply.body[0], _unwrap(reply.body[1])
    if code == 1:
      raise PortalError(f"{method}: cancelled by the user")
    if code != 0:
      raise PortalError(f"{method}: portal returned {code}")
    return results

  def _token(self, prefix: str) -> str:
    return f"beampilot_{prefix}_{os.getpid()}_{int(time.monotonic() * 1000) % 100000}"

  # -- session -------------------------------------------------------------
  def start(self):
    """Run the handshake. Returns (node_id, fd). Raises PortalError."""
    from jeepney import DBusAddress, new_method_call
    from jeepney.io.blocking import open_dbus_connection

    # enable_fds is required: OpenPipeWireRemote replies with a unix fd.
    self.conn = open_dbus_connection(bus="SESSION", enable_fds=True)

    tok = self._token("create")
    res = self._request("CreateSession", "a{sv}", ({
      "handle_token": ("s", tok),
      "session_handle_token": ("s", self._token("session")),
    },), tok)
    self.session_handle = res["session_handle"]

    options = {
      "handle_token": ("s", (tok := self._token("select"))),
      "types": ("u", self.source_types),
      "multiple": ("b", False),
      "cursor_mode": ("u", self.cursor_mode),
      # 2 = persist until the user revokes it, so the dialog appears once.
      "persist_mode": ("u", 2),
    }
    saved = self._load_restore_token() if self.use_restore_token else None
    if saved:
      options["restore_token"] = ("s", saved)
    self._request("SelectSources", "oa{sv}", (self.session_handle, options), tok)

    tok = self._token("start")
    res = self._request("Start", "osa{sv}",
                        (self.session_handle, "", {"handle_token": ("s", tok)}), tok)

    streams = res.get("streams") or []
    if not streams:
      raise PortalError("the portal returned no streams (nothing was selected)")
    node_id, props = streams[0][0], _unwrap(streams[0][1])
    self.node_id = int(node_id)
    size = props.get("size")
    if size:
      self.stream_size = (int(size[0]), int(size[1]))

    if self.use_restore_token and res.get("restore_token"):
      self._save_restore_token(res["restore_token"])

    addr = DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS, interface=SCREENCAST_IFACE)
    reply = self.conn.send_and_get_reply(
      new_method_call(addr, "OpenPipeWireRemote", "oa{sv}", (self.session_handle, {})))
    self.fd = reply.body[0].to_raw_fd()
    # The GStreamer subprocess has to inherit it.
    os.set_inheritable(self.fd, True)
    return self.node_id, self.fd

  def _load_restore_token(self) -> str | None:
    try:
      with open(restore_token_path()) as fh:
        return fh.read().strip() or None
    except OSError:
      return None

  def _save_restore_token(self, token: str):
    path = restore_token_path()
    try:
      os.makedirs(os.path.dirname(path), exist_ok=True)
      with open(path, "w") as fh:
        fh.write(token)
    except OSError:
      pass  # a re-prompt next run is not worth failing the session over

  def close(self):
    if self.fd is not None:
      try:
        os.close(self.fd)
      except OSError:
        pass
      self.fd = None
    if self.conn is not None:
      try:
        self.conn.close()
      except Exception:
        pass
      self.conn = None


class PipeWireNV12Source:
  """Latest-frame-wins NV12 reader fed by gst-launch on a PipeWire node.

  GStreamer does the colour conversion and the scale to the model's exact
  resolution, so what arrives here is already the tightly-packed NV12 the
  encoder wants.

  Frames are drained on a thread that keeps only the most recent one. The
  compositor may push 60fps while beamcamd consumes 20; without draining, the
  pipe fills, GStreamer blocks, and the frames we do get are progressively
  staler -- latency that would show up as the model reacting late.
  """

  def __init__(self, node_id: int, fd: int, width: int, height: int):
    if shutil.which("gst-launch-1.0") is None:
      raise PortalError("gst-launch-1.0 not found -- install gstreamer1.0-tools"
                        + " and gstreamer1.0-pipewire (Debian/Ubuntu),"
                        + " gst-plugins-base + gst-plugin-pipewire (Arch),"
                        + " or gstreamer1-plugins-base (Fedora)")
    self.width, self.height = width, height
    self.frame_bytes = width * height * 3 // 2
    cmd = [
      "gst-launch-1.0", "-q",
      "pipewiresrc", f"fd={fd}", f"path={node_id}", "do-timestamp=true",
      "!", "videoconvert",
      "!", "videoscale",
      "!", f"video/x-raw,format=NV12,width={width},height={height}",
      "!", "fdsink", "fd=1", "sync=false",
    ]
    self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, pass_fds=(fd,))
    self._latest: bytes | None = None
    self._lock = threading.Lock()
    self._stop = False
    self._thread = threading.Thread(target=self._pump, daemon=True)
    self._thread.start()

  def _pump(self):
    stream = self.proc.stdout
    while not self._stop:
      buf = stream.read(self.frame_bytes)          # read() on a pipe: exact or EOF
      if not buf or len(buf) < self.frame_bytes:
        break
      with self._lock:
        self._latest = buf

  def read(self) -> bytes | None:
    """The most recent complete frame, or None if none has arrived yet."""
    with self._lock:
      return self._latest

  def alive(self) -> bool:
    return self.proc.poll() is None

  def stderr_tail(self) -> str:
    try:
      if self.proc.stderr is not None and not self.alive():
        return self.proc.stderr.read().decode("utf-8", "replace")[-600:]
    except OSError:
      pass
    return ""

  def close(self):
    self._stop = True
    try:
      self.proc.terminate()
      self.proc.wait(timeout=3)
    except (subprocess.SubprocessError, OSError):
      try:
        self.proc.kill()
      except OSError:
        pass


def portal_available() -> bool:
  """Is an xdg-desktop-portal with a ScreenCast interface on the bus?"""
  if shutil.which("dbus-send") is None:
    return False
  out = subprocess.run(
    ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus",
     "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames"],
    capture_output=True, text=True, timeout=6).stdout
  return PORTAL_BUS in out


def open_portal_nv12(width: int, height: int, cursor_mode: int = CURSOR_HIDDEN,
                     use_restore_token: bool = True):
  """(session, source) ready to hand NV12 frames of exactly width x height."""
  session = PortalScreenCast(cursor_mode=cursor_mode, use_restore_token=use_restore_token)
  node_id, fd = session.start()
  try:
    source = PipeWireNV12Source(node_id, fd, width, height)
  except Exception:
    session.close()
    raise
  return session, source


if __name__ == "__main__":
  # Manual check: prompts, captures a few frames, reports whether they are
  # actually non-uniform (an all-zero frame is the green-screen failure).
  W, H = 1928, 1208
  print(f"portal on the bus: {portal_available()}")
  sess, src = open_portal_nv12(W, H)
  print(f"node={sess.node_id} fd={sess.fd} stream_size={sess.stream_size}")
  try:
    for i in range(50):
      time.sleep(0.1)
      frame = src.read()
      if frame is None:
        if not src.alive():
          print("gst-launch died:\n" + src.stderr_tail())
          break
        continue
      y = frame[:W * H]
      print(f"frame {i}: {len(frame)} bytes, Y min={min(y[:10000])} max={max(y[:10000])}"
            + f" mean={sum(y[:10000]) // 10000}"
            + ("   <-- ALL ZERO (green)" if max(y[:10000]) == 0 else ""))
      break
    else:
      print("no frame arrived")
  finally:
    src.close()
    sess.close()
