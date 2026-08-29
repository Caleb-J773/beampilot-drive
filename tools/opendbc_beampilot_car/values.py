"""The car beampilot is actually driving.

Until this existed, beampilot told openpilot it was a HONDA_CIVIC_2022. That was
never a lie about the CAN -- `beamngd` really does hand-pack Honda Bosch
radarless frames, and this platform keeps that DBC for exactly that reason --
but it was a lie about everything else. A Civic's steering rack, weight
distribution, engagement speeds and lateral tuning all came along with the
fingerprint, and the fake Honda's identity kept turning up in places it had no
business being.

BEAMPILOT is the same wire format with an honest name. Nothing here is a Honda's
except the message layout:

  * the geometry is a placeholder, because the real numbers are MEASURED. The
    mod reports the spawned vehicle's wheelbase, weight distribution, mass, yaw
    inertia and rack ratio, and `beamngd` patches them onto CarParams before it
    builds the VehicleModel (see openpilot/common/beampilot_vehicle.py). What is
    written below is only what gets used in the seconds before the first packet
    arrives, or if the feed is switched off.
  * no minimum engage speed and no minimum steer speed. Those exist on a real
    car because its EPS gives up at a walking pace; BeamNG's does not.
  * no `dashcamOnly`, no `alphaLongitudinalAvailable` gate -- openpilot drives
    the longitudinal here, because there is no ACC ECU to hand it back to.

The Honda CarState and CarController are reused verbatim rather than forked.
They branch on `flags` and on `DBC[carFingerprint]`, never on "is this a
Civic" -- so with BOSCH|BOSCH_RADARLESS set and the DBC registered they behave
identically, and every future opendbc fix to Honda's CAN parsing arrives for
free. Forking 700 lines to change a name would have been the worse trade.
"""
from opendbc.car import Bus, CarSpecs, PlatformConfig, Platforms
from opendbc.car.honda.values import DBC as HONDA_DBC
from opendbc.car.honda.values import HondaFlags


class CAR(Platforms):
  BEAMPILOT = PlatformConfig(
    # No car docs: this is not a car anyone can buy, and listing it would put a
    # simulator in opendbc's supported-vehicles table.
    [],
    CarSpecs(
      # Placeholders, overwritten by the mod's measurement of whatever BeamNG
      # spawned. Kept close to a mid-size car so the seconds before the first
      # geometry packet are unremarkable rather than wild.
      mass=1400.,
      wheelbase=2.7,
      steerRatio=15.0,
      centerToFrontRatio=0.44,
      # A real EPS quits below a walking pace and a real ACC refuses to engage
      # at all below its own floor. Neither is true of a simulator, and both
      # only ever showed up here as openpilot inexplicably refusing to steer.
      minEnableSpeed=-1.,
      minSteerSpeed=0.,
    ),
    # beamngd packs Honda Bosch radarless frames, so this is the layout that is
    # actually on the (fake) wire. Changing it means rewriting beamngd's packer.
    {Bus.pt: 'honda_bosch_radarless_generated'},
    # BOSCH so CanBus and the cruise state machine take the Bosch path;
    # RADARLESS because there is no radar ECU, which is also what puts the
    # powertrain and camera buses where beamngd sends them.
    flags=HondaFlags.BOSCH | HondaFlags.BOSCH_RADARLESS,
  )


DBC = CAR.create_dbc_map()

# Honda's CarState and CarController resolve their DBC through honda.values.DBC,
# which is built from Honda's own platforms and would KeyError on ours. Adding
# BEAMPILOT to that same dict object is what lets them be reused unmodified.
# Done here rather than in honda/values.py so the entire brand stays in one
# directory and nothing in opendbc's Honda support has to know we exist.
HONDA_DBC.update(DBC)
