"""Camera geometry shared by beamcamd, beamngd, and the BeamNG mod.

The simulator emulates a comma 3/3X camera pair.  In ``narrow`` mode only the
normal road stream exists.  ``wide_crop`` is an explicitly experimental
single-render approximation: BeamNG renders the wide lens, the full picture is
published as the wide stream, and a centered crop supplies the narrow stream.
"""
import math

from openpilot.common.beampilot_env import env_float, env_str

FRAME_WIDTH = 1928
FRAME_HEIGHT = 1208

# tici AR0231/OX03C10 intrinsics from common/transformations/camera.py. Keep
# these here as plain numbers so the BeamNG bridge does not need to import
# numpy and the full transformations module just to send one Lua setting.
NARROW_FOCAL_LENGTH = 2648.0
WIDE_FOCAL_LENGTH = 567.0

NARROW_VERTICAL_FOV = math.degrees(2 * math.atan((FRAME_HEIGHT / 2) / NARROW_FOCAL_LENGTH))
WIDE_VERTICAL_FOV = math.degrees(2 * math.atan((FRAME_HEIGHT / 2) / WIDE_FOCAL_LENGTH))
NARROW_HORIZONTAL_FOV = math.degrees(2 * math.atan((FRAME_WIDTH / 2) / NARROW_FOCAL_LENGTH))
WIDE_HORIZONTAL_FOV = math.degrees(2 * math.atan((FRAME_WIDTH / 2) / WIDE_FOCAL_LENGTH))
NARROW_FROM_WIDE_SCALE = WIDE_FOCAL_LENGTH / NARROW_FOCAL_LENGTH

CAMERA_MODE_NARROW = "narrow"
CAMERA_MODE_WIDE_CROP = "wide_crop"
CAMERA_MODES = (CAMERA_MODE_NARROW, CAMERA_MODE_WIDE_CROP)

_raw_mode = env_str("BEAMPILOT_CAMERA_MODE", CAMERA_MODE_NARROW).strip().lower()
CAMERA_MODE = _raw_mode if _raw_mode in CAMERA_MODES else CAMERA_MODE_NARROW
CAMERA_MODE_INVALID = _raw_mode not in CAMERA_MODES
WIDE_CROP_ENABLED = CAMERA_MODE == CAMERA_MODE_WIDE_CROP
CAPTURE_VERTICAL_FOV = WIDE_VERTICAL_FOV if WIDE_CROP_ENABLED else NARROW_VERTICAL_FOV
CAPTURE_HORIZONTAL_FOV = WIDE_HORIZONTAL_FOV if WIDE_CROP_ENABLED else NARROW_HORIZONTAL_FOV

WIDE_CAMERA_PLACEMENT_VEHICLE_FRONT = "vehicle_front"
WIDE_CAMERA_PLACEMENT_LEGACY = "legacy"
WIDE_CAMERA_PLACEMENTS = (WIDE_CAMERA_PLACEMENT_VEHICLE_FRONT, WIDE_CAMERA_PLACEMENT_LEGACY)
_raw_wide_placement = env_str("BEAMPILOT_WIDE_CAMERA_PLACEMENT", WIDE_CAMERA_PLACEMENT_VEHICLE_FRONT).strip().lower()
WIDE_CAMERA_PLACEMENT = (_raw_wide_placement if _raw_wide_placement in WIDE_CAMERA_PLACEMENTS
                         else WIDE_CAMERA_PLACEMENT_VEHICLE_FRONT)

# The device model assumes a camera about 1.22 m above the road.  In adaptive
# wide placement the BeamNG OOBB bottom is our vehicle-independent road proxy,
# and the small forward clearance leaves the complete vehicle behind the lens.
WIDE_CAMERA_HEIGHT_M = min(5.0, max(0.2, env_float("BEAMPILOT_WIDE_CAMERA_HEIGHT_M", 1.22)))
WIDE_CAMERA_CLEARANCE_M = min(2.0, max(0.02, env_float("BEAMPILOT_WIDE_CAMERA_CLEARANCE_M", 0.15)))


def lua_config() -> dict[str, float]:
  """The small, backwards-compatible camera block added to control JSON."""
  auto_place = WIDE_CROP_ENABLED and WIDE_CAMERA_PLACEMENT == WIDE_CAMERA_PLACEMENT_VEHICLE_FRONT
  return {
    "fov": CAPTURE_VERTICAL_FOV,
    "autoPlace": float(auto_place),
    "height": WIDE_CAMERA_HEIGHT_M,
    "clearance": WIDE_CAMERA_CLEARANCE_M,
  }


def narrow_crop_bounds(width: int, height: int) -> tuple[int, int, int, int]:
  """Return an even-aligned centered crop with the narrow lens's angular view.

  Even origins and dimensions keep an NV12 crop aligned to its 2x2 chroma
  samples.  At the native 1928x1208 resolution this is 412x258, within 0.08
  degrees of the ideal crop after the necessary pixel rounding.
  """
  if width < 2 or height < 2:
    raise ValueError("camera frame must be at least 2x2")

  crop_w = max(2, min(width, int(round(width * NARROW_FROM_WIDE_SCALE / 2)) * 2))
  crop_h = max(2, min(height, int(round(height * NARROW_FROM_WIDE_SCALE / 2)) * 2))
  left = ((width - crop_w) // 2) & ~1
  top = ((height - crop_h) // 2) & ~1
  return left, top, crop_w, crop_h
