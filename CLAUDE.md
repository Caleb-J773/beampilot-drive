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
| `openpilot/selfdrive/beamcamd/window_capture.py` | X11 window detection; KWin/Wayland diagnosis (`python -m ...window_capture` prints a full report) |
| `openpilot/selfdrive/beamcamd/portal_capture.py` | Wayland capture: xdg-desktop-portal ScreenCast + PipeWire |
| `tools/beamng_mod/beampilot_bridge/lua/vehicle/protocols/beampilot.lua` | the BeamNG mod: telemetry out, control in |
| `tools/beamng_mod/openpilot_cam/lua/ge/.../openpilot.lua` | rigid, FOV-matched camera (25.70° vertical). **Required** — `beampilot.lua` selects it by name at spawn |
| `openpilot/tools/sim/lib/simulated_car.py` | fake Honda CAN packing (shared with MetaDrive bridge) |
| `openpilot/tools/sim/lib/simulated_sensors.py` | fake IMU/GPS/DM publishing (shared) |
| `config_beampilot.sh` | car fingerprint, GPU backend, UI size |
| `tools/beampilot_monitor.py` | **live** view of every channel + carState/control values |
| `tools/beampilot_diag.py` | one-shot data-flow diagnostic |

## Running

```bash
source .venv/bin/activate.fish     # fish shell -- NOT bin/activate
./launch_beampilot.sh              # BEAMPILOT_LAUNCH_DELAY=5 for a countdown first
```

Cruise buttons are read from the **physical keyboard** via evdev (BeamNG has focus, so this
must be a device-level listener): `i` = SET/decel, `o` = RES/accel, `u` = cancel.
Not `c`/`v`/`b` — those collide with BeamNG's own bindings (`c` cycles camera).

