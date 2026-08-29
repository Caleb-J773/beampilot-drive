# beampilot

openpilot fork that drives a car in **BeamNG.drive** (the consumer Steam game).

> **Not BeamNG.tech.** We do not have it, so `beamngpy` (the .tech Python API) is not an
> option. Everything works through mechanisms BeamNG.drive exposes to ordinary mods: a Lua
> protocol file for telemetry/control, and screen capture for the camera.

## Architecture

Two daemons are ours, plus one BeamNG mod. Stock openpilot processes are **almost** unmodified:
these carry small, clearly-marked beampilot patches, all env-gated. `grep -rn BEAMPILOT_
openpilot/` finds them all. Every one defaults to upstream behaviour with an unset environment
**except** the blind spot lane change abort, which defaults on — and that one is still inert
upstream, because it only ever fires on `carState.leftBlindspot`/`rightBlindspot` and no car this
fork simulates has BSM messages in its DBC.

| File | Patch |
|---|---|
| `selfdrive/controls/lib/desire_helper.py` | `BEAMPILOT_AUTO_LANE_CHANGE` — commit on the blinker alone; `BEAMPILOT_LANE_CHANGE_ABORT` — cancel one in flight on a blind spot |
| `selfdrive/controls/lib/drive_helpers.py` | `BEAMPILOT_MAX_LAT_ACCEL` / `_JERK` / `MAX_CURVATURE` |
| `selfdrive/controls/lib/longitudinal_planner.py` | `BEAMPILOT_ACCEL_SCALE` / `_DECEL_SCALE` |
| `selfdrive/selfdrived/selfdrived.py` | `BEAMPILOT_IGNORE_COMM_ISSUE` |
| `selfdrive/car/card.py` | `BEAMPILOT_BSM` — overlay blind spot onto `carState`; `BEAMPILOT_RADAR` — fill in the empty `RadarData` (see gotchas) |
| `selfdrive/controls/radard.py` | `BEAMPILOT_RADAR_LEADS` — let a ground-truth track be the lead with no vision confirmation |
| `selfdrive/selfdrived/selfdrived.py` | the "Lane Change Cancelled" alert, added straight to the `AlertManager` |
| `selfdrive/ui/onroad/augmented_road_view.py` | renders `BlindSpotRenderer` at the file's own "custom UI extension point" |
| `system/manager/process_config.py` | adds `beamngd`/`beamcamd`, drops the hardware-only processes |

