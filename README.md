# beampilot-drive

**Run [openpilot](https://github.com/commaai/openpilot) — comma.ai's open source driver
assistance system — inside [BeamNG.drive](https://www.beamng.com/game/), the consumer Steam
game.**

openpilot sees the road through a screen capture of the game window, runs its real driving
model on those frames, and steers, accelerates and brakes the car through a BeamNG mod. Every
part of openpilot's stack is the genuine article — the same `modeld`, `controlsd`, `plannerd`
and `locationd` that run on real hardware. Only the sensors and actuators are swapped.

> **No BeamNG.tech required.** This works with the ordinary Steam version of BeamNG.drive.
> There is no dependency on `beamngpy` (which needs the paid research-licensed BeamNG.tech):
> telemetry and control both go through a small Lua mod that uses BeamNG's own public modding
> hooks.

---

## Status

Working, and it drives. Verified live:

- Steers along the road, tracking curves
- Holds a set cruise speed, accelerates and brakes on its own
- Changes lanes on the turn signal
- Follows and brakes for traffic

Known rough edges are tracked in [Known issues](#known-issues).

---

## Quick start

Five steps, assuming you already own and have launched BeamNG.drive once.

```bash
# 1. clone
git clone --recurse-submodules https://github.com/Caleb-J773/beampilot-drive.git
cd beampilot-drive

# 2. build (also installs the BeamNG mod)
./setup_beampilot.sh

# 3. start BeamNG.drive and spawn a car (freeroam is fine)

# 4. launch openpilot -- you get a 5s countdown to tab back into the game
source .venv/bin/activate.fish     # bash/zsh: source .venv/bin/activate
./launch_beampilot.sh
```

Then, **in the BeamNG window**, get the car rolling above ~20 mph and press **`i`** to engage.

### Controls

| Key | Action |
|---|---|
| `i` | Set cruise speed / engage (nudges speed down when already engaged) |
| `o` | Resume / increase speed |
| `u` | Cancel — hands control back to you |
| `,` | Left turn signal → openpilot changes lanes left |
| `.` | Right turn signal → openpilot changes lanes right |

Cruise keys are read from the keyboard device directly (via `evdev`), because BeamNG holds
window focus while you drive. They are `i`/`o`/`u` rather than the more obvious `c`/`v`/`b`
because those collide with BeamNG's own defaults (`c` cycles the camera).

Lane changes need >20 mph and openpilot engaged.

### Watch what it's doing

In a second terminal:

```bash
uv run python tools/beampilot_monitor.py
```

A live view of every channel's rate, what openpilot thinks the car is doing (`carState`), what
it's commanding, what the driving model predicts, and whether the turn limits are actually
binding. This is the first thing to run when something looks wrong.

`tools/beampilot_diag.py` is a one-shot version that also checks the raw UDP link from the mod.

---

## Requirements

* **Linux.** Tested on Arch; Ubuntu should be fine. (openpilot itself is Linux-only.)
* **BeamNG.drive** on Steam — the native Linux build, no Proton needed.
* **A GPU with a working [tinygrad](https://tinygrad.org) backend** (NVIDIA or AMD), which will
  be running the driving model *at the same time as* the game renders. A single mid-range card
  can do both, but expect the model to be the thing that suffers first.
* 4 GB VRAM / 16 GB RAM for standard models; 8 GB / 32 GB for chestnut-class.
* Membership in the `input` group (for reading the keyboard, and for `/dev/uinput` if you use
  the virtual-wheel control mode).

These are conservative, not hard-tested limits.

---

## How it works

```
BeamNG.drive
  │  Lua mod (tools/beamng_mod/beampilot_bridge)
  │    UDP 49152  telemetry out ────────────┐
  │    UDP 49153  control in   ◄──────────┐ │
  ▼                                       │ │
screen ──► beamcamd ──► VisionIPC ────────┼─┼──► modeld ──► modelV2
                                          │ │                 │
             beamngd ─────────────────────┘ │            plannerd
               ▲   fake CAN / IMU / GPS      │            controlsd
               └──────────────────────────────┘                │
                                              controlsState.desiredCurvature
```

**The steering path, end to end:**

1. `beamcamd` captures the BeamNG window (via `mss`), converts to NV12, publishes at 20 Hz
2. `modeld` — stock openpilot — runs the driving model and predicts a path
3. `plannerd`/`controlsd` — stock — turn that into a desired curvature (1/m)
4. `beamngd` converts curvature → steering wheel angle using opendbc's `VehicleModel`, maps it
   to BeamNG's `-1..1` input range, and sends it as JSON over UDP
5. The Lua mod applies it with `input.event("steering", v, FILTER_DIRECT)` — the same hook
   BeamNG's own AI driver uses

CAN traffic only flows *into* openpilot. `beamngd` synthesises Honda Civic 2022 CAN frames from
BeamNG telemetry so openpilot believes it's plugged into a real car; nothing is ever sent back
over CAN to the game.

### Why a Lua mod instead of a virtual gamepad

BeamNG auto-discovers any `.lua` file in a mod's `lua/vehicle/protocols/` folder and calls it
every physics tick — the same mechanism its stock `outgauge`/`motionSim` protocols use. That
gives direct access to `electrics.values.*` for telemetry and `input.event()` for control,
with no in-game binding and no OS-level fake device.

A `uinput` virtual wheel is also implemented as an alternative (`BEAMPILOT_CONTROL_MODE=joystick`),
mainly because [jackz314's ETS2/ATS bridge](https://github.com/jackz314/openpilot/tree/master/tools/truck_sim)
has to work that way — ETS2 has no scriptable input hook. BeamNG does, so the Lua path is
default and better.

---

## Configuration

Everything lives in **`config_beampilot.sh`**, which both scripts source.

### Car and hardware

| Setting | Default | Notes |
|---|---|---|
| `FINGERPRINT` | `HONDA_CIVIC_2022` | The car openpilot believes it's driving. **Changing this breaks `beamngd`** — CAN message layouts are per-car. |
| `SKIP_FW_QUERY` | `1` | Skip firmware fingerprinting. Must stay `1`. |
| `USE_NV` / `USE_AMD` | `USE_NV=1` | Pick one for your GPU. |
| `BIG` | `1` | Screen size: `1` = comma 3/3X (2160x1080), `0` = comma 4 (536x240, tiny). Not a scale knob — use `SCALE` for that. |
| `CHESTNUT` | `0` | Chestnut-class (eGPU) models. Needs 8 GB+ VRAM. |

### Driving behaviour

Stock openpilot follows EU/ISO passenger-comfort limits, which in a simulator are usually the
reason it won't take a corner or get up to speed. Each of these **defaults to the stock
upstream value if unset**, so commenting a line out restores unmodified openpilot.

| Setting | Stock | Effect |
|---|---|---|
| `BEAMPILOT_MAX_LAT_ACCEL` | `3.0` m/s² | Turning. Max curvature is `accel / v²` — stock allows only a ~300 m radius at 67 mph. |
| `BEAMPILOT_MAX_LAT_JERK` | `5.0` m/s³ | How fast curvature may change. **Raise cautiously — the likeliest cause of weaving.** |
| `BEAMPILOT_MAX_CURVATURE` | `0.2` 1/m | Geometric cap; only binds below ~11 mph. |
| `BEAMPILOT_ACCEL_SCALE` | `1.0` | Multiplier on the whole acceleration envelope. |
| `BEAMPILOT_DECEL_SCALE` | `1.0` | Same, for braking. |
| `BEAMPILOT_PERSONALITY` | `1` | Following distance: `0` aggressive (1.25 s), `1` standard (1.45 s), `2` relaxed (1.75 s). |

These are *permission* limits, not commands — raising them lets openpilot turn harder, it
doesn't make it want to. The monitor tells you whether a limit is actually binding.

### Simulator conveniences

| Setting | Effect |
|---|---|
| `BEAMPILOT_AUTO_LANE_CHANGE` | Commit a lane change from the blinker alone. Required here — stock also wants a steering-wheel nudge, which can't happen with no wheel. |
| `BEAMPILOT_IGNORE_COMM_ISSUE` | Stop inter-process timing hiccups from disengaging. **Behavioural, not cosmetic** — see [Safety](#safety). |
| `BEAMPILOT_CONTROL_MODE` | `lua` (default) or `joystick`. |
| `BEAMPILOT_CAM_MONITOR` | Which monitor to capture (default `1`). |
| `BEAMPILOT_CAM_REGION` | `left,top,width,height`, for a windowed BeamNG. |
| `BLOCK` | Comma-separated processes not to start. Includes `soundd` by default to mute alert chimes. |

---

## What's different from [ko6lvm/beampilot](https://github.com/ko6lvm/beampilot)

This fork started from Ryan's beampilot, which laid out the architecture — the process list,
the `beamngd`/`beamcamd` split, the config/launch scripts, and the goal of a BeamNG bridge.
At that point both daemons were stubs: `beamngd` published zeroed CAN/IMU/GPS and `beamcamd`
published a black frame, both marked *"currently inop"*.

**~1,650 lines changed across 22 files.** The substance:

### Made it actually drive

* **`beamngd` rewritten** from a zeroed stub into a real bridge: parses live telemetry from the
  mod, synthesises Honda CAN, reads cruise buttons from the keyboard via `evdev`, and converts
  openpilot's desired curvature into BeamNG steering using opendbc's `VehicleModel`.
* **`beamcamd` rewritten** from a black-frame generator into a real screen-capture pipeline
  (`mss` + OpenCV NV12 conversion), meeting the 20 Hz target (from 15.3 Hz before optimisation).
* **New BeamNG mod** (`tools/beamng_mod/beampilot_bridge`) — a Lua protocol that streams
  telemetry out and applies openpilot's control via `input.event()`.
* **`abeamngd.py` deleted** — dead code that depended on the unavailable `beamngpy`.

### Bugs that were blocking it

* **Camera calibration never converged.** `calibrationd` needs minutes of sustained straight
  highway driving before it trusts its extrinsics; until then the model's road-frame transform
  is wrong, which shows up as steering bias — not as an obvious error. Now seeded at launch.
* **Publish rates were badly wrong.** Measured: `gpsLocationExternal` at **1000 Hz (100× over
  spec)**, IMU at 5×, `pandaStates` 5× too slow. `SimulatedSensors`' burst helpers are sized
  for a ~20 Hz caller and `beamngd` ticks at 100 Hz. Wrong rates trigger `commIssue`, which
  both blocks engagement *and* disengages mid-drive.
* **Cruise speed was always zero.** This car is Bosch, so the set speed comes from
  `ACC_HUD.CRUISE_SPEED` on the camera bus — not `CRUISE.CRUISE_SPEED_PCM`, which is only read
  for non-Bosch Hondas.
* **openpilot wasn't doing longitudinal control at all.** Without `AlphaLongitudinalEnabled`,
  honda's interface leaves `pcmCruise=True` — openpilot steers but expects *the car's own ACC*
  to manage speed. BeamNG has no ACC, so nothing ever accelerated.
* **A throttle feedback loop disengaged it instantly.** BeamNG's throttle reading, while
  engaged, is openpilot's own command echoed back. Reporting it as the *driver's* pedal made
  `gasPressed` true and tripped the pedal-override disengage the moment it tried to accelerate.
* **Fake camera timestamps** made `locationd` reject every frame. (This same bug exists,
  unfixed, in openpilot's own `webcam/camerad.py` — it's just never exercised there.)
* **Controls Mismatch** — `pandaStates` hardcoded safety flags instead of mirroring the real
  `carParams.safetyConfigs`.

### Added

* **Lane changes** — signal-only, since there's no wheel to nudge.
* **Tunable driving limits** — the table above; all default to stock.
* **Diagnostics** — `tools/beampilot_monitor.py` (live) and `tools/beampilot_diag.py`.
* **`CLAUDE.md`** — architecture notes and a hard-won-gotchas list, so the debugging above
  doesn't have to be repeated.
* **Alert suppression** for simulator noise, gated so real-car behaviour is unchanged.

---

## Known issues

* `carState.vCruise` reports an implausible set speed (~240 mph) while `cruiseState.speed` is
  correct.
* **Steering geometry mismatch.** openpilot computes wheel angle from the *Civic's* steer ratio
  (15.38) and wheelbase (2.700 m), but the BeamNG vehicle has its own geometry — only its 510°
  steering lock has been measured. When commanded curvature isn't achieved, the car runs wide.
  The proper fix is to calibrate the mapping against measured yaw rate.
* `beamcamd` drops ~6% of frames; driver-monitoring channels publish at 33 Hz vs 20 expected.
* Model performance depends on the GPU also rendering the game — expect occasional
  `observation too old` bursts from `locationd` under load.

---

## Safety

**This is for simulation only.** Driver monitoring (`dmonitoringmodeld`/`dmonitoringd`) is
removed, and several safety alerts are suppressed under `SIMULATION`. Disabling driver
monitoring in a real vehicle is dangerous and violates comma's requirements. Do not put this
on a car.

One flag deserves specific attention: `BEAMPILOT_IGNORE_COMM_ISSUE` stops inter-process
communication failures from disengaging openpilot. That is not cosmetic — if `modeld` stalls
or dies, openpilot will keep steering on stale model output instead of handing back control.
It's opt-in and deliberately separate from the display-only suppressions. The underlying event
is still logged.

---

## Process changes vs. stock openpilot

**Removed** — hardware-specific or unnecessary on a desktop:
`camerad`, `webcamerad` (→ `beamcamd`) · `sensord`, `pandad`, `_pandad` (→ `beamngd`) ·
`micd` · `dmonitoringmodeld`, `dmonitoringd` · `updated` · `qcomgpsd`, `ubloxd`, `pigeond` ·
`modem`

**Added:**
* `beamngd` — telemetry, fake CAN/IMU/GPS, and control output (100 Hz)
* `beamcamd` — screen capture → camera frames (20 Hz)

Everything else — `modeld`, `controlsd`, `selfdrived`, `plannerd`, `locationd`, `calibrationd`,
`torqued`, `paramsd`, `radard`, `card`, `ui` — is stock openpilot, unmodified.

---

## Development

Architecture notes, the full gotcha list, and debugging guidance live in
[`CLAUDE.md`](CLAUDE.md). Read it before changing `beamngd`, `beamcamd`, or the Lua mod — most
of the non-obvious constraints are written down there.

Mod edits under `tools/beamng_mod/` apply live; reload with **Ctrl+L** in game.

```bash
# lint
uv run ruff check .

# individual processes
source .venv/bin/activate
openpilot/selfdrive/modeld/modeld.py
openpilot/tools/replay/replay --demo -b modelV2,drivingModelData,cameraOdometry
BIG=1 openpilot/selfdrive/ui/ui.py
```

---

## Credits

* **[commaai/openpilot](https://github.com/commaai/openpilot)** — the driver assistance system
  this is built on. MIT licensed; see [LICENSE](LICENSE).
* **[ko6lvm/beampilot](https://github.com/ko6lvm/beampilot)** — Ryan's original beampilot fork,
  which established the architecture and process layout this builds directly on.
* **[jackz314/openpilot](https://github.com/jackz314/openpilot)** — the ETS2/ATS `truck_sim`
  bridge, useful prior art for structure (built on openpilot 0.8.6).
* **[BeamNG.drive](https://www.beamng.com/game/)** — for exposing enough to modders that this
  was possible without the research licence.

## License

MIT, inherited from openpilot. See [LICENSE](LICENSE).
