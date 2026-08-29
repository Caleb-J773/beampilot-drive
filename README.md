# beampilot-drive

openpilot, driving a car in BeamNG.drive.

This is a fork of [openpilot](https://github.com/commaai/openpilot) that replaces the camera and
CAN bus with a screen capture of BeamNG.drive and a small Lua mod. The driving stack itself is
untouched: the same `modeld`, `controlsd`, `plannerd` and `locationd` that run on comma hardware
run here, and they have no idea they're looking at a video game.

It works with the normal Steam version of the game. You don't need BeamNG.tech, and there's no
`beamngpy` dependency — telemetry and control both go through BeamNG's ordinary modding hooks.

## What works

It steers, holds a set speed, brakes for traffic, and changes lanes when you signal.

It's not polished. The steering geometry is still calibrated for a Honda Civic rather than
whatever car you spawn, there's no real wide-angle camera, and it will happily drive into things
if you let it. See [Known problems](#known-problems).

## Setup

You'll need Linux, BeamNG.drive on Steam, and a GPU that tinygrad supports. The GPU renders the
game and runs the driving model at the same time, so a weaker card shows up as dropped frames
before anything else.

```bash
git clone --recurse-submodules https://github.com/Caleb-J773/beampilot-drive.git
cd beampilot-drive
./setup_beampilot.sh
```

That builds openpilot and symlinks the BeamNG mod into your userfolder. Run BeamNG at least once
first so the folder exists.

There's a config tool if you'd rather not edit shell variables by hand:

```bash
uv run python tools/beampilot_tui.py
```

It detects your GPUs, explains what each setting does, and writes to `config_beampilot.sh`. That
file is still the source of truth, so editing it directly works fine too — the tool reads
whatever you've written and preserves your comments.

## Running it

Start BeamNG, spawn a car, then:

```bash
source .venv/bin/activate.fish   # bash/zsh: source .venv/bin/activate
./launch_beampilot.sh
```

You get a five second countdown to alt-tab back into the game. Get above about 20 mph and press
`i`.

| Key | |
|---|---|
| `i` | Set speed and engage. Nudges the speed down if already engaged. |
| `o` | Resume, or speed up. |
| `u` | Cancel. |
| `,` `.` | Turn signals. Signal while engaged and it changes lanes. |

These are read straight from the keyboard device, because BeamNG has window focus while you're
driving. They're `i`/`o`/`u` instead of something more memorable because most of the good keys
are already bound in BeamNG (`c` cycles the camera). You can rebind them in the config.

To see what it's thinking:

```bash
uv run python tools/beampilot_monitor.py
```

Live rates for every message channel, what openpilot believes the car is doing, what it's
commanding, and whether the turn limits are actually constraining it. Start here when something
looks wrong. `tools/beampilot_diag.py` is a one-shot version that also checks the raw UDP link
from the mod.

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

`beamcamd` grabs the game window and publishes it as camera frames. `modeld` predicts a path
from those frames. `controlsd` turns that into a desired curvature. `beamngd` converts curvature
into a steering angle using opendbc's vehicle model, scales it to BeamNG's input range, and
sends it over UDP. The Lua mod applies it with `input.event()`, which is the same function
BeamNG's own AI driver uses.

CAN only goes one direction. `beamngd` fabricates Honda Civic CAN frames from BeamNG telemetry
so openpilot thinks it's plugged into a real car. Nothing is ever sent back to the game over CAN.

BeamNG runs any `.lua` file you drop in a mod's `lua/vehicle/protocols/` folder on every physics
tick, which is how the stock OutGauge and MotionSim protocols work. That gives direct access to
`electrics.values.*` for telemetry and `input.event()` for control, without binding anything
in-game or creating a fake input device.

There's also a `uinput` virtual wheel (`BEAMPILOT_CONTROL_MODE=joystick`) that exists mostly
because [jackz314's ETS2 bridge](https://github.com/jackz314/openpilot/tree/master/tools/truck_sim)
has to work that way — ETS2 has no scripting hook. BeamNG does, so the Lua path is the default
and the better one.

## Configuration

Everything lives in `config_beampilot.sh`. Every beampilot setting falls back to the stock
openpilot value when unset, so deleting a line gets you unmodified behaviour rather than a crash.

### Hardware

`USE_NV` or `USE_AMD` picks the GPU backend — set one. `CHESTNUT=1` selects the larger model
(8 GB VRAM instead of 4). `BIG` is the window resolution: `1` is the comma 3/3X screen at
2160x1080, `0` is the comma 4 at 536x240, which is unreadably small on a desktop. `SCALE`
multiplies that, so `0.6` gets you about 1296x648.

### Driving limits

Stock openpilot follows EU and ISO passenger comfort guidelines. In a simulator those are
usually the reason it won't take a corner:

| | Stock | |
|---|---|---|
| `BEAMPILOT_MAX_LAT_ACCEL` | 3.0 m/s² | Turning. Maximum curvature is `accel / v²`, so stock only allows about a 300 m radius at 67 mph. |
| `BEAMPILOT_MAX_LAT_JERK` | 5.0 m/s³ | How fast curvature can change. Raise this carefully; it's the usual cause of weaving. |
| `BEAMPILOT_MAX_CURVATURE` | 0.2 1/m | Geometric cap. Only matters below about 11 mph. |
| `BEAMPILOT_ACCEL_SCALE` | 1.0 | Multiplier on the acceleration envelope. |
| `BEAMPILOT_DECEL_SCALE` | 1.0 | Same for braking. |
| `BEAMPILOT_PERSONALITY` | 1 | Following distance. 0 is aggressive (1.25 s), 2 is relaxed (1.75 s). |

Raising a limit doesn't make openpilot want to turn harder, it just stops clipping it when it
does. The monitor tells you whether a limit is actually binding, which is worth checking before
you tune anything.

### Vehicle

`BEAMPILOT_STEER_LOCK_DEG` (default 510) is your BeamNG car's steering lock. Hold full lock and
read `steering_wheel_deg` in the monitor to find yours. Too low and openpilot oversteers, too
high and it runs wide. `BEAMPILOT_STEER_SWEEP_SECONDS` controls how quickly it chases a steering
target; lower is snappier and twitchier.

`FINGERPRINT` is the car openpilot thinks it's driving. Changing it breaks `beamngd`, which
packs Honda Bosch CAN specifically.

### Alerts

`BEAMPILOT_IGNORE_COMM_ISSUE` stops inter-process timing hiccups from disengaging. Unlike the
alerts suppressed under `SIMULATION`, this one changes behaviour rather than just what's on
screen: if `modeld` stalls, openpilot keeps steering on stale output instead of handing back
control. The event still gets logged.

`BLOCK` is a comma-separated list of processes not to start. `soundd` is in there by default,
which mutes the alert chimes.

## Known problems

**No wide-angle camera.** openpilot expects two cameras, a narrow one and a roughly 120° wide
one. `beamcamd` only has the one game view, so it publishes the same frame to both streams.
`modeld` sees a wide camera is present and applies the wide lens calibration
(`dc.wide_road.intrinsics`) to an image that doesn't have that field of view, so it misjudges
how far things sit off to the sides. This shows up worst in turns, since the wide camera is
normally what sees around a corner.

**Keep Experimental mode off** for the same reason. Its end-to-end longitudinal policy leans
much harder on wide-camera scene understanding, and it behaves noticeably worse here. Fixing
this properly means adding a second BeamNG camera at a genuinely wide FOV.

**Steering geometry mismatch.** openpilot computes a steering angle using the Civic's steer
ratio of 15.38 and wheelbase of 2.7 m. Your BeamNG car has neither. Only the steering lock has
been measured and made configurable, so commanded curvature and achieved curvature still don't
quite match, and the car runs wide. Calibrating the mapping against measured yaw rate is the
real fix.

**Other things.** `carState.vCruise` reports a nonsense set speed while `cruiseState.speed` is
correct. `beamcamd` drops around 6% of frames. Driver monitoring channels publish at 33 Hz where
20 is expected. Under GPU load `locationd` produces bursts of `observation too old`.

## Differences from [ko6lvm/beampilot](https://github.com/ko6lvm/beampilot)

Ryan's beampilot set up the architecture this builds on: the process list, the `beamngd` and
`beamcamd` split, the config and launch scripts, and the plan for a BeamNG bridge. Both daemons
were stubs at that point, marked "currently inop" — `beamngd` published zeroed CAN and
`beamcamd` published a black frame.

About 1,650 lines changed across 22 files. The substantial parts:

Both daemons were rewritten. `beamngd` now parses live telemetry, synthesises Honda CAN, reads
cruise buttons over evdev, and converts openpilot's desired curvature into BeamNG steering using
opendbc's `VehicleModel`. `beamcamd` became a real capture pipeline using mss and OpenCV, fast
enough for its 20 Hz target (it was managing 15.3 Hz before optimisation). The BeamNG mod is new.
`abeamngd.py` was deleted, since it depended on the unavailable `beamngpy`.

Several things were quietly broken and had to be found:

- `calibrationd` never converged, because it needs minutes of straight highway driving before it
  trusts its extrinsics. Until then the model's road frame is wrong, which looks like steering
  bias rather than an error. Now seeded at launch.
- Message rates were badly off. GPS was publishing at 1000 Hz against an expected 10, the IMU at
  5x, and `pandaStates` 5x too slow. `SimulatedSensors` emits bursts sized for a 20 Hz caller and
  `beamngd` ticks at 100. Bad rates trigger `commIssue`, which blocks engagement and disengages
  mid-drive.
- Cruise speed was always zero. This car is Bosch, so the set speed comes from
  `ACC_HUD.CRUISE_SPEED` on the camera bus, not `CRUISE.CRUISE_SPEED_PCM`.
- openpilot wasn't doing longitudinal control at all. Without `AlphaLongitudinalEnabled`, Honda's
  interface leaves `pcmCruise=True`, which means openpilot steers and waits for the car's own ACC
  to handle speed. BeamNG doesn't have one.
- BeamNG's throttle reading, while engaged, is openpilot's own command coming back. Reporting it
  as the driver's pedal made `gasPressed` true and tripped the pedal-override disengage the
  instant it tried to accelerate.
- Camera frames used a synthetic timestamp counter, so `locationd` rejected every one. The same
  bug is still in openpilot's own `webcam/camerad.py`; it just never runs there.
- `pandaStates` hardcoded safety flags instead of mirroring `carParams.safetyConfigs`, which
  produced a permanent Controls Mismatch.

Added since: signal-only lane changes, the tunable limits above, `tools/beampilot_tui.py` and
the two diagnostic tools, and `CLAUDE.md` with architecture notes and the list of things that
are non-obvious enough to be worth writing down.

## Process changes

Removed, being hardware-specific or pointless on a desktop: `camerad`, `webcamerad`, `sensord`,
`pandad`, `_pandad`, `micd`, `dmonitoringmodeld`, `dmonitoringd`, `updated`, `qcomgpsd`,
`ubloxd`, `pigeond`, `modem`.

Added: `beamngd` for telemetry, fake sensors and control at 100 Hz, and `beamcamd` for camera
frames at 20 Hz.

Everything else is stock.

## Safety

Simulation only. Driver monitoring is removed and several alerts are suppressed. Disabling
driver monitoring in a real car is dangerous and against comma's terms. Don't put this in a
vehicle.

## Development

`CLAUDE.md` has the architecture notes and a list of gotchas worth reading before touching
`beamngd`, `beamcamd` or the Lua mod. Most of the non-obvious constraints are written down there
so they don't have to be rediscovered.

Mod edits under `tools/beamng_mod/` apply live — reload with Ctrl+L in game.

```bash
uv run ruff check .
```

## Credits

Built on [openpilot](https://github.com/commaai/openpilot) by comma.ai, MIT licensed.
Forked from [ko6lvm/beampilot](https://github.com/ko6lvm/beampilot).
[jackz314/openpilot](https://github.com/jackz314/openpilot)'s ETS2 bridge was useful prior art.

MIT, inherited from openpilot. See [LICENSE](LICENSE).
