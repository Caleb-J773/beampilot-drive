#!/usr/bin/env python3
"""Full beampilot data-flow diagnostic: proves which daemons are/aren't publishing,
with measured rates and actual field values."""
import socket
import struct
import time

import openpilot.cereal.messaging as messaging
from openpilot.cereal.services import SERVICE_LIST

print("=" * 72)
print("STAGE 1: raw UDP telemetry from the BeamNG Lua mod (mod -> beamngd)")
print("=" * 72)

TELEMETRY_STRUCT = struct.Struct("<4sffffffiI" + "f" * 15)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(3.0)
try:
  sock.bind(("127.0.0.1", 49152))
  n, first = 0, None
  t0 = time.monotonic()
  while time.monotonic() - t0 < 3.0:
    try:
      data, _ = sock.recvfrom(4096)
    except TimeoutError:
      break
    n += 1
    if first is None and len(data) == TELEMETRY_STRUCT.size:
      first = TELEMETRY_STRUCT.unpack(data)
  el = time.monotonic() - t0
  if n == 0:
    print("  *** NO TELEMETRY PACKETS RECEIVED from the mod ***")
    print("  -> BeamNG not running, mod not loaded, or no vehicle spawned/seated")
  else:
    print(f"  OK: {n} packets in {el:.1f}s = {n/el:.1f} Hz  (mod is sending)")
    if first:
      print(f"  magic={first[0]!r} speed={first[1]:.2f} m/s  steer_input={first[2]:+.3f}  steer_deg={first[3]:+.1f}")
      print(f"  throttle={first[4]:.2f} brake={first[5]:.2f} gear={first[7]} pos=({first[9]:.1f},{first[10]:.1f},{first[11]:.1f})")
  sock.close()
except OSError:
  sock.close()
  # Expected whenever the stack is up: beamngd owns 49152, and only one socket
  # can receive a given UDP unicast stream (binding a second with SO_REUSEPORT
  # would STEAL packets from beamngd, breaking the very thing we're measuring).
  # beamngd holding the port is itself proof beamngd is running; STAGE 3 then
  # confirms the mod is really feeding it by checking live carState values.
  print("  port 49152 held by beamngd (expected while the stack is running)")
  print("  -> beamngd IS running; see STAGE 3 for whether the mod is feeding it")

print()
print("=" * 72)
print("STAGE 2: cereal channels (daemons -> openpilot)")
print("=" * 72)

CHANNELS = [
  'can', 'accelerometer', 'gyroscope', 'gpsLocationExternal', 'pandaStates',
  'peripheralState', 'driverStateV2', 'driverMonitoringState',
  'wideRoadCameraState', 'narrowRoadCameraState',
  'carState', 'carControl', 'controlsState', 'selfdriveState',
  'modelV2', 'cameraOdometry', 'deviceMotion', 'extrinsicsCalibration',
  'longitudinalPlan', 'liveTorqueParameters' if 'liveTorqueParameters' in SERVICE_LIST else 'lateralTorqueParameters',
]
CHANNELS = [c for c in CHANNELS if c in SERVICE_LIST]

socks = {c: messaging.sub_sock(c, conflate=False, timeout=10) for c in CHANNELS}
counts = dict.fromkeys(CHANNELS, 0)
DURATION = 5.0
t0 = time.monotonic()
while time.monotonic() - t0 < DURATION:
  for c in CHANNELS:
    counts[c] += len(messaging.drain_sock(socks[c]))
el = time.monotonic() - t0

print(f"{'channel':26s} {'measured':>10s} {'expected':>10s}  status")
print("-" * 72)
dead = []
for c in CHANNELS:
  meas = counts[c] / el
  exp = SERVICE_LIST[c].frequency
  if meas == 0:
    status = "*** SILENT ***"
    dead.append(c)
  elif exp and not (0.7 < meas / exp < 1.4):
    status = f"rate off ({meas/exp:.2f}x)"
  else:
    status = "ok"
  print(f"{c:26s} {meas:9.1f}Hz {exp:9.1f}Hz  {status}")

print()
print(f"SILENT channels ({len(dead)}): {dead if dead else 'none'}")

print()
print("=" * 72)
print("STAGE 3: live VALUES (is the data real, or just zeros?)")
print("=" * 72)
# Rates alone can't distinguish "publishing real telemetry" from "publishing
# zeros on a timer" -- these are the fields that only move if the BeamNG mod
# is genuinely feeding beamngd, so they answer "are the daemons sending data"
# in the sense that actually matters. Drive/turn the car while this samples.
sm = messaging.SubMaster(['carState', 'carControl', 'selfdriveState', 'modelV2'])
seen = {'vEgo': [], 'steeringAngleDeg': [], 'cruiseSpeed': [], 'engaged': [], 'modelPathY': []}
t0 = time.monotonic()
while time.monotonic() - t0 < 5.0:
  sm.update(50)
  if sm.updated['carState']:
    seen['vEgo'].append(sm['carState'].vEgo)
    seen['steeringAngleDeg'].append(sm['carState'].steeringAngleDeg)
    seen['cruiseSpeed'].append(sm['carState'].cruiseState.speed)
  if sm.updated['selfdriveState']:
    seen['engaged'].append(bool(sm['selfdriveState'].active))
  if sm.updated['modelV2'] and len(sm['modelV2'].position.y):
    seen['modelPathY'].append(sm['modelV2'].position.y[-1])

if not seen['vEgo']:
  print("  no carState received at all -- card/beamngd not publishing")
else:
  for k in ('vEgo', 'steeringAngleDeg', 'cruiseSpeed', 'modelPathY'):
    v = seen[k]
    if not v:
      print(f"  {k:18s} NO DATA")
      continue
    lo, hi = min(v), max(v)
    moved = "varying (real data)" if abs(hi - lo) > 1e-6 else "CONSTANT (suspicious if you were driving)"
    print(f"  {k:18s} min={lo:+9.3f} max={hi:+9.3f}  n={len(v):4d}  {moved}")
  print(f"  {'engaged':18s} {any(seen['engaged'])} (any sample active)")
