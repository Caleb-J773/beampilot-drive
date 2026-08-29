#!/usr/bin/env python3
import json
import math
import os
import socket
import struct
import time
import traceback
from dataclasses import dataclass

import evdev
from evdev import ecodes

from opendbc.car.honda.values import CruiseButtons
from opendbc.car.interfaces import ACCEL_MAX, ACCEL_MIN
from openpilot.common.beampilot_env import env_float, env_int, env_str
from openpilot.common.constants import CV
from opendbc.car.structs import car
from opendbc.car.vehicle_model import VehicleModel
from openpilot.cereal import messaging
from openpilot.common.params import Params
from openpilot.common.realtime import DT_DMON, Ratekeeper
from openpilot.tools.sim.lib.common import SimulatorState, vec3
from openpilot.tools.sim.lib.simulated_car import SimulatedCar
from openpilot.tools.sim.lib.simulated_sensors import SimulatedSensors
from openpilot.selfdrive.beamngd.virtual_joystick import BeamNGVirtualJoystick

# Talks to the beampilot_bridge BeamNG.drive mod (tools/beamng_mod/beampilot_bridge)
# over two plain UDP sockets:
#   TELEMETRY_PORT: the mod sends real vehicle telemetry here, ~100Hz.
#   CONTROL_PORT:   we send openpilot's desired steering/throttle/brake here; the
#                   mod applies it via BeamNG's input.event() when engaged.
# CAN/IMU/GPS/driver-monitoring publishing is delegated to the same generic
# SimulatorState/SimulatedCar/SimulatedSensors pipeline the MetaDrive bridge
# (openpilot/tools/sim/bridge/metadrive) already uses, instead of hand-rolling it.

BEAMNG_ADDRESS = env_str("BEAMPILOT_BEAMNG_ADDRESS", "127.0.0.1")
TELEMETRY_PORT = env_int("BEAMPILOT_TELEMETRY_PORT", 49152)
CONTROL_PORT = env_int("BEAMPILOT_CONTROL_PORT", 49153)
# NOTE: the Lua mod has its own copies of these ports. If you change them here,
# change TELEMETRY_PORT/CONTROL_PORT in
# tools/beamng_mod/beampilot_bridge/lua/vehicle/protocols/beampilot.lua too --
# vehicle Lua has no access to this process's environment.

# "lua" (default): send steering/throttle/brake to the beampilot_bridge mod's
# control socket, applied via input.event() straight into vehicle Lua -- no
# in-game setup needed.
# "joystick": emit through a uinput virtual wheel instead; BeamNG.drive must be
# told once (Options > Controls) which of its axes are steering/throttle/
# brake, same as setting up a real USB wheel. See virtual_joystick.py.
CONTROL_MODE = env_str("BEAMPILOT_CONTROL_MODE", "lua")
if CONTROL_MODE not in ("lua", "joystick"):
  raise ValueError(f"BEAMPILOT_CONTROL_MODE must be 'lua' or 'joystick', got {CONTROL_MODE!r}")

# CS.steeringAngleDeg (the feedback signal fed into opendbc's calc_curvature())
# is documented there as "Steering wheel angle [rad]" -- the actual hand-wheel
# rotation, NOT the front wheel/tire angle. A plain angle = curvature *
# wheelbase * steerRatio formula (what this used to be) ignores the
# speed-dependent understeer compensation calc_curvature's own inverse
# (VehicleModel.get_steer_from_curvature, used in send_control below)
# includes -- real tires need MORE wheel angle for the same curvature the
# faster you go, so a single flat formula/gain works at one speed and not
# another. That mismatch, not a broken actuation mechanism, was the actual
# cause of the growing-oscillation/wouldn't-track symptoms seen in testing
# (confirmed separately: a raw, hardcoded steering=-0.8 test command,
# bypassing openpilot entirely, turned the car hard enough to hit a wall).
# Measured directly, not estimated: holding full-left steering_input=-1.0 read
# back steering_wheel_deg=+510.00 (see electrics.values.steering, this car's
# actual v.data.input.steeringWheelLock -- not necessarily the same for every
# BeamNG vehicle, but this is the one actually spawned during testing).
# Per-vehicle: measure yours by holding full lock and reading steering_wheel_deg
# in tools/beampilot_monitor.py, then set BEAMPILOT_STEER_LOCK_DEG to match.
# Too low makes openpilot oversteer, too high makes it run wide.
MAX_STEERING_WHEEL_ANGLE_DEG = env_float("BEAMPILOT_STEER_LOCK_DEG", 510.0)

