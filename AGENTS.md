# beampilot — agent guide

openpilot fork that drives a car in **BeamNG.drive** (the consumer Steam game).

> **Not BeamNG.tech.** We do not have it, so `beamngpy` (the .tech Python API) is not an
> option. Everything works through mechanisms BeamNG.drive exposes to ordinary mods: a Lua
> protocol file for telemetry/control, and screen capture for the camera.

This file is the map. Read it before touching anything, especially `beamngd`, `beamcamd`, or the
Lua mod — nearly every "obvious" change to those three has already been tried and silently broken
something; the **Hard-won gotchas** section below is the record of that.

## You almost certainly cannot run this end-to-end

Driving the car requires **BeamNG.drive actually running** (a Windows/Steam game, playable on
Linux) plus a GPU capable of running the driving model (tinygrad `NV`/`AMD`, or `cuda`/`cl` as a
fallback — see Environment notes). Assume your sandbox has **neither** unless told otherwise.

What you *can* usually do without the game:
- Run the Python unit tests (`tools/test_runner.py` — see Testing below).
- Run the Lua tests with `luajit` (no BeamNG needed, see Testing).
- Run `uv run ruff check .` and read/edit code.
- Reason about the protocol/wire format changes against the tests that pin them.

What you cannot verify without a human at the keyboard with the game open: anything about actual
driving feel (weaving, braking timing, camera framing), anything about window capture, and
anything that only shows up "on screen" (UI, alerts). Say so explicitly rather than claiming a
change "works" — the repo owner will test those manually.

## Repo map

