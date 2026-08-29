#!/usr/bin/env python3
"""Tests for slowing down before a corner.

Run with: uv run python openpilot/selfdrive/controls/test_beampilot_curve.py

Stock openpilot holds the set speed through a bend -- it caps acceleration once
already cornering, but never brakes for one ahead. That is survivable on a real
car with a wide camera and a driver watching. Here the model's view of a corner
is a 25.70-degree narrow one, so it arrives late, and beampilot's raised lateral
limits mean the car will carry a speed into a bend it cannot then hold.
"""
import math
import unittest

from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib import beampilot_curve as bc

# The model's own time grid, out to 10s.
T_IDXS = [(i / 32.0) ** 2 * 10.0 for i in range(33)]


def straight_path(v=30.0):
  """No curvature: positions marching out, zero yaw rate."""
  x = [v * t for t in T_IDXS]
  return x, [v] * len(T_IDXS), [0.0] * len(T_IDXS)


def corner_path(radius, v=30.0, starts_at=None):
  """A bend of `radius` metres, optionally only after `starts_at` metres."""
  x = [v * t for t in T_IDXS]
  yaw_rate = [0.0 if (starts_at is not None and xi < starts_at) else v / radius for xi in x]
  return x, [v] * len(T_IDXS), yaw_rate


class TestSafeSpeed(unittest.TestCase):
  def test_a_straight_road_imposes_no_limit(self):
    self.assertEqual(bc.safe_speed(0.0), float("inf"))
    self.assertEqual(bc.safe_speed(1e-9), float("inf"))

  def test_speed_follows_sqrt_of_radius(self):
    # v = sqrt(a / k) = sqrt(a * r)
    for radius in (50, 150, 400):
      self.assertAlmostEqual(bc.safe_speed(1 / radius, lat_accel=2.0, min_speed=0.0),
                             math.sqrt(2.0 * radius), places=4)

  def test_direction_does_not_matter(self):
    self.assertEqual(bc.safe_speed(0.01), bc.safe_speed(-0.01))

  def test_it_never_asks_for_a_crawl(self):
    # A car park's worth of curvature must not produce a walking pace.
    self.assertGreaterEqual(bc.safe_speed(1 / 5.0, min_speed=5.0), 5.0)

  def test_tighter_corners_are_slower(self):
    speeds = [bc.safe_speed(1 / r) for r in (400, 200, 100, 50)]
    self.assertEqual(speeds, sorted(speeds, reverse=True))


class TestRequiredAccel(unittest.TestCase):
  def test_slowing_needs_negative_accel(self):
    self.assertLess(bc.required_accel(30.0, 20.0, 100.0), 0.0)

  def test_the_further_away_the_gentler(self):
    near = bc.required_accel(30.0, 20.0, 50.0)
    far = bc.required_accel(30.0, 20.0, 200.0)
    self.assertLess(near, far)

  def test_it_matches_the_kinematics(self):
    # v^2 = u^2 + 2as
    a = bc.required_accel(30.0, 20.0, 100.0)
    self.assertAlmostEqual(30.0 ** 2 + 2 * a * 100.0, 20.0 ** 2, places=6)

  def test_a_zero_distance_does_not_divide_by_zero(self):
    self.assertLess(bc.required_accel(30.0, 10.0, 0.0), 0.0)


class TestLimiter(unittest.TestCase):
  def setUp(self):
    self.previous = bc.CURVE_SLOWDOWN
    bc.CURVE_SLOWDOWN = True
    self.limiter = bc.CurveSpeedLimiter(DT_MDL)

  def tearDown(self):
    bc.CURVE_SLOWDOWN = self.previous

  def run_for(self, v_ego, path, ticks=40):
    """Settle the rate limiter, then return its steady request."""
    result = None
    for _ in range(ticks):
      result = self.limiter.update(v_ego, *path, T_IDXS)
    return result

  def test_a_straight_road_asks_for_nothing(self):
    self.assertIsNone(self.run_for(30.0, straight_path()))

  def test_a_corner_asks_for_braking(self):
    accel = self.run_for(30.0, corner_path(150))
    self.assertIsNotNone(accel)
    self.assertLess(accel, 0.0)

  def test_a_tighter_corner_asks_for_more(self):
    gentle = bc.CurveSpeedLimiter(DT_MDL)
    tight = bc.CurveSpeedLimiter(DT_MDL)
    for _ in range(40):
      g = gentle.update(30.0, *corner_path(300), T_IDXS)
      t = tight.update(30.0, *corner_path(80), T_IDXS)
    self.assertLess(t, g)

  def test_a_corner_it_is_already_slow_enough_for_asks_for_nothing(self):
    # 400m radius at 2.1 m/s^2 allows ~29 m/s; at 12 m/s there is nothing to do.
    self.assertIsNone(self.run_for(12.0, corner_path(400)))

  def test_nothing_below_the_enable_speed(self):
    self.assertIsNone(self.run_for(2.0, corner_path(50)))

  def test_switched_off_means_switched_off(self):
    bc.CURVE_SLOWDOWN = False
    self.assertIsNone(self.run_for(30.0, corner_path(60)))

  def test_it_never_exceeds_the_braking_limit(self):
    accel = self.run_for(35.0, corner_path(25), ticks=200)
    self.assertGreaterEqual(accel, bc.ACCEL_MIN)

  def test_the_request_is_rate_limited(self):
    # One tick from cold cannot produce full braking, however tight the corner.
    first = self.limiter.update(35.0, *corner_path(20), T_IDXS)
    self.assertGreaterEqual(first, -bc.CURVE_JERK * DT_MDL - 1e-9)

  def test_leaving_a_corner_releases_smoothly(self):
    braking = self.run_for(30.0, corner_path(80))
    self.assertLess(braking, -0.1)
    released = self.limiter.update(30.0, *straight_path(), T_IDXS)
    self.assertIsNotNone(released, "dropping the request outright would step the throttle")
    self.assertGreater(released, braking)
    for _ in range(200):
      released = self.limiter.update(30.0, *straight_path(), T_IDXS)
    self.assertIsNone(released, "it has to let go eventually")

  def test_a_corner_further_away_is_gentler(self):
    near = bc.CurveSpeedLimiter(DT_MDL)
    far = bc.CurveSpeedLimiter(DT_MDL)
    for _ in range(400):   # long enough for the rate limit not to be what differs
      n = near.update(30.0, *corner_path(80, starts_at=30), T_IDXS)
      f = far.update(30.0, *corner_path(80, starts_at=150), T_IDXS)
    self.assertLess(n, f)

  def test_it_reports_the_speed_the_corner_allows(self):
    self.run_for(30.0, corner_path(100))
    self.assertAlmostEqual(self.limiter.target_speed,
                           math.sqrt(bc.CURVE_LATERAL_ACCEL * 100), delta=0.5)

  def test_ragged_model_output_does_not_crash_it(self):
    for path in (([], [], []), ([0.0], [1.0], [0.0]), ([10.0, 20.0], [30.0], [0.0, 0.0])):
      self.limiter.update(30.0, *path, T_IDXS)


class TestDefaults(unittest.TestCase):
  def test_the_target_sits_below_the_hard_lateral_limit(self):
    # Arriving at exactly the limit leaves clip_curvature saturated for the
    # whole bend with nothing in reserve for a mid-corner correction.
    from openpilot.common.beampilot_limits import MAX_LATERAL_ACCEL_NO_ROLL
    self.assertLess(bc.CURVE_LATERAL_ACCEL, MAX_LATERAL_ACCEL_NO_ROLL)


if __name__ == "__main__":
  unittest.main(verbosity=2)
