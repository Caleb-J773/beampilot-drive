#!/usr/bin/env python3
"""Tests for beampilot's two blind spot behaviours in the lane change machine.

Run with: uv run python openpilot/selfdrive/controls/lib/test_desire_helper_bsm.py

  1. Refusing to START a lane change into an occupied lane. This is stock
     openpilot; the tests are here to pin it, because beampilot's auto lane
     change forces torque_applied and it would be easy to force it past the
     blind spot check too.
  2. CANCELLING one already under way. This is beampilot's, and it is the half
     that stock openpilot does not do at all.

DesireHelper is pure logic driven by carState, so all of it can be exercised
without the model, the car, or BeamNG.
"""
import unittest
from types import SimpleNamespace

from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib import desire_helper as dh

LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection

CRUISING = 30.0                       # m/s, well over LANE_CHANGE_SPEED_MIN
# Mid-manoeuvre the model is confident a lane change is happening, so
# lane_change_prob stays high and the "finished" branch never fires.
MID_MANOEUVRE_PROB = 1.0


def carstate(left_blinker=False, right_blinker=False,
             left_blindspot=False, right_blindspot=False, v_ego=CRUISING):
  return SimpleNamespace(vEgo=v_ego, leftBlinker=left_blinker, rightBlinker=right_blinker,
                         leftBlindspot=left_blindspot, rightBlindspot=right_blindspot,
                         steeringPressed=False, steeringTorque=0.0)


class LaneChangeTest(unittest.TestCase):
  """Auto lane change on, so a blinker alone commits -- beampilot's own config."""

  def setUp(self):
    self._auto = dh.AUTO_LANE_CHANGE
    self._abort = dh.LANE_CHANGE_ABORT
    self._window = dh.LANE_CHANGE_ABORT_MAX_TIME
    dh.AUTO_LANE_CHANGE = True
    dh.LANE_CHANGE_ABORT = True
    self.helper = dh.DesireHelper()

  def tearDown(self):
    dh.AUTO_LANE_CHANGE = self._auto
    dh.LANE_CHANGE_ABORT = self._abort
    dh.LANE_CHANGE_ABORT_MAX_TIME = self._window

  def tick(self, cs, seconds=DT_MDL, prob=MID_MANOEUVRE_PROB):
    for _ in range(max(1, round(seconds / DT_MDL))):
      self.helper.update(cs, True, prob)
    return self.helper.lane_change_state

  def arm(self, cs):
    """Blinker on: off -> preLaneChange. One tick, no delay elapsed yet."""
    self.helper.update(carstate(), True, 0.0)      # blinker off first, for the edge
    self.tick(cs)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.preLaneChange)

  def commit(self, cs):
    """Wait out AUTO_LANE_CHANGE_DELAY so the change actually starts."""
    self.tick(cs, dh.AUTO_LANE_CHANGE_DELAY + 0.1)


class TestRefusingToStart(LaneChangeTest):
  def test_commits_when_the_lane_is_clear(self):
    cs = carstate(left_blinker=True)
    self.arm(cs)
    self.commit(cs)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.laneChangeStarting)
    self.assertEqual(self.helper.desire, log.Desire.laneChangeLeft)

  def test_will_not_commit_into_an_occupied_lane(self):
    cs = carstate(left_blinker=True, left_blindspot=True)
    self.arm(cs)
    self.commit(cs)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.preLaneChange)
    self.assertEqual(self.helper.desire, log.Desire.none)

  def test_only_the_side_being_moved_into_blocks(self):
    # A car on the right must not stop a move to the left.
    cs = carstate(left_blinker=True, right_blindspot=True)
    self.arm(cs)
    self.commit(cs)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.laneChangeStarting)

  def test_commits_as_soon_as_the_lane_clears(self):
    blocked = carstate(left_blinker=True, left_blindspot=True)
    self.arm(blocked)
    self.commit(blocked)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.preLaneChange)
    self.tick(carstate(left_blinker=True))
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.laneChangeStarting)


