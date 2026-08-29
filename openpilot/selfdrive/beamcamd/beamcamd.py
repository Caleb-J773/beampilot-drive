import gc
import os
import time
import cv2
import numpy as np
import mss
import mss.exception

from openpilot.cereal.visionipc import VisionStreamType
from openpilot.cereal import messaging
from msgq.visionipc import VisionIpcServer
from openpilot.system.camerad.cameras.nv12_info import get_nv12_info
from openpilot.common.beampilot_env import env_float, env_int
from openpilot.selfdrive.beamcamd.window_capture import capture_support, find_window, window_geometry

# Captures the BeamNG.drive window off the X11 desktop (the openpilot_cam BeamNG
# mod, auto-selected by the beampilot_bridge protocol Lua, guarantees this is the
# rigidly-mounted, FOV-matched forward camera view, not a driver-controlled one)
# and republishes it as openpilot's road cameras.
#
# What gets captured, highest priority first (see get_capture_region):
#   BEAMPILOT_CAM_REGION   "left,top,width,height" -- a fixed rectangle.
#   BEAMPILOT_CAM_WINDOW   match text for the BeamNG window; tracked as it moves
#                          or resizes. Needs X11 and xdotool.
#   BEAMPILOT_CAM_MONITOR  index into mss's monitor list (1 = first physical
#                          monitor, 0 = all combined). Default 1; also the
#                          fallback when no window matches.

TR = [1.0, 0.0, 0.0,
      0.0, 1.0, 0.0,
      0.0, 0.0, 1.0]

# SERVICE_LIST expects road cameras at 20Hz; modeld is driven by these frames,
# so raising it only helps if the GPU can keep up with both the game and the
# model, and lowering it starves the model.
CAMERA_RATE_HZ = env_float("BEAMPILOT_CAM_RATE_HZ", 20.0)
# How often to re-read a tracked window's geometry. This is the CHEAP re-read
# (one xdotool call on a known window id, ~0.7ms measured); full rediscovery is
# ~68-285ms and only runs when the window actually disappears. See the retrack
# block in main() for why that distinction matters.
RETRACK_INTERVAL_S = env_float("BEAMPILOT_CAM_RETRACK_S", 2.0)
# VisionIPC ring buffer depth, per stream.
VIPC_BUFFER_COUNT = env_int("BEAMPILOT_VIPC_BUFFERS", 20)

# How the screen is read: "x11" (mss), "portal" (xdg-desktop-portal ScreenCast
# over PipeWire), or "auto".
#
# auto keeps X11 sessions on exactly the path they have always used, and only
# reaches for the portal on Wayland -- where an X11 grab cannot work at all.
# Wayland does not let a client read the screen, so mss returns nothing, the
# NV12 buffer is never written, and an all-zero NV12 buffer decodes to
# RGB(0,135,0): the "everything is green" report. Green is the signature of no
# data, since real black is Y=16/U=V=128, not zeros.
CAPTURE_BACKEND = os.environ.get("BEAMPILOT_CAPTURE_BACKEND", "auto").strip().lower()


def choose_capture_backend() -> str:
  """Resolve CAPTURE_BACKEND to "x11" or "portal"."""
  if CAPTURE_BACKEND in ("x11", "portal"):
    return CAPTURE_BACKEND
  # An explicit region or monitor is an X11 concept, and someone who set one
  # has said what they want; don't override it with a portal picker.
  if os.environ.get("BEAMPILOT_CAM_REGION"):
    return "x11"
  from openpilot.selfdrive.beamcamd.window_capture import session_type
  return "portal" if session_type() == "wayland" else "x11"