**Lane changes:** signal with `,` (left) / `.` (right) — BeamNG's own default bindings — while
engaged above 20 mph. Requires `BEAMPILOT_AUTO_LANE_CHANGE=1` (set in config): stock openpilot
also demands a steering-wheel nudge (`desire_helper.py`'s `torque_applied`), which can never
happen here since `beamngd` reports `user_torque = 0.0`, so a signalled change would arm into
`preLaneChange` and stall there forever.

## Driving limits

Stock openpilot follows EU/ISO passenger-comfort limits, which in a sim are usually what stops
it cornering or getting up to speed. All are env-tunable from `config_beampilot.sh`, and each
**defaults to the stock upstream value if unset** — an unset environment is unmodified openpilot.

| Env var | Stock | Controls |
|---|---|---|
| `BEAMPILOT_MAX_LAT_ACCEL` | 3.0 m/s² | turning. Max curvature is `accel / v²`, so stock allows only a ~300 m radius at 67 mph |
| `BEAMPILOT_MAX_LAT_JERK` | 5.0 m/s³ | how fast curvature may change. **Raise this cautiously — it's the most likely source of weaving/oscillation** |
| `BEAMPILOT_MAX_CURVATURE` | 0.2 1/m | geometric cap; only binds below ~11 mph |
| `BEAMPILOT_ACCEL_SCALE` | 1.0 | multiplier on the whole accel envelope (`A_CRUISE_MAX_VALS` *and* `ACCEL_MAX` — scaling only the first leaves `ACCEL_MAX` re-clamping it back down) |
| `BEAMPILOT_DECEL_SCALE` | 1.0 | same for braking |

These are *permission* limits, not commands: raising them lets openpilot turn harder, it doesn't
make it want to. If it still runs wide, suspect the geometry mismatch instead — openpilot
computes wheel angle from the **Civic's** steerRatio 15.38 / wheelbase 2.700 m, while the BeamNG
vehicle has its own (only its 510° lock has been measured).

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
- **`AlphaLongitudinalEnabled` must be set before the stack starts**, or honda's `interface.py`
  leaves `openpilotLongitudinalControl=False` / `pcmCruise=True` — openpilot steers but expects
  the *car's* ACC to manage speed, and BeamNG has none, so it never accelerates or holds speed.
- **Never report BeamNG's throttle/brake as the driver's pedal input while engaged.**
  `electrics.values.throttle` is our own `input.event` command echoed back; feeding it to
  `PEDAL_GAS` makes `CS.gasPressed` true, which trips `selfdrived.py`'s rising-edge
  `pedalPressed` check and disengages the instant openpilot tries to accelerate.
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
- **An all-green picture means the capture produced NO data — it is not a colour bug.** An
  untouched NV12 buffer is `Y=0, U=V=0`, which decodes to RGB(0,135,0); real black is `Y=16,
  U=V=128`. So green is the signature of an empty buffer, and the usual cause is the X11 backend
  on Wayland, where a root grab returns nothing. Don't debug the conversion; check the backend.
  `_warn_if_blank()` now detects a uniform frame and says so.
- **Wayland cannot be captured with X11 calls, and detecting the window proves nothing.** BeamNG
  is normally an XWayland client, so `xdotool` finds it and the region looks right — but pixels
  belong to the compositor, so the grab still returns nothing. Detection and capture are separate
  problems. Wayland capture goes through `portal_capture.py`; `BEAMPILOT_CAPTURE_BACKEND=auto`
  selects it on Wayland and leaves X11 sessions on the X11 grab.
- **KWin's window geometry is NOT a valid capture region.** It is the compositor's logical
  coordinate space and does not map onto the X root that mss grabs. `window_capture.py` queries
  KWin only to *explain* a failure, never to pick a rectangle. Wiring it into tracking would
  capture the wrong pixels while looking like it worked.
- **A capture rectangle must be clamped to the X root or `beamcamd` dies.** X11 `GetImage` raises
  BadMatch (error 8, opcode 73) if any part of the region is off-screen, and mss surfaces it as an
  uncaught `XProtoError`. beamcamd exits → camerad's VisionIPC streams vanish → `modeld`, which
  blocks in `available_streams()`, never starts → `process_not_running: modeld` and nothing else
  obviously wrong. A monitor whose edge is flush with the virtual screen leaves zero slack, so
  this is easy to hit.
- **tinygrad's NV/AMD backends are not CUDA/ROCm** — raw ioctls, Ampere-or-newer only (`NV`), or
  gfx942/gfx950/gfx11xx/gfx12xx (`AMD`). An older card doesn't run slowly, it dies at model load
  with a bare `StopIteration` in `ops_nv.py:440`. On a mixed-GPU box tinygrad defaults to index 0,
  which may be the unusable card. `config_beampilot.sh` detects the first usable one and sets
  `DEV=":<index>+NV"` — index BEFORE the `+`. Detected, not hardcoded: PCI enumeration shifts when
  cards move.
- **openpilot's `Ratekeeper` never resyncs**, so one long frame makes it sleep zero until it has
  caught up — a 285 ms hiccup emits ~6 frames ~13 ms apart and modeld sees the world lurch then
  stall. `beamcamd` uses `FramePacer`, which drops a backlog it can no longer deliver on time.
- **Don't run `find_window()` on a timer.** It shells out to pgrep/xdotool/xprop per candidate:
  68 ms+ measured, against a 50 ms frame budget, so a 2 s retrack guaranteed a dropped frame. Use
  `window_geometry()` on the known id (~0.7 ms) and only rediscover when the window disappears.
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
- **This machine has two NVIDIA cards**: a GTX 1660 SUPER (Turing, 7.5) at index 0 and the RTX
  3060 (Ampere, 8.6) at index 1. Only the 3060 can run the model, so `DEV=:1+NV` — auto-detected,
  and printed at launch as `[beampilot] tinygrad NV -> GPU 1`. The AMD iGPU is gfx1036, which
  tinygrad does not support, so `USE_AMD=1` is not an option here.
- `BEAMPILOT_CAPTURE_BACKEND` = `auto` | `x11` | `portal`. The portal backend also works on X11
  and measures smoother there (50.00 ms mean / 51.28 max vs 49.96 / 66.81 over 300 frames, since
  the frame is already in memory rather than the loop blocking on the X server) and skips window
  detection entirely — but it needs `gst-launch-1.0` + a desktop portal, and one dialog click, so
  it is not the X11 default. The portal's source choice is remembered in
  `~/.local/state/beampilot/screencast_restore_token`; delete it to get the picker back.
- **Testing status:** the portal path is verified end-to-end on X11/GNOME here and confirmed
  working on KDE Wayland by a user. The KWin *detection* path has only ever been exercised against
  a simulated compositor (`test_window_capture.py`) — no Plasma on this machine.

## Control mode (optional)

`BEAMPILOT_CONTROL_MODE` selects how control reaches the game:

- `lua` (**default, what we use**) — UDP → mod → `input.event()`. No in-game setup.
- `joystick` — a `uinput` virtual wheel (`virtual_joystick.py`); requires manually binding axes
  in BeamNG's Options > Controls. Built as a fallback, currently unused. This is the approach
  jackz314's ETS2/ATS bridge uses, because ETS2 has no scriptable input hook — BeamNG does.

## The wide camera problem

openpilot expects two road cameras: a narrow one and a ~120° wide one. `beamcamd` has only the
single BeamNG view, so it publishes **the same frame to both streams**. `modeld` computes
`has_wide_camera = use_extra_client or main_wide_camera`, which is `True` here, so it applies
`dc.wide_road.intrinsics` — the calibration for a wide lens — to an image that does not have
that field of view.

Consequences:
- The model misjudges how far objects and lane lines sit laterally, worst **in turns**, where
  the real wide camera is what sees around the corner.
- **Keep Experimental mode OFF.** Its end-to-end longitudinal policy leans much harder on
  wide-camera scene understanding and degrades noticeably here.

Proper fix: a second BeamNG camera at a genuinely wide FOV, published to
`VISION_STREAM_WIDE_ROAD` separately. The `openpilot_cam` mod already shows how to register a
rigidly-mounted, FOV-matched camera; a second one plus a second capture region would do it.

## Vehicles

Tested working: **ETK series** (best — the camera framing was tuned on these), **Bastion**,
**SBR4**, **Sunburst**.

`openpilot_cam`'s offsets are fixed, not per-vehicle, so outside the ETK series less of the hood
is visible and the view clips slightly. It still drives, but the model gets less of the visual
context it was trained on — suspect framing first when a particular car behaves badly, and tune
`offUp`/`offFwd` in the camera mod.

Steering lock is per-vehicle: set `BEAMPILOT_STEER_LOCK_DEG` when switching cars (hold full lock,
read `steering_wheel_deg` in the monitor).

The camera also lands **off-centre** on some cars: it's placed relative to `veh:getPosition()`,
the jbeam reference node, which isn't reliably on the centreline. Drives well regardless — lane
position comes from the whole scene, not from the lens being at one exact spot — so this is a
real imperfection that is usually *not* the cause of a given problem. `offRight` corrects it if a
car tracks consistently to one side.

BeamNG's stock dashboard camera can be used instead, but performs worse and carries heavy motion
blur (disable it in graphics settings). `openpilot_cam` exists precisely because openpilot assumes
a rigid lens with fixed intrinsics — head bob, look-ahead yaw, horizon stabilisation and FOV
smoothing all violate that, and it has none of them.

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
