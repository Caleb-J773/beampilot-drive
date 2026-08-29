from openpilot.cereal import log
from openpilot.common.beampilot_env import env_bool, env_float
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL

LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection

LANE_CHANGE_SPEED_MIN = 20 * CV.MPH_TO_MS
LANE_CHANGE_TIME_MAX = 10.
LANE_CHANGE_START_TIME = 0.5

# beampilot: commit a signalled lane change without waiting for the driver to
# nudge the steering wheel.
#
# Stock openpilot requires torque_applied -- CS.steeringPressed plus a
# steeringTorque in the blinker's direction -- before leaving preLaneChange.
# There is no physical wheel here to nudge, and beamngd reports
# user_torque = 0.0 always (BeamNG's steering reading while engaged is our own
# input.event command echoed back, not a driver input, so it cannot be used to
# detect an override). steeringPressed is therefore always False and a lane
# change would arm on the blinker and then sit in preLaneChange forever.
#
# With this set, the blinker alone commits the change after a short delay.
# jackz314's ETS2/ATS bridge does the same thing for the same reason.
# Unset/0 keeps stock nudge-required behavior.
AUTO_LANE_CHANGE = env_bool("BEAMPILOT_AUTO_LANE_CHANGE", False)
# Grace period between the blinker going on and the car starting to move over,
# so a signal tapped by accident (or to cancel) doesn't immediately commit.
AUTO_LANE_CHANGE_DELAY = 1.0  # seconds

# beampilot: blind spot monitoring can also CANCEL a lane change already under
# way, not just refuse to start one.
#
# Stock openpilot checks the blind spot exactly once, on the way out of
# preLaneChange. After that the manoeuvre is committed and nothing looks again
# -- on a real car that is defensible, because the driver had to nudge the
# wheel to commit and is watching the mirror light the whole way across. Here
# there is no driver: with AUTO_LANE_CHANGE above, the change commits on the
# blinker alone, so nothing at all is watching the target lane once it starts.
# A car that moves into that lane mid-manoeuvre gets driven into.
#
# Unlike AUTO_LANE_CHANGE this defaults ON, because it cannot change stock
# behaviour: it fires only on carState.leftBlindspot/rightBlindspot, and the
# simulated Honda's CAN has no BSM messages at all -- those fields are non-zero
# here only because beamngd feeds them in over a side channel (see
# openpilot/common/beampilot_bsm.py). With no feed they are always False and
# every branch below is dead code.
LANE_CHANGE_ABORT = env_bool("BEAMPILOT_LANE_CHANGE_ABORT", True)
# How late into the manoeuvre an abort may still fire. Early on, steering back
# is unambiguously right. Past roughly the halfway point the car is mostly in
# the new lane already, the vehicle now in the blind spot is behind it rather
# than beside it, and swerving back across is its own hazard -- so beyond this
# it commits and finishes. Set it far above LANE_CHANGE_TIME_MAX to allow an
# abort at any point in the move.
LANE_CHANGE_ABORT_MAX_TIME = env_float("BEAMPILOT_LANE_CHANGE_ABORT_S", 2.0)

