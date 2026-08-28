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
import time

import openpilot.cereal.messaging as messaging
from openpilot.cereal.services import SERVICE_LIST

# Everything our bridge produces, plus the downstream state it drives.
INPUT_CHANNELS = [
  'can', 'accelerometer', 'gyroscope', 'gpsLocationExternal', 'pandaStates',
  'peripheralState', 'driverStateV2', 'driverMonitoringState',
  'wideRoadCameraState', 'narrowRoadCameraState',
]
STATE_CHANNELS = [
  'carState', 'carControl', 'controlsState', 'selfdriveState', 'carOutput',
  'modelV2', 'cameraOdometry', 'deviceMotion', 'extrinsicsCalibration', 'longitudinalPlan',
]
ALL = [c for c in INPUT_CHANNELS + STATE_CHANNELS if c in SERVICE_LIST]

GREEN, RED, YEL, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def fmt_rate(name, count, elapsed):
  meas = count / elapsed if elapsed > 0 else 0.0
  exp = SERVICE_LIST[name].frequency
  if meas == 0:
    return f"{RED}{meas:7.1f}Hz SILENT{RST}"
  if exp and not (0.7 < meas / exp < 1.4):
    return f"{YEL}{meas:7.1f}Hz (want {exp:.0f}){RST}"
  return f"{GREEN}{meas:7.1f}Hz{RST}"


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
  ap.add_argument("--window", type=float, default=2.0, help="seconds per rate sample")
  args = ap.parse_args()

  socks = {c: messaging.sub_sock(c, conflate=False, timeout=5) for c in ALL}
  sm = messaging.SubMaster(['carState', 'carControl', 'controlsState', 'selfdriveState',
                            'carOutput', 'modelV2', 'deviceMotion', 'extrinsicsCalibration'])

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
      if not args.once:
        out.append("\033[2J\033[H")  # clear screen, home cursor
      out.append("=" * 78)
      out.append(f" BEAMPILOT LIVE MONITOR   ({el:.1f}s sample)")
      out.append("=" * 78)

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
      out.append(f"  vEgo            {cs.vEgo:8.3f} m/s ({cs.vEgo * 2.237:6.2f} mph)   vEgoRaw {cs.vEgoRaw:8.3f}")
      out.append(f"  steeringAngle   {cs.steeringAngleDeg:+8.2f} deg      steeringRate {cs.steeringRateDeg:+8.2f}")
      out.append(f"  steeringTorque  {cs.steeringTorque:+8.2f}          steeringPressed {cs.steeringPressed}")
      out.append(f"  gasPressed {cs.gasPressed}   brakePressed {cs.brakePressed}   parkingBrake {cs.parkingBrake}")
      cspd = cs.cruiseState.speed
      out.append(f"  cruise: enabled={cs.cruiseState.enabled} available={cs.cruiseState.available} speed={cspd:6.2f} m/s ({cspd * 2.237:5.1f} mph)")
      out.append(f"  vCruise {cs.vCruise:6.2f} ({cs.vCruise * 2.237:5.1f} mph)   <- the SET SPEED openpilot targets")
      out.append(f"  standstill {cs.standstill}   gear {cs.gearShifter}   blinkers L={cs.leftBlinker} R={cs.rightBlinker}")
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
      if len(md.position.y):
        out.append(f"  predicted path y: near={md.position.y[0]:+7.3f}  far={md.position.y[-1]:+7.3f} m   points={len(md.position.y)}")
        out.append(f"  frameId {md.frameId}   frameDropPerc {md.frameDropPerc:.1f}%")
      else:
        out.append(f"  {RED}no model output{RST}")

      dm, ec = sm['deviceMotion'], sm['extrinsicsCalibration']
      out.append(f"\n{DIM}--- localization / calibration ---{RST}")
      out.append(f"  deviceMotion inputsOK={dm.inputsOK} sensorsOK={dm.sensorsOK} posenetOK={dm.posenetOK}")
      out.append(f"  calibration status={ec.calStatus} validBlocks={ec.validBlocks} rpy={[round(v, 4) for v in ec.rpyCalib]}")

      print("\n".join(out), flush=True)
      if args.once:
        return
  except KeyboardInterrupt:
    print("\nstopped")


if __name__ == "__main__":
  main()
