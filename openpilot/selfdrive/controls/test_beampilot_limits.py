#!/usr/bin/env python3
"""Tests for how hard beampilot is allowed to drive the car.

Run with: uv run python openpilot/selfdrive/controls/test_beampilot_limits.py

Three separate ceilings decide this and they used to disagree, in ways that all
looked like "the tuning does nothing":

  * long_mpc bounded its own solution with opendbc's UNSCALED ACCEL_MAX, and
    the planner takes min(mpc, cruise), so the scale was mostly ignored.
  * the combined lat+long envelope was unscaled, so raising the lateral limit
    left nothing for the throttle mid-corner.
  * ExcessiveActuationCheck soft-disables on MEASURED actuation against
    hardcoded stock trip points, turning a raised limit into a disengagement.

Everything below is about those three agreeing, and about an unset environment
still being stock openpilot.
"""
import importlib
import os
import subprocess
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

BEAMPILOT_VARS = ("BEAMPILOT_ACCEL_SCALE", "BEAMPILOT_DECEL_SCALE", "BEAMPILOT_MAX_LAT_ACCEL",
                  "BEAMPILOT_MAX_LAT_JERK", "BEAMPILOT_MAX_CURVATURE", "BEAMPILOT_ACTUATION_MARGIN")


def limits_with(**env):
  """The module's constants as they would be under a given environment.

  A SNAPSHOT, not the module: importlib.reload returns the same module object
  it was handed, so restoring the environment afterwards would rewrite the very
  values the caller is about to assert on.
  """
  from openpilot.common import beampilot_limits
  saved = {k: os.environ.get(k) for k in BEAMPILOT_VARS}
  try:
    for k in BEAMPILOT_VARS:
      os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in env.items()})
    reloaded = importlib.reload(beampilot_limits)
    return types.SimpleNamespace(**{name: getattr(reloaded, name)
                                    for name in dir(reloaded) if name.isupper()})
  finally:
    for k, v in saved.items():
      os.environ.pop(k, None)
      if v is not None:
        os.environ[k] = v
    importlib.reload(beampilot_limits)


class TestStockIsUntouched(unittest.TestCase):
  def test_an_unset_environment_is_upstream_openpilot(self):
    from opendbc.car.interfaces import ACCEL_MAX, ACCEL_MIN
    from opendbc.car.lateral import ISO_LATERAL_ACCEL
    limits = limits_with()
    self.assertEqual(limits.ACCEL_MAX, ACCEL_MAX)
    self.assertEqual(limits.ACCEL_MIN, ACCEL_MIN)
    self.assertEqual(limits.MAX_LATERAL_ACCEL_NO_ROLL, ISO_LATERAL_ACCEL)
    self.assertEqual(limits.MAX_LATERAL_JERK, 5.0)
    self.assertEqual(limits.MAX_CURVATURE, 0.2)
    self.assertEqual(limits.A_TOTAL_MAX_SCALE, 1.0)

  def test_the_stock_actuation_trip_points_are_unchanged(self):
    # Upstream: 2x ISO lateral, 2x stock accel. Anything else here is a
    # behaviour change to a safety net, which is not a thing to do by accident.
    limits = limits_with()
    self.assertEqual(limits.EXCESSIVE_LATERAL_ACCEL, 6.0)
    self.assertEqual(limits.EXCESSIVE_ACCEL, 4.0)
    self.assertEqual(limits.EXCESSIVE_DECEL, -7.0)


class TestScaling(unittest.TestCase):
  def test_accel_and_decel_scale_independently(self):
    limits = limits_with(BEAMPILOT_ACCEL_SCALE=2.0, BEAMPILOT_DECEL_SCALE=1.5)
    self.assertAlmostEqual(limits.ACCEL_MAX, 4.0)
    self.assertAlmostEqual(limits.ACCEL_MIN, -5.25)

  def test_the_combined_envelope_follows_whichever_was_raised_further(self):
    # Grip is shared. If only the lateral limit goes up, the envelope still has
    # to grow or cornering eats the entire throttle budget.
    self.assertAlmostEqual(limits_with(BEAMPILOT_MAX_LAT_ACCEL=6.0).A_TOTAL_MAX_SCALE, 2.0)
    self.assertAlmostEqual(limits_with(BEAMPILOT_ACCEL_SCALE=3.0).A_TOTAL_MAX_SCALE, 3.0)
    self.assertAlmostEqual(
      limits_with(BEAMPILOT_ACCEL_SCALE=2.0, BEAMPILOT_MAX_LAT_ACCEL=4.5).A_TOTAL_MAX_SCALE, 2.0)

  def test_lowering_a_limit_never_lowers_the_safety_net_below_stock(self):
    # Someone tuning DOWN must not accidentally tighten the disengage threshold
    # under what upstream considers acceptable.
    limits = limits_with(BEAMPILOT_ACCEL_SCALE=0.5, BEAMPILOT_MAX_LAT_ACCEL=1.0)
    self.assertEqual(limits.EXCESSIVE_ACCEL, 4.0)
    self.assertEqual(limits.EXCESSIVE_LATERAL_ACCEL, 6.0)
    self.assertGreaterEqual(limits.A_TOTAL_MAX_SCALE, 0.5)


