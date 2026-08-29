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

from opendbc.car import scale_tire_stiffness
from opendbc.car.honda.values import CruiseButtons
from openpilot.common.beampilot_limits import ACCEL_MAX as ACCEL_MAX_SCALED
from openpilot.common.beampilot_limits import ACCEL_MIN as ACCEL_MIN_SCALED
from openpilot.common.beampilot_env import env_bool, env_float, env_int, env_str
from openpilot.common.beampilot_bsm import (BSM_ENABLED, BlindSpotSender,
                                            flags_from_dash_lights, resolve)
from openpilot.common.beampilot_bsm import lua_config as bsm_lua_config
from openpilot.common.beampilot_radar import lua_config as radar_lua_config
from openpilot.common.beampilot_vehicle import SteerRatioCache, VehicleGeometryReceiver
from openpilot.common.beampilot_vehicle import lua_config as vehicle_lua_config
from openpilot.common.beampilot_vehicle import resolve as resolve_geometry
from openpilot.common.beampilot_vehicle import env_overrides, steer_ratio_source
from openpilot.common.constants import CV
from opendbc.car.structs import car
from opendbc.car.vehicle_model import VehicleModel
from openpilot.cereal import log, messaging
from openpilot.common.params import Params
from openpilot.common.realtime import DT_CTRL, DT_DMON, Ratekeeper
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
# ...unless it was left unset, in which case the mod's measurement of the real
# vehicle replaces it. Explicitly setting it is a human decision and still
# wins -- see BeamNGBridge.apply_geometry.
STEER_LOCK_PINNED = bool(os.environ.get("BEAMPILOT_STEER_LOCK_DEG", "").strip())

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
# bits 4..7 are the BSM flags; see beampilot_bsm.flags_from_dash_lights. They
# ride in dashLights rather than in struct fields of their own so that a mod and
# a beamngd from different revisions still exchange telemetry -- parse_telemetry
# below rejects any packet whose length is not an exact match, so growing the
# struct turns "no BSM" into "no telemetry at all".

# The mod cannot read this process's environment, so the BSM tuning has to be
# pushed down inside the control packet. Re-sent periodically rather than every
# tick: it is ~200 bytes of JSON, and re-sending is what lets a vehicle reload
# (which resets the mod to its built-in defaults) pick the settings back up.
BSM_CONFIG_INTERVAL_TICKS = max(1, int(BEAMNGD_TICK_HZ * 2.0))

# Switch the blinker off once a lane change is done. Nothing in the game
# cancels an indicator that was never physically stalked -- BeamNG's own
# auto-cancel keys on the driver steering out of the turn, and openpilot's
# steering arrives through input.event, not the stalk. openpilot knows exactly
# when the manoeuvre finished, so it is the one that should say so.
SIGNAL_AUTO_CANCEL = env_bool("BEAMPILOT_SIGNAL_AUTO_CANCEL", True)
# A cancel is one-shot, and UDP drops. Repeating it is safe because the mod
# only ever toggles a signal that is currently ON, so the second and subsequent
# requests are no-ops. 1.5s rather than the 0.15s this started as: the short
# window was one of the two reasons the signal sometimes stayed on.
SIGNAL_CANCEL_REPEAT_TICKS = max(1, int(BEAMNGD_TICK_HZ * 1.5))
# The other reason. desire_helper.py can only abort a lane change while its
# timer is under BEAMPILOT_LANE_CHANGE_ABORT_S, so a manoeuvre that ran longer
# than that CANNOT have ended in an abort -- whatever the blind spot says at
# the instant it ends. Without this, a car sitting in the blind spot as the
# change completed (very common: you just overtook it) looked exactly like an
# abort, and the signal was deliberately left on for a resume that never came.
LANE_CHANGE_ABORT_WINDOW = env_float("BEAMPILOT_LANE_CHANGE_ABORT_S", 2.0)

# Report the real gear. car_events.py raises wrongGear/reverseGear off it, which
# is what stops openpilot engaging while the car rolls backwards -- correct for
# driving, wrong for anything where reversing under openpilot is the point
# (arcade mode, messing about, testing the bridge in a car park). Off pins the
# gear to drive, which is what the bridge did before it read the real one.
REPORT_GEAR = env_bool("BEAMPILOT_REPORT_GEAR", True)


