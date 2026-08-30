"""How hard beampilot is allowed to drive the car, in one place.

Stock openpilot's limits are tuned for a real car carrying real passengers to
EU/ISO comfort guidelines. In a simulator they are frequently the reason it
cannot follow a road at all. beampilot scales them -- but the scaling was
applied in some places and not others, and the places that missed it are not
obvious:

  * `long_mpc.py` bounds its own solution with opendbc's UNSCALED ACCEL_MAX.
    The planner then takes min(mpc_target, cruise_target), so the MPC's 2.0
    m/s^2 ceiling won whenever it was the lower of the two -- which is most of
    the time. BEAMPILOT_ACCEL_SCALE therefore did much less than it looked
    like it did.
  * `_A_TOTAL_MAX_V` is the combined lateral+longitudinal envelope, and
    `a_x_allowed = sqrt(a_total_max^2 - a_y^2)`. Raise the lateral limit
    without raising this and the car cannot accelerate THROUGH a corner: at
    20 m/s the envelope is 1.7, so 2 m/s^2 of cornering leaves nothing at all.
  * `ExcessiveActuationCheck` measures what the car ACHIEVES and soft-disables
    above hardcoded stock ceilings -- 2x ISO lateral, 2x stock accel. Raising
    the command limits past those does not make the car corner harder; it
    makes openpilot hand back control mid-corner. With the shipped config
    (ACCEL_SCALE=2.0) the commanded ceiling was already exactly the trip
    point.

Every default here is the stock upstream value, so an unset environment still
behaves as unmodified openpilot.
"""
from opendbc.car.interfaces import ACCEL_MAX as STOCK_ACCEL_MAX
from opendbc.car.interfaces import ACCEL_MIN as STOCK_ACCEL_MIN
from opendbc.car.interfaces import MAX_CTRL_SPEED as STOCK_MAX_CTRL_SPEED
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.lateral import ISO_LATERAL_ACCEL

from openpilot.common.beampilot_env import env_float

# --- longitudinal ---------------------------------------------------------
ACCEL_SCALE = env_float("BEAMPILOT_ACCEL_SCALE", 1.0)
DECEL_SCALE = env_float("BEAMPILOT_DECEL_SCALE", 1.0)
ACCEL_MAX = STOCK_ACCEL_MAX * ACCEL_SCALE
ACCEL_MIN = STOCK_ACCEL_MIN * DECEL_SCALE

# get_safe_obstacle_distance() in long_mpc.py is v^2/(2*COMFORT_BRAKE) +
# t_follow*v + STOP_DISTANCE -- the t_follow*v term is what makes the braking
# point move further out as speed rises, and it stacks with LongitudinalPersonality
# (get_T_FOLLOW: aggressive 1.25s, standard 1.45s, relaxed 1.75s). Once
# personality is already at aggressive, this scales the gap further; 1.0 is
# stock aggressive, so an unset environment changes nothing beyond personality.
T_FOLLOW_SCALE = env_float("BEAMPILOT_T_FOLLOW_SCALE", 1.0)

# car_events.py raises speedTooHigh ("Slow down to engage") and disengages
# above MAX_CTRL_SPEED -- that ceiling, not minEnableSpeed, is what actually
# caps how fast openpilot can be engaged. V_CRUISE_MAX (selfdrive/car/cruise.py,
# the max SET speed) has to move with it: opendbc keeps MAX_CTRL_SPEED a fixed
# 4 kph above V_CRUISE_MAX so engaging right at the ceiling doesn't instantly
# clip the set speed down and demand a slowdown. Reversing that same formula
# here (instead of importing cruise.py, which would import this module back)
# keeps both derived from the one scale.
MAX_ENGAGE_SPEED_SCALE = env_float("BEAMPILOT_MAX_ENGAGE_SPEED_SCALE", 1.0)
MAX_CTRL_SPEED = STOCK_MAX_CTRL_SPEED * MAX_ENGAGE_SPEED_SCALE
V_CRUISE_MAX_KPH = MAX_CTRL_SPEED / CV.KPH_TO_MS - 4

# --- lateral --------------------------------------------------------------
# The binding one on whether a corner can be taken at all: clip_curvature caps
# curvature at MAX_LATERAL_ACCEL / v^2, so stock 3.0 allows only a ~300m radius
# at 67mph. Stock equals ISO_LATERAL_ACCEL by definition.
MAX_LATERAL_ACCEL_NO_ROLL = env_float("BEAMPILOT_MAX_LAT_ACCEL", ISO_LATERAL_ACCEL)
MAX_LATERAL_JERK = env_float("BEAMPILOT_MAX_LAT_JERK", 5.0)   # m/s^3, how fast curvature may change
MAX_CURVATURE = env_float("BEAMPILOT_MAX_CURVATURE", 0.2)     # 1/m, only binds below ~11mph

# --- the combined envelope ------------------------------------------------
# Grip is shared between turning and accelerating, so the envelope has to grow
# with whichever of the two was raised further, or the other one silently
# consumes it.
A_TOTAL_MAX_SCALE = max(ACCEL_SCALE, MAX_LATERAL_ACCEL_NO_ROLL / ISO_LATERAL_ACCEL)

# --- the safety net -------------------------------------------------------
# ExcessiveActuationCheck exists to catch actuation running away, and it should
# keep doing that. Stock sets the trip at 2x whatever it allows; keeping the
# same MULTIPLE of what beampilot allows preserves the net's purpose instead of
# turning it into a cap on the limits above. Lower this to tighten it.
ACTUATION_MARGIN = env_float("BEAMPILOT_ACTUATION_MARGIN", 2.0)
EXCESSIVE_LATERAL_ACCEL = max(ISO_LATERAL_ACCEL, MAX_LATERAL_ACCEL_NO_ROLL) * ACTUATION_MARGIN
EXCESSIVE_ACCEL = max(STOCK_ACCEL_MAX, ACCEL_MAX) * ACTUATION_MARGIN
EXCESSIVE_DECEL = min(STOCK_ACCEL_MIN, ACCEL_MIN) * ACTUATION_MARGIN


def summary() -> str:
  """One line for the launch log, so the active envelope is never a mystery."""
  return (f"[beampilot] limits: lateral {MAX_LATERAL_ACCEL_NO_ROLL:.1f} m/s^2"
          + f" (jerk {MAX_LATERAL_JERK:.1f}), accel {ACCEL_MAX:+.1f} / {ACCEL_MIN:+.1f} m/s^2,"
          + f" combined envelope x{A_TOTAL_MAX_SCALE:.2f};"
          + f" excessive-actuation trips above {EXCESSIVE_LATERAL_ACCEL:.1f} lateral,"
          + f" {EXCESSIVE_ACCEL:+.1f} / {EXCESSIVE_DECEL:+.1f} longitudinal;"
          + f" engage/set-speed ceiling {V_CRUISE_MAX_KPH:.0f} km/h")


if __name__ == "__main__":
  print(summary())
