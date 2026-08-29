"""BSM -- blind spot monitoring, the side channel between beamngd and card.

openpilot has always known what to do with a blind spot. carState.leftBlindspot
/ rightBlindspot block a signalled lane change in
selfdrive/controls/lib/desire_helper.py and raise the "Car Detected in
Blindspot" alert in selfdrive/selfdrived/selfdrived.py. What a real car does
not have -- and what beampilot never had -- is anything to set them: they come
off a pair of rear-corner radars.

The BeamNG mod (tools/beamng_mod/beampilot_bridge) is that radar now. It sees
every other vehicle in the scene through mapmgr and reports blind spot
occupancy in spare bits of the telemetry packet's dashLights field.

The awkward part is the last hop. carState is built by card.py out of CAN, and
the simulated car is a HONDA_CIVIC_2022, whose only DBC (honda_bosch_radarless)
has no BSM messages at all -- a real Civic reads them off B-CAN with a separate
body DBC. Teaching it to would mean editing opendbc, which is a git submodule
pointing at commaai/opendbc: those edits could not be committed here and anyone
cloning beampilot would silently not get them.

So beamngd forwards the two flags to card over loopback UDP -- the same plain
socket pattern the mod link already uses -- and card overlays them onto the
CarState it publishes. One datagram per tick, five bytes.

Set BEAMPILOT_BSM=0 to switch the whole thing off; nothing binds a socket and
carState keeps whatever the CAN decode produced (always False for this car).
"""
import socket
import struct
import time

from openpilot.common.beampilot_env import env_bool, env_float, env_int

MAGIC = b"BSM1"
# magic + one flags byte. Deliberately versioned: a stale beamngd talking to a
# new card is better off being ignored than misread.
PACKET = struct.Struct("<4sB")

FLAG_LEFT = 1
FLAG_RIGHT = 2
FLAG_LEFT_APPROACHING = 4
FLAG_RIGHT_APPROACHING = 8

# Matches the DL_BSM_* bits in beampilot.lua, shifted down out of the four
# dashLights bits that were already spoken for.
DASH_BSM_SHIFT = 4
DASH_BSM_MASK = 0xF0

BSM_ENABLED = env_bool("BEAMPILOT_BSM", True)
BSM_PORT = env_int("BEAMPILOT_BSM_PORT", 49154)
BSM_ADDRESS = "127.0.0.1"
# Treat a vehicle that is not in the zone yet but closing fast enough to be
# there mid-manoeuvre as occupying it. This is what a real blind spot warning
# does, and it is the case a purely geometric zone misses.
BSM_APPROACHING = env_bool("BEAMPILOT_BSM_APPROACHING", True)
# If beamngd stops sending (crash, pause, not started), fall back to "clear"
# rather than latching the last warning -- a stuck flag would block every lane
# change for the rest of the drive with no way to tell why.
BSM_STALE_SECONDS = env_float("BEAMPILOT_BSM_STALE_SECONDS", 0.5)
# Keep a side "occupied" for this long after it stops reporting. A vehicle
# hovering exactly on the zone boundary otherwise chatters at the scan rate,
# and downstream that chatter is not cosmetic: it flickers the alert, and it
# can start an abort of a lane change already under way and then immediately
# un-start it. Real blind spot indicators hold their lamp for the same reason.
BSM_HOLD_SECONDS = env_float("BEAMPILOT_BSM_HOLD_S", 0.4)


def flags_from_dash_lights(dash_lights: int) -> int:
  """The four BSM bits out of the mod's dashLights field, as FLAG_* bits."""
  return (dash_lights & DASH_BSM_MASK) >> DASH_BSM_SHIFT


def resolve(flags: int) -> tuple[bool, bool]:
  """(left, right) as openpilot should see them, honouring BSM_APPROACHING."""
  mask = FLAG_LEFT | FLAG_RIGHT
  if BSM_APPROACHING:
    mask |= FLAG_LEFT_APPROACHING | FLAG_RIGHT_APPROACHING
  flags &= mask
  left = bool(flags & (FLAG_LEFT | FLAG_LEFT_APPROACHING))
  right = bool(flags & (FLAG_RIGHT | FLAG_RIGHT_APPROACHING))
  return left, right