BEAMNGD_TICK_HZ = env_float("BEAMPILOT_TICK_HZ", 100.0)
# actuators.torque has to become a steering *position* for BeamNG's
# input.event, and snapping straight to it (position = -torque) is too
# instant/twitchy -- but naively integrating torque as a *velocity*
# (position += torque * gain * dt, forever) is worse: that has no equilibrium
# at all. ANY sustained nonzero torque -- even a small, legitimate correction
# -- just keeps marching the position toward full lock forever, since nothing
# pulls it back. Stacked on top of openpilot's own PID (which already
# integrates error into torque), that's two integrators in series: a classic
# growing-oscillation setup, and exactly what "oscillates worse and worse,
# eventually runs off the road" looks like.
# Instead: treat torque directly as a TARGET position (it's already -1..1,
# same range as position) and rate-limit the approach to that target. Smooth,
# not instant, but with a real bounded equilibrium -- steady torque settles at
# a steady position and stays there, instead of growing without bound.
# Seconds for a full lock-to-lock sweep. Lower = snappier but twitchier;
# raise it if steering feels jittery, lower it if it responds too slowly.
STEER_FULL_SWEEP_SECONDS = env_float("BEAMPILOT_STEER_SWEEP_SECONDS", 0.15)
STEER_POSITION_RATE_LIMIT_PER_TICK = 2.0 / (STEER_FULL_SWEEP_SECONDS * BEAMNGD_TICK_HZ)

TELEMETRY_MAGIC = b"BPL1"
TELEMETRY_STRUCT = struct.Struct("<4sffffffiI" + "f" * 15)

DL_LEFT_BLINKER  = 1
DL_RIGHT_BLINKER = 2
DL_PARKING_BRAKE = 4
DL_IGNITION_ON   = 8

# Cruise-control keys, as single letters. Defaults are i/o/u -- NOT the more
# obvious c/v/b, which collide with BeamNG.drive's own bindings (c cycles the
# camera). If you rebind these, check settings/inputmaps/keyboard.json in the
# BeamNG install for conflicts first: a key bound on both sides will do both.
def _key_from_letter(letter: str, fallback: int) -> int:
  code = getattr(ecodes, f"KEY_{letter.strip().upper()[:1]}", None)
  return code if isinstance(code, int) else fallback


KEY_SET = _key_from_letter(env_str("BEAMPILOT_KEY_SET", "i"), ecodes.KEY_I)
KEY_RESUME = _key_from_letter(env_str("BEAMPILOT_KEY_RESUME", "o"), ecodes.KEY_O)
KEY_CANCEL = _key_from_letter(env_str("BEAMPILOT_KEY_CANCEL", "u"), ecodes.KEY_U)
CRUISE_KEYS = (KEY_SET, KEY_RESUME, KEY_CANCEL)  # DECEL_SET, RES_ACCEL, CANCEL

# Per-tap speed nudge. 1 mph matches a real Honda's SET/RES behavior.
CRUISE_SPEED_STEP = env_float("BEAMPILOT_CRUISE_STEP_MPH", 1.0) * CV.MPH_TO_MS
GPS_RATE_HZ = 10.0  # SERVICE_LIST['gpsLocationExternal'].frequency

# The acceleration range the longitudinal planner may actually command, after
# BEAMPILOT_ACCEL_SCALE/DECEL_SCALE. Kept in sync with longitudinal_planner.py's
# own scaling (imported from opendbc there too) rather than importing that
# module directly, which would drag the whole longitudinal MPC into beamngd.
ACCEL_MAX_SCALED = ACCEL_MAX * env_float("BEAMPILOT_ACCEL_SCALE", 1.0)
ACCEL_MIN_SCALED = ACCEL_MIN * env_float("BEAMPILOT_DECEL_SCALE", 1.0)
# c/v/b collide with BeamNG.drive's own default bindings (c = cycle camera,
# among others) -- checked settings/inputmaps/keyboard.json for genuinely
# unbound plain keys before picking i/o/u.


@dataclass
class Telemetry:
  speed: float
  steering_input: float
  steering_wheel_deg: float
  throttle: float
  brake: float
  clutch: float
  gear: int
  dash_lights: int
  pos: vec3
  vel: vec3
  acc: vec3
  roll_pos: float
  pitch_pos: float
  yaw_pos: float
  roll_vel: float
  pitch_vel: float
  yaw_vel: float