# Where the fake Honda's identity used to reach the driving: beamngd turns
# openpilot's desiredCurvature into a wheel angle with whatever geometry
# CarParams carries, and nothing anywhere closes a loop on the result -- the
# model does eventually notice the car is off its path, but no controller
# integrates the error -- so a vehicle that needs more lock for the same
# curvature simply under-turns forever. steerRatio dominates: the wheel angle
# scales with it almost exactly.
#
# Two things now fix that, in order of preference.
#
# paramsd estimates steerRatio and tyre stiffness from how the car actually
# responds, and openpilot feeds that back into its own VehicleModel every tick.
# Following it is almost always right here, because the starting point is a
# Honda's and the car is not a Honda. Off restores the pre-fix behaviour, which
# is the static CarParams value forever.
USE_LIVE_STEER_PARAMS = env_bool("BEAMPILOT_LIVE_STEER_PARAMS", True)

# Better still: stop inferring, and MEASURE. The mod reads the vehicle BeamNG
# actually spawned -- wheelbase, weight distribution, mass, yaw inertia, the
# steering lock -- straight out of its node table, and fits the rack ratio from
# the real steering wheel angle against the real road wheel angle.
# openpilot/common/beampilot_vehicle.py is that feed; resolve_geometry() merges
# it with the manual BEAMPILOT_* overrides, which win over both.
#
# Where the two overlap -- steerRatio -- the measurement wins: paramsd infers it
# through a model of the wrong car, and clamps the answer to 0.5x..2.0x a
# Honda's, which is not a bound that means anything for a bus. Its tyre
# stiffness estimate is still followed, since that genuinely cannot be measured
# off the geometry.
#
# The old path is intact and one switch away: BEAMPILOT_BEAMNG_GEOMETRY=0, an
# un-reinstalled mod, or simply no packets arriving all leave CarParams exactly
# as the fingerprint built it -- a Honda Civic, which is what beampilot drove on
# before any of this existed. The FINGERPRINT itself is untouched either way;
# this only ever changes those few geometry numbers.


def gear_for(gear_index: int, report: bool | None = None) -> str:
  """BeamNG's gearIndex as one of SimulatorState's four gear strings.

  -1 and below is reverse, 0 is neutral, 1 and up is a forward ratio. Park is
  not distinguishable from neutral without the gear STRING, and both are
  non-engageable, so park reports as neutral.

  With reporting off the answer is always "drive", which is what the bridge did
  before it read the real gear at all -- openpilot then never sees reverse, so
  it never raises reverseGear, so it will drive the car backwards.
  """
  if not (REPORT_GEAR if report is None else report):
    return "drive"
  if gear_index < 0:
    return "reverse"
  if gear_index == 0:
    return "neutral"
  return "drive"

LaneChangeState = log.LaneChangeState
LaneChangeDirection = log.LaneChangeDirection


class PhaseClock:
  """Fires at a fixed average rate on a caller that ticks much faster.

  A plain `now - last > interval` gate quantises to the caller's tick: at
  beamngd's 100Hz a 20Hz (50ms) gate actually fires every 60ms -- 16.7Hz,
  under what SERVICE_LIST asks for. Halving the interval to compensate (what
  the driver-monitoring publisher used to do) overshoots the other way, and
  measured 33Hz against 20 expected. Advancing a phase by exactly one interval
  per fire keeps the long-run average right whichever way the tick lands.
  """

  def __init__(self, rate_hz: float):
    self.interval = 1.0 / rate_hz
    self.next_at = 0.0

  def due(self, now: float) -> bool:
    if now < self.next_at:
      return False
    # First call, or a gap much longer than the interval (the game was paused,
    # or the process stalled): resync instead of firing a burst to make up time
    # that has already gone.
    if self.next_at == 0.0 or (now - self.next_at) > self.interval:
      self.next_at = now + self.interval
    else:
      self.next_at += self.interval
    return True

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
PERIPHERAL_RATE_HZ = 4.0  # SERVICE_LIST['peripheralState'].frequency

