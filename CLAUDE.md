# beampilot

openpilot fork that drives a car in **BeamNG.drive** (the consumer Steam game).

> **Not BeamNG.tech.** We do not have it, so `beamngpy` (the .tech Python API) is not an
> option. Everything works through mechanisms BeamNG.drive exposes to ordinary mods: a Lua
> protocol file for telemetry/control, and screen capture for the camera.

## Architecture

Stock openpilot processes run **unmodified**. Only two daemons are ours, plus one BeamNG mod:

```
BeamNG.drive
  │  (Lua mod: tools/beamng_mod/beampilot_bridge)
  │  UDP 49152  telemetry out  ──────────┐
  │  UDP 49153  control in     ◄───────┐ │
  ▼                                    │ │
screen  ──► beamcamd.py ──► VisionIPC ─┼─┼──► modeld ──► modelV2
                                       │ │                  │
            beamngd.py ────────────────┘ │             plannerd/controlsd
              ▲  fake CAN/IMU/GPS        │                  │
              └───────────────────────────┘         controlsState.desiredCurvature
```

### Steering chain (end to end)

1. `beamcamd.py` grabs the BeamNG window (mss) → NV12 → VisionIPC @20Hz
2. `modeld` (stock) → `modelV2` predicted path
3. `plannerd`/`controlsd` (stock) → `controlsState.desiredCurvature` (1/m)
4. `beamngd.py:send_control()` — **our translation layer**:
   - `VehicleModel.get_steer_from_curvature(curvature, speed, 0.0)` → wheel angle (rad)
   - → degrees → `/ MAX_STEERING_WHEEL_ANGLE_DEG (510.0)` → normalized `-1..1`
   - rate-limited, then sent as JSON over UDP to `127.0.0.1:49153`