def parse_telemetry(data: bytes) -> Telemetry | None:
  if len(data) != TELEMETRY_STRUCT.size:
    return None
  fields = TELEMETRY_STRUCT.unpack(data)
  if fields[0] != TELEMETRY_MAGIC:
    return None
  (_, speed, steering_input, steering_wheel_deg, throttle, brake, clutch, gear, dash_lights,
   pos_x, pos_y, pos_z, vel_x, vel_y, vel_z, acc_x, acc_y, acc_z,
   roll_pos, pitch_pos, yaw_pos, roll_vel, pitch_vel, yaw_vel) = fields
  return Telemetry(
    speed=speed, steering_input=steering_input, steering_wheel_deg=steering_wheel_deg,
    throttle=throttle, brake=brake, clutch=clutch,
    gear=gear, dash_lights=dash_lights,
    pos=vec3(pos_x, pos_y, pos_z), vel=vec3(vel_x, vel_y, vel_z), acc=vec3(acc_x, acc_y, acc_z),
    roll_pos=roll_pos, pitch_pos=pitch_pos, yaw_pos=yaw_pos,
    roll_vel=roll_vel, pitch_vel=pitch_vel, yaw_vel=yaw_vel,
  )


def find_keyboard_devices() -> list[evdev.InputDevice]:
  """Physical keyboards only, matched by capability (KEY_I/O/U), not by name.
  Reading here is passive (no .grab()) so BeamNG's own keyboard input is unaffected.
  Opened non-blocking so read_cruise_button can drain the kernel's buffered event
  queue every tick instead of sampling instantaneous key state -- point-sampling
  (active_keys()) can miss a press entirely if it's shorter than the gap between
  two ~10ms ticks, which gets worse under the load this whole pipeline puts on
  the system. Reading real KEYDOWN events instead means nothing is ever missed,
  regardless of tick timing, since the kernel buffers events until read."""
  devices = []
  for path in evdev.list_devices():
    try:
      dev = evdev.InputDevice(path)
    except OSError:
      continue
    keys = dev.capabilities().get(ecodes.EV_KEY, [])
    if all(k in keys for k in CRUISE_KEYS):
      os.set_blocking(dev.fd, False)
      devices.append(dev)
  return devices


def read_cruise_button(devices: list[evdev.InputDevice]) -> int:
  result = 0
  for dev in devices:
    try:
      for ev in dev.read():
        if ev.type == ecodes.EV_KEY and ev.value == 1:  # key down (0=up, 1=down, 2=repeat)
          if ev.code == KEY_SET:
            result = CruiseButtons.DECEL_SET
          elif ev.code == KEY_RESUME:
            result = CruiseButtons.RES_ACCEL
          elif ev.code == KEY_CANCEL:
            result = CruiseButtons.CANCEL
    except (BlockingIOError, OSError):
      continue
  return result


