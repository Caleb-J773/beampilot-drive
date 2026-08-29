import os
import time
import cv2
import numpy as np
import mss

from openpilot.cereal.visionipc import VisionStreamType
from openpilot.cereal import messaging
from openpilot.common.realtime import Ratekeeper
from msgq.visionipc import VisionIpcServer
from openpilot.system.camerad.cameras.nv12_info import get_nv12_info

# Captures the BeamNG.drive window off the X11 desktop (the openpilot_cam BeamNG
# mod, auto-selected by the beampilot_bridge protocol Lua, guarantees this is the
# rigidly-mounted, FOV-matched forward camera view, not a driver-controlled one)
# and republishes it as openpilot's road cameras.
#
# Capture is a fixed monitor/region, not an auto-detected window: BeamNG.drive is
# expected to be running fullscreen/borderless on the configured display. Override
# via env vars if that assumption doesn't hold for a given setup:
#   BEAMPILOT_CAM_MONITOR  index into mss's monitor list (1 = first physical monitor,
#                          0 = all monitors combined). Default 1.
#   BEAMPILOT_CAM_REGION   "left,top,width,height" to capture a sub-region (e.g. a
#                          windowed, non-fullscreen BeamNG instance) instead of the
#                          whole monitor.

TR = [1.0, 0.0, 0.0,
      0.0, 1.0, 0.0,
      0.0, 0.0, 1.0]


def get_capture_region(sct: mss.MSS) -> dict:
  region_env = os.environ.get("BEAMPILOT_CAM_REGION")
  if region_env:
    left, top, width, height = (int(v) for v in region_env.split(","))
    return {"left": left, "top": top, "width": width, "height": height}

  monitor_idx = int(os.environ.get("BEAMPILOT_CAM_MONITOR", "1"))
  return sct.monitors[monitor_idx]


def resize_nearest(img: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
  # cv2.resize is SIMD-optimized C++ under the hood -- ~48x faster than a
  # numpy fancy-indexing gather for the same nearest-neighbor result (exact
  # pixel selection can differ by a fraction of a pixel from a hand-rolled
  # formula due to rounding conventions; imperceptible for camera input).
  return cv2.resize(img, (out_w, out_h), interpolation=cv2.INTER_NEAREST)


def bgra_to_nv12_tight(bgra: np.ndarray) -> bytes:
  """BGRA -> tightly-packed NV12 (w*h Y bytes, then (h//2)*w interleaved UV
  bytes, no row padding). cv2 has no direct BGRA->NV12 code, only planar
  I420/YV12, so this converts to I420 (Y, then U, then V as separate blocks)
  and interleaves U/V into NV12's single UV plane. Much faster than a manual
  numpy BT.601 implementation (SIMD-optimized C++ under the hood) and skips
  the separate BGRA->RGB channel-reorder copy entirely, since cv2 takes BGRA
  directly."""
  h, w = bgra.shape[:2]
  i420 = cv2.cvtColor(bgra, cv2.COLOR_BGRA2YUV_I420)
  y = i420[:h, :]
  u = i420[h:h + h // 4, :].reshape(h // 2, w // 2)
  v = i420[h + h // 4:h + h // 2, :].reshape(h // 2, w // 2)

  uv = np.empty((h // 2, w), dtype=np.uint8)
  uv[:, 0::2] = u
  uv[:, 1::2] = v

  return y.tobytes() + uv.tobytes()


def pack_nv12_padded(nv12_bytes: bytes, w: int, h: int, stride: int, y_height: int, uv_height: int, uv_offset: int, size: int) -> bytes:
  """Places a tightly-packed NV12 buffer (as produced by bgra_to_nv12_tight:
  w*h Y bytes then (h//2)*w interleaved UV bytes, no row padding) into the
  strided/padded VENUS-style buffer layout get_nv12_info describes -- no
  existing helper in the repo handles this padded layout, only the
  tightly-packed one."""
  out = np.zeros(size, dtype=np.uint8)

  y_plane = np.frombuffer(nv12_bytes, dtype=np.uint8, count=w * h).reshape(h, w)
  uv_plane = np.frombuffer(nv12_bytes, dtype=np.uint8, count=(h // 2) * w, offset=w * h).reshape(h // 2, w)

  y_view = out[:uv_offset].reshape(y_height, stride)
  y_view[:h, :w] = y_plane

  uv_view = out[uv_offset:uv_offset + stride * uv_height].reshape(uv_height, stride)
  uv_view[:h // 2, :w] = uv_plane

  return out.tobytes()


def main():
  # openpilot's model input resolution. Not a capture setting -- the captured
  # region is rescaled to this regardless of your monitor size, and modeld
  # expects exactly these dimensions, so don't change them to "match" a display.
  W, H = 1928, 1208

  server = VisionIpcServer("camerad")
  stride, y_height, uv_height, size = get_nv12_info(W, H)
  uv_offset = stride * y_height

  server.create_buffers_with_sizes(VisionStreamType.VISION_STREAM_WIDE_ROAD, 20, W, H, size, stride, uv_offset)
  server.create_buffers_with_sizes(VisionStreamType.VISION_STREAM_NARROW_ROAD, 20, W, H, size, stride, uv_offset)
  server.create_buffers_with_sizes(VisionStreamType.VISION_STREAM_CABIN, 4, W, H, size, stride, uv_offset)

  server.start_listener()

  pm = messaging.PubMaster(["wideRoadCameraState", "narrowRoadCameraState"])

  sct = mss.mss()
  region = get_capture_region(sct)

  rate = Ratekeeper(20, print_delay_threshold=None)
  frame_id = 0

  while True:
    shot = sct.grab(region)
    bgra = np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)
    # Downsize BEFORE color conversion, not after: resize_nearest's
    # fancy-indexing cost is ~proportional to the OUTPUT size regardless of
    # channel count, so shrinking first means cv2 only has to touch the (much
    # smaller) target resolution instead of the full captured monitor
    # resolution. Fancy indexing always returns a fresh contiguous array, so
    # bgra_small is already safe to hand straight to cv2.
    bgra_small = resize_nearest(bgra, H, W)

    nv12_bytes = pack_nv12_padded(bgra_to_nv12_tight(bgra_small), W, H, stride, y_height, uv_height, uv_offset, size)

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
    server.send(VisionStreamType.VISION_STREAM_WIDE_ROAD, nv12_bytes, frame_id, timestamp, timestamp)
    server.send(VisionStreamType.VISION_STREAM_NARROW_ROAD, nv12_bytes, frame_id, timestamp, timestamp)

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

    # update frame count / id
    frame_id += 1

    # wait for 20hz
    rate.keep_time()

if __name__ == "__main__":
  main()
