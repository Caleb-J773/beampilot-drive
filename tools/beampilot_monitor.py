#!/usr/bin/env python3
"""Live monitor of everything beampilot feeds into openpilot.

Shows, refreshing in place:
  - the raw BeamNG mod telemetry (only when beamngd is stopped -- it owns the port)
  - every sensor/CAN channel beamngd + beamcamd publish, with live rates
  - carState: what openpilot ACTUALLY sees after decoding our CAN
  - carControl / controlsState / selfdriveState: what openpilot decides to do
  - modelV2: what the driving model predicts from beamcamd's frames

Usage:
  uv run python tools/beampilot_monitor.py          # live, refreshes until Ctrl+C
  uv run python tools/beampilot_monitor.py --once   # one snapshot, for pasting
"""
import argparse
import re
import shutil
import sys
import time

import openpilot.cereal.messaging as messaging
from openpilot.cereal.services import SERVICE_LIST
from openpilot.common.beampilot_env import env_float
from openpilot.selfdrive.controls.lib.drive_helpers import MAX_CURVATURE, MAX_LATERAL_ACCEL_NO_ROLL, MIN_SPEED

# Everything our bridge produces, plus the downstream state it drives.
INPUT_CHANNELS = [
  'can', 'accelerometer', 'gyroscope', 'gpsLocationExternal', 'pandaStates',
  'peripheralState', 'driverStateV2', 'driverMonitoringState',
  'wideRoadCameraState', 'narrowRoadCameraState',
]
STATE_CHANNELS = [
  'carState', 'carControl', 'controlsState', 'selfdriveState', 'carOutput',
  'modelV2', 'radarState', 'cameraOdometry', 'deviceMotion', 'extrinsicsCalibration',
  'longitudinalPlan',
]
ALL = [c for c in INPUT_CHANNELS + STATE_CHANNELS if c in SERVICE_LIST]

WIDTH = 78                      # header rule width; output is clipped to the real terminal width
WINDOW_SECONDS = env_float("BEAMPILOT_MONITOR_WINDOW", 2.0)
# A channel is flagged when its measured rate falls outside this band around the
# rate SERVICE_LIST expects. Wide enough not to cry over normal jitter.
RATE_OK_LOW = env_float("BEAMPILOT_MONITOR_RATE_LOW", 0.7)
RATE_OK_HIGH = env_float("BEAMPILOT_MONITOR_RATE_HIGH", 1.4)
MS_PER_S = 2.237                # m/s -> mph
KPH_TO_MPH = 0.621371

GREEN, RED, YEL, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
ANSI_RE = re.compile(r"\033\[[0-9;?]*[a-zA-Z]")


def fmt_rate(name, count, elapsed):
  meas = count / elapsed if elapsed > 0 else 0.0
  exp = SERVICE_LIST[name].frequency
  if meas == 0:
    return f"{RED}{meas:7.1f}Hz SILENT{RST}"
  if exp and not (RATE_OK_LOW < meas / exp < RATE_OK_HIGH):
    return f"{YEL}{meas:7.1f}Hz (want {exp:.0f}){RST}"
  return f"{GREEN}{meas:7.1f}Hz{RST}"


def visible_len(s: str) -> int:
  """Length ignoring ANSI colour codes, so padding lines up."""
  return len(ANSI_RE.sub("", s))