This repo *is* an openpilot fork (root-level `SConstruct`, `RELEASES.md`, `LICENSE`, `docs/`,
`.github/` are openpilot's own). The actual Python package lives one level down, at `openpilot/`
(matches `pyproject.toml`'s `packages = ["openpilot"]`), so watch for that repeated segment in
paths below — it's real, not a typo.

| Path | What it is |
|---|---|
| `openpilot/` | The forked openpilot package: `cereal`, `common`, `selfdrive`, `system`, `tools`. Stock upstream code except for the small, `BEAMPILOT_`-gated patches listed under **Architecture** below. |
| `openpilot/selfdrive/beamngd/` | **Ours.** UDP bridge: telemetry in, fake CAN/IMU/GPS out, control out. |
| `openpilot/selfdrive/beamcamd/` | **Ours.** Screen capture → VisionIPC camera frames (X11 and Wayland/portal backends). |
| `tools/beamng_mod/` | **Ours.** The BeamNG Lua mod (telemetry/control protocol + the FOV-matched camera). Edits apply live in-game — reload with `Ctrl+L`. |
| `tools/opendbc_beampilot_car/` | **Ours.** The `BEAMPILOT` opendbc car platform (source of truth; gets installed into the `opendbc` submodule at setup/launch time, never edited there directly). |
| `config_beampilot.sh` | The config file: car fingerprint, GPU backend, driving limits, UI size. Source of truth for all `BEAMPILOT_*` env vars — `beampilot_tui.py` only rewrites lines it manages. |
| `launch_beampilot.sh` / `setup_beampilot.sh` | Entry points — see **Running it** below. |
| `tools/beampilot_setup.py` | Interactive, read-only-until-confirmed system check (GPU, display server, BeamNG install, permissions). |
| `tools/beampilot_tui.py` | Terminal UI over `config_beampilot.sh` for all 71 settings. |
| `tools/beampilot_monitor.py` | **Live** view of every channel + carState/control values — first thing to run when something looks wrong in-game. |
| `tools/beampilot_diag.py` | One-shot data-flow diagnostic (checks the raw UDP link from the mod without the full stack). |
| `tools/test_runner.py` | Custom pytest-ish runner for this repo's Python tests (see Testing). |
| `msgq_repo/`, `opendbc_repo/`, `panda/`, `rednose_repo/`, `teleoprtc_repo/`, `tinygrad_repo/` | **Git submodules**, symlinked into the tree (`msgq`, `opendbc`, etc.). Never commit fixes inside a submodule checkout — see the submodule rule under Conventions. |
| `README.md` | User-facing docs: install, configuration reference, full settings list, troubleshooting. Longer and more procedural than this file. |
| `CLAUDE.md` | Symlink to this file — Claude Code and Codex read the same content, so it can't drift out of sync. |
| `docs/` | Upstream openpilot's own docs (safety, architecture, contributing) — not beampilot-specific. |

## Setup, running, testing

```bash
# one-time setup (checks system, offers to install mod + build)
uv run python tools/beampilot_setup.py

# configure (TUI over config_beampilot.sh), or just edit that file directly
uv run python tools/beampilot_tui.py

# run it (BeamNG must already be running with a car spawned)
source .venv/bin/activate.fish     # fish shell — NOT bin/activate. bash/zsh: source .venv/bin/activate
./launch_beampilot.sh              # BEAMPILOT_LAUNCH_DELAY=5 for a countdown first

# watch it while it runs, in a second terminal
uv run python tools/beampilot_monitor.py
```

`launch_beampilot.sh` needs its shebang intact — without it, fish's `ENOEXEC` fallback silently
runs it under `dash` and the whole config is quietly lost (see gotchas).

### Testing

```bash
uv run python tools/test_runner.py                       # everything
uv run python tools/test_runner.py openpilot/selfdrive/beamngd   # one directory
uv run python tools/test_runner.py -k bsm                # by name substring
uv run ruff check .                                       # lint (CI-enforced style)
```

Targeted tests worth knowing by name — run these after touching the matching code:

| Test | Covers |
|---|---|
| `openpilot/selfdrive/beamngd/test_bsm.py` | BSM wire format |
| `openpilot/selfdrive/controls/lib/test_desire_helper_bsm.py` | Lane-change state machine: refusing to start, cancelling in flight |
| `openpilot/selfdrive/beamngd/test_radar.py` | Radar wire format + receiver, cross-checked against the real Lua encoder |
| `openpilot/selfdrive/beamngd/test_carstate_signals.py` | Gear / parking brake / steering rate round-tripped through the Honda DBC |
| `openpilot/selfdrive/beamngd/test_camera_calibration.py` | In-game request validation, disengaged gate, and extrinsics-only reset |
| `openpilot/selfdrive/controls/test_radard_beampilot.py` | Radar-only lead selection, including the in-lane test on a bend |
| `tools/test_beampilot_tui.py` | TUI defaults match the code's actual `env_bool`/`env_float` defaults |

Lua tests need `luajit`, not the in-game interpreter (see gotchas re: `table.clear`):
```bash
luajit tools/beamng_mod/test_beampilot_bsm.lua   # BSM zone geometry vs BeamNG's real mathlib
```

Anything that consumes `modelV2` or another capnp message **must** be tested against a real
`messaging.new_message('modelV2').as_reader()`, never a hand-built Python list — capnp lists
support `len()`/indexing but not slicing, so list-based tests pass while the real thing raises
`TypeError` on the first live frame. This has already taken down `plannerd` once.

> Pushes from this fork need `git push --no-verify`. openpilot's inherited `.lfsconfig` points LFS
> at comma's own GitLab over SSH, which nobody but comma can authenticate to. Nothing here is
> LFS-tracked, so skipping the hook is lossless — the LFS *fetch* URL stays public.

## Architecture

Two daemons are ours, plus one BeamNG mod. Stock openpilot processes are **almost** unmodified:
they carry small, clearly-marked beampilot patches, all env-gated. `grep -rn BEAMPILOT_
openpilot/` finds every one. Each defaults to upstream behaviour with an unset environment
**except** the blind-spot lane-change abort, which defaults on — and that one is still inert
upstream, since it only fires on `carState.leftBlindspot`/`rightBlindspot`, and no car this fork
simulates has BSM messages in its DBC.

| File | Patch |
|---|---|
| `selfdrive/controls/lib/desire_helper.py` | `BEAMPILOT_AUTO_LANE_CHANGE` — commit on the blinker alone; `BEAMPILOT_LANE_CHANGE_ABORT` — cancel one in flight on a blind spot |
| `selfdrive/controls/lib/drive_helpers.py` | `BEAMPILOT_MAX_LAT_ACCEL` / `_JERK` / `MAX_CURVATURE` |
| `selfdrive/controls/lib/longitudinal_planner.py` | `BEAMPILOT_ACCEL_SCALE` / `_DECEL_SCALE` |
| `selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` | `BEAMPILOT_T_FOLLOW_SCALE` — multiplier on `get_T_FOLLOW()`'s time gap, on top of `BEAMPILOT_PERSONALITY`; `BEAMPILOT_COMFORT_BRAKE_SCALE` — multiplier on `COMFORT_BRAKE`, **compiled into the MPC solver, not read live** (see gotchas) |
| `selfdrive/selfdrived/selfdrived.py` | `BEAMPILOT_IGNORE_COMM_ISSUE`; also the "Lane Change Cancelled" alert, added straight to the `AlertManager` |
| `selfdrive/car/card.py` | `BEAMPILOT_BSM` — overlay blind spot onto `carState`; `BEAMPILOT_RADAR` — fill in the empty `RadarData` (see gotchas) |
| `selfdrive/car/car_events.py`, `selfdrive/car/cruise.py` | `BEAMPILOT_MAX_ENGAGE_SPEED_SCALE` — raises `MAX_CTRL_SPEED` (the `speedTooHigh` disengage ceiling) and `V_CRUISE_MAX` (max set speed) together |
| `tools/opendbc_beampilot_car/` | our own opendbc platform, `BEAMPILOT` — the car openpilot is actually driving |
| `tools/install_beampilot_car.py` | installs that platform into the opendbc submodule; run from setup AND launch |
| `selfdrive/controls/radard.py` | `BEAMPILOT_RADAR_LEADS` — let a ground-truth track be the lead with no vision confirmation; `match_vision_to_track()` also gates on lateral position (only when `BEAMPILOT_RADAR` is on) so it can't pick a next-lane car that merely scored well on distance/speed |
| `selfdrive/ui/onroad/augmented_road_view.py` | renders `BlindSpotRenderer` at the file's own "custom UI extension point" |
| `selfdrive/beamcamd/beamcamd.py` + `openpilot_cam` | `BEAMPILOT_CAMERA_MODE` — narrow-only, or a wide render plus centred narrow crop |
| `system/manager/process_config.py` | adds `beamngd`/`beamcamd`, drops the hardware-only processes |

```
BeamNG.drive
  │  (Lua mod: tools/beamng_mod/beampilot_bridge)
  │  UDP 49152  telemetry out  ──────────┐   (+ blind spot flags in dashLights)
  │  UDP 49153  control in     ◄───────┐ │   (+ BSM/radar/geometry/camera tuning, every 2s)
  │  UDP 49155  radar points ──────────┼─┼──► card ──► radarTracks
  │  UDP 49156  vehicle geometry ──────┤ │   (wheelbase, mass, COG, yaw inertia,
  │                                    │ │    steering lock, measured rack ratio)
  │  UDP 49157  calibration command ───┤ │   (in-game camera tuner → beamngd)
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
   - `VehicleModel.get_steer_from_curvature(curvature, speed, roll)` → wheel angle (rad)
   - → degrees → `/ self.steer_lock_deg` → normalized `-1..1`
   - rate-limited, then sent as JSON over UDP to `127.0.0.1:49153`

   The `VehicleModel` is **not** the fingerprinted Civic's. `beampilot_vehicle.py` receives the
   mod's measurement of the vehicle BeamNG actually spawned (UDP 49156) and
   `refresh_vehicle_model()` rebuilds the model on it — including re-deriving tyre stiffness at
   the new geometry, since opendbc's `scale_tire_stiffness()` computes it from mass and weight
   distribution. `steer_lock_deg` comes from the same packet. `BEAMPILOT_BEAMNG_GEOMETRY=0`, or
   simply no packets arriving, restores the Civic's numbers exactly.
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
| `tools/beamng_mod/openpilot_cam/lua/ge/.../openpilot.lua` | rigid, runtime FOV-matched camera (25.70° narrow / 93.62° wide). **Required** — `beampilot.lua` selects it by name at spawn |
| `tools/beamng_mod/openpilot_cam/lua/ge/extensions/beampilotCameraTuner.lua` | sparse per-JBeam pose overrides + pause-menu bridge; saved vehicle values layer above TUI defaults |
| `openpilot/selfdrive/controls/lib/beampilot_curve.py` | brake for a corner before reaching it, from the model's own predicted curvature |
| `openpilot/common/beampilot_limits.py` | how hard the car may be driven: accel/lateral limits, the combined envelope, and the excessive-actuation trip points, all scaling together |
| `openpilot/common/beampilot_bsm.py` | BSM wire format + the `beamngd`→`card` socket, and the tuning pushed down to the mod |
| `openpilot/common/beampilot_radar.py` | radar wire format + the `mod`→`card` socket (one hop; no relay) |
| `openpilot/selfdrive/ui/onroad/blindspot_renderer.py` | the onroad mirror lamps (amber chevrons, steady/flashing; OFF by default) |
| `openpilot/selfdrive/ui/onroad/radar_renderer.py` | radar tracks drawn on the road, ringed on the lead |
| `openpilot/tools/sim/lib/simulated_car.py` | fake Honda CAN packing (shared with MetaDrive bridge) |
| `openpilot/tools/sim/lib/simulated_sensors.py` | fake IMU/GPS/DM publishing (shared) |

## Conventions

- **New behaviour is a `BEAMPILOT_*` env var, defaulting to stock upstream when unset.** Don't
  hardcode a new limit or toggle — add an env-gated patch and list it in the Architecture table
  above and in `config_beampilot.sh`/`beampilot_tui.py`.
- **Never edit inside a submodule checkout** (`opendbc_repo/`, `panda/`, `tinygrad_repo/`, etc.).
  A commit there records no new submodule SHA in this repo, so it works on one machine and breaks
  every clone the moment `git submodule update` runs. If opendbc needs a change, it goes through
  `tools/opendbc_beampilot_car/` + `tools/install_beampilot_car.py` (see the `BEAMPILOT` platform
  gotchas below) — that's the one sanctioned way to affect the submodule's checked-out state.
- **Lua locals must be declared above every function that references them**, or Lua silently
  creates an implicit global instead of closing over the local. Check with
  `luajit -bl <file> | grep GSET` — a clean file writes no globals at all.
- **Growing the UDP telemetry struct breaks every old mod build.** `parse_telemetry()` rejects any
  packet whose length isn't an exact match. New data gets its own packet on its own port; only
  cheap boolean flags go in spare `dashLights` bits.
- Run the relevant test file (table above) plus `uv run ruff check .` before calling a change
  done. There is no CI you can trigger from here — `tests.yaml` runs on push/PR only.

## Driving limits

Stock openpilot follows EU/ISO passenger-comfort limits, which in a sim are usually what stops it
cornering or getting up to speed. All are env-tunable from `config_beampilot.sh`, and each
**defaults to the stock upstream value if unset** — an unset environment is unmodified openpilot.

| Env var | Stock | Controls |
|---|---|---|
| `BEAMPILOT_MAX_LAT_ACCEL` | 3.0 m/s² | turning. Max curvature is `accel / v²`, so stock allows only a ~300 m radius at 67 mph |
| `BEAMPILOT_MAX_LAT_JERK` | 5.0 m/s³ | how fast curvature may change. **Raise this cautiously — it's the most likely source of weaving/oscillation** |
| `BEAMPILOT_MAX_CURVATURE` | 0.2 1/m | geometric cap; only binds below ~11 mph |
| `BEAMPILOT_ACCEL_SCALE` | 1.0 | multiplier on the whole accel envelope (`A_CRUISE_MAX_VALS` *and* `ACCEL_MAX` — scaling only the first leaves `ACCEL_MAX` re-clamping it back down) |
| `BEAMPILOT_DECEL_SCALE` | 1.0 | same for braking |

These are *permission* limits, not commands: raising them lets openpilot turn harder, it doesn't
make it want to. If it still runs wide, suspect the geometry mismatch instead — openpilot computes
wheel angle from the **Civic's** steerRatio 15.38 / wheelbase 2.700 m, while the BeamNG vehicle has
its own (only its 510° lock has been measured).

**Raising a driving limit means raising THREE things**, all in `common/beampilot_limits.py`:
`long_mpc.py` also bounds its solution with opendbc's *unscaled* `ACCEL_MAX` and the planner takes
`min(mpc, cruise)`, so the accel scale alone was mostly ignored; `_A_TOTAL_MAX_V` (the combined
lat+long envelope) was unscaled, so raising the lateral limit left nothing for the throttle
mid-corner; and `ExcessiveActuationCheck` soft-disables on *measured* actuation against stock trip
points, so past those you get a disengagement rather than performance.

## Hard-won gotchas

Each of these was a real bug that cost significant debugging time. Don't regress them — if a
change you're making looks like it should touch one of these areas, read the relevant entry
first.

### Startup, timing, control loop

- **`set_params_enabled()` must run before the stack starts** (`launch_beampilot.sh`). It seeds
  `CalibrationParams` (validBlocks=20, rpy=[0,0,0]) only when no learned value exists. Without it
  `calibrationd` needs *minutes* of
  sustained 15+mph straight driving to converge, and until then the model's road-frame transform
  is wrong — which manifests as biased/drifting steering, not as an obvious error.
- **`SimulatedSensors` helpers emit multi-sample bursts** (5x IMU, 10x GPS) sized for a ~20Hz
  caller. `beamngd` ticks at 100Hz, so it must pass `count=1` and rate-limit GPS to 10Hz. Before
  this was fixed: GPS published at **1000Hz (100x over spec)**, IMU at 5x.
- **`commIssue` is NOT cosmetic.** It's registered as both `NO_ENTRY` and `SOFT_DISABLE`
  (`selfdrived/events.py`) — it blocks engagement *and* disengages mid-drive. Wrong publish rates
  cause it. Treat those log lines as real.
- **A `now - last > interval` gate undershoots on a fast caller.** At beamngd's 100Hz tick, a 50ms
  gate fires every 60ms (16.7Hz). Halving the interval to compensate overshoots to 33Hz. Use
  `PhaseClock` (advance a phase by one interval per fire); it measures 20.0Hz exactly.
- **openpilot's `Ratekeeper` never resyncs**, so one long frame makes it sleep zero until it has
  caught up — a 285ms hiccup emits ~6 frames ~13ms apart and modeld sees the world lurch then
  stall. `beamcamd` uses `FramePacer`, which drops a backlog it can no longer deliver on time.
- **`beamcamd` timestamps must be `time.monotonic_ns()`**, not a synthetic frame counter.
  `locationd._validate_timestamp` rejects fake ones on every frame, permanently. (openpilot's own
  `system/camerad/webcam/camerad.py` has this same bug, unfixed — it's just never exercised there.)
- **`launch_beampilot.sh` needs its shebang.** Without it, fish's ENOEXEC fallback runs it under
  `dash`, `source` fails silently, and the whole config is quietly lost.

### Car model / CAN

- **This car is Bosch.** `cruiseState.speed` comes from `ACC_HUD.CRUISE_SPEED` (camera bus),
  **not** `CRUISE.CRUISE_SPEED_PCM` — that field is only read for non-Bosch Hondas. See
  `opendbc/car/honda/carstate.py`, the `if self.CP.flags & HondaFlags.BOSCH:` branch.
- **`pcmCruise=True`** for this fingerprint: openpilot waits for the *car* to report ACC on before
  engaging. So `ACC_STATUS` must be driven by the cruise button directly (`self.acc_engaged`),
  never from `selfdriveState.active` — that's a deadlock.
- **`AlphaLongitudinalEnabled` must be set before the stack starts**, or honda's `interface.py`
  leaves `openpilotLongitudinalControl=False` / `pcmCruise=True` — openpilot steers but expects the
  *car's* ACC to manage speed, and BeamNG has none, so it never accelerates or holds speed.
- **Never report BeamNG's throttle/brake as the driver's pedal input while engaged.**
  `electrics.values.throttle` is our own `input.event` command echoed back; feeding it to
  `PEDAL_GAS` makes `CS.gasPressed` true, which trips `selfdrived.py`'s rising-edge `pedalPressed`
  check and disengages the instant openpilot tries to accelerate.
- **Use `electrics.values.steering`** (real post-dynamics wheel angle, true degrees) as the
  feedback signal — **not** `steering_input`, which is just an echo of the last command and feeds
  the PID its own output back.
- **Steering self-calibration is default-deny until beamngd identifies the vehicle and rack.**
  The first config packet arrives before the first 1Hz geometry packet; treating "not identified"
  as "not cached" made every cached vehicle begin another sweep at startup. beamngd now sends an
  exact `vehicle|rounded-lock` authorization key only for an uncached rack, and Lua verifies it
  before actuation. A live ratio below `RATIO_CACHE_MIN_SAMPLES` is still thin — do not cancel the
  sweep until it crosses the persistence threshold. Revoking permission mid-sweep must emit a
  neutral steering event; merely returning leaves the last direct-input value held.
- **`CANParser.vl` only materialises a message once `vl` has been INDEXED for it.** `update()`
  alone leaves the values at zero forever. Never bites the real stack (carstate.py reads `cp.vl`
  every cycle) but it makes a test read zeros and pass on nothing. The parser also ignores a
  packet whose timestamp hasn't advanced, and drops a repeat whose counter hasn't moved.

### The `BEAMPILOT` opendbc platform

- **`BEAMPILOT` lives in THIS repo, not in the opendbc submodule.**
  `tools/opendbc_beampilot_car/` is the source of truth; `tools/install_beampilot_car.py` symlinks
  it into `opendbc_repo/opendbc/car/beampilot` and patches two of comma.ai's files (the brand
  import + `Platform` union in `car/values.py`, and a `torque_data/override.toml` entry —
  `get_std_params` KeyErrors without the latter). A `git submodule update` reverts both patches
  silently, which is why launch re-runs the installer, and why it falls back to
  `HONDA_CIVIC_2022` rather than starting on a car that doesn't exist.
- **`BEAMPILOT` is a real opendbc platform, not a renamed Honda.** It REUSES Honda's
  `CarState`/`CarController` verbatim, because `beamngd` hand-packs Honda Bosch radarless frames
  and those classes branch on `flags` and `DBC[carFingerprint]`, never on "is this a Civic".
  `values.py` mutates `honda.values.DBC` to register `BEAMPILOT` in it — without that,
  `DBC[CP.carFingerprint]` KeyErrors inside Honda's CarState. Three things bit during the port, all
  silent: `pcmCruise` (the LIVE Civic is alpha-long, so pcmCruise is FALSE, not True as an old
  comment claimed); `transmissionType` (unset means `unknown`, which happens to take the right
  branch in Honda's CarState — correct by accident, now stated); `steerControlType` (`angle` is
  tempting since beamngd sends a position, but `LatControlAngle` flags saturation past 2.5deg of
  error and the mod's rack lags more than that through any corner — permanent "Turn Exceeds
  Steering Limit"). Verify a platform change by decoding real `SimulatedCar` CAN through both
  platforms and diffing `carState`; `send_can_messages` RETURNS NONE and publishes over pub/sub, so
  a harness that uses its return value silently compares two all-zero states.
- **`car_events.py` branches on `CP.brand`**, and `brand` is now `beampilot`, not `honda`. Every
  event in that Honda branch is guarded by `if self.CP.pcmCruise`, and `pcmCruise` is pinned False
  — exactly why it's pinned rather than left free to follow `alpha_long`.
- **There is no way to add an `EventName`.** The enum lives in `opendbc/car/car.capnp`, a
  submodule. For a one-off alert, build an `Alert`, set `alert_type` (any string) and
  `event_type = ET.WARNING`, and append it to the list `selfdrived.update_alerts()` hands to
  `AlertManager.add_many` — the manager keys on that string, no schema change needed.
- **`selfdrived.py` imports two different `Priority` symbols.** `common.realtime.Priority` is
  thread scheduling; `selfdrived.events.Priority` is alert ordering. Alias one or it silently
  shadows the other.

### Blind spot monitoring (BSM)

- **BSM cannot go in over the fake CAN.** `HONDA_CIVIC_2022`'s DBC dict has no `Bus.body` entry, so
  `honda/carstate.py` never builds the body parser `BSM_STATUS_LEFT`/`_RIGHT` live in. Adding one
  means editing the `opendbc` submodule — forbidden, see the submodule rule. Hence the loopback UDP
  hop into `card.state_update()`.
- **BSM rides in spare `dashLights` bits (4..7), not new telemetry struct fields** — see the
  telemetry-struct convention above.
- **A stale BSM feed must fail to *clear*, not to *blocked*.** A latched warning blocks every lane
  change for the rest of the drive with nothing on screen to explain it. 0.5s timeout.
- **The BSM flag needs a hold, not just a raw read** (`BSM_HOLD_SECONDS`, 0.4s). A vehicle hovering
  on the zone boundary chatters at the scan rate, and since `desire_helper.py` cancels an in-flight
  lane change on this flag, chatter would abort the manoeuvre and instantly restart it. Staleness
  still wins over the hold.
- **The turn signal auto-cancel needs the DURATION, not just the state transition.** Completion and
  blind-spot abort both leave `laneChangeStarting` for `preLaneChange`, and a car in the blind spot
  as the change completed (common — you just overtook it) looked exactly like an abort, so the
  signal stayed on for a resume that never came. `desire_helper` can only abort while its timer is
  under `BEAMPILOT_LANE_CHANGE_ABORT_S`, so a longer manoeuvre did NOT end in an abort — exact, not
  a heuristic. The repeat window also had to go from 0.15s to 1.5s.
- **A cancelled lane change goes to `preLaneChange`, not `off`.** The blinker stays on, so it
  re-commits once the lane clears, and `selfdrived.py` only raises `laneChangeBlocked` in
  `preLaneChange` — `off` would abort silently with nothing on screen. `auto_lane_change_timer` is
  zeroed on every tick spent outside `preLaneChange`, which is what makes the re-commit wait out the
  full delay instead of strobing.
- **Use `obj:getDirectionVectorRight()` for handedness**, don't derive it from a cross product.
  Getting it backwards swaps left and right BSM, which looks plausible right up until it matters.

### Radar

- **radarTracks must go through `card`, not alongside it.** `RadarInterfaceBase.update()` returns
  an EMPTY `RadarData` every 5th frame, so card is *already* publishing `radarTracks` at 20Hz.
  msgq permits a second publisher, so a `PubMaster` in `beamngd` binds happily — then radard sees
  our points and card's empties alternating, and the lead flickers. Fill in the RadarData card is
  already building instead.
- **`radard.get_lead()` ignores radar unless the CAMERA already sees a lead** (`lead_prob > .5`; the
  only exception is `potential_low_speed_lead`, below `V_EGO_STATIONARY`). Ground truth can
  therefore only REFINE by default. `BEAMPILOT_RADAR_LEADS` lifts it; the in-lane test uses
  `modelV2.position` so it follows a bend instead of meaning "straight ahead".
- **A radar-only lead has to report `modelProb = 1.0`.** `long_mpc.py` gates its forward collision
  check on `modelProb > 0.9`, so passing the camera's opinion (0.0) would silently disable FCW on
  exactly the leads the camera missed. `process_lead` itself only checks `present`, so following and
  braking work either way.
- **`yRel` is LEFT positive** (`car.capnp`: "m in car frame, left positive"), while
  `modelV2.position`/`leadsV3` are in the device frame, y-RIGHT. radard bridges the two with
  `-lead.y[0]`; anything producing radar points has to do the same.
- **Ground-truth radar makes openpilot brake absurdly early if you let it invent leads.**
  `BEAMPILOT_RADAR_LEADS` lifts radard's vision gate, handing openpilot leads the camera never saw —
  distance management starts far earlier than normal. Off by default; radar refines a vision lead
  instead, fixing the distance error without moving the timing. Oncoming traffic must also be
  filtered at the source: an approaching car picked as a lead is a hard-braking event for a car that
  was going to pass on the other side, and the in-path test on a narrow road will pick one.
- **Reusing a Lua table across scans means `table.sort` sorts the leftovers too.** Radar rows are
  pooled to keep the GC out of the 20Hz loop, so the array still holds rows from whenever traffic
  was heaviest. Sort the ACTIVE PREFIX only, or vehicles that have gone get re-reported.
- **The Lua beam is a straight cone off the current heading and grows wider than a lane fast.**
  `beampilot.lua`'s scan gate is `halfWidthM + spread * dRel`; the stock defaults (3.0, 0.07)
  reach 6.5m half-width by 50m and 10.7m by max range — nearly three lanes — which is how it
  locked onto traffic in the next lane over before anything downstream even ran. Narrowed to
  1.8/0.025 (matching `BEAMPILOT_RADAR_LEAD_HALF_WIDTH_M`'s own "in lane" assumption), but this
  cone does NOT follow a bend the way `get_radar_only_lead`'s path-relative test does — too narrow
  and a real in-lane car drops out mid-corner. Widen `_SPREAD` first if that happens.
- **`match_vision_to_track()`'s own probability score has no lateral veto**, and stock has good
  reason not to add one — a real radar's bearing estimate is genuinely noisy, and rejecting a
  drifted-but-real return is worse than a slightly-off match. With GROUND-TRUTH points (only when
  `BEAMPILOT_RADAR` is on) that noise doesn't exist, so `radard.py` adds a hard gate against the
  camera's own believed lead position (`RADAR_LEAD_HALF_WIDTH`) that stays off otherwise — a same-
  distance, same-speed car one lane over in traffic can otherwise outscore the correct match on
  `prob_d * prob_y * prob_v` alone.

### Curve braking / longitudinal limits

- **Stock openpilot never slows down FOR a corner.** It caps acceleration once already cornering
  (`a_x = sqrt(a_total^2 - a_y^2)`) but the cruise path just holds the setpoint. Curve braking is
  beampilot's (`beampilot_curve.py`), offered to `longitudinal_planner`'s `candidates` list so
  `min()` keeps a lead car winning when it should. Two things that bite: take curvature from
  `modelV2.orientationRate.z / velocity.x`, NOT the steering angle (the angle says what the car is
  doing *now*, which arrives too late), and floor the planning distance at ~1.5s of travel or
  curvature at the first path point divides by ~zero and demands maximum braking on every corner
  the car is already in.
- **capnp lists are NOT Python lists** — see Testing above. This is exactly how the curve limiter
  took `plannerd` down once.
- **`COMFORT_BRAKE` appears TWICE in `long_mpc.py`, and only one of them is compiled. They must
  agree.** `get_safe_obstacle_distance(v_ego)` = `v^2/(2*COMFORT_BRAKE) + t_follow*v +
  STOP_DISTANCE` is called from `gen_long_ocp()`, so it is substituted into the symbolic
  cost/constraint expressions at CODEGEN time and lives only in the compiled solver.
  `get_stopped_equivalence_factor(v_lead)` = `v_lead^2/(2*COMFORT_BRAKE)` is called from
  `update()`, live in plannerd. Upstream shares one constant between them deliberately: at a
  matched speed the two quadratic terms CANCEL and the steady-state gap is just
  `t_follow*v + STOP_DISTANCE`. `COMFORT_BRAKE` therefore does not set the following gap at all —
  it sets the APPROACH to something slower or stopped.
  Break that symmetry and the planner credits the lead with braking harder than itself, so every
  moving car is treated as roughly a wall. `BEAMPILOT_COMFORT_BRAKE_SCALE` at 2.6 did exactly
  that — ours ran at 2.5, the lead's at 6.5 — and demanded a ~130m gap at 67mph, braking to a
  dead stop to open one, then accelerating once it cleared. Stop-and-go with no lead doing
  anything unusual, on vision leads and radar leads alike.
- **Two separate SCons mechanisms hid that for the entire life of the feature.** `SConstruct`'s
  `Environment(ENV={...})` is an explicit whitelist and SCons deliberately does not inherit
  `os.environ`, so `python3 long_mpc.py` codegen never saw `BEAMPILOT_COMFORT_BRAKE_SCALE` and
  always resolved it to the 1.0 default. And `SConstruct` sets `Decider('MD5-timestamp')`, which
  falls back to comparing CONTENT when the timestamp moves — so the old `touch long_mpc.py` +
  sentinel hack in `launch_beampilot.sh` never triggered a rebuild either (`CacheDir` would have
  served the identical artifact regardless). Both are fixed: `SConstruct` passes the whole
  `BEAMPILOT_*` prefix into the build env, and `longitudinal_mpc_lib/SConscript` puts the resolved
  scale in the codegen `source_list` as an `env.Value()` node, so the VALUE is in the dependency
  signature. **Do not go back to touching a file to force a rebuild — under this decider it does
  nothing.** Verify a change to this knob by grepping the generated C for the divisor:
  `grep -A2 'a3=casadi_sq(a2);' c_generated_code/long_cost/long_cost_y_fun.c` prints `2*COMFORT_BRAKE`.
- **`test_following_distance.py` cannot catch a compiled/live mismatch.** It computes equilibrium
  as `get_safe_obstacle_distance() - get_stopped_equivalence_factor()`, both imported from the
  Python module, so it is internally consistent no matter what the solver was actually built with.
  The generated C is the only source of truth for the compiled half.

### UI / rendering

- **Per-vehicle camera pose wins over the TUI base, field by field.** The bridge deliberately
  refreshes `OPENPILOT_CAM` every two seconds; the camera tuner must keep sparse JBeam-keyed
  profiles separate and overlay them at render time. Never write a saved pose back into that base
  table or the next refresh will erase it. FOV and placement mode stay bridge-owned.

- **The UI is Python + raylib** (`pyray`), not Qt. `augmented_road_view.py` has an explicit "custom
  UI extension point"; a widget subclasses `Widget` and gets `_update_state()` called from
  `render()` for free. Size everything as a fraction of the passed rect — the UI runs at 2160x1080
  (`BIG=1`) or 536x240 (`BIG=0`), times `SCALE`.
- **Project UI markers with ModelRenderer's transform, and give them a FIXED shape.** Car space is
  x-forward, y-RIGHT, z-DOWN with the road at `extrinsicsCalibration.height`, and radar yRel is LEFT
  positive, so the projection takes `-yRel`. Size from the projected footprint (correct perspective)
  but do NOT let the marker flatten with it: a diamond lying on the road plane is edge-on past ~30m
  and renders as a dash. openpilot's own lead chevron stands up for the same reason.
- **`rl.draw_line_ex` has butt caps**, so two strokes meeting at a point leave a notch. Cap it with a
  disc — and dim by colour value, never alpha, or the overlap composites into a bright spot exactly
  where the disc is.
- **A screen-space `rl.draw_circle_v` marker, unlike a 3D shape projected onto the road, does not
  need the FIXED-shape treatment above at all** — its size still comes from perspective (project the
  footprint, take the pixel radius), but the circle itself is drawn flat in 2D, so it never flattens
  edge-on the way a shape lying on the road plane would past ~30m. Simpler than a fixed aspect ratio
  when a circle already reads as the right thing (radar_renderer.py's dots).
- **`model_renderer.py`'s stock lead chevron already covers a vision-only lead.** `_update_leads()`
  only checks `lead_data.present`, never `lead_data.radar`, and `render_lead_indicator` only
  requires `openpilotLongitudinalControl` — which this fork always has on. Before adding "what is
  it actually reacting to" visibility to `radar_renderer.py`, check whether the stock chevron
  already answers it; it does for source, just not for "which of the dots on screen is that".
- **`get_current_monitor()` on a probe window is not deterministic.** The UI's auto-scale opened a
  1x1 window to ask which screen it was on; with two monitors that answer changed between runs, and
  half the time it sized the window for the wrong one. Fit the SMALLEST monitor. Auto-scale also has
  to work in BOTH directions — `BIG=0` is a 536x240 base, which needs growing, not shrinking.

### Camera capture (X11 / Wayland)

- **An all-green picture means the capture produced NO data — it is not a colour bug.** An
  untouched NV12 buffer is `Y=0, U=V=0`, which decodes to RGB(0,135,0); real black is `Y=16,
  U=V=128`. Green is the signature of an empty buffer, usually the X11 backend on Wayland, where a
  root grab returns nothing. Don't debug the conversion; check the backend. `_warn_if_blank()`
  detects a uniform frame and says so.
- **Wayland cannot be captured with X11 calls, and detecting the window proves nothing.** BeamNG is
  normally an XWayland client, so `xdotool` finds it and the region looks right — but pixels belong
  to the compositor, so the grab still returns nothing. Detection and capture are separate problems.
  Wayland capture goes through `portal_capture.py`; `BEAMPILOT_CAPTURE_BACKEND=auto` selects it on
  Wayland and leaves X11 sessions on the X11 grab.
- **KWin's window geometry is NOT a valid capture region.** It's the compositor's logical coordinate
  space and doesn't map onto the X root that mss grabs. `window_capture.py` queries KWin only to
  *explain* a failure, never to pick a rectangle.
- **A capture rectangle must be clamped to the X root or `beamcamd` dies.** X11 `GetImage` raises
  BadMatch (error 8, opcode 73) if any part of the region is off-screen, and mss surfaces it as an
  uncaught `XProtoError`. beamcamd exits → camerad's VisionIPC streams vanish → `modeld` blocks in
  `available_streams()` and never starts → `process_not_running: modeld`, nothing else obviously
  wrong. A monitor whose edge is flush with the virtual screen leaves zero slack, so this is easy to
  hit.
- **The capture rectangle's ASPECT has to be 1928/1208 (1.5960), not just any rectangle.**
  `FrameEncoder.encode` resizes straight to the frame size with no aspect preservation, so a 16:9
  window is squeezed ~11% horizontally: the selected lens's vertical field is right, but its
  horizontal field does not match that lens's intrinsics. Depends on the window's shape, not its
  size — 1440p is exactly as wrong as 1080p. `BEAMPILOT_CAM_ASPECT=crop` trims the sides; cropping
  top/bottom would cut the vertical field and is never done. The real capture fix is a 1928x1208
  window: exact aspect, no capture crop or resample.
- **Don't run `find_window()` on a timer.** It shells out to pgrep/xdotool/xprop per candidate: 68ms+
  measured, against a 50ms frame budget, so a 2s retrack guaranteed a dropped frame. Use
  `window_geometry()` on the known id (~0.7ms) and only rediscover when the window disappears.
- **A backup inside `mods/unpacked` is still an ACTIVE mod.** BeamNG mounts every directory there,
  suffix notwithstanding. The old setup path `openpilot_cam.replaced-TIMESTAMP` therefore loaded a
  stale camera beside the symlink and could override the live FOV code by virtual-filesystem load
  order. `install_mods()` keeps recoverable copies in `<userfolder>/beampilot-mod-backups` instead,
  and `check_beamng()` reports any old active backups that need moving.

### GPU / tinygrad

- **tinygrad's NV/AMD backends are not CUDA/ROCm** — raw ioctls, Ampere-or-newer only (`NV`), or
  gfx942/gfx950/gfx11xx/gfx12xx (`AMD`). An older card doesn't run slowly, it dies at model load with
  a bare `StopIteration` in `ops_nv.py:441` (`next()` over `AMPERE_COMPUTE_B` etc. on a card that
  exposes the Turing classes). Not about tensor cores — a 2060 has them and fails the same way.
  `BEAMPILOT_BACKEND=cuda` or `=cl` goes through the vendor stack and has no such limit.
- **The device index reaches each backend differently, and the obvious spelling is silently
  wrong.** `NV`/`AMD` are HCQ backends: the index is a visibility filter read from `Target.indices`,
  so it goes BEFORE the `+` (`DEV=":1+NV"`). `DEV=NV:1` parses as renderer `"1"`
  (`Target.parse`, `tinygrad/helpers.py`) and opens GPU 0 anyway. `CUDA`/`CL` are not HCQ and take no
  index at all (`DEV=CL:1` → "CL has no renderer '1'"); the card is chosen with
  `CUDA_VISIBLE_DEVICES`, honoured by NVIDIA's OpenCL ICD too — pair it with
  `CUDA_DEVICE_ORDER=PCI_BUS_ID` or the numbering is fastest-first, the reverse of nvidia-smi's.
  `config_beampilot.sh` works all of this out once and exports `BEAMPILOT_TG_BACKEND`/`_TG_DEV` for
  the build, so build and runtime cannot disagree.

### Lua / BeamNG specifics

- **Vehicle Lua cannot read the environment.** Anything configurable in the mod has to travel down
  inside the control packet; `beamngd` re-sends the BSM/radar/geometry blocks every 2s so a vehicle
  reload (which resets the mod to its own defaults) picks the settings back up.
- **jbeam node coordinates are y-forward, x-right — but don't rely on it.** `esc.lua` builds
  forward/right vectors from `v.data.refNodes[0]` (`.ref`, `.back`, `.up`) and classifies wheels by
  dot product, precisely so a differently-oriented jbeam still works. `beampilot.lua`'s geometry
  measurement does the same and projects everything (axles, COG, track width) onto those axes.
- **`v.data.information.name` is not reliably a string.** On some vehicles it's a table, and
  BeamNG's own `chassisData.lua:98` assumes otherwise. `tostring()` on it yields a changing address,
  which silently destroyed the steer ratio cache. Use `v.data.model` (`"etk800"`);
  `vehicleDirectory` is the next best fallback. The Python side refuses to store a name matching
  `^(table|function|userdata|thread): 0x`.
- **There is no `obj:getTotalMass()`.** Mass is `sum(node.nodeWeight)` over `v.data.nodes`, which is
  also where COG and yaw inertia come from.
- **BeamNG's steering lock is a PART, not a car property.** An ETK 800 ships locks of 275, 360, 400,
  450, 510 degrees; the range across all vehicles is 270–900. The steer-ratio cache keys on
  `name|round(lock)` for exactly this reason. Within a vehicle the hydro `factor` barely moves
  across rack options, so road wheel angle at full lock is roughly fixed, making
  `ratio ~= lock / 33` a usable seed before anything is measured.
- **BeamNG has no steering-ratio field** — a rack ratio is emergent from the steering geometry.
  Measure it: `obj:nodeVecPlanarCosRightForward(wheel.node1, wheel.node2)` gives the road wheel angle
  (same as `esc.lua`'s `wheelAngleFront`), fit against `electrics.values.steering`. Average the two
  front wheels — Ackermann and toe-in both cancel. `v.data.input.steeringWheelLock` IS declared, and
  is centre-to-lock, not lock-to-lock.
- **`table.clear` exists in BeamNG's Lua but not in a bare `luajit`.** `require("table.clear")` in
  any standalone harness dies inside its `pcall`, and the symptom is a feature that silently never
  fires.

## Environment notes

- `config_beampilot.sh`: `BEAMPILOT_BACKEND=nv` (`nv` | `amd` | `cuda` | `cl`; `USE_NV`/`USE_AMD` are
  the upstream spelling and only consulted when unset), `CHESTNUT=0` (setting 1 forces `DEV=AMD`
  regardless of backend — built for comma's USB eGPU, not this machine), `BIG=1` (a *resolution*
  switch, not a UI scale knob — use `SCALE` for that).
- `SKIP_FW_QUERY=1` + `FINGERPRINT=HONDA_CIVIC_2022` — the car identity is set here, not
  fingerprinted from CAN. Changing it requires matching `beamngd`/`beamcamd` updates.
- **This machine has two NVIDIA cards**: a GTX 1660 SUPER (Turing, 7.5) at index 0 and the RTX 3060
  (Ampere, 8.6) at index 1, plus a gfx1036 AMD iGPU that tinygrad's `AMD` backend doesn't support.
  `BEAMPILOT_BACKEND=nv` picks the 3060 automatically (`DEV=:1+NV`, printed at launch as
  `[beampilot] tinygrad NV -> GPU 1`). The 1660 is only reachable through `cuda` or `cl`, and runs the
  standard model fine — 8.3 ms/frame against a 50 ms budget. The 3060 also rotates between VM/host
  duty on this machine (see the global `~/.claude/CLAUDE.md` VFIO notes) — it must be in host/nvidia
  mode for beampilot to run at all.
- `BEAMPILOT_CAPTURE_BACKEND` = `auto` | `x11` | `portal`. The portal backend also works on X11 and
  measures smoother there (50.00ms mean / 51.28 max vs 49.96/66.81 over 300 frames — the frame is
  already in memory rather than the loop blocking on the X server) and skips window detection
  entirely, but needs `gst-launch-1.0` + a desktop portal and one dialog click, so it isn't the X11
  default. The portal's source choice is remembered in
  `~/.local/state/beampilot/screencast_restore_token`; delete it to get the picker back.
- **Testing status:** the portal path is verified end-to-end on X11/GNOME here and confirmed working
  on KDE Wayland by a user. The KWin *detection* path has only ever been exercised against a
  simulated compositor (`test_window_capture.py`) — no Plasma on this machine.

## Control mode

`BEAMPILOT_CONTROL_MODE` selects how control reaches the game:
- `lua` (**default, what we use**) — UDP → mod → `input.event()`. No in-game setup.
- `joystick` — a `uinput` virtual wheel (`virtual_joystick.py`); requires manually binding axes in
  BeamNG's Options > Controls. Built as a fallback, currently unused.

## Experimental single-render wide camera

`BEAMPILOT_CAMERA_MODE=narrow` (default) publishes only the calibrated 25.70° vertical narrow
view. Omitting the wide VisionIPC buffer is intentional: its presence is how `modeld` detects a
two-camera device, and the old duplicate narrow frame made it apply wide intrinsics to the wrong
pixels. Stock `modeld` supports the resulting single-camera path.

`wide_crop` makes `openpilot_cam` render the calibrated 93.62° vertical / 119.07° horizontal wide
lens. `beamcamd` publishes the full image as wideRoad and enlarges the centred 412x258 angular crop
as narrowRoad. Geometry and timestamps match, but narrow angular detail is lower than a genuine
second render. This is the experimental option for trying openpilot Experimental mode; actual
driving quality must be A/B tested in game. A synchronized second off-screen render remains the
quality upgrade if ordinary BeamNG.drive exposes a practical route to one.

Wide mode defaults to `BEAMPILOT_WIDE_CAMERA_PLACEMENT=vehicle_front`. The camera mod measures the
spawned vehicle's undeformed oriented bounding box once, anchors 1.22m above its bottom and 0.15m
ahead of its front, and therefore does not depend on the per-JBeam reference-node location. This
keeps the body behind the 119° lens instead of showing a dashboard/bonnet or clipping into a cabin.
`BEAMPILOT_WIDE_CAMERA_HEIGHT_M` and `_CLEARANCE_M` are trims for unusual mod vehicles; `legacy`
restores fixed offsets. Narrow mode intentionally retains its previously tuned placement.

## Vehicles

Tested working: **ETK series** (best — narrow-camera framing was tuned on these), **Bastion**,
**SBR4**, **Sunburst**. Narrow mode still uses fixed offsets, so outside the ETK series its framing
can vary; tune `offUp`/`offFwd` in the camera mod if needed. Wide mode uses the per-vehicle bounding
anchor described above, though its actual on-screen framing must still be verified in BeamNG.

Steering lock and rack ratio are per-vehicle and measured automatically (UDP 49156). The ratio
needs the wheel actually turned to be measurable — turn lock to lock once after switching cars.
`BEAMPILOT_STEER_LOCK_DEG` / `BEAMPILOT_STEER_RATIO` pin a value if wanted;
`BEAMPILOT_BEAMNG_GEOMETRY=0` restores the fingerprinted Civic's numbers entirely.

## Known outstanding issues

- `carState.vCruise` reads ~108 (241 mph) while `cruiseState.speed` is correct — set-speed target is
  wrong.
- `gasPressed` reads True when the throttle isn't touched (BeamNG throttle → `PEDAL_GAS`), which
  openpilot treats as a driver override.
- `beamcamd` shows ~6% frame drop; `driverStateV2`/`driverMonitoringState` run 33Hz vs 20 expected.
- The measured rack ratio is a single number fitted through the origin, so a genuinely progressive
  rack is approximated by one average. The curvature → steering conversion is still open loop;
  `paramsd` partly covers the residual, a real yaw-rate loop would not.

## Reference

Prior art: [jackz314/openpilot](https://github.com/jackz314/openpilot) `tools/truck_sim` — an
ETS2/ATS bridge on openpilot **0.8.6**. Its telemetry path (SCS SDK shared memory) has no BeamNG
equivalent, and its steering is hand-tuned constants rather than `VehicleModel`, so little of it
transfers. Useful mainly as a sanity check on overall structure.

For install/config/troubleshooting written for a human running the game, see `README.md` — it's
more procedural and screenshot-adjacent than this file.
