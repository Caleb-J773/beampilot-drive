"""Ground-truth radar: the BeamNG mod's view of nearby traffic, as radarTracks.

openpilot's lead detection for this car is vision-only. HONDA_CIVIC_2022 is
BOSCH_RADARLESS, so opendbc's RadarInterfaceBase.update() returns an EMPTY
RadarData at 20Hz and radard falls back entirely on modelV2.leadsV3 -- through
a camera that, as the README admits, is fed the wide-lens intrinsics for an
image that is not wide. Distance to the car in front is exactly the quantity
that misjudges.

The simulator knows the answer exactly. The same mapmgr table that feeds blind
spot monitoring carries every vehicle's position and velocity, so the mod can
emit real radar points and radard can fuse them the way it would on a car with
a radar.

Wire path, deliberately one hop: the mod sends straight here, not via beamngd.
Blind spot rides inside the telemetry struct beamngd already parses, so it has
to be relayed; radar is its own variable-length packet with no reason to pass
through an intermediary.

    beampilot.lua  --UDP-->  card.py  -->  radarTracks  -->  radard

card is the receiver rather than beamngd because card ALREADY publishes
radarTracks -- an empty one, every fifth frame. A second publisher on the same
service would interleave our points with those empties and radard would see the
lead flicker in and out. Filling in the RadarData card is already building
leaves exactly one publisher, and every hop downstream is stock.

What this does and does not buy, precisely: radard's get_lead() only consults
radar tracks when the VISION model already reports a lead with prob > 0.5 (the
sole exception is potential_low_speed_lead, below V_EGO_STATIONARY). So by
default ground truth *refines* a lead the model has already found -- which is
the half that was wrong -- but does not conjure one the model missed. See
BEAMPILOT_RADAR_LEADS in radard.py for lifting that, which is defensible here
in a way it is not on a real car: these points cannot be a false positive.
"""
import socket
import struct
import time

from openpilot.common.beampilot_env import env_bool, env_float, env_int

MAGIC = b"BPR1"
HEADER = struct.Struct("<4sB")            # magic, track count
# trackId, dRel, yRel, vRel -- the only fields radard reads off a RadarPoint
# (aRel/yvRel/measured are in the deprecated group in car.capnp, and radard
# derives acceleration itself with a Kalman filter).
TRACK = struct.Struct("<Ifff")
MAX_TRACKS = 24
MAX_PACKET = HEADER.size + MAX_TRACKS * TRACK.size

# Off by default. It is the simulator's object list, not a sensor: it sees
# through hills, in fog, and knows exact velocities, and openpilot behaves
# unrealistically well on that. What is left when it IS enabled is deliberately
# a poorer instrument than mapmgr could provide -- see lua_config below.
RADAR_ENABLED = env_bool("BEAMPILOT_RADAR", False)
RADAR_PORT = env_int("BEAMPILOT_RADAR_PORT", 49155)
RADAR_ADDRESS = "127.0.0.1"
# Same reasoning as the blind spot feed: if the mod stops sending, report no
# tracks rather than holding the last ones. A frozen lead is worse than none --
# the longitudinal planner would brake for a car that is no longer there.
RADAR_STALE_SECONDS = env_float("BEAMPILOT_RADAR_STALE_SECONDS", 0.5)


def lua_config() -> dict[str, float]:
  """Radar tuning pushed down to the mod inside the control packet.

  Keys mirror RADAR_DEFAULTS in beampilot.lua. Vehicle Lua cannot read this
  process's environment, so this is the only way BEAMPILOT_RADAR_* reaches it.
  """
  return {
    "enabled": 1.0 if RADAR_ENABLED else 0.0,
    "port": float(RADAR_PORT),
    # Shorter than real radar reaches (~150m), on purpose. The camera would
    # not have seen a lead at 150m, so handing openpilot one changes when it
    # starts managing distance -- which reads as braking absurdly early.
    "rangeM": env_float("BEAMPILOT_RADAR_RANGE_M", 110.0),
    # Half-width of the beam at the sensor, plus a spread with distance -- a
    # crude cone. Narrow enough not to fill the track list with the next
    # carriageway; wide enough to hold your own lane round a bend.
    "halfWidthM": env_float("BEAMPILOT_RADAR_HALF_WIDTH_M", 3.0),
    "spread": env_float("BEAMPILOT_RADAR_SPREAD", 0.07),
    # Report vehicles travelling towards us. Off by default: an oncoming car is
    # not a lead, and on a narrow road the in-path test is quite capable of
    # picking one, which is a hard-braking event for a car that was going to
    # pass on the other side anyway. A vehicle facing the other way but
    # STATIONARY is still reported -- that is a broken-down car in your lane.
    "oncoming": 1.0 if env_bool("BEAMPILOT_RADAR_ONCOMING", False) else 0.0,
    # Drop anything with no line of sight, against static geometry only, so a
    # car does not hide the car behind it (radar does see under and around
    # one). This is the big one for realism: without it the radar reads through
    # hills and buildings.
    "occlusion": 1.0 if env_bool("BEAMPILOT_RADAR_OCCLUSION", True) else 0.0,
    # Range and range-rate noise. Real radar is not exact, and openpilot's
    # Kalman filter is built expecting it not to be.
    "noiseM": env_float("BEAMPILOT_RADAR_NOISE_M", 0.12),
    "noiseMs": env_float("BEAMPILOT_RADAR_NOISE_MS", 0.06),
    # Points behind the front bumper are not something a forward radar sees.
    "minDRelM": env_float("BEAMPILOT_RADAR_MIN_DREL_M", 0.5),
    "maxTracks": float(min(MAX_TRACKS, env_int("BEAMPILOT_RADAR_MAX_TRACKS", 12))),
    "rateHz": env_float("BEAMPILOT_RADAR_RATE_HZ", 20.0),
    # Logs the nearest track to the BeamNG console every scan. For checking the
    # mod is seeing what you think it is; noisy in traffic.
    "debug": 1.0 if env_bool("BEAMPILOT_RADAR_DEBUG", False) else 0.0,
  }