# The acceleration range the longitudinal planner may actually command. Shared
# with the planner and the MPC through common/beampilot_limits.py -- three
# copies of the same env arithmetic is how they drifted apart in the first place.
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

    # openpilot only decides to actually engage *after* seeing the car's own
    # cruise report go true: opendbc's honda carstate.py sets
    # cruiseState.enabled straight from the ACC_STATUS CAN signal, and
    # openpilot expects the car's ACC to turn on by itself, exactly like a real
    # Honda's cruise button does independent of whatever software is watching.
    # So ACC_STATUS (state.is_engaged, sent via SimulatedCar) has to be driven
    # directly from the cruise button here, NOT from selfdriveState.active --
    # otherwise nothing is ever the first thing to flip cruiseState.enabled
    # true, and openpilot can never engage no matter how many times the button
    # is pressed.
    #
    # This used to be explained as following from pcmCruise=True on a
    # non-alpha-long Civic. It does not: launch_beampilot.sh sets
    # AlphaLongitudinalEnabled, so pcmCruise has always been False in practice
    # -- on the Civic and on BEAMPILOT alike. The button still has to be what
    # starts it either way.
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
    self.static_steer_ratio = 0.0
    self.live_params_applied = False
    # The divisor that turns a steering wheel angle into BeamNG's -1..1 input.
    # Starts at the configured/default guess and is replaced by the vehicle's
    # real lock once the mod reports it (unless it was pinned by hand).
    self.steer_lock_deg = MAX_STEERING_WHEEL_ANGLE_DEG
    self.steer_lock_warned = False
    self.steer_ratio_source = "the fingerprint's own value"

    # Mirrors the Lua mod's own isControlling gating: only push a neutral
    # reset on the disengage EDGE, never on every not-engaged tick -- emitting
    # steer=0/throttle=0/brake=0 every tick while disengaged would otherwise
    # fight (and win against) the player's own bound wheel/keyboard input in
    # BeamNG's own controller layer, same bug already fixed once on the Lua
    # side.
    self.joystick_controlling = False

    # vehicleParameters carries the road roll paramsd estimates. modelV2 is
    # only for the lane change state the signal auto-cancel keys on.
    self.sm = messaging.SubMaster(['carControl', 'controlsState', 'selfdriveState',
                                   'vehicleParameters', 'modelV2'])

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
    self.car_params_bytes: bytes | None = None

    # What the mod measured about the vehicle actually spawned. None if the
    # feed is switched off or its port is taken, which is not an error: the
    # VehicleModel then stays on CarParams exactly as it always did.
    self.geometry = VehicleGeometryReceiver.create()
    self.geometry_applied: dict[str, float] = {}
    # A steer ratio has to be driven to be measured, so it is remembered: keyed
    # on vehicle AND steering lock, because BeamNG's racks are parts rather than
    # car properties. Until a vehicle has ever been measured the lock alone
    # implies a far better ratio than a Honda's -- see beampilot_vehicle.py.
    self.ratio_cache = SteerRatioCache()
    if self.geometry is None:
      print("[beamngd] BeamNG vehicle geometry off -- steering uses the"
            + " fingerprinted Civic's numbers", flush=True)

    # Rates SERVICE_LIST expects, held by a phase clock rather than a
    # `now - last > interval` gate -- see PhaseClock for why that undershoots.
    self.gps_clock = PhaseClock(GPS_RATE_HZ)
    self.dmon_clock = PhaseClock(1.0 / DT_DMON)
    self.perp_clock = PhaseClock(PERIPHERAL_RATE_HZ)

    # steeringRateDeg is not in the telemetry struct, so it is differenced from
    # the wheel angle here. Lightly filtered: at a 100Hz tick the raw
    # difference of a ~500 degree-scale angle is mostly quantisation noise.
    self.last_steering_angle: float | None = None
    self.last_telemetry_time = 0.0

    # Turn signal auto-cancel. Tracks the lane change state machine's edge out
    # of laneChangeStarting; see maybe_cancel_signal.
    self.prev_lane_change_state = LaneChangeState.off
    self.signal_cancel_side: str | None = None
    self.signal_cancel_ticks = 0
    self.lane_change_started_at = 0.0

    # BSM. The mod does the detection (it can see every vehicle in the scene;
    # we cannot), and this just relays the answer to card.py, which overlays it
    # onto carState -- see openpilot/common/beampilot_bsm.py for why it takes a
    # socket rather than a CAN message.
    self.bsm_sender = BlindSpotSender() if BSM_ENABLED else None
    self.bsm_flags = 0
    self.bsm_reported: tuple[bool, bool] | None = None
    self.last_bsm_print = 0.0
    self.tick_count = 0

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
    self.bsm_flags = flags_from_dash_lights(telemetry.dash_lights)

    # The mod has always sent these; nothing was reading them, so openpilot
    # believed the car was permanently in drive with the handbrake off.
    self.state.gear = gear_for(telemetry.gear)
    self.state.parking_brake = bool(telemetry.dash_lights & DL_PARKING_BRAKE)

    now = time.monotonic()
    dt = now - self.last_telemetry_time
    if self.last_steering_angle is not None and 1e-4 < dt < 0.5:
      raw_rate = (telemetry.steering_wheel_deg - self.last_steering_angle) / dt
      # First-order filter, ~50ms. Enough to take the quantisation edge off
      # without lagging the signal into uselessness.
      alpha = min(1.0, dt / 0.05)
      self.state.steering_rate += alpha * (raw_rate - self.state.steering_rate)
    self.last_steering_angle = telemetry.steering_wheel_deg
    self.last_telemetry_time = now

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

    if self.gps_clock.due(now):
      self.sim_sensors.send_gps_message(self.state, count=1)

    if self.dmon_clock.due(now):
      self.sim_sensors.send_fake_driver_monitoring()

    if self.perp_clock.due(now):
      self.sim_sensors.send_peripheral_state()

  def send_bsm(self):
    """Relay the mod's blind spot flags to card.py, which owns carState."""
    if self.bsm_sender is None:
      return
    self.bsm_sender.send(self.bsm_flags)

    # Worth saying out loud -- a lane change that quietly refuses to start is
    # otherwise indistinguishable from a broken one. Rate limited because in
    # heavy traffic the flags can flip several times a second.
    reported = resolve(self.bsm_flags)
    now = time.monotonic()
    if reported != self.bsm_reported and (now - self.last_bsm_print) > 1.0:
      left, right = reported
      if left or right:
        sides = " and ".join(s for s, on in (("left", left), ("right", right)) if on)
        print(f"[beamngd] blind spot: {sides} occupied", flush=True)
      elif self.bsm_reported is not None:
        print("[beamngd] blind spot: clear", flush=True)
      self.bsm_reported = reported
      self.last_bsm_print = now

  def bsm_config_due(self) -> bool:
    return self.tick_count % BSM_CONFIG_INTERVAL_TICKS == 0

  def apply_geometry(self):
    """Take on what the mod measured about the vehicle BeamNG actually spawned.

    Two separate consumers, because they are two separate errors:
      - the CarParams fields feed VehicleModel, which decides how much ROAD
        WHEEL angle a given curvature needs;
      - the steering lock is beamngd's own divisor, turning that road wheel
        angle into the -1..1 number BeamNG's input.event takes.
    Get one right and the other wrong and the result is still a constant
    scaling error on every steering command, which is exactly the symptom this
    is here to remove.
    """
    measured = self.geometry.values
    name, samples = self.geometry.name, self.geometry.samples
    # Remember a good measurement before resolving, so this run's answer is the
    # one that gets stored and the next spawn starts from it.
    if "steerRatio" in measured and measured.get("steerLockDeg"):
      if self.ratio_cache.put(name, measured["steerLockDeg"], measured["steerRatio"], samples):
        print(f"[beamngd] remembered steerRatio {measured['steerRatio']:.2f} for"
              + f" {name or 'this vehicle'} at {measured['steerLockDeg']:.0f} deg lock"
              + f" -> {self.ratio_cache.path}", flush=True)

    values = resolve_geometry(measured, samples=samples, name=name, cache=self.ratio_cache)
    if values != self.geometry_applied:
      self.geometry_applied = values
      self.steer_ratio_source = steer_ratio_source(measured, samples=samples, name=name,
                                                   cache=self.ratio_cache)
      self.refresh_vehicle_model(force=True)

    lock = self.geometry.values.get("steerLockDeg")
    if lock and not STEER_LOCK_PINNED and abs(lock - self.steer_lock_deg) > 0.5:
      print(f"[beamngd] steering lock {self.steer_lock_deg:.0f} -> {lock:.0f} deg"
            + f" (measured on {self.geometry.name or 'the spawned vehicle'})", flush=True)
      self.steer_lock_deg = lock
    elif lock and STEER_LOCK_PINNED and abs(lock - self.steer_lock_deg) > 0.1 * lock:
      # Pinned by hand and more than 10% off what the car actually has. Not
      # overridden -- an explicit setting is an explicit setting -- but said
      # out loud once, because it silently scales every steering command.
      if not self.steer_lock_warned:
        self.steer_lock_warned = True
        print("[beamngd] WARNING: BEAMPILOT_STEER_LOCK_DEG is pinned to"
              + f" {self.steer_lock_deg:.0f} but this vehicle's lock is {lock:.0f} deg."
              + " Unset it to use the measured value.", flush=True)

  def ratio_is_known(self) -> bool:
    """Is a steer ratio for the vehicle currently spawned already settled?

    True if it is pinned by hand, measured this run, or remembered from a
    previous drive on this same vehicle and rack. Only the unknown case is
    worth interrupting the car to sweep for.
    """
    if "steerRatio" in env_overrides():
      return True
    if self.geometry is None:
      return False
    if "steerRatio" in self.geometry.values:
      return True
    lock = self.geometry.values.get("steerLockDeg", 0.0)
    return bool(lock) and self.ratio_cache.get(self.geometry.name, lock) is not None

  def refresh_vehicle_model(self, force: bool = False):
    """(Re)build the VehicleModel from CarParams plus any measured geometry.

    Cheap on the common path: once a model exists and nothing has changed this
    is a single attribute test. Rebuilding rather than mutating because
    VehicleModel copies the CarParams floats it needs into its own attributes
    at construction time -- there is no setter for wheelbase.

    CarParams comes from the persistent Params store (written by card.py with
    block=True) rather than a cereal subscription: carParams is published once,
    right after fingerprinting, and a plain SubMaster can miss it entirely if
    beamngd's socket was not connected at that exact moment -- a classic
    slow-joiner race, and one whose symptom is the steering command staying at
    zero forever.
    """
    if self.vehicle_model is not None and not force:
      return
    if self.car_params_bytes is None:
      self.car_params_bytes = self.params.get("CarParams")
      if self.car_params_bytes is None:
        return

    with car.CarParams.from_bytes(self.car_params_bytes) as CP:
      builder = CP.as_builder()
      for field, value in self.geometry_applied.items():
        setattr(builder, field, value)
      # Tyre stiffness is not independent of the geometry: opendbc derives it
      # from mass and weight distribution (scale_tire_stiffness, called by
      # every CarInterface), so overriding those and leaving the Civic's
      # stiffness behind describes a car that cannot exist -- and it is the
      # cF/cR ratio that sets how much EXTRA lock the model asks for as speed
      # rises. Back the fingerprint's own tire_stiffness_factor out of the
      # numbers it published, then re-derive at the real geometry.
      if {"mass", "wheelbase", "centerToFront"} & set(self.geometry_applied):
        base_front, _ = scale_tire_stiffness(CP.mass, CP.wheelbase, CP.centerToFront, 1.0)
        factor = CP.tireStiffnessFront / base_front if base_front > 0 else 1.0
        builder.tireStiffnessFront, builder.tireStiffnessRear = scale_tire_stiffness(
          builder.mass, builder.wheelbase, builder.centerToFront, factor)
      self.static_steer_ratio = builder.steerRatio
      self.vehicle_model = VehicleModel(builder)
      summary = (f"steerRatio={builder.steerRatio:.2f} ({self.steer_ratio_source})"
                 + f" wheelbase={builder.wheelbase:.2f}m"
                 + f" centerToFront={builder.centerToFront:.2f}m mass={builder.mass:.0f}kg"
                 + f" J={builder.rotationalInertia:.0f}")
    source = self.geometry.name if (self.geometry is not None and self.geometry.name) else None
    if self.geometry_applied:
      print(f"[beamngd] VehicleModel for {source or 'the spawned vehicle'}: {summary}",
            flush=True)
    else:
      print(f"[beamngd] VehicleModel from the fingerprinted CarParams: {summary}", flush=True)

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

    self.refresh_vehicle_model()
    if self.vehicle_model is not None:
      # get_steer_from_curvature() is opendbc's own inverse of calc_curvature(),
      # including the speed-dependent understeer compensation (curvature_factor)
      # a plain angle = curvature * wheelbase * steerRatio formula ignores
      # entirely -- real tires need MORE wheel angle for the same curvature the
      # faster you go. That's very likely why a single flat gain worked at one
      # speed and not another.
      # Roll comes from vehicleParameters, the same estimate openpilot's own
      # lateral controllers pass to this function (controlsd.py, and every
      # latcontrol_*.py). The telemetry does carry a true roll from BeamNG, but
      # its sign convention is motionSim's, not paramsd's, and a roll fed in
      # backwards is worse than no roll at all -- matching the rest of the
      # stack is worth more here than the extra accuracy.
      lp = self.sm['vehicleParameters']
      roll = lp.roll
      # Track paramsd's LIVE estimate, exactly as controlsd.py:80 does for its
      # own VehicleModel. This was the one place beampilot silently diverged
      # from stock: openpilot estimates steerRatio and tyre stiffness from the
      # car's actual response and feeds them back, but beamngd built a separate
      # VehicleModel from CarParams and never updated it -- so the conversion
      # that actually reaches BeamNG stayed frozen at the fake Civic's 15.38
      # while openpilot's own copy was being corrected. Since nothing else
      # closes a loop on beamngd's output, that error had nowhere to go: the
      # car just under-turned forever.
      #
      # Gated on validity. paramsd clamps its estimate to 0.5x..2.0x the
      # CarParams value and flags it invalid outside that, and an unconverged
      # or out-of-range estimate is worse than the static number.
      #
      # A ratio MEASURED from the vehicle's own steering geometry beats an
      # inferred one, so when the mod has sent one it is pinned and only the
      # tyre stiffness is followed. paramsd infers steerRatio from yaw rate
      # against steering angle, which also absorbs every other error in the
      # model it is inferring through -- and it is clamped to 0.5x..2.0x a
      # Honda's, which is not a bound that means anything for a bus.
      if USE_LIVE_STEER_PARAMS and lp.steerRatioValid and lp.stiffnessFactorValid:
        measured = self.geometry_applied.get("steerRatio")
        self.vehicle_model.update_params(max(lp.stiffnessFactor, 0.1),
                                         measured or max(lp.steerRatio, 0.1))
        self.live_params_applied = True
      elif self.live_params_applied:
        # Fall back rather than keep driving on a stale estimate that has since
        # gone out of range.
        self.vehicle_model.update_params(1.0, self.static_steer_ratio)
        self.live_params_applied = False
      target_wheel_angle_rad = self.vehicle_model.get_steer_from_curvature(desired_curvature, self.state.speed, roll)
      target_wheel_angle_deg = math.degrees(target_wheel_angle_rad)
      # NOTE: sign flipped from the expected "openpilot left-positive, negate
      # for BeamNG" convention -- that produced a constant rightward bias in
      # testing (curvature_factor/slip_factor confirmed NOT the cause: Honda's
      # real specs give a normal negative/understeer slip factor with no sign
      # flip at any real speed), so empirically this one needs the opposite.
      target_position = target_wheel_angle_deg / self.steer_lock_deg
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
      # Even with the wheel driving, the mod still needs its BSM tuning, and it
      # can only arrive over the control socket. engaged=false here is the same
      # thing the mod sees on every not-engaged tick in lua mode, and its
      # releaseControl() only acts on the edge out of engagement, so this
      # cannot fight the joystick.
      payload = {"engaged": False}
      self.add_mod_config(payload)
      if len(payload) > 1:
        self.control_sock.sendto(json.dumps(payload).encode(), (BEAMNG_ADDRESS, CONTROL_PORT))
      return

    payload = {
      "engaged": engaged,
      "steering": steer_out,
      "throttle": throttle_out,
      "brake": brake_out,
    }
    self.add_mod_config(payload)
    self.control_sock.sendto(json.dumps(payload).encode(), (BEAMNG_ADDRESS, CONTROL_PORT))

  def add_mod_config(self, payload: dict) -> None:
    """Attach whatever the mod needs told: tuning, and one-shot commands.

    Tuning goes down periodically rather than every tick -- it is a few hundred
    bytes of JSON, and re-sending is what lets a vehicle reload (which resets
    the mod to its own defaults) pick the settings back up on its own.
    """
    if self.bsm_config_due():
      payload["bsm"] = bsm_lua_config()
      payload["radar"] = radar_lua_config()
      # Only ask for a calibration sweep if we do not already know this
      # vehicle and rack. Nothing to learn otherwise, and a steering wheel that
      # moves by itself on every spawn gets old fast.
      payload["vehicle"] = vehicle_lua_config(calibrate=not self.ratio_is_known())
    if self.signal_cancel_ticks > 0:
      payload["cancelSignal"] = self.signal_cancel_side
      self.signal_cancel_ticks -= 1

  def maybe_cancel_signal(self):
    """Switch the blinker off once a lane change has actually finished.

    Both the completion path and the blind-spot abort path leave
    laneChangeStarting for preLaneChange (see desire_helper.py), so the state
    transition alone cannot tell them apart. An abort must NOT cancel the
    signal -- leaving it armed is exactly what lets the change resume once the
    lane clears -- so getting this wrong in either direction is visible: cancel
    an abort and it never resumes, miss a completion and the indicator stays on.

    Two things separate them, and the first is exact rather than a heuristic:
    desire_helper can only abort while its timer is under the abort window, so
    a manoeuvre that ran longer than that did not end in an abort no matter
    what the blind spot says at the instant it ends. Only inside that window
    does the blind spot decide.
    """
    meta = self.sm['modelV2'].meta
    state, direction = meta.laneChangeState, meta.laneChangeDirection
    was_changing = self.prev_lane_change_state == LaneChangeState.laneChangeStarting
    now = time.monotonic()
    if state == LaneChangeState.laneChangeStarting and not was_changing:
      self.lane_change_started_at = now
    self.prev_lane_change_state = state

    if not (SIGNAL_AUTO_CANCEL and was_changing):
      return
    if state not in (LaneChangeState.off, LaneChangeState.preLaneChange):
      return

    duration = now - self.lane_change_started_at
    if duration <= LANE_CHANGE_ABORT_WINDOW + DT_CTRL:
      # Short enough to have been an abort, so let the blind spot decide.
      left, right = resolve(self.bsm_flags)
      if ((left and direction == LaneChangeDirection.left) or
          (right and direction == LaneChangeDirection.right)):
        return

    side = "left" if direction == LaneChangeDirection.left else "right"
    self.signal_cancel_side = side
    self.signal_cancel_ticks = SIGNAL_CANCEL_REPEAT_TICKS
    print(f"[beamngd] lane change finished after {duration:.1f}s,"
          + f" cancelling the {side} signal", flush=True)

  def tick(self):
    self.sm.update(0)

    if self.geometry is not None and self.geometry.update():
      self.apply_geometry()

    telemetry = self.read_latest_telemetry()
    if telemetry is not None:
      self.update_state_from_telemetry(telemetry)

    self.sim_car.update(self.state)
    self.send_sensors()
    self.send_bsm()
    self.maybe_cancel_signal()
    self.send_control()
    self.tick_count += 1


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