```
BeamNG.drive
  │  (Lua mod: tools/beamng_mod/beampilot_bridge)
  │  UDP 49152  telemetry out  ──────────┐   (+ blind spot flags in dashLights)
  │  UDP 49153  control in     ◄───────┐ │   (+ BSM tuning, re-sent every 2s)
  ▼                                    │ │
screen  ──► beamcamd.py ──► VisionIPC ─┼─┼──► modeld ──► modelV2
                                       │ │                  │
            beamngd.py ────────────────┘ │             plannerd/controlsd
              ▲  fake CAN/IMU/GPS        │                  │
              │  UDP 49154 ──► card ─────┼──► carState.leftBlindspot/rightBlindspot
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
| `openpilot/common/beampilot_limits.py` | how hard the car may be driven: accel/lateral limits, the combined envelope, and the excessive-actuation trip points, all scaling together |
| `openpilot/common/beampilot_bsm.py` | BSM wire format + the `beamngd`→`card` socket, and the tuning pushed down to the mod |
| `openpilot/common/beampilot_radar.py` | radar wire format + the `mod`→`card` socket (one hop; no relay) |
| `openpilot/selfdrive/beamngd/test_bsm.py` | BSM unit tests (`uv run python ...`) |
| `openpilot/selfdrive/controls/lib/test_desire_helper_bsm.py` | lane change state machine: refusing to start, and cancelling in flight |
| `openpilot/selfdrive/beamngd/test_radar.py` | radar wire format, receiver, and a cross-language check against the real Lua encoder |
| `openpilot/selfdrive/beamngd/test_carstate_signals.py` | gear / parking brake / steering rate round-tripped through the Honda DBC |
| `openpilot/selfdrive/controls/test_radard_beampilot.py` | radar-only lead selection, including the in-lane test on a bend |
| `openpilot/selfdrive/ui/onroad/blindspot_renderer.py` | the onroad mirror lamps (amber chevrons, steady/flashing; OFF by default) |
| `openpilot/selfdrive/ui/onroad/radar_renderer.py` | radar tracks drawn on the road, ringed on the lead |
| `tools/beamng_mod/test_beampilot_bsm.lua` | BSM zone geometry tests against BeamNG's real `mathlib` (`luajit ...`) |
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

BSM gates both ends of that: a change will not start into an occupied lane (stock behaviour,
newly reachable), and one already under way is cancelled if the lane fills up within
`BEAMPILOT_LANE_CHANGE_ABORT_S` (beampilot's own addition). A cancel returns to `preLaneChange`
with the blinker still armed, so it resumes on its own once the lane clears.

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
  creates an implicit global instead of using the closure variable. `luajit -bl <file> | grep
  GSET` catches this: a clean file writes no globals at all.
- **radarTracks must go through `card`, not alongside it.** `RadarInterfaceBase.update()` returns
  an EMPTY `RadarData` every 5th frame, so card is *already* publishing `radarTracks` at 20Hz.
  msgq permits a second publisher, so a `PubMaster` in `beamngd` binds happily -- and then
  radard sees our points and card's empties alternating, and the lead flickers. Fill in the
  RadarData card is already building instead.
- **`radard.get_lead()` ignores radar unless the CAMERA already sees a lead** (`lead_prob > .5`;
  the only exception is `potential_low_speed_lead`, below `V_EGO_STATIONARY`). Ground truth can
  therefore only REFINE by default, which is useless for the case the wide-camera problem
  causes. `BEAMPILOT_RADAR_LEADS` lifts it; the in-lane test uses `modelV2.position` so it
  follows a bend instead of meaning "straight ahead".
- **A radar-only lead has to report `modelProb = 1.0`.** `long_mpc.py` gates its forward
  collision check on `modelProb > 0.9`, so passing the camera's opinion (0.0) would silently
  disable FCW on exactly the leads the camera missed. `process_lead` itself only checks
  `present`, so following and braking work either way.
- **`yRel` is LEFT positive** (`car.capnp`: "m in car frame, left positive"), while
  `modelV2.position`/`leadsV3` are in the device frame, which is **y-RIGHT**. radard bridges the
  two with `-lead.y[0]`; anything producing radar points has to do the same.
- **BSM cannot go in over the fake CAN.** `HONDA_CIVIC_2022`'s DBC dict has no `Bus.body` entry,
  so `honda/carstate.py` never builds the body parser that `BSM_STATUS_LEFT`/`_RIGHT` live in.
  Adding one means editing `opendbc`, which is a **git submodule** pointing at `commaai/opendbc`
  — those edits cannot be committed here, so it would work on one machine and nowhere else.
  Hence the loopback UDP hop into `card.state_update()`.
- **BSM rides in spare `dashLights` bits (4..7), not in new telemetry struct fields.**
  `parse_telemetry()` rejects any packet whose length is not an exact match, so growing the
  struct turns "old mod, no BSM" into "old mod, no telemetry at all".
- **A stale BSM feed must fail to *clear*, not to *blocked*.** A latched warning blocks every
  lane change for the rest of the drive with nothing on screen to explain it. 0.5s timeout.
- **Raising a driving limit means raising THREE things.** `long_mpc.py` bounded its solution with
  opendbc's unscaled `ACCEL_MAX` and the planner takes `min(mpc, cruise)`, so the scale was
  mostly ignored; `_A_TOTAL_MAX_V` (the combined lat+long envelope) was unscaled, so raising the
  lateral limit left nothing for the throttle mid-corner; and `ExcessiveActuationCheck`
  soft-disables on MEASURED actuation against stock trip points, so past those you get a
  disengagement rather than performance. All in `common/beampilot_limits.py` now.
- **`get_current_monitor()` on a probe window is not deterministic.** The UI's auto-scale opened
  a 1x1 window to ask which screen it was on; with two monitors that answer changed between
  runs, and half the time it sized the window for the wrong one. Fit the SMALLEST monitor.
  Auto-scale also has to work in BOTH directions -- BIG=0 is a 536x240 base, which needs
  growing, not shrinking.
- **The capture rectangle's ASPECT has to be 1928/1208 (1.5960), not just any rectangle.**
  `FrameEncoder.encode` resizes straight to the frame size with no aspect preservation, so a 16:9
  window is squeezed ~11% horizontally: vertically right (the mod renders 25.70 deg VERTICAL) but
  spanning 44.15 deg where the intrinsics claim 40.01. Depends on the window's shape, not its
  size -- 1440p is exactly as wrong as 1080p. `BEAMPILOT_CAM_ASPECT=crop` trims the sides;
  cropping top/bottom instead would cut the vertical field and is never done. The real fix is a
  1928x1208 window: exact aspect, no crop, no resample.
- **The BSM flag needs a hold, not just a raw read** (`BSM_HOLD_SECONDS`, 0.4s). A vehicle
  hovering on the zone boundary chatters at the scan rate, and since `desire_helper.py` cancels
  an in-flight lane change on this flag, chatter would abort the manoeuvre and instantly restart
  it. Staleness still wins over the hold.
- **There is no way to add an `EventName`.** The enum lives in `opendbc/car/car.capnp`, a
  submodule. For a one-off alert, build an `Alert`, set `alert_type` (any string) and
  `event_type = ET.WARNING`, and append it to the list `selfdrived.update_alerts()` hands to
  `AlertManager.add_many` — the manager keys on that string, so no schema change is needed.
- **`selfdrived.py` imports two different `Priority` symbols.** `common.realtime.Priority` is
  thread scheduling; `selfdrived.events.Priority` is alert ordering. The second has to be
  aliased or it silently shadows the first.
- **The UI is Python + raylib** (`pyray`), not Qt. `augmented_road_view.py` has an explicit
  "custom UI extension point"; a widget subclasses `Widget` and gets `_update_state()` called
  from `render()` for free. Size everything as a fraction of the passed rect — the UI runs at
  2160x1080 (`BIG=1`) or 536x240 (`BIG=0`), times `SCALE`.
- **Project UI markers with ModelRenderer's transform, and give them a FIXED shape.** Car space
  is x-forward, y-RIGHT, z-DOWN with the road at `extrinsicsCalibration.height`, and radar yRel
  is LEFT positive, so the projection takes `-yRel`. Size from the projected footprint (correct
  perspective) but do NOT let the marker flatten with it: a diamond lying on the road plane is
  edge-on past ~30m and renders as a dash. openpilot's own lead chevron stands up for the same
  reason.
- **`rl.draw_line_ex` has butt caps**, so two strokes meeting at a point leave a notch. Cap it
  with a disc — and dim by colour value, never alpha, or the overlap composites into a bright
  spot exactly where the disc is.
- **A cancelled lane change goes to `preLaneChange`, not `off`.** The blinker is still on, so it
  re-commits by itself once the lane clears ("wait for the gap"), and `selfdrived.py` only raises
  `laneChangeBlocked` in `preLaneChange` — so `off` would abort silently with no on-screen reason.
  `auto_lane_change_timer` is zeroed on every tick spent outside `preLaneChange`, which is what
  makes the re-commit wait out the full delay instead of strobing.
- **Use `obj:getDirectionVectorRight()` for handedness**, don't derive it from a cross product.
  Getting it backwards swaps left and right BSM, which looks plausible right up until it matters.
- **A `now - last > interval` gate undershoots on a fast caller.** At beamngd's 100Hz tick, a
  50ms gate fires every 60ms (16.7Hz). Halving the interval to compensate overshoots to 33Hz --
  which is what the driver-monitoring rate complaint in the README was. Use `PhaseClock`
  (advance a phase by one interval per fire); measures 20.0Hz exactly.
- **Reusing a Lua table across scans means `table.sort` sorts the leftovers too.** Radar rows are
  pooled to keep the GC out of a 20Hz loop, so the array still holds rows from whenever traffic
  was heaviest. Sort the ACTIVE PREFIX only, or vehicles that have gone get re-reported.
- **`CANParser.vl` only materialises a message once `vl` has been INDEXED for it.** `update()`
  alone leaves the values at zero forever. Never bites the real stack (carstate.py reads `cp.vl`
  every cycle) but it makes a test read zeros and pass on nothing. The parser also ignores a
  packet whose timestamp has not advanced, and drops a repeat whose counter has not moved.
- **Vehicle Lua cannot read the environment.** Anything configurable in the mod has to travel
  down inside the control packet; `beamngd` re-sends the BSM block every 2s so a vehicle reload
  (which resets the mod to its own defaults) picks the settings back up.
- **`table.clear` exists in BeamNG's Lua but not in a bare `luajit`.** `require("table.clear")`
  in any standalone harness, or the scan dies inside its `pcall` and the symptom is a feature
  that silently never fires.
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
