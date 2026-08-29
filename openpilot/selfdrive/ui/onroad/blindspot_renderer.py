"""beampilot: a blind spot indicator on the onroad screen.

Stock openpilot has no blind spot display of any kind. It reads
carState.leftBlindspot / rightBlindspot -- to refuse a lane change, and to
raise "Car Detected in Blindspot" -- but only ever while a lane change is
armed. Drive along with a car sitting beside you and nothing appears at all,
which makes a working blind spot feed indistinguishable from a broken one.

This is the mirror lamp: a marker on each edge of the road view that lights
whenever that side is occupied, matching what the warning light in a real
car's door mirror does.

  * steady amber -- a vehicle is there.
  * flashing     -- and you are signalling into it, so the lane change is
                    being refused or has just been cancelled. Real BSM systems
                    flash for exactly this case, and here it is the difference
                    between "noted" and "this is why the car will not move
                    over".

Nothing is drawn when both sides are clear, so on a car with no blind spot
feed at all -- every car this fork simulates by default, since their DBCs have
no BSM messages -- this widget is invisible and costs one branch per frame.
"""
import math

import pyray as rl

from openpilot.cereal import log
from openpilot.common.beampilot_env import env_bool
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.widgets import Widget

LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection

# Amber, like a mirror-mounted warning lamp, and distinct from every colour the
# rest of the HUD uses (white speed, green engaged, blue disengaged border).
AMBER = rl.Color(255, 179, 0, 255)
# The off half of the flash. Dimmed by VALUE, not by alpha: the chevron is two
# strokes plus a disc that overlap at the tip, and a translucent colour
# composites with itself there into a brighter spot than the rest of the shape.
AMBER_DIM = rl.Color(102, 72, 0, 255)
BACKING = rl.Color(0, 0, 0, 140)

CHEVRON_COUNT = 2
# Everything below is a fraction of the road view's height, not a pixel count:
# the UI renders at 2160x1080 on BIG=1 and 536x240 on BIG=0, and SCALE moves it
# again, so a fixed size is either invisible or enormous depending on the build.
CHEVRON_HEIGHT = 0.13     # vertical span of one chevron
CHEVRON_WIDTH = 0.42      # horizontal reach, relative to its own height
CHEVRON_GAP = 0.26        # between chevrons, relative to chevron height
CHEVRON_THICKNESS = 0.13  # relative to chevron height
PLATE_PAD = 0.22          # backing plate padding, relative to chevron height
EDGE_MARGIN = 0.025       # in from the edge, as a fraction of the view WIDTH
FLASH_HZ = 2.5            # a real BSM lamp flashes at roughly this rate

# The lamps are already invisible whenever both sides are clear, so this is not
# needed to get them out of the way -- it is for anyone who wants the blind spot
# to keep gating lane changes without anything appearing on screen for it.
BSM_INDICATOR = env_bool("BEAMPILOT_BSM_INDICATOR", True)


class BlindSpotRenderer(Widget):
  def __init__(self):
    super().__init__()
    self.left_occupied = False
    self.right_occupied = False
    self.left_blocking = False
    self.right_blocking = False

  def _update_state(self) -> None:
    sm = ui_state.sm
    if not BSM_INDICATOR or not ui_state.started or sm.recv_frame["carState"] < ui_state.started_frame:
      self.left_occupied = self.right_occupied = False
      self.left_blocking = self.right_blocking = False
      return

    car_state = sm['carState']
    self.left_occupied = car_state.leftBlindspot
    self.right_occupied = car_state.rightBlindspot

    # "Blocking" means the driver has asked to go that way and cannot. In
    # preLaneChange the change is armed and being refused; in laneChangeStarting
    # it is under way and about to be cancelled (see desire_helper.py). Both are
    # worth flashing for -- the steady lamp alone does not say "this is why".
    meta = sm['modelV2'].meta
    arming = meta.laneChangeState in (LaneChangeState.preLaneChange, LaneChangeState.laneChangeStarting)
    self.left_blocking = self.left_occupied and arming and meta.laneChangeDirection == LaneChangeDirection.left
    self.right_blocking = self.right_occupied and arming and meta.laneChangeDirection == LaneChangeDirection.right

  def _render(self, rect: rl.Rectangle) -> None:
    if not (self.left_occupied or self.right_occupied):
      return

    # One phase for both sides, so they can never flash out of step.
    lit = math.sin(rl.get_time() * FLASH_HZ * 2 * math.pi) > 0
    centre_y = rect.y + rect.height / 2
    height = rect.height * CHEVRON_HEIGHT
    margin = rect.width * EDGE_MARGIN

    # The OUTERMOST tip is what sits `margin` in from the edge; the rest of the
    # group steps back toward the middle of the screen from there. Anchoring the
    # innermost chevron instead runs the group off the edge of the view.
    if self.left_occupied:
      self._draw_side(rect.x + margin, centre_y, -1, height, self.left_blocking, lit)
    if self.right_occupied:
      self._draw_side(rect.x + rect.width - margin, centre_y, 1, height, self.right_blocking, lit)

  def _draw_side(self, tip_x: float, centre_y: float, direction: int, height: float,
                 blocking: bool, lit: bool) -> None:
    """Chevrons pointing outward, away from the car, toward the occupied lane."""
    colour = AMBER_DIM if (blocking and not lit) else AMBER
    width = height * CHEVRON_WIDTH
    gap = height * CHEVRON_GAP
    pad = height * PLATE_PAD
    span = CHEVRON_COUNT * width + (CHEVRON_COUNT - 1) * gap

    # A dark plate behind them, so amber stays readable against bright sky.
    inner_x = tip_x - direction * span
    plate = rl.Rectangle(
      min(tip_x, inner_x) - pad,
      centre_y - height / 2 - pad,
      span + 2 * pad,
      height + 2 * pad,
    )
    rl.draw_rectangle_rounded(plate, 0.35, 10, BACKING)

    for i in range(CHEVRON_COUNT):
      # i = 0 is the outermost; each subsequent one steps back inward.
      base_x = tip_x - direction * (width + i * (width + gap))
      self._draw_chevron(base_x, centre_y, direction, height, width, colour)

  @staticmethod
  def _draw_chevron(base_x: float, centre_y: float, direction: int, height: float,
                    width: float, colour: rl.Color) -> None:
    """A '>' (or '<'), drawn as two thick lines meeting at the tip."""
    half = height / 2
    thickness = max(2.0, height * CHEVRON_THICKNESS)
    tip = rl.Vector2(base_x + direction * width, centre_y)
    rl.draw_line_ex(rl.Vector2(base_x, centre_y - half), tip, thickness, colour)
    rl.draw_line_ex(rl.Vector2(base_x, centre_y + half), tip, thickness, colour)
    # draw_line_ex has butt caps, so the two strokes meet at the tip with a
    # visible notch bitten out of the vertex. A disc the width of the stroke
    # fills it in.
    rl.draw_circle_v(tip, thickness / 2, colour)
