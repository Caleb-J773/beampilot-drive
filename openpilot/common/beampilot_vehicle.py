"""Ground-truth vehicle geometry: what BeamNG actually spawned, as CarParams.

openpilot has to be told what car it is driving. beampilot tells it
HONDA_CIVIC_2022, because a fingerprint is not optional and a Civic is the
best-supported one -- but BeamNG is not spawning a Civic. It might be spawning
a Covet, a pickup, or a bus.

Almost none of that mismatch matters. The fake Civic's CAN parsing, its
button layout and its safety model are all beampilot's own code underneath.
What DOES matter is the handful of numbers openpilot uses to turn a desired
PATH into a steering command:

    desiredCurvature  --[VehicleModel: steerRatio, wheelbase,
                         centerToFront, mass, rotationalInertia]-->
    road wheel angle  --[steerRatio]--> steering wheel angle
                      --[steering lock]--> BeamNG's -1..1 steering input

Every one of those is a Civic's, and beamngd's conversion is OPEN LOOP -- no
controller anywhere integrates the error between the curvature asked for and
the curvature achieved. So if the spawned vehicle needs more lock for the same
corner, it simply under-turns forever. That is the "can't take a ramp at speed"
symptom, and no amount of raising BEAMPILOT_MAX_LAT_ACCEL fixes it, because the
lateral accel cap was never what was binding.

The simulator knows all of these exactly. Every one is computable from
v.data.nodes and wheels.wheels in vehicle Lua -- the same node table BeamNG's
own ESC controller (lua/vehicle/controller/esc.lua) measures wheelbase, COG and
track width from, for exactly the same bicycle-model reason.

Wire path, one hop, same shape as the radar feed:

    beampilot.lua  --UDP 49156-->  beamngd.py  -->  VehicleModel

beamngd is the receiver rather than card because beamngd is what builds the
VehicleModel that converts curvature to a steering command. card publishes
carState and never touches vehicle dynamics.

Two of the numbers are measured rather than read:

  steerRatio     -- BeamNG has no such field; a rack is emergent from the
                    steering geometry. So the mod watches the real steering
                    wheel angle against the real road wheel angle (the same
                    obj:nodeVecPlanarCosRightForward() call esc.lua uses) and
                    fits a ratio through the origin. It needs the wheel to
                    actually be turned a meaningful amount, so it stays 0 --
                    meaning "no answer yet", not "zero" -- until it has seen
                    some real steering. Turning the wheel lock to lock once
                    before engaging measures it instantly.
  steerLockDeg   -- v.data.input.steeringWheelLock, centre to full lock. This
                    is the divisor beamngd needs to turn a steering wheel angle
                    into BeamNG's -1..1 input, and getting it wrong scales
                    every steering command by a constant factor.

With both of those right the chain closes exactly: beamngd multiplies by
steerRatio and divides by steerLockDeg, and BeamNG multiplies by steerLockDeg
and divides by the same rack. Nothing is left to a fudge factor.
"""
import os
import socket
import struct
import time

from openpilot.common.beampilot_env import env_bool, env_float, env_int

MAGIC = b"BPV1"
NAME_LEN = 32
# Field order is the wire format; it must match beampilot_vehicle_packet_t in
# beampilot.lua exactly. Fixed size, so a length mismatch is a version mismatch
# and the packet is dropped rather than misread.
PACKET = struct.Struct("<4s" + f"{NAME_LEN}s" + "8f" + "I")
FIELDS = ("wheelbase", "centerToFront", "trackWidth", "mass",
          "rotationalInertia", "steerRatio", "steerLockDeg", "maxWheelAngleDeg")

# On by default: BeamNG's own numbers for the vehicle actually spawned beat a
# Honda's for a vehicle that is not one, and an un-reinstalled mod simply never
# sends, which falls back to exactly the old behaviour.
GEOMETRY_ENABLED = env_bool("BEAMPILOT_BEAMNG_GEOMETRY", True)
GEOMETRY_PORT = env_int("BEAMPILOT_GEOMETRY_PORT", 49156)
GEOMETRY_ADDRESS = "127.0.0.1"
# Unlike radar and blind spot, this does NOT expire. Geometry does not go stale
# -- the car is still the same car -- and dropping back to a Civic's steerRatio
# mid-corner because a datagram went missing would be far worse than holding a
# measurement from a second ago. A respawn resets the mod, which re-measures.

