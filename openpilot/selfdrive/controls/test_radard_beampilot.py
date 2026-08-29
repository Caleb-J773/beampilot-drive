#!/usr/bin/env python3
"""Tests for letting a ground-truth radar track become a lead on its own.

Run with: uv run python openpilot/selfdrive/controls/test_radard_beampilot.py

Stock radard will not look at a radar track unless the camera already reports a
lead (prob > 0.5). beampilot lifts that, because these points come from the
simulator's object list rather than a radar and cannot be a false positive --
and because the case worth fixing is the model MISSING a lead, which is exactly
what the gate blocks.

The risk that buys is picking a car in the next lane on a bend, so the in-lane
test follows the model's predicted path rather than straight ahead. That is
what most of these tests are about.
"""
import unittest

from openpilot.cereal import messaging
from openpilot.selfdrive.controls import radard
from openpilot.selfdrive.controls.radard import KalmanParams, Track
from openpilot.common.realtime import DT_MDL

KALMAN = KalmanParams(DT_MDL)


def track(track_id: int, d_rel: float, y_rel: float, v_rel: float = 0.0, v_ego: float = 25.0) -> Track:
  t = Track(track_id, v_rel + v_ego, KALMAN)
  t.update(d_rel, y_rel, v_rel, v_rel + v_ego)
  return t


def tracks(*args) -> dict[int, Track]:
  return {t.identifier: t for t in args}


def straight_path(length: int = 33):
  """A model path going straight ahead: x out to ~190m, y all zero."""
  xs = [float(i * i * 0.18) for i in range(length)]
  return xs, [0.0] * length


def curving_path(curvature: float, length: int = 33):
  """A constant-curvature path. Positive curvature bends RIGHT, since
  modelV2.position is in the device frame and that is y-positive."""
  xs = [float(i * i * 0.18) for i in range(length)]
  return xs, [0.5 * curvature * x * x for x in xs]


def lead_msg(prob: float = 0.0):
  msg = messaging.new_message('modelV2')
  msg.modelV2.leadsV3 = [{'prob': prob, 'x': [40.0], 'y': [0.0], 'v': [25.0], 'a': [0.0],
                          'xStd': [1.0], 'yStd': [1.0], 'vStd': [1.0]}] * 2
  return msg.modelV2.leadsV3[0]


class TestPathLateral(unittest.TestCase):
  def test_no_path_degrades_to_straight_ahead(self):
    self.assertEqual(radard.path_lateral_at(30.0, None, None), 0.0)
    self.assertEqual(radard.path_lateral_at(30.0, [1.0], [0.0]), 0.0)

  def test_straight_path_is_zero_everywhere(self):
    xs, ys = straight_path()
    self.assertAlmostEqual(radard.path_lateral_at(50.0, xs, ys), 0.0)

  def test_sign_is_flipped_into_the_radar_convention(self):
    # modelV2.position is device frame, y RIGHT positive; radar yRel is LEFT
    # positive. A path bending right must report a NEGATIVE lateral.
    xs, ys = curving_path(0.005)
    self.assertLess(radard.path_lateral_at(50.0, xs, ys), 0.0)
    xs, ys = curving_path(-0.005)
    self.assertGreater(radard.path_lateral_at(50.0, xs, ys), 0.0)