def clamp_region(region: dict, bounds: dict) -> dict | None:
  """Intersect a capture rectangle with the X root window, or None if disjoint.

  THIS IS NOT COSMETIC -- skipping it kills the daemon. X11's GetImage raises
  BadMatch (error 8, opcode 73) if ANY part of the requested rectangle falls
  outside the drawable, and mss surfaces that as an uncaught
  mss.linux.xcbhelpers.XProtoError. beamcamd exits, camerad's VisionIPC streams
  vanish, and modeld -- which blocks in VisionIpcClient.available_streams()
  waiting for them -- never starts, so manager reports
  {"event": "process_not_running", "not_running": "{'modeld'}"}.

  It is very easy to hit: a monitor whose bottom or right edge is flush with the
  edge of the virtual screen (e.g. a 1920x1080 panel at +2560+360 on a
  4480x1440 root) leaves zero slack, so a BeamNG window there that is moved down
  even slightly -- or just carries a title bar -- extends past the root.

  Clamping changes the captured aspect ratio a little, since the frame is
  rescaled to the model's fixed 1928x1208 either way. That is a far better
  failure mode than exiting, and it only applies while the window is genuinely
  half off-screen.
  """
  left = max(region["left"], bounds["left"])
  top = max(region["top"], bounds["top"])
  right = min(region["left"] + region["width"], bounds["left"] + bounds["width"])
  bottom = min(region["top"] + region["height"], bounds["top"] + bounds["height"])
  if right - left < 2 or bottom - top < 2:
    return None
  return {"left": left, "top": top, "width": right - left, "height": bottom - top}


def get_capture_region(sct: mss.MSS) -> tuple[dict, str | None]:
  """Explicit region > tracked window > whole monitor.

  Returns (region, window_id). The id is None unless we're tracking a window;
  the caller uses it to re-read that window's geometry cheaply instead of
  re-running discovery, which is expensive (see the retrack block in main).

  Window tracking is the nicest option when it works (a windowed, moved or
  resized BeamNG still gets captured, and you keep a second monitor free for
  the openpilot UI), but it needs X11 and xdotool, so it degrades to a plain
  monitor grab rather than failing.
  """
  bounds = sct.monitors[0]

  region_env = os.environ.get("BEAMPILOT_CAM_REGION")
  if region_env:
    left, top, width, height = (int(v) for v in region_env.split(","))
    asked = {"left": left, "top": top, "width": width, "height": height}
    region = clamp_region(asked, bounds)
    if region is None:
      print(f"[beamcamd] BEAMPILOT_CAM_REGION {region_env!r} is entirely outside the screen"
            + f" ({bounds['width']}x{bounds['height']}); ignoring it", flush=True)
    else:
      if region != asked:
        print("[beamcamd] BEAMPILOT_CAM_REGION clamped to the screen ->"
              + f" {region['width']}x{region['height']} at +{region['left']}+{region['top']}", flush=True)
      return region, None

  match = os.environ.get("BEAMPILOT_CAM_WINDOW", "").strip()
  if match:
    win = find_window(match)
    if win is not None:
      region = clamp_region({k: win[k] for k in ("left", "top", "width", "height")}, bounds)
      if region is not None:
        print(f"[beamcamd] tracking window {win['id']} ({win['name']!r}, matched by {win['how']}):"
              + f" {win['width']}x{win['height']} at +{win['left']}+{win['top']}", flush=True)
        return region, win["id"]
      print(f"[beamcamd] window {win['id']} is entirely off-screen; falling back to monitor capture", flush=True)
    else:
      print(f"[beamcamd] no window matching {match!r} found --"
            + " is BeamNG running? falling back to monitor capture", flush=True)

  monitor_idx = int(os.environ.get("BEAMPILOT_CAM_MONITOR", "1"))
  if monitor_idx >= len(sct.monitors):
    print(f"[beamcamd] BEAMPILOT_CAM_MONITOR={monitor_idx} but only {len(sct.monitors) - 1}"
          + " monitor(s) exist; using the primary", flush=True)
    monitor_idx = 1
  return dict(sct.monitors[monitor_idx]), None


