#!/usr/bin/env python3
"""Tests for BEAMPILOT_CAM_ASPECT -- the capture rectangle's shape.

Run with: uv run python openpilot/selfdrive/beamcamd/test_capture_geometry.py

The encoder resizes whatever rectangle it grabs straight to 1928x1208 (aspect
1.5960). A full-screen 16:9 window is 1.7778, so the picture is squeezed
horizontally by ~11%: vertically correct, because the mod renders a 25.70 degree
VERTICAL field to match openpilot's road camera, but spanning 44.15 degrees
horizontally where the intrinsics claim 40.01. Everything reads as ~11% closer
to the centre of the lane than it is.

Cropping the sides to 1.5960 first makes the resize uniform, and the remaining
field is exactly the 40.01 degrees the intrinsics describe. These tests pin the
arithmetic, both directions, including the case cropping cannot fix.
"""
import math
import unittest

import numpy as np

from openpilot.common import beampilot_camera as camera
from openpilot.common.beampilot_camera import (FRAME_HEIGHT, FRAME_WIDTH, NARROW_FOCAL_LENGTH,
                                               NARROW_HORIZONTAL_FOV, NARROW_VERTICAL_FOV,
                                               WIDE_FOCAL_LENGTH, WIDE_HORIZONTAL_FOV,
                                               WIDE_VERTICAL_FOV, narrow_crop_bounds)
from openpilot.selfdrive.beamcamd.beamcamd import FrameEncoder, crop_region_to_aspect, narrow_view_from_wide

W, H = FRAME_WIDTH, FRAME_HEIGHT
FRAME_ASPECT = W / H
VERTICAL_FOV = NARROW_VERTICAL_FOV
NARROW_H_FOV = NARROW_HORIZONTAL_FOV


def horizontal_fov(aspect: float, vertical_fov: float = VERTICAL_FOV) -> float:
  """BeamNG's fov setting is vertical, so the horizontal field follows aspect."""
  return math.degrees(2 * math.atan(math.tan(math.radians(vertical_fov / 2)) * aspect))


def rect(left, top, width, height):
  return {"left": left, "top": top, "width": width, "height": height}


