import math
from enum import StrEnum, auto

from openpilot.cereal import messaging
from opendbc.car.structs import car
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.locationd.helpers import Pose
from opendbc.car import ACCELERATION_DUE_TO_GRAVITY
# beampilot: the trip points scale with what beampilot ALLOWS, not with stock
# openpilot's limits. Left at stock they are a hard ceiling on the limits rather
# than a net under them: raise BEAMPILOT_MAX_LAT_ACCEL past 6.0 and the car does
# not corner harder, it soft-disables mid-corner instead. See
# common/beampilot_limits.py. Unset environment == stock trip points.
from openpilot.common.beampilot_limits import (EXCESSIVE_ACCEL, EXCESSIVE_DECEL,
                                               EXCESSIVE_LATERAL_ACCEL)

MIN_EXCESSIVE_ACTUATION_COUNT = int(0.25 / DT_CTRL)
MIN_LATERAL_ENGAGE_BUFFER = int(1 / DT_CTRL)


class ExcessiveActuationType(StrEnum):
  LONGITUDINAL = auto()
  LATERAL = auto()


class ExcessiveActuationCheck:
  def __init__(self):
    self._excessive_counter = 0
    self._engaged_counter = 0

  def update(self, sm: messaging.SubMaster, CS: car.CarState, calibrated_pose: Pose) -> ExcessiveActuationType | None:
    # CS.aEgo can be noisy to bumps in the road, transitioning from standstill, losing traction, etc.
    # longitudinal
    accel_calibrated = calibrated_pose.acceleration.x
    excessive_long_actuation = sm['carControl'].longActive and (accel_calibrated > EXCESSIVE_ACCEL
                                                                or accel_calibrated < EXCESSIVE_DECEL)

    # lateral
    yaw_rate = calibrated_pose.angular_velocity.yaw
    roll = sm['vehicleParameters'].roll
    roll_compensated_lateral_accel = (CS.vEgo * yaw_rate) - (math.sin(roll) * ACCELERATION_DUE_TO_GRAVITY)

    # Prevent false positives after overriding
    excessive_lat_actuation = False
    self._engaged_counter = self._engaged_counter + 1 if sm['carControl'].latActive and not CS.steeringPressed else 0
    if self._engaged_counter > MIN_LATERAL_ENGAGE_BUFFER:
      if abs(roll_compensated_lateral_accel) > EXCESSIVE_LATERAL_ACCEL:
        excessive_lat_actuation = True

    # deviceMotion acceleration can be noisy due to bad mounting or aliased deviceMotion measurements
    device_motion_valid = abs(CS.aEgo - accel_calibrated) < 2
    self._excessive_counter = self._excessive_counter + 1 if device_motion_valid and (excessive_long_actuation or excessive_lat_actuation) else 0

    excessive_type = None
    if self._excessive_counter > MIN_EXCESSIVE_ACTUATION_COUNT:
      if excessive_long_actuation:
        excessive_type = ExcessiveActuationType.LONGITUDINAL
      else:
        excessive_type = ExcessiveActuationType.LATERAL

    return excessive_type
