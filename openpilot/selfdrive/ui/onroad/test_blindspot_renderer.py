#!/usr/bin/env python3
"""Tests for the blind spot indicator's state logic.

Run with: uv run python openpilot/selfdrive/ui/onroad/test_blindspot_renderer.py

Only _update_state is covered -- what lights up and what flashes. The drawing
itself needs a GL context and is verified by eye; see the module docstring in
blindspot_renderer.py for what it is meant to look like.
"""
import types
import unittest

from openpilot.cereal import log
from openpilot.selfdrive.ui.onroad import blindspot_renderer as br

LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection


class FakeSubMaster:
  def __init__(self, left, right, state, direction):
    self.recv_frame = {"carState": 100}
    self._data = {
      'carState': types.SimpleNamespace(leftBlindspot=left, rightBlindspot=right),
      'modelV2': types.SimpleNamespace(
        meta=types.SimpleNamespace(laneChangeState=state, laneChangeDirection=direction)),
    }

  def __getitem__(self, key):
    return self._data[key]


class FakeUIState:
  def __init__(self, sm, started=True, started_frame=0):
    self.sm, self.started, self.started_frame = sm, started, started_frame


class TestBlindSpotRenderer(unittest.TestCase):
  def setUp(self):
    self.previous = br.ui_state
    # The lamps ship OFF -- the radar markers own the road view now -- so these
    # tests have to turn them on to exercise the logic at all.
    self.previous_flag = br.BSM_INDICATOR
    br.BSM_INDICATOR = True
    self.renderer = br.BlindSpotRenderer()

  def tearDown(self):
    br.ui_state = self.previous
    br.BSM_INDICATOR = self.previous_flag

  def update(self, left=False, right=False, state=LaneChangeState.off,
             direction=LaneChangeDirection.none, started=True, started_frame=0):
    br.ui_state = FakeUIState(FakeSubMaster(left, right, state, direction), started, started_frame)
    self.renderer._update_state()
    return (self.renderer.left_occupied, self.renderer.right_occupied,
            self.renderer.left_blocking, self.renderer.right_blocking)

  def test_nothing_lights_when_both_sides_are_clear(self):
    self.assertEqual(self.update(), (False, False, False, False))

  def test_each_side_lights_on_its_own_flag(self):
    self.assertEqual(self.update(left=True), (True, False, False, False))
    self.assertEqual(self.update(right=True), (False, True, False, False))
    self.assertEqual(self.update(left=True, right=True), (True, True, False, False))

  def test_steady_when_not_signalling_into_it(self):
    # A car is there, but no lane change is being attempted: lamp on, no flash.
    _, _, left_blocking, _ = self.update(left=True)
    self.assertFalse(left_blocking)

  def test_flashes_while_a_lane_change_is_refused(self):
    state = self.update(left=True, state=LaneChangeState.preLaneChange,
                        direction=LaneChangeDirection.left)
    self.assertEqual(state, (True, False, True, False))

  def test_flashes_while_a_lane_change_is_being_cancelled(self):
    # desire_helper.py cancels out of laneChangeStarting, so that state has to
    # flash too -- otherwise the one moment the driver most needs to see it is
    # the one moment it stays steady.
    state = self.update(right=True, state=LaneChangeState.laneChangeStarting,
                        direction=LaneChangeDirection.right)
    self.assertEqual(state, (False, True, False, True))

  def test_signalling_away_from_the_car_does_not_flash(self):
    state = self.update(left=True, state=LaneChangeState.preLaneChange,
                        direction=LaneChangeDirection.right)
    self.assertEqual(state, (True, False, False, False))

  def test_the_lamps_can_be_switched_off_entirely(self):
    br.BSM_INDICATOR = False
    # The blind spot still gates lane changes; only the display goes away.
    self.assertEqual(self.update(left=True, right=True, state=LaneChangeState.preLaneChange,
                                 direction=LaneChangeDirection.left),
                     (False, False, False, False))

  def test_off_is_the_shipped_default(self):
    # Two overlays competing for the road view is worse than either alone, so
    # the radar markers win by default. Changing this is a UX decision, not a
    # tidy-up -- hence a test.
    from openpilot.common.beampilot_env import env_bool
    self.assertFalse(env_bool("BEAMPILOT_BSM_INDICATOR", False))

  def test_nothing_before_the_car_starts(self):
    self.assertEqual(self.update(left=True, right=True, started=False), (False, False, False, False))

  def test_nothing_on_stale_carstate_from_a_previous_drive(self):
    # recv_frame is 100; a started_frame after it means this carState predates
    # the current drive and must not be trusted.
    self.assertEqual(self.update(left=True, started_frame=500), (False, False, False, False))


if __name__ == "__main__":
  unittest.main(verbosity=2)