5. Lua mod `pollControl()` → `input.event("steering", v, FILTER_DIRECT)`
   (the same function BeamNG's own AI driver uses)

CAN only flows **into** openpilot (fake sensors), never back into the game.

## Key files

| Path | Role |
|---|---|
| `openpilot/selfdrive/beamngd/beamngd.py` | bridge: telemetry in, fake CAN/IMU/GPS out, control out |
| `openpilot/selfdrive/beamcamd/beamcamd.py` | screen capture → VisionIPC camera frames |
| `tools/beamng_mod/beampilot_bridge/lua/vehicle/protocols/beampilot.lua` | the BeamNG mod |
| `openpilot/tools/sim/lib/simulated_car.py` | fake Honda CAN packing (shared with MetaDrive bridge) |
| `openpilot/tools/sim/lib/simulated_sensors.py` | fake IMU/GPS/DM publishing (shared) |
| `config_beampilot.sh` | car fingerprint, GPU backend, UI size |
| `tools/beampilot_monitor.py` | **live** view of every channel + carState/control values |
| `tools/beampilot_diag.py` | one-shot data-flow diagnostic |

## Running

```bash
cd /home/caleb/Pictures/beampilot
source .venv/bin/activate.fish     # fish shell -- NOT bin/activate
./launch_beampilot.sh              # 5s countdown to tab into BeamNG
```

Cruise buttons are read from the **physical keyboard** via evdev (BeamNG has focus, so this
must be a device-level listener): `i` = SET/decel, `o` = RES/accel, `u` = cancel.
Not `c`/`v`/`b` — those collide with BeamNG's own bindings (`c` cycles camera).

Debug while running, in a second terminal:
```bash
uv run python tools/beampilot_monitor.py    # live, Ctrl+C to stop
```

## Hard-won gotchas

These were each a real bug that cost significant debugging time. Don't regress them.

- **`set_params_enabled()` must run before the stack starts** (`launch_beampilot.sh`). It seeds
  `CalibrationParams` (validBlocks=20, rpy=[0,0,0]). Without it `calibrationd` needs *minutes* of
  sustained 15+mph straight driving to converge, and until then the model's road-frame transform
  is wrong — which manifests as biased/drifting steering, not as an obvious error.
- **`SimulatedSensors` helpers emit multi-sample bursts** (5x IMU, 10x GPS) sized for a ~20Hz
  caller. `beamngd` ticks at 100Hz, so it must pass `count=1` and rate-limit GPS to 10Hz.
  Before this was fixed: GPS published at **1000Hz (100x over spec)**, IMU at 5x.
- **This car is Bosch.** `cruiseState.speed` comes from `ACC_HUD.CRUISE_SPEED` (camera bus),
  **not** `CRUISE.CRUISE_SPEED_PCM` — that field is only read for non-Bosch Hondas.
  See `opendbc/car/honda/carstate.py`, the `if self.CP.flags & HondaFlags.BOSCH:` branch.
- **`pcmCruise=True`** for this fingerprint: openpilot waits for the *car* to report ACC on
  before engaging. So `ACC_STATUS` must be driven by the cruise button directly
  (`self.acc_engaged`), never from `selfdriveState.active` — that's a deadlock.
- **`commIssue` is NOT cosmetic.** It's registered as both `NO_ENTRY` and `SOFT_DISABLE`
  (`selfdrived/events.py`) — it blocks engagement *and* disengages mid-drive. Wrong publish
  rates cause it. Treat those log lines as real.
- **Use `electrics.values.steering`** (real post-dynamics wheel angle, true degrees) as the
  feedback signal — **not** `steering_input`, which is just an echo of the last command and
  feeds the PID its own output back.
- **`releaseControl()` must only fire on the disengage edge** (`isControlling` flag). Firing it
  every not-engaged tick fights and beats the player's own WASD input.
- **Lua locals must be declared above every function that references them**, or Lua silently
  creates an implicit global instead of using the closure variable.
- **`launch_beampilot.sh` needs its shebang.** Without it, fish's ENOEXEC fallback runs it under
  `dash`, `source` fails silently, and the whole config is quietly lost.
- **`beamcamd` timestamps must be `time.monotonic_ns()`**, not a synthetic frame counter.
  `locationd._validate_timestamp` rejects fake ones on every frame, permanently.
  (openpilot's own `system/camerad/webcam/camerad.py` has this same bug, unfixed — it's just
  never exercised there.)

## Environment notes

- `config_beampilot.sh`: `USE_NV=1` (AMD iGPU compute needs the user in `render` group),
  `CHESTNUT=0` (setting 1 forces `DEV=AMD` regardless of `USE_NV`), `BIG=1`
  (this is a *resolution* switch, not a UI scale knob — use `SCALE` for that).
- `SKIP_FW_QUERY=1` + `FINGERPRINT=HONDA_CIVIC_2022` — the car identity is set here, not
  fingerprinted from CAN. Changing it requires matching `beamngd`/`beamcamd` updates.

## Control mode (optional)

`BEAMPILOT_CONTROL_MODE` selects how control reaches the game:

- `lua` (**default, what we use**) — UDP → mod → `input.event()`. No in-game setup.
- `joystick` — a `uinput` virtual wheel (`virtual_joystick.py`); requires manually binding axes
  in BeamNG's Options > Controls. Built as a fallback, currently unused. This is the approach
  jackz314's ETS2/ATS bridge uses, because ETS2 has no scriptable input hook — BeamNG does.

## Known outstanding issues

- `carState.vCruise` reads ~108 (241 mph) while `cruiseState.speed` is correct — set-speed
  target is wrong.
- `gasPressed` reads True when the throttle isn't touched (BeamNG throttle → `PEDAL_GAS`),
  which openpilot treats as a driver override.
- `beamcamd` shows ~6% frame drop; `driverStateV2`/`driverMonitoringState` run 33Hz vs 20 expected.

## Reference

Prior art: [jackz314/openpilot](https://github.com/jackz314/openpilot) `tools/truck_sim` — an
ETS2/ATS bridge on openpilot **0.8.6**. Its telemetry path (SCS SDK shared memory) has no BeamNG
equivalent, and its steering is hand-tuned constants rather than `VehicleModel`, so little of it
transfers. Useful mainly as a sanity check on overall structure.