# The manual override, unchanged in meaning: set one of these and it wins over
# both BeamNG and CarParams. Blank means "whatever the layer below says".
_ENV_OVERRIDES = {
  "steerRatio": "BEAMPILOT_STEER_RATIO",
  "wheelbase": "BEAMPILOT_WHEELBASE_M",
  "centerToFront": "BEAMPILOT_CENTER_TO_FRONT_M",
  "mass": "BEAMPILOT_MASS_KG",
  "rotationalInertia": "BEAMPILOT_ROTATIONAL_INERTIA",
}

# Sanity bounds. A packet outside these is a measurement that went wrong (a
# vehicle mid-spawn, a crashed one whose nodes have moved, a trailer with no
# front axle), and driving on it is worse than driving on the Civic's numbers.
LIMITS = {
  "wheelbase": (1.0, 12.0),
  "centerToFront": (0.2, 11.0),
  "trackWidth": (0.5, 4.0),
  "mass": (150.0, 40000.0),
  "rotationalInertia": (50.0, 500000.0),
  "steerRatio": (4.0, 40.0),
  "steerLockDeg": (30.0, 1200.0),
  "maxWheelAngleDeg": (5.0, 90.0),
}


def lua_config() -> dict[str, float]:
  """Geometry-reporting tuning pushed down to the mod in the control packet.

  Keys mirror VEHICLE_DEFAULTS in beampilot.lua. Vehicle Lua cannot read this
  process's environment, so this is the only way BEAMPILOT_* reaches it.
  """
  return {
    "enabled": 1.0 if GEOMETRY_ENABLED else 0.0,
    "port": float(GEOMETRY_PORT),
    # Nothing here changes quickly, and the steer ratio estimate only improves.
    "rateHz": env_float("BEAMPILOT_GEOMETRY_RATE_HZ", 1.0),
    # Only fit the steer ratio from samples with real steering in them. Below
    # this the road wheel angle is mostly toe-in and node jitter, and dividing
    # by it produces nonsense. Raising it means a better ratio measured less
    # often; lowering it means a faster but noisier one.
    "minSteerDeg": env_float("BEAMPILOT_GEOMETRY_MIN_STEER_DEG", 20.0),
    "minWheelDeg": env_float("BEAMPILOT_GEOMETRY_MIN_WHEEL_DEG", 0.3),
    # Logs the measured numbers to the BeamNG console each time they are sent.
    "debug": 1.0 if env_bool("BEAMPILOT_GEOMETRY_DEBUG", False) else 0.0,
  }


def encode(name: str, values: dict[str, float], samples: int = 0) -> bytes:
  """Only used by the tests and the __main__ demo; the real encoder is in Lua."""
  packed_name = name.encode("utf-8", "replace")[:NAME_LEN]
  return PACKET.pack(MAGIC, packed_name,
                     *(float(values.get(f, 0.0)) for f in FIELDS),
                     samples & 0xFFFFFFFF)


def decode(data: bytes) -> tuple[str, dict[str, float], int] | None:
  """(vehicle name, measurements, steer-ratio sample count), or None.

  A field that failed its sanity check is dropped from the dict rather than
  failing the whole packet: an unmeasurable steer ratio should not also cost us
  a perfectly good wheelbase.
  """
  if len(data) != PACKET.size:
    return None
  unpacked = PACKET.unpack(data)
  if unpacked[0] != MAGIC:
    return None
  name = unpacked[1].split(b"\x00", 1)[0].decode("utf-8", "replace")
  values = {}
  for field, value in zip(FIELDS, unpacked[2:2 + len(FIELDS)], strict=True):
    lo, hi = LIMITS[field]
    if value == value and lo <= value <= hi:   # value == value rejects NaN
      values[field] = value
  return name, values, unpacked[-1]


def env_overrides() -> dict[str, float]:
  """The BEAMPILOT_* geometry overrides that are actually set."""
  out = {}
  for field, var in _ENV_OVERRIDES.items():
    raw = os.environ.get(var, "").strip()
    if not raw:
      continue
    try:
      value = float(raw)
    except ValueError:
      continue
    if value > 0:
      out[field] = value
  return out