def encode(tracks: list[tuple[int, float, float, float]]) -> bytes:
  """Only used by the tests; the real encoder is in Lua."""
  tracks = tracks[:MAX_TRACKS]
  out = [HEADER.pack(MAGIC, len(tracks))]
  for track_id, d_rel, y_rel, v_rel in tracks:
    out.append(TRACK.pack(track_id & 0xFFFFFFFF, d_rel, y_rel, v_rel))
  return b"".join(out)


def decode(data: bytes) -> list[tuple[int, float, float, float]] | None:
  """(trackId, dRel, yRel, vRel) per point, or None if this is not our packet."""
  if len(data) < HEADER.size:
    return None
  magic, count = HEADER.unpack_from(data, 0)
  if magic != MAGIC or count > MAX_TRACKS:
    return None
  if len(data) != HEADER.size + count * TRACK.size:
    return None
  return [TRACK.unpack_from(data, HEADER.size + i * TRACK.size) for i in range(count)]


class RadarReceiver:
  """card's end: the most recent set of points, or none if the feed went quiet."""

  def __init__(self, port: int = RADAR_PORT, address: str = RADAR_ADDRESS,
               stale_seconds: float = RADAR_STALE_SECONDS):
    self.stale_seconds = stale_seconds
    self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.sock.setblocking(False)
    self.sock.bind((address, port))
    self.tracks: list[tuple[int, float, float, float]] = []
    self.last_packet = 0.0

  @staticmethod
  def create(port: int = RADAR_PORT) -> "RadarReceiver | None":
    """A receiver, or None if radar is off or the port cannot be bound.

    Never raises: a radar feed failing to start must not stop card from
    publishing carState.
    """
    if not RADAR_ENABLED:
      return None
    try:
      return RadarReceiver(port=port)
    except OSError:
      return None

  def update(self) -> list[tuple[int, float, float, float]]:
    while True:
      try:
        data = self.sock.recv(MAX_PACKET + 64)
      except (BlockingIOError, OSError):
        break
      decoded = decode(data)
      if decoded is not None:
        self.tracks = decoded
        self.last_packet = time.monotonic()

    if self.last_packet and (time.monotonic() - self.last_packet) > self.stale_seconds:
      self.tracks = []
    return self.tracks

  def close(self) -> None:
    self.sock.close()


if __name__ == "__main__":
  # Manual check, with openpilot stopped (card holds the port while it runs).
  print(f"radar enabled={RADAR_ENABLED} port={RADAR_PORT}")
  print(f"config pushed to the mod: {lua_config()}")
  rx = RadarReceiver.create()
  if rx is None:
    raise SystemExit(f"could not listen on {RADAR_ADDRESS}:{RADAR_PORT}"
                     + " -- card.py already has it, or BEAMPILOT_RADAR is off")
  print("listening; drive up behind something in BeamNG. Ctrl+C to stop.")
  try:
    while True:
      tracks = rx.update()
      if tracks:
        nearest = min(tracks, key=lambda t: t[1])
        print(f"{len(tracks):2d} tracks  nearest: id={nearest[0]} "
              + f"dRel={nearest[1]:6.1f}m yRel={nearest[2]:+5.1f}m vRel={nearest[3]:+5.1f}m/s",
              flush=True)
      time.sleep(0.25)
  except KeyboardInterrupt:
    rx.close()
    print("\nstopped")