class DesireHelper:
  def __init__(self):
    self.lane_change_state = LaneChangeState.off
    self.lane_change_direction = LaneChangeDirection.none
    self.lane_change_timer = 0.0
    self.auto_lane_change_timer = 0.0
    self.prev_one_blinker = False
    self.desire = log.Desire.none

  @staticmethod
  def get_lane_change_direction(CS):
    return LaneChangeDirection.left if CS.leftBlinker else LaneChangeDirection.right

  @staticmethod
  def blindspot_occupied(carstate, direction):
    """Is the lane we are moving into occupied, on that side only?"""
    return ((carstate.leftBlindspot and direction == LaneChangeDirection.left) or
            (carstate.rightBlindspot and direction == LaneChangeDirection.right))

  def update(self, carstate, lateral_active, lane_change_prob):
    v_ego = carstate.vEgo
    one_blinker = carstate.leftBlinker != carstate.rightBlinker
    below_lane_change_speed = v_ego < LANE_CHANGE_SPEED_MIN

    # Keep the auto-commit delay scoped to one continuous stay in preLaneChange:
    # zero it in every other state so each fresh arm waits the full delay, and
    # so returning to preLaneChange for a second consecutive lane change doesn't
    # instantly re-commit with a stale timer.
    if self.lane_change_state != LaneChangeState.preLaneChange:
      self.auto_lane_change_timer = 0.0

    if not lateral_active or self.lane_change_timer > LANE_CHANGE_TIME_MAX:
      self.lane_change_state = LaneChangeState.off
      self.lane_change_direction = LaneChangeDirection.none
      self.lane_change_timer = 0.0
    else:
      if self.lane_change_state == LaneChangeState.off and one_blinker and not self.prev_one_blinker and not below_lane_change_speed:
        self.lane_change_state = LaneChangeState.preLaneChange
        self.lane_change_timer = 0.0
        # Initialize lane change direction to prevent UI alert flicker
        self.lane_change_direction = self.get_lane_change_direction(carstate)

      elif self.lane_change_state == LaneChangeState.preLaneChange:
        # Update lane change direction
        self.lane_change_direction = self.get_lane_change_direction(carstate)

        torque_applied = carstate.steeringPressed and \
                         ((carstate.steeringTorque > 0 and self.lane_change_direction == LaneChangeDirection.left) or
                          (carstate.steeringTorque < 0 and self.lane_change_direction == LaneChangeDirection.right))

        # Tracked separately from lane_change_timer on purpose: that one gates
        # LANE_CHANGE_TIME_MAX, and incrementing it here would make a blinker
        # left on for >10s cancel the arm -- a stock behavior change even with
        # auto lane change disabled.
        self.auto_lane_change_timer += DT_MDL
        if AUTO_LANE_CHANGE and self.auto_lane_change_timer > AUTO_LANE_CHANGE_DELAY:
          torque_applied = True

        blindspot_detected = self.blindspot_occupied(carstate, self.lane_change_direction)

        if not one_blinker or below_lane_change_speed:
          self.lane_change_state = LaneChangeState.off
          self.lane_change_direction = LaneChangeDirection.none
          self.lane_change_timer = 0.0
        elif torque_applied and not blindspot_detected:
          self.lane_change_state = LaneChangeState.laneChangeStarting
          self.lane_change_timer = 0.0

      elif self.lane_change_state == LaneChangeState.laneChangeStarting:
        self.lane_change_timer += DT_MDL

        # beampilot: the target lane filled up after we committed. Drop the
        # desire, which steers back to the lane we came from.
        #
        # Back to preLaneChange rather than straight to off, on purpose: the
        # blinker is still on, so once the lane clears the change re-commits by
        # itself after the usual delay -- "wait for the gap" rather than "give
        # up and forget about it". It also puts the state back where
        # selfdrived.py raises laneChangeBlocked, so "Car Detected in
        # Blindspot" is on screen for as long as the reason holds. Zeroing the
        # timer matches what the normal exit from this state does two branches
        # down, and re-arming still waits out AUTO_LANE_CHANGE_DELAY because
        # auto_lane_change_timer was zeroed at the top of every tick spent here.
        if LANE_CHANGE_ABORT and self.lane_change_timer <= LANE_CHANGE_ABORT_MAX_TIME \
           and self.blindspot_occupied(carstate, self.lane_change_direction):
          self.lane_change_state = LaneChangeState.preLaneChange
          self.lane_change_timer = 0.0

        elif lane_change_prob < 0.02 and self.lane_change_timer >= LANE_CHANGE_START_TIME:
          self.lane_change_timer = 0.0
          if one_blinker:
            self.lane_change_state = LaneChangeState.preLaneChange
            self.lane_change_direction = self.get_lane_change_direction(carstate)
          else:
            self.lane_change_state = LaneChangeState.off
            self.lane_change_direction = LaneChangeDirection.none

    self.prev_one_blinker = one_blinker and lateral_active

    self.desire = log.Desire.none
    if self.lane_change_state == LaneChangeState.laneChangeStarting:
      if self.lane_change_direction == LaneChangeDirection.left:
        self.desire = log.Desire.laneChangeLeft
      elif self.lane_change_direction == LaneChangeDirection.right:
        self.desire = log.Desire.laneChangeRight