class FrameEncoder:
  """Captured BGRA -> the padded NV12 buffer VisionIPC expects, allocation-free.

  Every intermediate is allocated once and rewritten in place. The obvious
  version -- cv2 returning a fresh array per stage, then .tobytes() per stage --
  churns ~25MB per frame, which at 20Hz is ~500MB/s of garbage. That doesn't
  just cost the copies (measured 1.91ms vs 0.98ms per frame here); it drags
  CPython's generational GC into a periodic gen-2 sweep whose pause lands on a
  random frame, which is exactly the kind of intermittent hitch this pipeline
  must not have. Byte-for-byte identical output to the allocating version.
  """

  def __init__(self, w: int, h: int, stride: int, y_height: int, uv_height: int, uv_offset: int, size: int):
    self.w, self.h = w, h

    # The VENUS-style padded layout get_nv12_info describes: a stride-wide Y
    # plane of y_height rows, then a stride-wide interleaved UV plane. No
    # existing helper in the repo builds this layout, only the tightly-packed
    # one. The padding columns/rows are zeroed once here and never touched
    # again -- only the w x h and (w x h/2) sub-rectangles change per frame.
    self.out = np.zeros(size, dtype=np.uint8)
    self.y_view = self.out[:uv_offset].reshape(y_height, stride)
    self.uv_view = self.out[uv_offset:uv_offset + stride * uv_height].reshape(uv_height, stride)

    self.small = np.empty((h, w, 4), dtype=np.uint8)
    self.i420 = np.empty((h + h // 2, w), dtype=np.uint8)

  def encode(self, bgra: np.ndarray) -> np.ndarray:
    # Downsize BEFORE colour conversion, not after: the resize cost scales with
    # the OUTPUT size, so shrinking first means the (much more expensive)
    # colour conversion only ever touches the model's resolution instead of the
    # full captured monitor. cv2.resize is SIMD-optimized C++ under the hood --
    # ~48x faster than a numpy fancy-indexing gather for the same
    # nearest-neighbour result.
    cv2.resize(bgra, (self.w, self.h), dst=self.small, interpolation=cv2.INTER_NEAREST)

    # cv2 has no direct BGRA->NV12 code, only planar I420/YV12, so convert to
    # I420 (Y, then U, then V as separate blocks) and interleave U/V into NV12's
    # single UV plane. Still much faster than a manual numpy BT.601
    # implementation, and it takes BGRA directly, skipping a channel-reorder copy.
    cv2.cvtColor(self.small, cv2.COLOR_BGRA2YUV_I420, dst=self.i420)

    h, w = self.h, self.w
    self.y_view[:h, :w] = self.i420[:h, :]
    self.uv_view[:h // 2, 0:w:2] = self.i420[h:h + h // 4, :].reshape(h // 2, w // 2)
    self.uv_view[:h // 2, 1:w:2] = self.i420[h + h // 4:h + h // 2, :].reshape(h // 2, w // 2)

    # Returned as an ndarray, not bytes: VisionIpcServer.send takes a Cython
    # typed memoryview (const unsigned char[:]), so it reads this buffer
    # directly and a .tobytes() copy of ~4.8MB per frame is pure waste.
    return self.out

  def encode_nv12(self, tight: bytes) -> np.ndarray:
    """Place an already-NV12 frame into the padded layout.

    The portal path gets here: GStreamer has already converted and scaled to
    exactly w x h NV12, so there is nothing to do but move it into the strided
    buffer -- no resize, no colour conversion.
    """
    h, w = self.h, self.w
    y = np.frombuffer(tight, dtype=np.uint8, count=w * h).reshape(h, w)
    uv = np.frombuffer(tight, dtype=np.uint8, count=(h // 2) * w, offset=w * h).reshape(h // 2, w)
    self.y_view[:h, :w] = y
    self.uv_view[:h // 2, :w] = uv
    return self.out


class FramePacer:
  """Fixed-rate pacing that DROPS a backlog instead of sprinting to clear it.

  openpilot's Ratekeeper is deliberately not used here. It advances its
  deadline by exactly one interval per call with no resync, so after one long
  frame (a 68ms rediscovery, a compositor stall) its deadline is already in the
  past and it sleeps zero for every frame until it has caught up. At 20Hz a
  single 285ms hiccup therefore emits ~6 frames back-to-back, ~13ms apart, each
  with an honest monotonic timestamp. modeld consumes them as fast as they
  arrive, so the model's temporal context sees the world lurch forward and then
  stall -- the stutter shows up in the driving, not just in a log.

  Frames we are already too late to deliver on time are worthless (the screen
  has moved on), so when we fall more than one full interval behind we abandon
  the backlog and re-phase to now. Small overruns still self-correct normally.
  """

  def __init__(self, rate_hz: float):
    self.interval = 1.0 / rate_hz
    self.next_frame = time.monotonic() + self.interval
    self.dropped = 0

  def reset(self) -> None:
    """Re-phase to now without counting a drop."""
    self.next_frame = time.monotonic() + self.interval

  def wait(self) -> None:
    remaining = self.next_frame - time.monotonic()
    if remaining > 0:
      time.sleep(remaining)
      self.next_frame += self.interval
    elif -remaining > self.interval:
      self.dropped += 1
      # Worth surfacing: in steady state this should never fire, so if it does
      # the machine is not keeping up with capture + game + model and that is
      # the thing to fix, not the pacing. Rate-limited so it can't itself
      # become the stall.
      if self.dropped == 1 or self.dropped % 100 == 0:
        print(f"[beamcamd] behind by {-remaining * 1000:.0f}ms, resyncing"
              + f" (dropped {self.dropped} frame slots so far)", flush=True)
      self.next_frame = time.monotonic() + self.interval
    else:
      self.next_frame += self.interval


def _publish(server, pm, nv12, frame_id: int):
  # Real monotonic-clock timestamp, matching cereal's own logMonoTime
  # (time.monotonic()) -- NOT a synthetic frame_id*dt counter. modeld derives
  # cameraOdometry.timestampEof from this, and locationd's _validate_timestamp
  # compares that against the Kalman filter's real elapsed time; a fake,
  # near-zero counter fails that check on every single frame, forever,
  # permanently blocking deviceMotion.inputsOK (this exact bug also exists,
  # unfixed, in openpilot's own reference system/camerad/webcam/camerad.py --
  # it's just never exercised there since that driver is disabled here).
  timestamp = time.monotonic_ns()

  # KNOWN LIMITATION: the same frame goes to BOTH streams, so openpilot's
  # "wide" camera is really a second copy of the narrow view. modeld sets
  # has_wide_camera = use_extra_client or main_wide_camera (modeld.py), which
  # is True here, so it applies dc.wide_road.intrinsics -- the calibration for
  # a ~120 degree lens -- to an image that doesn't have that field of view.
  # The model therefore misjudges how far objects and lane lines sit off to
  # the sides, which shows up most in turns, where the real wide camera is
  # what normally sees around the corner. Keep Experimental mode OFF: its
  # end-to-end longitudinal policy leans much harder on wide-camera scene
  # understanding. Fixing this properly needs a second BeamNG camera at a
  # genuinely wide FOV published to VISION_STREAM_WIDE_ROAD.
  server.send(VisionStreamType.VISION_STREAM_WIDE_ROAD, nv12, frame_id, timestamp, timestamp)
  server.send(VisionStreamType.VISION_STREAM_NARROW_ROAD, nv12, frame_id, timestamp, timestamp)

  # update cereal (wide road)
  dat = messaging.new_message("wideRoadCameraState", valid=True)
  msg = {"frameId": frame_id, "transform": TR, "sensor": "unknown"}
  dat.wideRoadCameraState = msg
  pm.send("wideRoadCameraState", dat)

  # update cereal (narrow road)
  dat = messaging.new_message("narrowRoadCameraState", valid=True)
  msg = {"frameId": frame_id, "transform": TR, "sensor": "unknown"}
  dat.narrowRoadCameraState = msg
  pm.send("narrowRoadCameraState", dat)


# Frames at which to check for a blank picture: once the stream should have
# settled, then again later in case the source went away.
_BLANK_CHECK_FRAMES = (40, 400)


def _warn_if_blank(encoder: FrameEncoder, frame_id: int, backend: str):
  """Say so when the published picture carries no image at all.

  A uniform frame is the one failure that looks like a working pipeline: every
  process runs, frames flow at 20Hz, and the driver just sees a flat colour.
  Worse, the flat colour is *green* rather than black, because an untouched
  buffer is Y=0/U=V=0 while real black is Y=16/U=V=128 -- so it doesn't even
  read as "no signal". Cheap to detect, and it turns a baffling symptom into a
  sentence.
  """
  if frame_id not in _BLANK_CHECK_FRAMES:
    return
  y = encoder.y_view[:encoder.h, :encoder.w]
  # Subsample: a full 2.3MP min/max every frame would not be worth it, and a
  # sparse grid is more than enough to tell "flat" from "a picture".
  sample = y[::32, ::32]
  if int(sample.max()) != int(sample.min()):
    return
  level = int(sample.min())
  colour = "green" if level == 0 else ("black" if level <= 16 else f"flat (Y={level})")
  msg = (f"[beamcamd] WARNING: the captured picture is completely {colour} at frame"
         + f" {frame_id} -- openpilot is being fed no image.")
  if backend == "x11" and level == 0:
    msg += (" An all-zero frame means the X11 grab returned nothing, which is what happens"
            + " on Wayland. Set BEAMPILOT_CAPTURE_BACKEND=portal to capture through"
            + " xdg-desktop-portal instead.")
  elif backend == "portal":
    msg += " Check that the shared window/monitor is the one BeamNG is on."
  print(msg, flush=True)


def main():
  # openpilot's model input resolution. Not a capture setting -- the captured
  # region is rescaled to this regardless of your monitor size, and modeld
  # expects exactly these dimensions, so don't change them to "match" a display.
  W, H = 1928, 1208

  server = VisionIpcServer("camerad")
  stride, y_height, uv_height, size = get_nv12_info(W, H)
  uv_offset = stride * y_height

  server.create_buffers_with_sizes(VisionStreamType.VISION_STREAM_WIDE_ROAD, VIPC_BUFFER_COUNT, W, H, size, stride, uv_offset)
  server.create_buffers_with_sizes(VisionStreamType.VISION_STREAM_NARROW_ROAD, VIPC_BUFFER_COUNT, W, H, size, stride, uv_offset)
  server.create_buffers_with_sizes(VisionStreamType.VISION_STREAM_CABIN, 4, W, H, size, stride, uv_offset)

  server.start_listener()

  pm = messaging.PubMaster(["wideRoadCameraState", "narrowRoadCameraState"])

  usable, explanation = capture_support()
  print(f"[beamcamd] {explanation}", flush=True)
  if not usable:
    print("[beamcamd] cannot capture the screen; frames will be blank", flush=True)

  backend = choose_capture_backend()
  encoder = FrameEncoder(W, H, stride, y_height, uv_height, uv_offset, size)
  sct = region = tracked_id = None
  portal_session = portal_source = None

  if backend == "portal":
    from openpilot.selfdrive.beamcamd.portal_capture import PortalError, open_portal_nv12
    try:
      portal_session, portal_source = open_portal_nv12(W, H)
      print("[beamcamd] capturing via xdg-desktop-portal: PipeWire node"
            + f" {portal_session.node_id}, source {portal_session.stream_size}", flush=True)
    except (PortalError, OSError, ImportError) as e:
      # Falling back to X11 on Wayland will produce green frames, so say why
      # rather than leaving the user to work it out from the picture.
      print(f"[beamcamd] portal capture unavailable ({type(e).__name__}: {e})", flush=True)
      print("[beamcamd] falling back to X11 capture -- on Wayland this cannot read the"
            + " screen and every frame will come out green.", flush=True)
      backend = "x11"

  if backend == "x11":
    sct = mss.MSS()
    region, tracked_id = get_capture_region(sct)

  # Everything that lives for the whole run is allocated by now, so promote it
  # out of the GC's reach and stop collecting. On a soft-realtime loop the cycle
  # collector only buys periodic pauses that land on a random frame. openpilot
  # does the same for its own realtime processes (common/realtime.py's
  # config_realtime_process).
  #
  # Safe because the steady-state loop creates no reference cycles: the only
  # per-frame Python objects are the capnp messages, and they are refcounted
  # away immediately. Verified rather than assumed -- 40,000 publish iterations
  # (~33 min at 20Hz) with the collector off showed no RSS growth at all and a
  # following gc.collect() freed zero cycle objects.
  gc.collect()
  gc.freeze()
  gc.disable()

  pacer = FramePacer(CAMERA_RATE_HZ)
  frame_id = 0

  track_window = bool(os.environ.get("BEAMPILOT_CAM_WINDOW", "").strip()) and \
                 not os.environ.get("BEAMPILOT_CAM_REGION")
  last_retrack = time.monotonic()
  # Set when a grab fails or the tracked window vanishes: the next retrack tick
  # runs full discovery instead of the cheap geometry re-read.
  need_rediscover = False
  grab_errors = 0

  # The portal picks its own source and streams only that, so there is no
  # rectangle to track and no window to re-find.
  if backend == "portal":
    track_window = False

  while True:
    now = time.monotonic()
    if backend == "x11" and (track_window or need_rediscover) \
       and (now - last_retrack) > RETRACK_INTERVAL_S:
      last_retrack = now

      # The cheap path: re-read the geometry of the window we ALREADY found, by
      # id, so moving or resizing the game doesn't leave us grabbing a stale
      # rectangle. Measured on this machine: window_geometry() on a known id is
      # a single xdotool call at ~0.7ms, while find_window() is 68ms+ (it shells
      # out to pgrep/xdotool/xprop once per candidate) -- more than a whole 50ms
      # frame budget, so running discovery on a timer guaranteed a dropped frame
      # every RETRACK_INTERVAL_S. That was the periodic stutter.
      new_region = None
      if tracked_id is not None and not need_rediscover:
        geo = window_geometry(tracked_id)
        if geo is not None:
          new_region = clamp_region(geo, sct.monitors[0])

      if new_region is None:
        # The window is gone (closed, or the game restarted), or a grab just
        # failed. Only now is full rediscovery worth its cost; it also drops us
        # back to monitor capture if nothing matches, rather than freezing on a
        # stale rectangle. Rebuild the MSS connection too: it re-reads the
        # monitor layout, which the old object caches, and recovers a
        # connection broken by an X server hiccup.
        try:
          sct.close()
        except Exception:
          pass
        sct = mss.MSS()
        region, tracked_id = get_capture_region(sct)
        need_rediscover = False
      elif new_region != region:
        print(f"[beamcamd] window moved/resized -> {new_region['width']}x{new_region['height']}"
              + f" at +{new_region['left']}+{new_region['top']}", flush=True)
        region = new_region

    if backend == "portal":
      tight = portal_source.read()
      if tight is not None:
        nv12 = encoder.encode_nv12(tight)
        grab_errors = 0
      else:
        # No frame yet. A compositor legitimately sends nothing while the
        # source is occluded or idle, so only a dead GStreamer is an error.
        nv12 = encoder.out
        if not portal_source.alive():
          grab_errors += 1
          if grab_errors == 1:
            tail = portal_source.stderr_tail()
            print("[beamcamd] the PipeWire capture pipeline exited"
                  + (f":\n{tail}" if tail else " (no output)"), flush=True)
      _warn_if_blank(encoder, frame_id, backend)
      _publish(server, pm, nv12, frame_id)
      frame_id += 1
      if frame_id == 1:
        pacer.reset()
      pacer.wait()
      continue

    # A failed grab must never kill the daemon -- see clamp_region's docstring
    # for what taking beamcamd down does to the rest of the stack. Clamping
    # prevents the common out-of-bounds case, but the window can still move
    # between the clamp and the grab, and the X server can fail a request for
    # reasons of its own, so this stays guarded. XProtoError subclasses
    # mss.exception.ScreenShotError, so that one catch covers both the X
    # protocol errors and mss's own (e.g. a zero-size region).
    #
    # Only the grab is guarded: an exception out of encode() would be a bug in
    # our own buffer arithmetic, and swallowing that would just hide it.
    shot = None
    try:
      shot = sct.grab(region)
    except mss.exception.ScreenShotError as e:
      grab_errors += 1
      need_rediscover = True
      # Rate-limited: a persistently bad region would otherwise print at 20Hz.
      if grab_errors == 1 or grab_errors % 200 == 0:
        print(f"[beamcamd] screen grab failed ({type(e).__name__}: {e}); region was {region}."
              + f" [{grab_errors} in a row] Re-finding the window;"
              + " republishing the last frame meanwhile.", flush=True)
    else:
      grab_errors = 0

    if shot is not None:
      bgra = np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)
      nv12 = encoder.encode(bgra)
    else:
      # Keep publishing the last good frame at the normal rate rather than
      # going quiet: openpilot registers commIssue as both NO_ENTRY and
      # SOFT_DISABLE, so a gap in the camera stream disengages it mid-drive.
      nv12 = encoder.out

    _warn_if_blank(encoder, frame_id, backend)
    _publish(server, pm, nv12, frame_id)

    # update frame count / id
    frame_id += 1

    # The first frame pays for one-time lazy initialisation (cv2 loading its
    # kernels, the first VisionIPC send) and reliably overruns. That is not a
    # pacing failure, so start the clock properly once it's out of the way
    # rather than reporting a dropped slot on every single launch.
    if frame_id == 1:
      pacer.reset()

    # wait for 20hz
    pacer.wait()

if __name__ == "__main__":
  main()