class TestCropToAspect(unittest.TestCase):
  def test_16_9_is_cropped_to_the_frame_aspect(self):
    for width, height, expected in ((1920, 1080, 1724), (2560, 1440, 2298), (3840, 2160, 3447)):
      out = crop_region_to_aspect(rect(0, 0, width, height), FRAME_ASPECT)
      self.assertEqual(out["width"], expected, f"{width}x{height}")
      self.assertEqual(out["height"], height, "cropping must never touch the vertical field")

  def test_the_crop_is_centred(self):
    out = crop_region_to_aspect(rect(100, 50, 1920, 1080), FRAME_ASPECT)
    self.assertEqual(out["top"], 50)
    self.assertEqual(out["left"], 100 + (1920 - 1724) // 2)
    # Equal margins either side, to within the odd pixel.
    left_margin = out["left"] - 100
    right_margin = (100 + 1920) - (out["left"] + out["width"])
    self.assertLessEqual(abs(left_margin - right_margin), 1)

  def test_the_result_really_is_40_degrees(self):
    # The whole point: the remaining field has to match what openpilot's
    # intrinsics say, not merely be "less wrong".
    out = crop_region_to_aspect(rect(0, 0, 1920, 1080), FRAME_ASPECT)
    self.assertAlmostEqual(horizontal_fov(out["width"] / out["height"]), NARROW_H_FOV, delta=0.05)

  def test_an_uncropped_16_9_window_really_is_wrong(self):
    # Guards the premise. If this ever stops being true the feature is pointless.
    self.assertAlmostEqual(horizontal_fov(16 / 9), 44.15, delta=0.05)
    lateral_error = math.tan(math.radians(44.15 / 2)) / math.tan(math.radians(NARROW_H_FOV / 2)) - 1
    self.assertGreater(lateral_error, 0.10)

  def test_an_exactly_shaped_window_is_left_alone(self):
    original = rect(0, 0, W, H)
    self.assertEqual(crop_region_to_aspect(original, FRAME_ASPECT), original)

  def test_16_10_is_barely_touched(self):
    # 1.600 vs 1.5960 -- a 16:10 window is almost exactly right already.
    out = crop_region_to_aspect(rect(0, 0, 2560, 1600), FRAME_ASPECT)
    self.assertLess(2560 - out["width"], 10)

  def test_ultrawide_loses_a_lot_and_should(self):
    # 21:9 spans 57 degrees horizontally; the intrinsics claim 40.
    self.assertGreater(horizontal_fov(3440 / 1440), 57.0)
    out = crop_region_to_aspect(rect(0, 0, 3440, 1440), FRAME_ASPECT)
    self.assertEqual(out["width"], 2298)
    self.assertAlmostEqual(horizontal_fov(out["width"] / out["height"]), NARROW_H_FOV, delta=0.05)

  def test_a_window_narrower_than_the_frame_is_returned_untouched(self):
    # Cropping top and bottom would cut the vertical field below 25.70 degrees,
    # trading a horizontal error for a worse vertical one. Leave it, and let
    # main() tell the user to widen the window.
    original = rect(0, 0, 1200, 1080)   # 1.111, narrower than 1.596
    self.assertIs(crop_region_to_aspect(original, FRAME_ASPECT), original)

  def test_degenerate_rectangles_do_not_explode(self):
    for bad in (rect(0, 0, 0, 0), rect(0, 0, 100, 0), rect(0, 0, 0, 100)):
      self.assertIs(crop_region_to_aspect(bad, FRAME_ASPECT), bad)


class TestPortalPipelineRatio(unittest.TestCase):
  def test_the_gstreamer_ratio_matches_the_frame(self):
    from openpilot.selfdrive.beamcamd.portal_capture import aspect_ratio_fraction
    # aspectratiocrop wants an integer fraction; 1928x1208 reduces exactly.
    self.assertEqual(aspect_ratio_fraction(W, H), "241/151")
    self.assertAlmostEqual(241 / 151, FRAME_ASPECT, places=9)


class TestWideCropGeometry(unittest.TestCase):
  def test_fovs_come_from_openpilot_intrinsics(self):
    self.assertAlmostEqual(NARROW_VERTICAL_FOV, 25.698296, places=6)
    self.assertAlmostEqual(NARROW_HORIZONTAL_FOV, 40.007903, places=6)
    self.assertAlmostEqual(WIDE_VERTICAL_FOV, 93.619537, places=6)
    self.assertAlmostEqual(WIDE_HORIZONTAL_FOV, 119.074105, places=6)

  def test_native_crop_is_even_aligned_and_centred(self):
    left, top, width, height = narrow_crop_bounds(W, H)
    self.assertEqual((left, top, width, height), (758, 474, 412, 258))
    self.assertEqual((left % 2, top % 2, width % 2, height % 2), (0, 0, 0, 0))
    self.assertLessEqual(abs(left - (W - left - width)), 2)
    self.assertLessEqual(abs(top - (H - top - height)), 2)

  def test_crop_angles_match_the_narrow_lens_after_pixel_rounding(self):
    _, _, width, height = narrow_crop_bounds(W, H)
    cropped_h_fov = math.degrees(2 * math.atan((width / 2) / WIDE_FOCAL_LENGTH))
    cropped_v_fov = math.degrees(2 * math.atan((height / 2) / WIDE_FOCAL_LENGTH))
    self.assertAlmostEqual(cropped_h_fov, NARROW_HORIZONTAL_FOV, delta=0.08)
    self.assertAlmostEqual(cropped_v_fov, NARROW_VERTICAL_FOV, delta=0.08)

  def test_crop_scale_is_the_ratio_of_the_two_focal_lengths(self):
    ideal_width = W * WIDE_FOCAL_LENGTH / NARROW_FOCAL_LENGTH
    ideal_height = H * WIDE_FOCAL_LENGTH / NARROW_FOCAL_LENGTH
    _, _, width, height = narrow_crop_bounds(W, H)
    self.assertAlmostEqual(width, ideal_width, delta=1)
    self.assertAlmostEqual(height, ideal_height, delta=1)

  def test_numpy_crop_returns_the_expected_view(self):
    frame = np.arange(H * W, dtype=np.uint32).reshape(H, W)
    view = narrow_view_from_wide(frame)
    self.assertEqual(view.shape, (258, 412))
    self.assertTrue(np.shares_memory(frame, view))
    self.assertEqual(view[0, 0], frame[474, 758])
    self.assertEqual(view[-1, -1], frame[731, 1169])

  def test_portal_nv12_crop_keeps_chroma_alignment(self):
    # At 8x8 the focal-length ratio rounds to an even 2x2 crop at +2+2.
    # Give that crop unique constant Y/U/V values: resizing it should fill the
    # whole output with those values, proving UV was cropped on its 2x2 grid.
    w = h = 8
    y = np.zeros((h, w), dtype=np.uint8)
    uv = np.zeros((h // 2, w), dtype=np.uint8)
    left, top, crop_w, crop_h = narrow_crop_bounds(w, h)
    y[top:top + crop_h, left:left + crop_w] = 77
    uv[top // 2:(top + crop_h) // 2, left:left + crop_w:2] = 99
    uv[top // 2:(top + crop_h) // 2, left + 1:left + crop_w:2] = 155
    tight = np.concatenate((y.ravel(), uv.ravel())).tobytes()
    encoder = FrameEncoder(w, h, w, h, h // 2, w * h, w * h * 3 // 2)

    encoder.encode_nv12_center_crop(tight)

    self.assertTrue(np.all(encoder.y_view[:h, :w] == 77))
    self.assertTrue(np.all(encoder.uv_view[:h // 2, 0:w:2] == 99))
    self.assertTrue(np.all(encoder.uv_view[:h // 2, 1:w:2] == 155))


class TestBeamNGCameraConfig(unittest.TestCase):
  def setUp(self):
    self.saved = (camera.WIDE_CROP_ENABLED, camera.CAPTURE_VERTICAL_FOV,
                  camera.WIDE_CAMERA_PLACEMENT, camera.WIDE_CAMERA_HEIGHT_M,
                  camera.WIDE_CAMERA_CLEARANCE_M)

  def tearDown(self):
    (camera.WIDE_CROP_ENABLED, camera.CAPTURE_VERTICAL_FOV,
     camera.WIDE_CAMERA_PLACEMENT, camera.WIDE_CAMERA_HEIGHT_M,
     camera.WIDE_CAMERA_CLEARANCE_M) = self.saved

  def test_narrow_mode_keeps_legacy_pose(self):
    camera.WIDE_CROP_ENABLED = False
    camera.CAPTURE_VERTICAL_FOV = NARROW_VERTICAL_FOV
    camera.WIDE_CAMERA_PLACEMENT = camera.WIDE_CAMERA_PLACEMENT_VEHICLE_FRONT
    cfg = camera.lua_config()
    self.assertEqual(cfg["autoPlace"], 0.0)
    self.assertAlmostEqual(cfg["fov"], NARROW_VERTICAL_FOV)

  def test_wide_mode_sends_per_vehicle_anchor(self):
    camera.WIDE_CROP_ENABLED = True
    camera.CAPTURE_VERTICAL_FOV = WIDE_VERTICAL_FOV
    camera.WIDE_CAMERA_PLACEMENT = camera.WIDE_CAMERA_PLACEMENT_VEHICLE_FRONT
    camera.WIDE_CAMERA_HEIGHT_M = 1.3
    camera.WIDE_CAMERA_CLEARANCE_M = 0.2
    cfg = camera.lua_config()
    self.assertEqual(cfg, {"fov": WIDE_VERTICAL_FOV, "autoPlace": 1.0,
                           "height": 1.3, "clearance": 0.2})

  def test_legacy_option_disables_adaptive_anchor(self):
    camera.WIDE_CROP_ENABLED = True
    camera.WIDE_CAMERA_PLACEMENT = camera.WIDE_CAMERA_PLACEMENT_LEGACY
    self.assertEqual(camera.lua_config()["autoPlace"], 0.0)


if __name__ == "__main__":
  unittest.main(verbosity=2)
