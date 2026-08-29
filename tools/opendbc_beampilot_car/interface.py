#!/usr/bin/env python3
"""CarParams for the simulator, with the Honda's assumptions taken back out.

Every value here that differs from Honda's is either (a) something a real car
imposes because of hardware beampilot does not have, or (b) something the
simulator can answer better. Nothing is loosened for its own sake: the limits
that actually shape the driving live in openpilot's planner
(BEAMPILOT_MAX_LAT_ACCEL, _MAX_LAT_JERK, _ACCEL_SCALE), not in CarParams, and
this file does not touch those.
"""
from opendbc.car import get_safety_config, structs
from opendbc.car.beampilot.carcontroller import CarController
from opendbc.car.beampilot.carstate import CarState
from opendbc.car.beampilot.radar_interface import RadarInterface
from opendbc.car.honda.values import HondaSafetyFlags
from opendbc.car.interfaces import CarInterfaceBase


class CarInterface(CarInterfaceBase):
  CarState = CarState
  CarController = CarController
  RadarInterface = RadarInterface

  # Honda reports its gear as `sport` over CAN and lists that as drivable;
  # beamngd packs the same signal, so the same gear is what arrives.
  DRIVABLE_GEARS = (structs.CarState.GearShifter.sport,)

  @staticmethod
  def get_pid_accel_limits(CP, current_speed, cruise_speed):
    # The planner's own limits, not a Bosch ECU's. beampilot_limits.py is the
    # single place acceleration is bounded, and BEAMPILOT_ACCEL_SCALE /
    # _DECEL_SCALE move it -- returning Honda's BOSCH_ACCEL_MIN/MAX here would
    # quietly clamp the range those settings just opened up.
    from openpilot.common.beampilot_limits import ACCEL_MAX, ACCEL_MIN
    return ACCEL_MIN, ACCEL_MAX

  @staticmethod
  def _get_params(ret: structs.CarParams, candidate, fingerprint, car_fw, alpha_long, is_release, docs) -> structs.CarParams:
    ret.brand = "beampilot"

    # The safety model is still Honda Bosch, because the frames still are, and
    # because beampilot's fake panda is built to answer for one. This is a
    # statement about the CAN layout, not about the car.
    ret.safetyConfigs = [get_safety_config(structs.CarParams.SafetyModel.hondaBosch)]
    ret.safetyConfigs[-1].safetyParam |= HondaSafetyFlags.RADARLESS.value

    # No radar ECU. Ground-truth radar, when enabled, reaches radard through
    # card.py rather than through a RadarInterface -- see beampilot_radar.py.
    ret.radarUnavailable = True

    # Stated rather than sniffed. Honda infers this from which gearbox message
    # is on the bus, which works but leaves the answer depending on what
    # beamngd happened to send this run; beamngd always packs GEARBOX_AUTO
    # (0x1A3), so it is automatic by construction. Left unset it comes out
    # `unknown`, which happens to take the same branch in Honda's CarState --
    # correct by accident is not the same as correct.
    ret.transmissionType = structs.CarParams.TransmissionType.automatic

    # Longitudinal. Getting this pair wrong breaks engagement outright, in a way
    # that looks like openpilot simply ignoring the cruise keys: with pcmCruise
    # True openpilot waits for the car's own ACC to report enabled before it
    # will engage; with it False openpilot owns the cruise state itself.
    #
    # openpilot always drives the longitudinal here, and unlike the Honda this
    # is NOT gated on alpha_long. On a real Bosch Honda that gate exists because
    # taking longitudinal means disabling the car's own AEB, so it should be a
    # deliberate choice; here there is no AEB to disable and no ACC ECU to hand
    # control back to. launch_beampilot.sh already sets AlphaLongitudinalEnabled
    # unconditionally, so this is what beampilot has always actually run as --
    # pinning it just removes a second, untested configuration that would
    # otherwise be one Params key away.
    #
    # It also keeps car_events.py's brand switch honest. That file has a
    # `honda` branch this platform no longer matches, and every event in it is
    # guarded by `if self.CP.pcmCruise` -- so with pcmCruise pinned False the
    # branch is a no-op for the Civic too, and skipping it changes nothing.
    # Leave pcmCruise free to become True and that stops being the case.
    ret.alphaLongitudinalAvailable = False   # nothing to opt into; it is always on
    ret.openpilotLongitudinalControl = True
    ret.pcmCruise = False
    ret.autoResumeSng = True

    # No EPS that quits at low speed, and no ACC floor. On a real car these are
    # hardware limits; here they only ever manifested as openpilot refusing to
    # steer or engage for no visible reason. CarSpecs sets them too; repeated
    # here because this is where someone will look for them.
    ret.minEnableSpeed = -1.
    ret.minSteerSpeed = 0.

    # Left as torque, which is the default and what the Civic used.
    #
    # It is tempting to call this `angle`, since beamngd converts curvature to a
    # road wheel angle itself and the torque output never reaches the car. But
    # steerControlType picks the LATERAL CONTROLLER, and LatControlAngle flags
    # saturation whenever the desired angle and the measured angle differ by
    # more than 2.5 degrees. The mod's rack is deliberately rate-limited, so it
    # lags by more than that through any real corner -- the result is a
    # permanent "Turn Exceeds Steering Limit", earned by a controller whose
    # output nothing reads. `angle` also expects the CarController to report
    # actuatorsOutput.steeringAngleDeg, which Honda's (correctly) does not.
    #
    # None of the three types actually describes what happens here, because
    # beamngd reads controlsState.desiredCurvature directly and bypasses the
    # lateral controller entirely. controlsd publishes desiredCurvature no
    # matter which is chosen, so this is free to be the one that behaves.

    # LatControlPID's gains. Nothing consumes the output -- see above -- but
    # controlsd constructs the controller from these at startup and an empty
    # breakpoint list is a crash, not a no-op. Same numbers the Civic used.
    ret.lateralTuning.pid.kpBP, ret.lateralTuning.pid.kpV = [[0, 10], [0.05, 0.5]]
    ret.lateralTuning.pid.kiBP, ret.lateralTuning.pid.kiV = [[0, 10], [0.0125, 0.125]]

    # Kept as Honda's: the input.event() steering rack in the mod is rate
    # limited to roughly a real rack's speed, so the delay openpilot should
    # expect is genuinely similar.
    ret.steerActuatorDelay = 0.1
    ret.steerLimitTimer = 0.8

    # The STEERING_CONTROL message's own torque field range -- a property of
    # the CAN layout, not a limit on the car. honda/values.py's
    # CarControllerParams reads STEER_MAX straight off it, so the CarController
    # cannot even be constructed without it. Nothing consumes the resulting
    # torque here (steerControlType is angle, and beamngd sends a position),
    # but openpilot still expects a CarController to exist.
    ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 4096], [0, 4096]]

    return ret
