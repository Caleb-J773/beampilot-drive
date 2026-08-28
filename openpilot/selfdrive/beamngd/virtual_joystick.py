"""Virtual analog wheel (uinput) -- an alternative control-output path for
beamngd, used when BEAMPILOT_CONTROL_MODE=joystick.

Normal operation (BEAMPILOT_CONTROL_MODE unset/"lua") sends steering/throttle/
brake to the beampilot_bridge BeamNG mod over UDP, which applies them with
input.event() straight into the vehicle's Lua physics loop -- no in-game setup
needed, since that hook doesn't go through BeamNG's own controller-binding
layer at all.

This module is the other approach: create a regular Linux joystick device via
uinput, and let BeamNG.drive read it exactly like a real USB wheel through its
own normal Options > Controls binding screen. This is the same trick
jackz314/openpilot's ETS2/ATS truck_sim bridge uses (joystick.py there) --
except for them it's not optional, ETS2/ATS has no scriptable input hook at
all, so a virtual OS device is the *only* way in. For BeamNG it's a genuine
alternative worth A/B testing against the direct Lua injection, not a
necessity.

One-time setup in BeamNG.drive after beamngd starts with this mode enabled:
Options > Controls > find "BeamPilot Virtual Wheel" in the device list > bind
its Steering/Throttle/Brake axes to the corresponding BeamNG input actions
(same procedure as setting up a real wheel).
"""
import uinput

STEER_RANGE = 32767  # uinput.ABS_X, signed: -STEER_RANGE (full left) .. +STEER_RANGE (full right)
PEDAL_RANGE = 65535  # uinput.ABS_Z / ABS_RZ, unsigned: 0 (released) .. PEDAL_RANGE (full)

STEER_AXIS = uinput.ABS_X + (-STEER_RANGE, STEER_RANGE, 0, 0)
THROTTLE_AXIS = uinput.ABS_Z + (0, PEDAL_RANGE, 0, 0)
BRAKE_AXIS = uinput.ABS_RZ + (0, PEDAL_RANGE, 0, 0)


class BeamNGVirtualJoystick:
  def __init__(self, name: str = "BeamPilot Virtual Wheel"):
    self.device = uinput.Device(
      (STEER_AXIS, THROTTLE_AXIS, BRAKE_AXIS),
      name=name,
      bustype=0x03,    # BUS_USB
      vendor=0x1209,   # pid.codes shared test VID -- not a real wheel's identity, deliberately generic
      product=0x0001,
      version=0x0100,
    )

  def emit(self, steer: float, throttle: float, brake: float):
    """steer: -1..1, left negative (matches beampilot_bridge's own
    steering_input convention, so callers don't need to re-derive sign
    per control mode). throttle/brake: 0..1."""
    steer_raw = int(max(-STEER_RANGE, min(STEER_RANGE, steer * STEER_RANGE)))
    throttle_raw = int(max(0, min(PEDAL_RANGE, throttle * PEDAL_RANGE)))
    brake_raw = int(max(0, min(PEDAL_RANGE, brake * PEDAL_RANGE)))
    # Emit pedals first with syn=False so they land in the same input frame as
    # the steering emit below (syn=True) -- avoids BeamNG ever reading a tick
    # with steering updated but pedals still stale, or vice versa.
    self.device.emit(uinput.ABS_Z, throttle_raw, syn=False)
    self.device.emit(uinput.ABS_RZ, brake_raw, syn=False)
    self.device.emit(uinput.ABS_X, steer_raw, syn=True)

  def close(self):
    self.device.destroy()