class TestTheNetIsAlwaysAboveTheLimit(unittest.TestCase):
  """The bug class this module exists to prevent.

  ExcessiveActuationCheck soft-disables on measured actuation. If its trip point
  can sit at or below what the planner is allowed to COMMAND, then asking for
  more performance produces a disengagement instead -- and with the config this
  repo shipped (ACCEL_SCALE=2.0) the commanded ceiling was exactly the trip
  point.
  """

  CASES = [{}, {"BEAMPILOT_ACCEL_SCALE": 2.0}, {"BEAMPILOT_ACCEL_SCALE": 4.0},
           {"BEAMPILOT_DECEL_SCALE": 2.5}, {"BEAMPILOT_MAX_LAT_ACCEL": 8.0},
           {"BEAMPILOT_ACCEL_SCALE": 3.0, "BEAMPILOT_DECEL_SCALE": 2.0,
            "BEAMPILOT_MAX_LAT_ACCEL": 7.0}]

  def test_the_trip_point_is_never_reachable_by_a_legal_command(self):
    for case in self.CASES:
      limits = limits_with(**case)
      self.assertGreater(limits.EXCESSIVE_ACCEL, limits.ACCEL_MAX, case)
      self.assertLess(limits.EXCESSIVE_DECEL, limits.ACCEL_MIN, case)
      self.assertGreater(limits.EXCESSIVE_LATERAL_ACCEL, limits.MAX_LATERAL_ACCEL_NO_ROLL, case)

  def test_the_margin_is_tunable_but_still_leaves_headroom(self):
    limits = limits_with(BEAMPILOT_ACCEL_SCALE=2.0, BEAMPILOT_ACTUATION_MARGIN=1.25)
    self.assertAlmostEqual(limits.EXCESSIVE_ACCEL, 5.0)
    self.assertGreater(limits.EXCESSIVE_ACCEL, limits.ACCEL_MAX)


class TestEveryConsumerAgrees(unittest.TestCase):
  """The planner, the MPC and the bridge must end up with the same numbers.

  Run in a subprocess: these modules read the environment once at import, so a
  reload in-process would not reach the ones already loaded.
  """

  def chain(self, **env):
    script = (
      "import openpilot.selfdrive.controls.lib.longitudinal_planner as lp;"
      + "import openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc as mpc;"
      + "import openpilot.selfdrive.controls.lib.drive_helpers as dh;"
      + "import openpilot.selfdrive.selfdrived.helpers as sh;"
      + "print(lp.ACCEL_MAX, lp.ACCEL_MIN, mpc.ACCEL_MAX, mpc.ACCEL_MIN,"
      + " dh.MAX_LATERAL_ACCEL_NO_ROLL, sh.EXCESSIVE_ACCEL, sh.EXCESSIVE_LATERAL_ACCEL,"
      + " lp._A_TOTAL_MAX_V[0])"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True,
                            text=True, timeout=300,
                            env={**os.environ, **{k: str(v) for k, v in env.items()}})
    self.assertEqual(result.returncode, 0, result.stderr[-2000:])
    return [float(v) for v in result.stdout.split()]

  def test_the_mpc_bound_tracks_the_planner_clip(self):
    # The one that was actually broken: the MPC solved against 2.0 while the
    # planner clipped at 4.0, and min(mpc, cruise) meant the MPC won.
    a_max, a_min, mpc_max, mpc_min, *_ = self.chain(BEAMPILOT_ACCEL_SCALE=2.0,
                                                    BEAMPILOT_DECEL_SCALE=1.5)
    self.assertAlmostEqual(a_max, 4.0, places=3)
    self.assertAlmostEqual(mpc_max, a_max, places=6)
    self.assertAlmostEqual(mpc_min, a_min, places=6)

  def test_the_whole_chain_is_stock_with_no_environment(self):
    values = self.chain(BEAMPILOT_ACCEL_SCALE=1.0, BEAMPILOT_DECEL_SCALE=1.0,
                        BEAMPILOT_MAX_LAT_ACCEL=3.0, BEAMPILOT_ACTUATION_MARGIN=2.0)
    a_max, a_min, mpc_max, mpc_min, lat, exc_a, exc_lat, envelope = values
    self.assertEqual((a_max, a_min, mpc_max, mpc_min), (2.0, -3.5, 2.0, -3.5))
    self.assertEqual((lat, exc_a, exc_lat), (3.0, 4.0, 6.0))
    self.assertAlmostEqual(envelope, 1.7, places=3)

  def test_raising_the_lateral_limit_also_frees_the_throttle_mid_corner(self):
    *_, envelope = self.chain(BEAMPILOT_MAX_LAT_ACCEL=6.0)
    self.assertAlmostEqual(envelope, 3.4, places=3)


if __name__ == "__main__":
  unittest.main(verbosity=2)