class TestRadarOnlyLead(unittest.TestCase):
  def setUp(self):
    self.xs, self.ys = straight_path()

  def pick(self, ts, rank=0, path=None):
    xs, ys = path or (self.xs, self.ys)
    return radard.get_radar_only_lead(ts, xs, ys, rank)

  def test_nothing_when_there_are_no_tracks(self):
    self.assertIsNone(self.pick({}))

  def test_picks_the_car_in_our_lane(self):
    chosen = self.pick(tracks(track(1, 30.0, 0.0)))
    self.assertIsNotNone(chosen)
    self.assertEqual(chosen.identifier, 1)

  def test_ignores_the_next_lane_over(self):
    self.assertIsNone(self.pick(tracks(track(1, 30.0, 3.5))))
    self.assertIsNone(self.pick(tracks(track(1, 30.0, -3.5))))

  def test_picks_the_nearest_of_several(self):
    chosen = self.pick(tracks(track(1, 60.0, 0.0), track(2, 20.0, 0.0), track(3, 40.0, 0.0)))
    self.assertEqual(chosen.identifier, 2)

  def test_rank_one_is_the_second_nearest(self):
    ts = tracks(track(1, 60.0, 0.0), track(2, 20.0, 0.0), track(3, 40.0, 0.0))
    self.assertEqual(self.pick(ts, rank=0).identifier, 2)
    self.assertEqual(self.pick(ts, rank=1).identifier, 3)
    self.assertEqual(self.pick(ts, rank=2).identifier, 1)
    self.assertIsNone(self.pick(ts, rank=3))

  def test_on_a_bend_it_follows_the_lane_not_straight_ahead(self):
    # Bending right at 0.005 1/m: at 50m the lane has moved ~6m right, so
    # yRel ~ -6. The car actually in our lane is the one at -6, and the one
    # dead ahead is in the next lane over -- the phantom-braking case.
    path = curving_path(0.005)
    offset = radard.path_lateral_at(50.0, *path)
    in_lane = track(1, 50.0, offset)
    straight_ahead = track(2, 50.0, 0.0)
    chosen = self.pick(tracks(in_lane, straight_ahead), path=path)
    self.assertIsNotNone(chosen)
    self.assertEqual(chosen.identifier, 1, "picked the car that is straight ahead, not the one in our lane")

  def test_ignores_anything_behind_the_bumper(self):
    self.assertIsNone(self.pick(tracks(track(1, -5.0, 0.0))))


class TestGetLead(unittest.TestCase):
  def setUp(self):
    self.previous = radard.RADAR_LEADS
    self.xs, self.ys = straight_path()

  def tearDown(self):
    radard.RADAR_LEADS = self.previous

  def call(self, ts, prob, rank=0):
    return radard.get_lead(25.0, True, ts, lead_msg(prob), 25.0, prob,
                           low_speed_override=False, rank=rank, path_x=self.xs, path_y=self.ys)

  def test_stock_behaviour_needs_the_camera_to_agree(self):
    radard.RADAR_LEADS = False
    self.assertFalse(self.call(tracks(track(1, 30.0, 0.0)), prob=0.0)['present'])

  def test_ground_truth_alone_produces_a_lead(self):
    radard.RADAR_LEADS = True
    lead = self.call(tracks(track(1, 30.0, 0.0)), prob=0.0)
    self.assertTrue(lead['present'])
    self.assertAlmostEqual(lead['dRel'], 30.0, delta=0.01)
    self.assertTrue(lead['radar'])

  def test_a_radar_only_lead_reports_full_confidence(self):
    # long_mpc.py's forward collision check needs modelProb > 0.9. Reporting
    # the camera's opinion (0.0) would disable FCW on exactly the leads the
    # camera missed, which are the ones this feature exists for.
    radard.RADAR_LEADS = True
    self.assertEqual(self.call(tracks(track(1, 30.0, 0.0)), prob=0.0)['modelProb'], 1.0)

  def test_no_lead_at_all_when_the_lane_ahead_is_empty(self):
    radard.RADAR_LEADS = True
    self.assertFalse(self.call(tracks(track(1, 30.0, 5.0)), prob=0.0)['present'])
    self.assertFalse(self.call({}, prob=0.0)['present'])

  def test_the_camera_still_wins_when_it_does_see_something(self):
    # With a confident vision lead, the stock match_vision_to_track path runs
    # and the radar-only branch is never reached.
    radard.RADAR_LEADS = True
    lead = self.call(tracks(track(1, 40.0 - radard.RADAR_TO_CAMERA, 0.0)), prob=1.0)
    self.assertTrue(lead['present'])
    self.assertEqual(lead['modelProb'], 1.0)

  def test_not_ready_produces_nothing(self):
    radard.RADAR_LEADS = True
    lead = radard.get_lead(25.0, False, tracks(track(1, 30.0, 0.0)), lead_msg(0.0), 25.0, 0.0,
                           low_speed_override=False, rank=0, path_x=self.xs, path_y=self.ys)
    self.assertFalse(lead['present'])


if __name__ == "__main__":
  unittest.main(verbosity=2)
