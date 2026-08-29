"""beampilot: slow down for a corner before reaching it.

Stock openpilot holds the set speed through a curve. It caps *acceleration*
while already cornering -- `a_x_allowed = sqrt(a_total_max^2 - a_y^2)` in
longitudinal_planner -- but nothing ever asks it to brake for a bend ahead. On
a real car that is survivable: the driver is watching, the wide camera sees
round the corner, and the comfort limits keep speeds modest anyway.

Here none of those hold. The camera is a 25.70-degree narrow view with no
genuine wide companion (see the README), so the model's picture of a corner
arrives late and thin, and beampilot's raised lateral limits mean it will
happily carry a speed into a bend that it cannot then hold. The result is
running wide at the entry rather than a smooth line.

So: read the curvature the model has already predicted along its own path,
work out the fastest speed that keeps lateral acceleration at a chosen level,
and if that is slower than the current speed, ask for the deceleration that
gets there by the time the corner arrives. The answer is offered to the
planner as one more candidate; it only wins if it is the most restrictive one,
so a lead car or the cruise setpoint still take precedence when they are.

Curvature comes from the model rather than from the steering angle on purpose.
The steering angle says what the car is doing NOW, which is exactly the
information that arrives too late.
"""
import math

from openpilot.common.beampilot_env import env_bool, env_float
from openpilot.common.beampilot_limits import ACCEL_MIN, MAX_LATERAL_ACCEL_NO_ROLL

# EXPERIMENTAL, and off by default. It adds a planning layer stock openpilot
# does not have, so it wants comparing against not having it rather than being
# assumed better -- flip it and drive the same road twice. It is also the only
# beampilot feature that changes longitudinal behaviour on its own, without a
# feed from the mod, which is another reason not to have it on unasked.
CURVE_SLOWDOWN = env_bool("BEAMPILOT_CURVE_SLOWDOWN", False)

# The lateral acceleration to aim for in a corner. Deliberately BELOW the hard
# lateral limit: arriving at exactly the limit leaves clip_curvature saturated
# for the whole bend with nothing in reserve for a mid-corner correction. 0.7
# of the limit means the car reaches the corner already able to turn harder
# than it needs to.
CURVE_LATERAL_ACCEL = env_float("BEAMPILOT_CURVE_LAT_ACCEL",
                                round(0.7 * MAX_LATERAL_ACCEL_NO_ROLL, 3))
# Never slow below this for curvature alone. Tight junctions and car parks are
# full of curvature that a speed limiter would crawl through.
CURVE_MIN_SPEED = env_float("BEAMPILOT_CURVE_MIN_SPEED_MS", 5.0)
# Below this there is nothing to gain by slowing further.
CURVE_ENABLE_SPEED = env_float("BEAMPILOT_CURVE_ENABLE_SPEED_MS", 6.0)
# How far ahead to look, in seconds of the model's own plan.
CURVE_LOOKAHEAD_S = env_float("BEAMPILOT_CURVE_LOOKAHEAD_S", 6.0)
# Caps how fast the request may change, so a single jumpy curvature prediction
# cannot put a step into the throttle.
CURVE_JERK = env_float("BEAMPILOT_CURVE_JERK", 4.0)  # m/s^3
# The shortest distance, as seconds of travel, over which a speed change is
# planned. Without it, curvature at the very first path point -- a corner the
# car is already IN -- divides by a distance near zero and demands maximum
# braking every time. Being in a corner is the lateral limit's problem; this
# one is for corners still ahead.
CURVE_MIN_PLAN_S = env_float("BEAMPILOT_CURVE_MIN_PLAN_S", 1.5)

# Below this the path is straight enough that sqrt(a/k) is a meaningless number.
MIN_CURVATURE = 1e-4      # 1/m, a 10km radius
MIN_DISTANCE = 2.0        # m, keeps the required-accel division sane


def safe_speed(curvature: float, lat_accel: float = None, min_speed: float = None) -> float:
  """The fastest speed that holds lateral acceleration at `lat_accel`."""
  lat_accel = CURVE_LATERAL_ACCEL if lat_accel is None else lat_accel
  min_speed = CURVE_MIN_SPEED if min_speed is None else min_speed
  k = abs(curvature)
  if k < MIN_CURVATURE:
    return float("inf")
  return max(min_speed, math.sqrt(lat_accel / k))


def required_accel(v_ego: float, v_target: float, distance: float) -> float:
  """Constant acceleration that reaches v_target in `distance` metres."""
  return (v_target ** 2 - v_ego ** 2) / (2.0 * max(distance, MIN_DISTANCE))


def path_curvatures(velocity_x, orientation_rate_z, points: int | None = None):
  """Geometric curvature (1/m) along the model's predicted path.

  yaw rate over speed, not the steering angle: curvature is a property of the
  road, so it stays valid after we decide to arrive more slowly.

  Indexed rather than sliced or zipped. These arrive as capnp lists, which
  support len() and integer indexing but NOT slicing -- and a plain Python list
  supports all three, so a test written against lists cannot tell the
  difference. It raised TypeError on the first real frame.
  """
  if points is None:
    points = min(len(velocity_x), len(orientation_rate_z))
  return [orientation_rate_z[i] / max(velocity_x[i], 0.1) for i in range(points)]


class CurveSpeedLimiter:
  """Holds the previous request, so the output can be rate limited."""

  def __init__(self, dt: float):
    self.dt = dt
    self.prev_accel = 0.0
    # Exposed for the monitor: what speed the tightest upcoming curve allows.
    self.target_speed = float("inf")
    self.active = False

  def reset(self):
    self.prev_accel = 0.0
    self.target_speed = float("inf")
    self.active = False

  def update(self, v_ego: float, position_x, velocity_x, orientation_rate_z,
             t_idxs, accel_min: float = None) -> float | None:
    """The acceleration the upcoming curvature demands, or None if it does not.

    None means "no opinion" -- the caller should leave its own target alone,
    which is different from asking for zero.
    """
    accel_min = ACCEL_MIN if accel_min is None else accel_min
    if not CURVE_SLOWDOWN or v_ego < CURVE_ENABLE_SPEED:
      self.reset()
      return None

    points = min(len(position_x), len(velocity_x), len(orientation_rate_z), len(t_idxs))
    if points < 2:
      self.reset()
      return None

    curvatures = path_curvatures(velocity_x, orientation_rate_z, points)
    plan_floor = max(MIN_DISTANCE, v_ego * CURVE_MIN_PLAN_S)
    worst = None
    tightest_speed = float("inf")
    for i in range(points):
      if t_idxs[i] > CURVE_LOOKAHEAD_S:
        break
      v_safe = safe_speed(curvatures[i])
      tightest_speed = min(tightest_speed, v_safe)
      if v_safe >= v_ego:
        continue
      accel = required_accel(v_ego, v_safe, max(position_x[i], plan_floor))
      worst = accel if worst is None else min(worst, accel)

    self.target_speed = tightest_speed
    if worst is None:
      # Let the request decay back toward zero rather than dropping it, so
      # leaving a corner is as smooth as entering one.
      self.active = False
      if self.prev_accel >= -1e-3:
        self.prev_accel = 0.0
        return None
      self.prev_accel = min(0.0, self.prev_accel + CURVE_JERK * self.dt)
      return self.prev_accel

    worst = max(worst, accel_min)
    # Rate limit both directions: a jumpy curvature prediction must not put a
    # step into the throttle.
    worst = max(self.prev_accel - CURVE_JERK * self.dt,
                min(self.prev_accel + CURVE_JERK * self.dt, worst))
    self.prev_accel = worst
    self.active = True
    return worst
