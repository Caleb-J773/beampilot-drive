#!/usr/bin/env python3
"""Tests for the on-screen radar track markers.

Run with: uv run python openpilot/selfdrive/ui/onroad/test_radar_renderer.py

_draw_marker is the only call in _render that touches raylib, so stubbing it
lets the real code path run without a GL context -- which covers the parts
worth covering: which tracks get drawn, where they land, and which one gets the
lead ring. What the markers actually look like is judged by eye; see the module
docstring in radar_renderer.py.
"""
import types
import unittest

import numpy as np

from openpilot.selfdrive.ui.onroad import radar_renderer as rr


class FakeRect:
  def __init__(self, w=2160.0, h=1080.0):
    self.x, self.y, self.width, self.height = 0.0, 0.0, w, h


def transform(width=2160.0):
  """openpilot's narrow intrinsics, car space -> screen, scaled to the window."""
  fx = fy = 2648.0
  intrinsics = np.array([[fx, 0, 1928 / 2], [0, fy, 1208 / 2], [0, 0, 1.0]])
  view_from_car = np.array([[0., 1., 0.], [0., 0., 1.], [1., 0., 0.]])
  zoom = width / 1928
  return np.array([[zoom, 0, 0], [0, zoom, 0], [0, 0, 1.0]]) @ intrinsics @ view_from_car


def point(track_id, d_rel, y_rel, v_rel=0.0):
  return types.SimpleNamespace(trackId=track_id, dRel=d_rel, yRel=y_rel, vRel=v_rel)


def lead(track_id=None, radar=True):
  return types.SimpleNamespace(present=track_id is not None, radar=radar,
                               radarTrackId=track_id if track_id is not None else -1)


class FakeSubMaster:
  def __init__(self, points, lead_one, lead_two, height=1.22, recv_frame=100):
    self.recv_frame = {"radarTracks": recv_frame}
    self._d = {
      'radarTracks': types.SimpleNamespace(points=points),
      'extrinsicsCalibration': types.SimpleNamespace(height=[height] if height else []),
      'radarState': types.SimpleNamespace(leadOne=lead_one, leadTwo=lead_two),
    }

  def __getitem__(self, key):
    return self._d[key]


class FakeUIState:
  def __init__(self, sm, started=True, started_frame=0):
    self.sm, self.started, self.started_frame = sm, started, started_frame


class RadarRendererTest(unittest.TestCase):
  def setUp(self):
    self.previous_ui, self.previous_flag = rr.ui_state, rr.RADAR_INDICATOR
    rr.RADAR_INDICATOR = True
    self.renderer = rr.RadarRenderer()
    self.renderer.set_transform(transform())
    self.drawn: list[dict] = []
    self.renderer._draw_marker = lambda centre, hw, hh, is_lead: self.drawn.append(
      {"x": centre[0], "y": centre[1], "half_w": hw, "half_h": hh, "lead": is_lead})

  def tearDown(self):
    rr.ui_state, rr.RADAR_INDICATOR = self.previous_ui, self.previous_flag

  def render(self, points, lead_one=None, lead_two=None, started=True, started_frame=0, height=1.22):
    sm = FakeSubMaster(points, lead_one or lead(), lead_two or lead(), height=height)
    rr.ui_state = FakeUIState(sm, started=started, started_frame=started_frame)
    self.drawn.clear()
    self.renderer._render(FakeRect())
    return self.drawn


class TestWhatGetsDrawn(RadarRendererTest):
  def test_nothing_with_no_tracks(self):
    self.assertEqual(self.render([]), [])

  def test_one_marker_per_track(self):
    self.assertEqual(len(self.render([point(1, 20.0, 0.0), point(2, 40.0, 2.0)])), 2)

  def test_nothing_when_switched_off(self):
    rr.RADAR_INDICATOR = False
    self.assertEqual(self.render([point(1, 20.0, 0.0)]), [])

  def test_nothing_before_a_transform_arrives(self):
    fresh = rr.RadarRenderer()
    fresh._draw_marker = lambda *a: self.fail("drew without a calibration transform")
    rr.ui_state = FakeUIState(FakeSubMaster([point(1, 20.0, 0.0)], lead(), lead()))
    fresh._render(FakeRect())

  def test_nothing_before_the_car_starts(self):
    self.assertEqual(self.render([point(1, 20.0, 0.0)], started=False), [])

  def test_nothing_on_stale_tracks_from_a_previous_drive(self):
    self.assertEqual(self.render([point(1, 20.0, 0.0)], started_frame=500), [])

  def test_points_behind_the_bumper_are_skipped(self):
    self.assertEqual(self.render([point(1, -5.0, 0.0), point(2, 0.0, 0.0)]), [])

  def test_points_past_the_draw_distance_are_skipped(self):
    self.assertEqual(self.render([point(1, rr.MAX_DRAW_DISTANCE + 10, 0.0)]), [])


