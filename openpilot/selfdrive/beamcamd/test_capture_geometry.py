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

from openpilot.selfdrive.beamcamd.beamcamd import crop_region_to_aspect

W, H = 1928, 1208
FRAME_ASPECT = W / H
VERTICAL_FOV = 25.70          # degrees; what the openpilot_cam mod renders
NARROW_H_FOV = 40.01          # degrees; what dc.narrow_road.intrinsics describe


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


if __name__ == "__main__":
  unittest.main(verbosity=2)