def resolve(measured: dict[str, float]) -> dict[str, float]:
  """What to actually put on CarParams, in precedence order.

  1. BEAMPILOT_* environment -- an explicit human decision, always wins.
  2. BeamNG's measurement of the vehicle actually spawned.
  3. (whatever is left) CarParams' own value, by simply not appearing here.

  centerToFront is dropped if it does not sit inside the wheelbase it arrived
  with, because VehicleModel computes aR = wheelbase - centerToFront and a
  negative rear axle distance produces a silently inverted car.
  """
  out = {f: v for f, v in measured.items() if f in _ENV_OVERRIDES}
  out.update(env_overrides())
  wheelbase = out.get("wheelbase")
  if wheelbase is not None and not (0.0 < out.get("centerToFront", -1.0) < wheelbase):
    out.pop("centerToFront", None)
  return out


class VehicleGeometryReceiver:
  """beamngd's end: the last geometry the mod reported, if any.

  Never raises on construction failure -- geometry is an improvement over the
  Civic's numbers, not a prerequisite for driving at all.
  """

  def __init__(self, port: int = GEOMETRY_PORT, address: str = GEOMETRY_ADDRESS):
    self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.sock.setblocking(False)
    self.sock.bind((address, port))
    self.name = ""
    self.values: dict[str, float] = {}
    self.samples = 0
    self.last_packet = 0.0
    self.updated = False   # set on any change; the caller clears it

  @staticmethod
  def create(port: int = GEOMETRY_PORT) -> "VehicleGeometryReceiver | None":
    if not GEOMETRY_ENABLED:
      return None
    try:
      return VehicleGeometryReceiver(port=port)
    except OSError:
      return None

  def update(self) -> bool:
    """Drain the socket. True if anything worth rebuilding on changed."""
    changed = False
    while True:
      try:
        data = self.sock.recv(PACKET.size + 64)
      except (BlockingIOError, OSError):
        break
      decoded = decode(data)
      if decoded is None:
        continue
      name, values, samples = decoded
      self.last_packet = time.monotonic()
      self.samples = samples
      if name != self.name or self._materially_different(values):
        self.name, self.values = name, values
        changed = True
      else:
        # Keep the freshest numbers even when the change is below the
        # threshold, so small drifts still accumulate rather than being lost.
        self.values = values
    self.updated = self.updated or changed
    return changed

  def _materially_different(self, values: dict[str, float]) -> bool:
    """Ignore measurement jitter, so the VehicleModel is not rebuilt at 1Hz.

    0.5% is far below anything that changes how the car drives and far above
    the node-position noise the measurements are made from.
    """
    if set(values) != set(self.values):
      return True
    return any(abs(v - self.values[f]) > 0.005 * max(abs(v), 1e-6)
               for f, v in values.items())

  def close(self) -> None:
    self.sock.close()


if __name__ == "__main__":
  # Manual check, with openpilot stopped (beamngd holds the port while it runs).
  print(f"geometry enabled={GEOMETRY_ENABLED} port={GEOMETRY_PORT}")
  print(f"config pushed to the mod: {lua_config()}")
  if env_overrides():
    print(f"environment overrides (these win): {env_overrides()}")
  rx = VehicleGeometryReceiver.create()
  if rx is None:
    raise SystemExit(f"could not listen on {GEOMETRY_ADDRESS}:{GEOMETRY_PORT}"
                     + " -- beamngd.py already has it, or BEAMPILOT_BEAMNG_GEOMETRY is off")
  print("listening; spawn a vehicle in BeamNG and turn the wheel. Ctrl+C to stop.")
  try:
    while True:
      if rx.update():
        applied = resolve(rx.values)
        print(f"\n{rx.name or '<unnamed>'}  ({rx.samples} steering samples)")
        for field in FIELDS:
          if field in rx.values:
            mark = " <- overridden" if field in env_overrides() else ""
            print(f"  {field:<20} {rx.values[field]:10.4f}{mark}")
        print(f"  applied to CarParams: {applied}", flush=True)
      time.sleep(0.25)
  except KeyboardInterrupt:
    rx.close()
    print("\nstopped")
