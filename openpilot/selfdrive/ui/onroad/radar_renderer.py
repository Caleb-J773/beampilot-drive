"""beampilot: draw the ground-truth radar's tracks on the road view.

openpilot already puts a chevron on the lead car, but only on the lead, and
only when it has longitudinal control. Nothing shows the radar itself. On a
real car that is reasonable -- there is no radar worth looking at. Here the
points come from the simulator's own object list, and the first question about
them is always "is it seeing what I think it is", which a lead chevron cannot
answer: a missing chevron could mean no traffic, no radar feed, a stale mod, or
a lead the model rejected.

So: a cyan dot on the road under every scanned track (radar/minimap
convention for a detection, not a shape openpilot's own UI uses elsewhere),
and a concentric ring around whichever one radard picked as the lead. Nothing
is drawn when there are no tracks, so with BEAMPILOT_RADAR off this is
invisible. (model_renderer.py already puts its own chevron on radarState's
lead regardless of source -- vision-only or radar-confirmed -- whenever
openpilot has longitudinal control, so a vision-only lead with no scanned
track is never actually invisible; this widget only adds the "which of the
tracks on screen is that" answer a plain chevron can't give.)

Projection is the same one ModelRenderer uses -- car space (x forward, y RIGHT,
z down) through the calibration and video transforms -- so the markers land
where the model thinks the road is, not merely somewhere plausible. Note the
frame flip: radar yRel is LEFT positive (car.capnp), car space is y-right.
"""
import numpy as np
import pyray as rl

from openpilot.common.beampilot_env import env_bool
from openpilot.selfdrive.locationd.calibrationd import HEIGHT_INIT
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.widgets import Widget

RADAR_INDICATOR = env_bool("BEAMPILOT_RADAR_INDICATOR", True)

# Cyan reads as "sensor data" against the model's green path and the amber
# blind spot lamps, and is not otherwise used on this screen.
TRACK = rl.Color(0, 220, 255, 190)
TRACK_EDGE = rl.Color(0, 60, 80, 200)
LEAD_RING = rl.Color(255, 255, 255, 230)

MAX_DRAW_DISTANCE = 150.0
# The marker is a footprint on the road, roughly a car's, projected through the
# same perspective as everything else -- so it shrinks with distance because the
# scene does, not because of a fudge factor. Half-extents in METRES.
FOOTPRINT_LEN = 1.3
FOOTPRINT_WIDTH = 0.9
# ...but clamped in pixels afterwards, or a track at 120m is two pixels across
# and invisible, which defeats the point of drawing it.
MIN_RADIUS_PX, MAX_RADIUS_PX = 6.0, 42.0
CLIP_MARGIN = 200


class RadarRenderer(Widget):
  def __init__(self):
    super().__init__()
    self._car_space_transform = np.zeros((3, 3))
    self._has_transform = False
    self._path_offset_z = HEIGHT_INIT[0]

  def set_transform(self, transform: np.ndarray):
    self._car_space_transform = transform.astype(np.float32)
    self._has_transform = True

  def _render(self, rect: rl.Rectangle) -> None:
    if not RADAR_INDICATOR or not self._has_transform or not ui_state.started:
      return

    sm = ui_state.sm
    if sm.recv_frame["radarTracks"] < ui_state.started_frame:
      return

    points = sm['radarTracks'].points
    if not len(points):
      return

    calibration = sm['extrinsicsCalibration']
    if calibration.height:
      self._path_offset_z = calibration.height[0]

    # radard reports which track it settled on, so the lead can be picked out
    # of the crowd rather than guessed at by distance.
    radar_state = sm['radarState']
    lead_ids = {lead.radarTrackId for lead in (radar_state.leadOne, radar_state.leadTwo)
                if lead.present and lead.radar and lead.radarTrackId >= 0}

    for point in points:
      if not 0.0 < point.dRel <= MAX_DRAW_DISTANCE:
        continue
      centre, radius = self._project(point.dRel, point.yRel, rect)
      if centre is None:
        continue
      self._draw_marker(centre, radius, point.trackId in lead_ids, TRACK, TRACK_EDGE)

  def _project(self, d_rel: float, y_rel: float,
              rect: rl.Rectangle) -> tuple[tuple[float, float] | None, float]:
    # car space is y-RIGHT; radar yRel is y-LEFT.
    car_y = -y_rel
    centre = self._map_to_screen(d_rel, car_y, self._path_offset_z, rect)
    if centre is None:
      return None, 0.0
    # Project the footprint's far corner too, so the marker's size comes out
    # of the same perspective as the road rather than an invented curve.
    edge = self._map_to_screen(d_rel - FOOTPRINT_LEN, car_y + FOOTPRINT_WIDTH,
                               self._path_offset_z, rect)
    if edge is None:
      return None, 0.0
    radius = float(np.clip(abs(edge[0] - centre[0]), MIN_RADIUS_PX, MAX_RADIUS_PX))
    return centre, radius

  @staticmethod
  def _draw_marker(centre: tuple[float, float], radius: float, is_lead: bool,
                   fill: rl.Color, edge_color: rl.Color) -> None:
    # A dot, not a diamond: this is a sensor blip, and a circle reads as one
    # (radar/minimap convention) at every viewing angle instead of flattening
    # to a dash edge-on past ~30m the way a shape with a fixed aspect would.
    x, y = centre
    center = rl.Vector2(x, y)
    rl.draw_circle_v(center, radius, fill)
    rl.draw_ring(center, radius - max(1.5, radius * 0.12), radius, 0, 360, 24, edge_color)
    if is_lead:
      # One concentric ring outside the dot marks which track radard settled
      # on -- a target reticle, not a second competing shape.
      outer = radius + max(3.0, radius * 0.35)
      rl.draw_ring(center, outer - max(2.0, radius * 0.12), outer, 0, 360, 32, LEAD_RING)

  def _map_to_screen(self, in_x: float, in_y: float, in_z: float,
                     rect: rl.Rectangle) -> tuple[float, float] | None:
    """Car space to screen, matching ModelRenderer._map_to_screen."""
    pt = self._car_space_transform @ np.array([in_x, in_y, in_z])
    if abs(pt[2]) < 1e-6:
      return None
    x, y = pt[0] / pt[2], pt[1] / pt[2]
    if not (rect.x - CLIP_MARGIN <= x <= rect.x + rect.width + CLIP_MARGIN
            and rect.y - CLIP_MARGIN <= y <= rect.y + rect.height + CLIP_MARGIN):
      return None
    return (x, y)