class TestCancellingInFlight(LaneChangeTest):
  def start(self, direction="left", **blindspot):
    cs = carstate(**{f"{direction}_blinker": True}, **blindspot)
    self.arm(cs)
    self.commit(cs)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.laneChangeStarting)
    return cs

  def test_a_car_arriving_mid_manoeuvre_cancels_it(self):
    self.start()
    self.tick(carstate(left_blinker=True), 0.5)   # half a second in, still clear
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.laneChangeStarting)

    # ...and now someone is there.
    self.tick(carstate(left_blinker=True, left_blindspot=True))
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.preLaneChange)
    self.assertEqual(self.helper.desire, log.Desire.none,
                     "the desire must drop the same tick, or the model keeps steering over")

  def test_cancelling_is_immediate_not_gradual(self):
    self.start()
    before = self.helper.desire
    self.assertEqual(before, log.Desire.laneChangeLeft)
    self.helper.update(carstate(left_blinker=True, left_blindspot=True), True, MID_MANOEUVRE_PROB)
    self.assertEqual(self.helper.desire, log.Desire.none)

  def test_a_car_on_the_other_side_does_not_cancel(self):
    self.start()
    self.tick(carstate(left_blinker=True, right_blindspot=True), 0.5)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.laneChangeStarting)

  def test_it_resumes_by_itself_once_the_lane_clears(self):
    self.start()
    self.tick(carstate(left_blinker=True, left_blindspot=True), 0.2)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.preLaneChange)

    # Blinker still on, so it re-commits -- but only after the full delay, not
    # instantly, or a flickering blind spot would strobe the manoeuvre.
    self.tick(carstate(left_blinker=True), dh.AUTO_LANE_CHANGE_DELAY - 0.2)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.preLaneChange)
    self.tick(carstate(left_blinker=True), 0.4)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.laneChangeStarting)

  def test_cancelling_the_blinker_ends_it_entirely(self):
    self.start()
    self.tick(carstate(left_blinker=True, left_blindspot=True), 0.2)
    self.tick(carstate(), 0.2)   # blinker off
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.off)
    self.assertEqual(self.helper.desire, log.Desire.none)

  def test_too_late_in_the_manoeuvre_it_finishes_instead(self):
    # Past the window the car is mostly in the new lane; swerving back is worse
    # than completing. This is the deliberate limit, not an oversight.
    dh.LANE_CHANGE_ABORT_MAX_TIME = 2.0
    self.start()
    self.tick(carstate(left_blinker=True), 2.5)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.laneChangeStarting)
    self.tick(carstate(left_blinker=True, left_blindspot=True), 0.2)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.laneChangeStarting)
    self.assertEqual(self.helper.desire, log.Desire.laneChangeLeft)

  def test_the_window_is_configurable(self):
    dh.LANE_CHANGE_ABORT_MAX_TIME = 30.0   # effectively "abort at any point"
    self.start()
    self.tick(carstate(left_blinker=True), 5.0)
    self.tick(carstate(left_blinker=True, left_blindspot=True))
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.preLaneChange)

  def test_right_side_cancels_too(self):
    self.start("right")
    self.tick(carstate(right_blinker=True, right_blindspot=True), 0.2)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.preLaneChange)

  def test_disabled_means_stock_behaviour(self):
    dh.LANE_CHANGE_ABORT = False
    self.start()
    self.tick(carstate(left_blinker=True, left_blindspot=True), 0.5)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.laneChangeStarting)
    self.assertEqual(self.helper.desire, log.Desire.laneChangeLeft)


class TestStockPathIsUntouched(unittest.TestCase):
  """With no beampilot environment set, this has to behave like upstream."""

  def setUp(self):
    self._auto, self._abort = dh.AUTO_LANE_CHANGE, dh.LANE_CHANGE_ABORT
    dh.AUTO_LANE_CHANGE = False        # stock: a wheel nudge is required
    dh.LANE_CHANGE_ABORT = True        # the default, and still inert without a BSM feed
    self.helper = dh.DesireHelper()

  def tearDown(self):
    dh.AUTO_LANE_CHANGE, dh.LANE_CHANGE_ABORT = self._auto, self._abort

  def test_blinker_alone_never_commits(self):
    cs = carstate(left_blinker=True)
    self.helper.update(carstate(), True, 0.0)
    for _ in range(200):               # ten seconds
      self.helper.update(cs, True, 0.0)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.preLaneChange)

  def test_abort_is_dead_code_without_a_blindspot_feed(self):
    # carState.leftBlindspot/rightBlindspot are always False on a car whose DBC
    # has no BSM messages, which is every car this fork simulates by default.
    cs = SimpleNamespace(vEgo=CRUISING, leftBlinker=True, rightBlinker=False,
                         leftBlindspot=False, rightBlindspot=False,
                         steeringPressed=True, steeringTorque=1.0)
    self.helper.update(carstate(), True, 0.0)
    self.helper.update(cs, True, 0.0)                 # arm
    self.helper.update(cs, True, MID_MANOEUVRE_PROB)  # nudge commits it
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.laneChangeStarting)
    for _ in range(40):
      self.helper.update(cs, True, MID_MANOEUVRE_PROB)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.laneChangeStarting)

  def test_below_the_speed_threshold_nothing_arms(self):
    slow = carstate(left_blinker=True, v_ego=5 * CV.MPH_TO_MS)
    self.helper.update(carstate(v_ego=5 * CV.MPH_TO_MS), True, 0.0)
    self.helper.update(slow, True, 0.0)
    self.assertEqual(self.helper.lane_change_state, LaneChangeState.off)


if __name__ == "__main__":
  unittest.main(verbosity=2)