class BeamNGBridge:
  def __init__(self):
    self.telemetry_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.telemetry_sock.setblocking(False)
    self.telemetry_sock.bind((BEAMNG_ADDRESS, TELEMETRY_PORT))

    self.control_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    self.joystick = BeamNGVirtualJoystick() if CONTROL_MODE == "joystick" else None
    if self.joystick is not None:
      print("[beamngd] control mode: joystick -- bind 'BeamPilot Virtual Wheel' axes in BeamNG.drive's Options > Controls before engaging", flush=True)

    self.keyboard_devices = find_keyboard_devices()
    if not self.keyboard_devices:
      print("[beamngd] no keyboard device found for cruise control buttons (c/v/b); cruise button input will be unavailable")

    self.sim_car = SimulatedCar()
    self.sim_sensors = SimulatedSensors(create_camera=False)  # beamcamd.py owns the camera pipeline
    self.state = SimulatorState()

    # HONDA_CIVIC_2022 (non-alpha-long) has pcmCruise=True: opendbc's honda
    # carstate.py sets cruiseState.enabled straight from the ACC_STATUS CAN
    # signal, and openpilot only decides to actually engage *after* seeing
    # that go true -- it expects the car's own ACC to turn on by itself,
    # exactly like a real Honda's cruise button does independent of whatever
    # software is watching. So ACC_STATUS (state.is_engaged, sent via
    # SimulatedCar) has to be driven directly from the cruise button here, NOT
    # from selfdriveState.active -- otherwise nothing is ever the first thing
    # to flip cruiseState.enabled true, and openpilot can never engage no
    # matter how many times the button is pressed.
    self.acc_engaged = False

    # cruiseState.speed (see SimulatedCar.send_can_messages's CRUISE_SPEED_PCM)
    # is what openpilot trusts as the target speed for this pcmCruise=True
    # config -- a real Honda PCM computes this itself in response to SET/RES
    # button presses; since we're simulating the PCM, we have to track it too.
    # Without this it silently stays at 0 forever and openpilot just holds/brakes.
    self.cruise_speed = 0.0

    # actuators.torque is an *effort* command; input.event("steering", x,
    # FILTER_DIRECT) instead sets the steering *position* directly, with no
    # smoothing. Rate-limiting the approach to torque-as-target-position (see
    # send_control) approximates a steering rack's response speed while still
    # giving a real, bounded equilibrium for any given torque -- unlike a pure
    # velocity integrator, which has no equilibrium and just grows forever.
    # Kept in BeamNG's own steering_input sign convention throughout
    # (confirmed empirically: left == -1.0).
    self.steering_position = 0.0

    # Mirrors the Lua mod's own isControlling gating: only push a neutral
    # reset on the disengage EDGE, never on every not-engaged tick -- emitting
    # steer=0/throttle=0/brake=0 every tick while disengaged would otherwise
    # fight (and win against) the player's own bound wheel/keyboard input in
    # BeamNG's own controller layer, same bug already fixed once on the Lua
    # side.
    self.joystick_controlling = False

    self.sm = messaging.SubMaster(['carControl', 'controlsState', 'selfdriveState'])

    # Built lazily once CarParams is available. Read from the persistent
    # Params key-value store (written by card.py with block=True, i.e.
    # synchronously committed) rather than a cereal pub-sub subscription --
    # carParams is normally published once, right after fingerprinting, and a
    # plain SubMaster subscription can miss it entirely if beamngd's socket
    # wasn't fully connected yet at that exact moment (a classic pub-sub
    # "slow joiner" race, and a very plausible explanation for the steering
    # command staying permanently at zero: self.vehicle_model would just
    # never get built). Polling the Params store instead has no such race --
    # it's a plain key-value read that works regardless of subscription timing.
    # get_steer_from_curvature() is the actual, physically-correct inverse of
    # calc_curvature(), including the speed-dependent understeer compensation
    # (curvature_factor) the earlier fixed-formula + fudge-factor gain was
    # missing entirely -- likely why a single flat gain worked at one speed
    # and not another.
    self.params = Params()
    self.vehicle_model: VehicleModel | None = None

    self.last_dmon_update = 0.0
    self.last_perp_update = 0.0
    self.last_gps_update = 0.0

  def read_latest_telemetry(self) -> Telemetry | None:
    latest = None
    while True:
      try:
        data, _ = self.telemetry_sock.recvfrom(4096)
      except BlockingIOError:
        break
      latest = data
    if latest is None:
      return None
    return parse_telemetry(latest)

  def update_state_from_telemetry(self, telemetry: Telemetry):
    self.state.valid = True
    self.state.ignition = bool(telemetry.dash_lights & DL_IGNITION_ON)

    self.state.velocity = telemetry.vel
    bearing_deg = math.degrees(telemetry.yaw_pos) % 360.0
    self.state.bearing = bearing_deg
    self.state.gps.from_xy((telemetry.pos.x, telemetry.pos.y))

    self.state.imu.accelerometer = telemetry.acc
    self.state.imu.gyroscope = vec3(telemetry.roll_vel, telemetry.pitch_vel, telemetry.yaw_vel)
    self.state.imu.bearing = bearing_deg

    # steering_wheel_deg (electrics.values.steering) is the REAL post-dynamics
    # wheel angle, already in true degrees for this car -- unlike
    # steering_input (an ~instant echo of the last commanded input, not a
    # genuine measurement) times an assumed MAX_STEERING_WHEEL_ANGLE_DEG, which
    # fed the PID its own recent output back as if it were an independent
    # measurement. BeamNG's own formula for `steering` already includes a
    # negation (-toInputSpace(...)), so its sign convention needs its own
    # empirical check -- do not assume it matches steering_input's.
    self.state.steering_angle = telemetry.steering_wheel_deg
    # electrics.values.throttle/brake report the vehicle's CURRENT pedal state,
    # which while engaged is our OWN input.event command echoed back -- not the
    # driver. Reporting it as user_gas/user_brake creates a feedback loop that
    # disengages openpilot the instant it tries to accelerate: throttle command
    # -> BeamNG applies it -> telemetry echoes it -> PEDAL_GAS -> CS.gasPressed
    # -> selfdrived's rising-edge pedalPressed check (selfdrived.py:250) fires
    # EventName.pedalPressed -> disengage. Same for brake.
    # While we're driving there is no separate user pedal input to report
    # anyway: pollControl() calls input.event(FILTER_DIRECT) every tick, which
    # overwrites whatever the player's own keys/pedals set.
    if self.acc_engaged:
      self.state.user_gas = 0.0
      self.state.user_brake = 0.0
    else:
      self.state.user_gas = telemetry.throttle
      self.state.user_brake = telemetry.brake
    self.state.user_torque = 0.0

    if not self.acc_engaged:
      # keep the integrator synced to the real (player-controlled) wheel
      # position while not driving, so re-engaging doesn't snap/jump from
      # some stale, unrelated position
      self.steering_position = telemetry.steering_input

    self.state.left_blinker = bool(telemetry.dash_lights & DL_LEFT_BLINKER)
    self.state.right_blinker = bool(telemetry.dash_lights & DL_RIGHT_BLINKER)

    cruise_button = read_cruise_button(self.keyboard_devices)
    self.state.cruise_button = cruise_button
    # Mirrors a real Honda PCM's SET/RES behavior: SET grabs the current speed
    # (first press) or nudges down a notch (already engaged); RES resumes the
    # last remembered speed (first press after a cancel) or nudges up a notch.
    if cruise_button == CruiseButtons.DECEL_SET:
      self.cruise_speed = max(0.0, self.cruise_speed - CRUISE_SPEED_STEP) if self.acc_engaged else telemetry.speed
      self.acc_engaged = True
    elif cruise_button == CruiseButtons.RES_ACCEL:
      self.cruise_speed = self.cruise_speed + CRUISE_SPEED_STEP if self.acc_engaged else max(self.cruise_speed, telemetry.speed)
      self.acc_engaged = True
    elif cruise_button == CruiseButtons.CANCEL:
      self.acc_engaged = False
    self.state.is_engaged = self.acc_engaged
    self.state.cruise_speed = self.cruise_speed

  def send_sensors(self):
    # Mirrors SimulatedSensors.update(), minus send_camera_images (beamcamd.py
    # owns the camera pipeline separately in this bridge).
    now = time.monotonic()
    # count=1: this loop already ticks at 100Hz, so the helpers' default
    # multi-sample bursts (5x IMU, 10x GPS -- sized for a ~20Hz caller) would
    # publish at 500Hz and 1000Hz respectively. Measured directly before
    # fixing: accelerometer/gyroscope 500Hz vs 104Hz expected, and
    # gpsLocationExternal 1000Hz vs 10Hz expected (100x over).
    self.sim_sensors.send_imu_message(self.state, count=1)

    if (now - self.last_gps_update) > 1.0 / GPS_RATE_HZ:
      self.sim_sensors.send_gps_message(self.state, count=1)
      self.last_gps_update = now

    if (now - self.last_dmon_update) > DT_DMON / 2:
      self.sim_sensors.send_fake_driver_monitoring()
      self.last_dmon_update = now

    if (now - self.last_perp_update) > 0.25:
      self.sim_sensors.send_peripheral_state()
      self.last_perp_update = now

  def send_control(self):
    actuators = self.sm['carControl'].actuators
    # actuators.torque (and carOutput.actuatorsOutput.torque, the version rate-
    # limited by honda/carcontroller.py the way a real car's safety firmware
    # would) is a closed-loop PID output: it's only as good as torqued's
    # latAccelFactor/latAccelOffset/friction calibration actually matching how
    # THIS vehicle responds to torque -- calibration learned for a real Civic,
    # applied to whatever physically-different vehicle BeamNG spawns. That
    # mismatch is a plausible source of persistent oscillation no amount of
    # rate-limiting fixes, since the controller and the plant disagree about
    # the car's response curve.
    # controlsState.desiredCurvature sidesteps this: it's model_v2's desired
    # path converted to curvature (1/m), a purely geometric target with no
    # torque-response calibration involved at all. This is the same shortcut
    # jackz314/openpilot's truck_sim bridge takes (their old, now-deprecated
    # controlsState.steeringAngleDesiredDeg).
    desired_curvature = self.sm['controlsState'].desiredCurvature

    if self.vehicle_model is None:
      cp_bytes = self.params.get("CarParams")
      if cp_bytes is not None:
        with car.CarParams.from_bytes(cp_bytes) as CP:
          # VehicleModel.__init__ copies out plain floats it needs (mass,
          # wheelbase, etc.) into its own attributes, so the resulting object
          # remains valid after this `with` block exits.
          self.vehicle_model = VehicleModel(CP)
        print("[beamngd] CarParams loaded, VehicleModel ready for steering", flush=True)

    if self.vehicle_model is not None:
      # get_steer_from_curvature() is opendbc's own inverse of calc_curvature(),
      # including the speed-dependent understeer compensation (curvature_factor)
      # a plain angle = curvature * wheelbase * steerRatio formula ignores
      # entirely -- real tires need MORE wheel angle for the same curvature the
      # faster you go. That's very likely why a single flat gain worked at one
      # speed and not another. roll=0.0: no roll telemetry from BeamNG yet, and
      # roll_compensation is a minor correction relative to this.
      target_wheel_angle_rad = self.vehicle_model.get_steer_from_curvature(desired_curvature, self.state.speed, 0.0)
      target_wheel_angle_deg = math.degrees(target_wheel_angle_rad)
      # NOTE: sign flipped from the expected "openpilot left-positive, negate
      # for BeamNG" convention -- that produced a constant rightward bias in
      # testing (curvature_factor/slip_factor confirmed NOT the cause: Honda's
      # real specs give a normal negative/understeer slip factor with no sign
      # flip at any real speed), so empirically this one needs the opposite.
      target_position = target_wheel_angle_deg / MAX_STEERING_WHEEL_ANGLE_DEG
      target_position = max(-1.0, min(1.0, target_position))
    else:
      target_position = self.steering_position  # no carParams yet -- hold still
    # Still rate-limit the approach instead of snapping straight there --
    # smooths out any per-tick noise in the model's desired path.
    delta = target_position - self.steering_position
    delta = max(-STEER_POSITION_RATE_LIMIT_PER_TICK, min(STEER_POSITION_RATE_LIMIT_PER_TICK, delta))
    self.steering_position += delta
    steer_out = self.steering_position
    # Map openpilot's requested acceleration (m/s^2) onto BeamNG's 0..1 pedals.
    # Scaled by the SAME limits the longitudinal planner is allowed to command,
    # so full pedal lines up with the top of that range. These used to be
    # hardcoded /2.0 and /4.0, matching the stock ACCEL_MAX of 2.0; once
    # BEAMPILOT_ACCEL_SCALE raised that ceiling, the constants no longer matched
    # and the pedal saturated partway up the range, leaving the rest flat.
    accel = actuators.accel
    throttle_out = max(0.0, min(1.0, accel / ACCEL_MAX_SCALED))
    brake_out = max(0.0, min(1.0, -accel / abs(ACCEL_MIN_SCALED)))
    steer_out = max(-1.0, min(1.0, steer_out))

    # self.state.is_engaged mirrors the simulated *car's* ACC state (see
    # update_state_from_telemetry) -- this needs openpilot's own actual
    # engaged decision instead, so control is only ever actually applied once
    # openpilot itself is really driving.
    engaged = bool(self.sm['selfdriveState'].active)

    if self.joystick is not None:
      if engaged:
        self.joystick.emit(steer_out, throttle_out, brake_out)
        self.joystick_controlling = True
      elif self.joystick_controlling:
        self.joystick.emit(0.0, 0.0, 0.0)
        self.joystick_controlling = False
      return

    payload = {
      "engaged": engaged,
      "steering": steer_out,
      "throttle": throttle_out,
      "brake": brake_out,
    }
    self.control_sock.sendto(json.dumps(payload).encode(), (BEAMNG_ADDRESS, CONTROL_PORT))

  def tick(self):
    self.sm.update(0)

    telemetry = self.read_latest_telemetry()
    if telemetry is not None:
      self.update_state_from_telemetry(telemetry)

    self.sim_car.update(self.state)
    self.send_sensors()
    self.send_control()


def main():
  bridge = BeamNGBridge()
  rk = Ratekeeper(100, print_delay_threshold=None)
  print(f"[beamngd] bridge running, waiting for beampilot_bridge telemetry on {BEAMNG_ADDRESS}:{TELEMETRY_PORT}...")

  while True:
    try:
      bridge.tick()
    except Exception:
      traceback.print_exc()
      raise
    rk.keep_time()


if __name__ == "__main__":
  main()