def lua_config() -> dict[str, float]:
  """The BSM tuning to push down to the mod, in the Lua table's own key names.

  Vehicle Lua cannot read this process's environment, so every BEAMPILOT_BSM_*
  knob has to travel to the mod inside the control packet. The keys and the
  defaults here mirror BSM_DEFAULTS in beampilot.lua exactly; the mod restores
  its own default for anything missing, so an older mod paired with a newer
  beamngd just ignores keys it does not know.
  """
  return {
    "enabled": 1.0 if BSM_ENABLED else 0.0,
    # Forward edge, measured behind the front bumper -- roughly the driver's
    # shoulder, so a car being overtaken does not trip it early.
    "frontM": env_float("BEAMPILOT_BSM_FRONT_M", 1.5),
    # Rear edge, measured behind the rear bumper.
    "rearM": env_float("BEAMPILOT_BSM_REAR_M", 4.0),
    # Inner and outer edges, measured out from the ego's own flank. 3.6m out is
    # about one lane, so the zone covers the adjacent lane and not the one past it.
    "innerM": env_float("BEAMPILOT_BSM_INNER_M", 0.2),
    "outerM": env_float("BEAMPILOT_BSM_WIDTH_M", 3.6),
    # Half-height, so traffic on an overpass or under a bridge is not "beside" us.
    "heightM": env_float("BEAMPILOT_BSM_HEIGHT_M", 2.0),
    "approachS": env_float("BEAMPILOT_BSM_APPROACH_S", 2.0) if BSM_APPROACHING else 0.0,
    "approachMaxM": env_float("BEAMPILOT_BSM_APPROACH_MAX_M", 20.0),
    "minSpeedMs": env_float("BEAMPILOT_BSM_MIN_SPEED_MS", 1.4),
    "rangeM": env_float("BEAMPILOT_BSM_RANGE_M", 60.0),
    "ignoreTouching": 1.0 if env_bool("BEAMPILOT_BSM_IGNORE_TOUCHING", True) else 0.0,
    "rateHz": env_float("BEAMPILOT_BSM_RATE_HZ", 20.0),
    "debug": 1.0 if env_bool("BEAMPILOT_BSM_DEBUG", False) else 0.0,
  }


class BlindSpotSender:
  """beamngd's end: one datagram per tick, fire and forget."""

  def __init__(self, address: str = BSM_ADDRESS, port: int = BSM_PORT):
    self.address, self.port = address, port
    self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

  def send(self, flags: int) -> None:
    try:
      self.sock.sendto(PACKET.pack(MAGIC, flags & 0xFF), (self.address, self.port))
    except OSError:
      # Nothing listening yet (card not up) is the normal startup case on
      # Linux, where an unbound port answers with ICMP and the NEXT send
      # raises ECONNREFUSED. Never worth interrupting the bridge for.
      pass

  def close(self) -> None:
    self.sock.close()


class BlindSpotReceiver:
  """card's end: latest-wins, with a staleness timeout that fails to 'clear'."""

  def __init__(self, port: int = BSM_PORT, address: str = BSM_ADDRESS,
               stale_seconds: float = BSM_STALE_SECONDS,
               hold_seconds: float = BSM_HOLD_SECONDS):
    self.stale_seconds = stale_seconds
    self.hold_seconds = hold_seconds
    self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.sock.setblocking(False)
    self.sock.bind((address, port))
    self.flags = 0
    self.last_packet = 0.0
    self.hold_left_until = 0.0
    self.hold_right_until = 0.0

  @staticmethod
  def create(port: int = BSM_PORT) -> "BlindSpotReceiver | None":
    """A receiver, or None if BSM is off or the port cannot be bound.

    Never raises: a blind spot feed failing to start must not stop the car
    interface from publishing carState.
    """
    if not BSM_ENABLED:
      return None
    try:
      return BlindSpotReceiver(port=port)
    except OSError:
      return None

  def update(self) -> tuple[bool, bool]:
    """Drain the socket and return (leftBlindspot, rightBlindspot)."""
    while True:
      try:
        data = self.sock.recv(64)
      except BlockingIOError:
        break
      except OSError:
        break
      if len(data) == PACKET.size:
        magic, flags = PACKET.unpack(data)
        if magic == MAGIC:
          self.flags = flags
          self.last_packet = time.monotonic()

    now = time.monotonic()
    if self.last_packet and (now - self.last_packet) > self.stale_seconds:
      # A dead feed clears immediately and ignores the hold: holding a warning
      # we can no longer confirm is the one failure mode worth avoiding here.
      self.flags = 0
      self.hold_left_until = self.hold_right_until = 0.0
      return False, False

    left, right = resolve(self.flags)
    if left:
      self.hold_left_until = now + self.hold_seconds
    if right:
      self.hold_right_until = now + self.hold_seconds
    return left or now < self.hold_left_until, right or now < self.hold_right_until

  def close(self) -> None:
    self.sock.close()


if __name__ == "__main__":
  # Manual check: prints whatever beamngd is reporting, as it changes.
  print(f"BSM enabled={BSM_ENABLED} port={BSM_PORT} approaching={BSM_APPROACHING}")
  print(f"config pushed to the mod: {lua_config()}")
  rx = BlindSpotReceiver.create()
  if rx is None:
    # card.py holds this port for the whole drive, so this check only works
    # with openpilot stopped -- use tools/beampilot_monitor.py while it runs.
    raise SystemExit(f"could not listen on {BSM_ADDRESS}:{BSM_PORT}"
                     + " -- card.py already has it, or BEAMPILOT_BSM is off")
  print("listening; drive past some traffic in BeamNG. Ctrl+C to stop.")
  previous = None
  try:
    while True:
      state = rx.update()
      if state != previous:
        left, right = state
        print(f"{time.strftime('%H:%M:%S')}  left={'BLOCKED' if left else 'clear  '}"
              + f"  right={'BLOCKED' if right else 'clear  '}  raw=0b{rx.flags:04b}", flush=True)
        previous = state
      time.sleep(0.02)
  except KeyboardInterrupt:
    rx.close()
    print("\nstopped")