def draw(lines: list[str]) -> None:
  """Repaint in place without scrolling.

  A plain "clear screen then print" (\\033[2J) only clears the VISIBLE screen.
  As soon as the output is taller than the terminal, the terminal scrolls and
  every refresh shoves another screenful into the scrollback -- which is how you
  end up with a thousand copies. Instead: home the cursor, overwrite each line
  and erase the rest of it (\\033[K), truncate to the window height so nothing
  can ever scroll, then erase everything below (\\033[J).
  """
  cols, rows = shutil.get_terminal_size((80, 24))
  # Section headers embed a leading "\n", so one list entry can be two terminal
  # rows. Flatten first or the height truncation undercounts and the display
  # scrolls anyway -- the exact thing this function exists to prevent.
  flat: list[str] = []
  for entry in lines:
    flat.extend(entry.split("\n"))
  body = flat[:max(rows - 1, 1)]
  buf = ["\033[H"]
  for line in body:
    # trim to the terminal width so a long line can't wrap and push everything down
    while visible_len(line) > cols:
      line = line[:-1]
    buf.append(line + "\033[K\n")
  buf.append("\033[J")
  sys.stdout.write("".join(buf))
  sys.stdout.flush()


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
  ap.add_argument("--window", type=float, default=WINDOW_SECONDS,
                  help=f"seconds per rate sample (default {WINDOW_SECONDS})")
  args = ap.parse_args()

  socks = {c: messaging.sub_sock(c, conflate=False, timeout=5) for c in ALL}
  sm = messaging.SubMaster(['carState', 'carControl', 'controlsState', 'selfdriveState',
                            'carOutput', 'modelV2', 'radarState', 'deviceMotion',
                            'extrinsicsCalibration'])

  try:
    while True:
      counts = dict.fromkeys(ALL, 0)
      t0 = time.monotonic()
      while time.monotonic() - t0 < args.window:
        for c in ALL:
          counts[c] += len(messaging.drain_sock(socks[c]))
        sm.update(0)
        time.sleep(0.005)
      el = time.monotonic() - t0

      out = []
      out.append("=" * WIDTH)
      out.append(f" BEAMPILOT LIVE MONITOR   ({el:.1f}s sample)")
      out.append("=" * WIDTH)

      out.append(f"\n{DIM}--- INPUTS: what beamngd + beamcamd send INTO openpilot ---{RST}")
      for c in INPUT_CHANNELS:
        if c in ALL:
          out.append(f"  {c:24s} {fmt_rate(c, counts[c], el)}")

      out.append(f"\n{DIM}--- DOWNSTREAM: what openpilot produces from them ---{RST}")
      for c in STATE_CHANNELS:
        if c in ALL:
          out.append(f"  {c:24s} {fmt_rate(c, counts[c], el)}")

      cs = sm['carState']
      out.append(f"\n{DIM}--- carState (what openpilot sees after decoding our CAN) ---{RST}")
      out.append(f"  vEgo            {cs.vEgo:8.3f} m/s ({cs.vEgo * MS_PER_S:6.2f} mph)   vEgoRaw {cs.vEgoRaw:8.3f}")
      out.append(f"  steeringAngle   {cs.steeringAngleDeg:+8.2f} deg      steeringRate {cs.steeringRateDeg:+8.2f}")
      out.append(f"  steeringTorque  {cs.steeringTorque:+8.2f}          steeringPressed {cs.steeringPressed}")
      out.append(f"  gasPressed {cs.gasPressed}   brakePressed {cs.brakePressed}   parkingBrake {cs.parkingBrake}")
      cspd = cs.cruiseState.speed
      out.append(f"  cruise: enabled={cs.cruiseState.enabled} available={cs.cruiseState.available} speed={cspd:6.2f} m/s ({cspd * MS_PER_S:5.1f} mph)")
      # vCruise is KM/H (cruise.py assigns v_cruise_kph straight into it), not
      # m/s like every other speed on this screen. Multiplying it by 2.237
      # printed a 30m/s set speed as "241 mph", which is the entry that sat in
      # the README's known problems as a broken set speed. The car was fine.
      out.append(f"  vCruise {cs.vCruise:6.2f} km/h ({cs.vCruise * KPH_TO_MPH:5.1f} mph)   <- the SET SPEED openpilot targets")
      out.append(f"  standstill {cs.standstill}   gear {cs.gearShifter}   blinkers L={cs.leftBlinker} R={cs.rightBlinker}")
      out.append(f"  parkingBrake {cs.parkingBrake}   steeringRate {cs.steeringRateDeg:+7.1f} deg/s")
      # BSM. Both clear is also what you see when the feed is dead (an old mod,
      # or BEAMPILOT_BSM off), so treat "never lights up" as a reason to check
      # the mod version rather than proof the road is empty.
      bsm_l = f"{RED}OCCUPIED{RST}" if cs.leftBlindspot else f"{GREEN}clear{RST}"
      bsm_r = f"{RED}OCCUPIED{RST}" if cs.rightBlindspot else f"{GREEN}clear{RST}"
      blocked = " <- lane change into it is blocked" if (cs.leftBlindspot or cs.rightBlindspot) else ""
      out.append(f"  blind spot: L={bsm_l} R={bsm_r}{blocked}")
      out.append(f"  canValid {cs.canValid}   canTimeout {cs.canTimeout}")

      ss = sm['selfdriveState']
      out.append(f"\n{DIM}--- selfdriveState (is it driving?) ---{RST}")
      out.append(f"  active={ss.active}  enabled={ss.enabled}  engageable={ss.engageable}  state={ss.state}")
      if ss.alertText1 or ss.alertText2:
        out.append(f"  {YEL}ALERT: {ss.alertText1} | {ss.alertText2}{RST}")

      cc, co, ctl = sm['carControl'], sm['carOutput'], sm['controlsState']
      out.append(f"\n{DIM}--- control output (what openpilot commands) ---{RST}")
      out.append(f"  latActive={cc.latActive} longActive={cc.longActive}")
      out.append(f"  actuators: torque={cc.actuators.torque:+7.3f}  steerAngle={cc.actuators.steeringAngleDeg:+8.2f}  accel={cc.actuators.accel:+6.2f}")
      out.append(f"  actuatorsOutput.torque {co.actuatorsOutput.torque:+7.3f}   (safety rate-limited)")
      out.append(f"  desiredCurvature {ctl.desiredCurvature:+9.5f} 1/m")

      md = sm['modelV2']
      out.append(f"\n{DIM}--- modelV2 (what the driving model sees from beamcamd) ---{RST}")
      # The lane change state machine, so a change that refuses to start or gets
      # cancelled shows a reason instead of just not happening. "preLaneChange"
      # while a blind spot is occupied IS the block -- see desire_helper.py.
      lc_state, lc_dir = str(md.meta.laneChangeState), str(md.meta.laneChangeDirection)
      side = {"left": cs.leftBlindspot, "right": cs.rightBlindspot}.get(lc_dir, False)
      note = f"  {RED}<- blocked by the blind spot{RST}" if (side and lc_state == "preLaneChange") else ""
      out.append(f"  lane change: {lc_state} {lc_dir}{note}")
      if len(md.position.y):
        out.append(f"  predicted path y: near={md.position.y[0]:+7.3f}  far={md.position.y[-1]:+7.3f} m   points={len(md.position.y)}")
        out.append(f"  frameId {md.frameId}   frameDropPerc {md.frameDropPerc:.1f}%")
      else:
        out.append(f"  {RED}no model output{RST}")

      # Is the lateral-accel limit actually BINDING, or is the model asking for
      # less than the ceiling anyway? clip_curvature() caps curvature at
      # MAX_LAT_ACCEL/v^2, but that only changes behavior when the model's own
      # request exceeds it -- otherwise raising the limit does nothing at all.
      # This is the check that tells you whether the limit is the real
      # constraint or whether the car runs wide for some other reason.
      out.append(f"\n{DIM}--- is the turn limit actually binding? ---{RST}")
      requested = md.action.desiredCurvature
      v = max(cs.vEgo, MIN_SPEED)
      cap = min(MAX_LATERAL_ACCEL_NO_ROLL / v**2, MAX_CURVATURE)
      binding = abs(requested) > cap + 1e-6
      mark = f"{YEL}BINDING -- raising the limit helps{RST}" if binding else "not binding -- limit is NOT the constraint"
      out.append(f"  model wants  {requested:+9.5f} 1/m")
      out.append(f"  cap now      {cap:+9.5f} 1/m  ({1 / cap:6.0f} m radius)  [lat accel {MAX_LATERAL_ACCEL_NO_ROLL} m/s^2]")
      out.append(f"  after clip   {ctl.desiredCurvature:+9.5f} 1/m  {mark}")

      # The lead car. On beampilot this comes from the BeamNG mod's ground-truth
      # radar (radar=True) rather than from the camera (radar=False) -- which
      # is the whole point, since the camera is fed the wrong intrinsics.
      lead = sm['radarState'].leadOne
      out.append(f"\n{DIM}--- lead car (radarState.leadOne) ---{RST}")
      if lead.present:
        source = f"{GREEN}ground-truth radar{RST}" if lead.radar else f"{YEL}camera only{RST}"
        out.append(f"  dRel {lead.dRel:6.2f} m   vRel {lead.vRel:+6.2f} m/s   vLead {lead.vLead:6.2f} m/s "
                   + f"({lead.vLead * MS_PER_S:5.1f} mph)")
        out.append(f"  aLeadK {lead.aLeadK:+6.2f} m/s^2   modelProb {lead.modelProb:.2f}   source: {source}")
      else:
        out.append("  no lead")

      dm, ec = sm['deviceMotion'], sm['extrinsicsCalibration']
      out.append(f"\n{DIM}--- localization / calibration ---{RST}")
      out.append(f"  deviceMotion inputsOK={dm.inputsOK} sensorsOK={dm.sensorsOK} posenetOK={dm.posenetOK}")
      out.append(f"  calibration status={ec.calStatus} validBlocks={ec.validBlocks} rpy={[round(v, 4) for v in ec.rpyCalib]}")

      if args.once:
        print("\n".join(out), flush=True)
        return
      draw(out)
  except KeyboardInterrupt:
    print("\nstopped")


if __name__ == "__main__":
  main()