class TestWhereTheyLand(RadarRendererTest):
  def test_a_car_dead_ahead_is_centred(self):
    drawn = self.render([point(1, 30.0, 0.0)])
    self.assertAlmostEqual(drawn[0]["x"], 1928 / 2 * (2160 / 1928), delta=2)

  def test_left_positive_yrel_draws_to_the_left(self):
    # car.capnp: yRel is LEFT positive; car space is y-RIGHT. Getting this
    # backwards puts every marker in the wrong lane.
    left = self.render([point(1, 30.0, 3.5)])[0]
    right = self.render([point(1, 30.0, -3.5)])[0]
    centre = self.render([point(1, 30.0, 0.0)])[0]
    self.assertLess(left["x"], centre["x"])
    self.assertGreater(right["x"], centre["x"])

  def test_nearer_tracks_sit_lower_on_screen(self):
    near = self.render([point(1, 15.0, 0.0)])[0]
    far = self.render([point(1, 90.0, 0.0)])[0]
    self.assertGreater(near["y"], far["y"], "a near car must draw below a distant one")

  def test_nearer_tracks_are_bigger(self):
    near = self.render([point(1, 15.0, 0.0)])[0]
    far = self.render([point(1, 90.0, 0.0)])[0]
    self.assertGreater(near["half_w"], far["half_w"])

  def test_marker_size_stays_within_its_clamps(self):
    for distance in (1.0, 5.0, 25.0, 80.0, 149.0):
      drawn = self.render([point(1, distance, 0.0)])
      if drawn:
        self.assertGreaterEqual(drawn[0]["half_w"], rr.MIN_HALF_PX, f"{distance}m")
        self.assertLessEqual(drawn[0]["half_w"], rr.MAX_HALF_PX, f"{distance}m")

  def test_the_shape_is_fixed_not_flattened_by_distance(self):
    # A physically flat diamond goes edge-on past ~30m and renders as a dash.
    for distance in (15.0, 45.0, 120.0):
      drawn = self.render([point(1, distance, 0.0)])[0]
      self.assertAlmostEqual(drawn["half_h"] / drawn["half_w"], rr.MARKER_ASPECT, places=5)


class TestTheLeadRing(RadarRendererTest):
  TRACKS = [point(1, 20.0, 0.0), point(2, 45.0, 2.0), point(3, 70.0, -1.0)]

  def rings(self, **kwargs):
    return [d["lead"] for d in self.render(self.TRACKS, **kwargs)]

  def test_no_ring_when_radard_picked_nothing(self):
    self.assertEqual(self.rings(), [False, False, False])

  def test_the_ring_follows_radards_choice_not_the_nearest(self):
    self.assertEqual(self.rings(lead_one=lead(2)), [False, True, False])

  def test_both_leads_can_be_ringed(self):
    self.assertEqual(self.rings(lead_one=lead(1), lead_two=lead(3)), [True, False, True])

  def test_a_camera_only_lead_gets_no_ring(self):
    # radar=False means radard matched vision, not one of our tracks -- there is
    # no track id to point at, and radarTrackId is -1.
    self.assertEqual(self.rings(lead_one=lead(2, radar=False)), [False, False, False])


class TestProjection(unittest.TestCase):
  def setUp(self):
    self.renderer = rr.RadarRenderer()
    self.renderer.set_transform(transform())

  def test_a_point_behind_the_camera_projects_to_nothing(self):
    self.assertIsNone(self.renderer._map_to_screen(0.0, 0.0, 1.22, FakeRect()))

  def test_a_point_far_off_to_the_side_is_clipped(self):
    self.assertIsNone(self.renderer._map_to_screen(5.0, 400.0, 1.22, FakeRect()))

  def test_a_point_ahead_projects_inside_the_frame(self):
    screen = self.renderer._map_to_screen(30.0, 0.0, 1.22, FakeRect())
    self.assertIsNotNone(screen)
    self.assertTrue(0 <= screen[0] <= 2160)


if __name__ == "__main__":
  unittest.main(verbosity=2)
