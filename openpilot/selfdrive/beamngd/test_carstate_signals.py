#!/usr/bin/env python3
"""Signals beampilot packs into the fake Honda CAN, decoded back out again.

Run with: uv run python openpilot/selfdrive/beamngd/test_carstate_signals.py

Three of these were being dropped on the floor: the mod sent gear and the
parking brake in its telemetry and nothing read them, and nothing ever produced
a steering rate at all. openpilot therefore believed the car was permanently in
drive, handbrake off, wheel never turning.

Packing something openpilot cannot decode fails silently -- the signal just
reads zero forever -- so this round-trips through the same DBC opendbc uses.
The mapping from these signal names to carState fields is one line each in
opendbc/car/honda/carstate.py; the names and value tables are what is easy to
get wrong.
"""
import unittest

from opendbc.can.packer import CANPacker
from opendbc.can.parser import CANParser
from openpilot.tools.sim.lib.common import SimulatorState
from openpilot.tools.sim.lib.simulated_car import GEAR_SHIFTER_VALUES, SimulatedCar

DBC = 'honda_bosch_radarless_generated'


class SignalRoundTrip(unittest.TestCase):
  """Packs exactly what SimulatedCar.send_can_messages would, then decodes it."""

  MESSAGES = ("GEARBOX_AUTO", "STEERING_SENSORS", "SCM_FEEDBACK")

  def setUp(self):
    self.packer = CANPacker(DBC)
    self.parser = CANParser(DBC, [], 0)
    # A message's values only ever materialise in parser.vl if vl has been
    # INDEXED for it -- update() alone leaves them at zero forever. Never bites
    # the real stack, where carstate.py reads cp.vl every single cycle, but it
    # makes a test that only reads at the end silently pass or fail on nothing.
    for name in self.MESSAGES:
      _ = self.parser.vl[name]
    # CANParser also ignores a packet whose timestamp has not advanced past the
    # last one.
    self.nanos = 0

  def pack(self, state: SimulatorState):
    """The three messages the fields under test ride in, packed exactly as
    SimulatedCar.send_can_messages packs them."""
    return [
      self.packer.make_can_msg("GEARBOX_AUTO", 0,
                               {"GEAR_SHIFTER": GEAR_SHIFTER_VALUES[state.gear]}),
      self.packer.make_can_msg("STEERING_SENSORS", 0, {
        "STEER_ANGLE": state.steering_angle,
        "STEER_ANGLE_RATE": state.steering_rate,
      }),
      self.packer.make_can_msg("SCM_FEEDBACK", 0, {
        "MAIN_ON": 1,
        "LEFT_BLINKER": state.left_blinker,
        "RIGHT_BLINKER": state.right_blinker,
        "PARKING_BRAKE_ON": state.parking_brake,
      }),
    ]

  def decode(self, state: SimulatorState):
    # A couple of ticks, not one: the first only establishes the parser's time
    # base. The messages are REPACKED each time rather than resent -- these
    # carry a counter, and the parser drops a repeat whose counter has not
    # moved. Neither is a concern in the real bridge, where SimulatedCar packs
    # fresh messages every tick.
    for _ in range(2):
      self.nanos += 10_000_000
      self.parser.update([(self.nanos, self.pack(state))])
    return {name: dict(self.parser.vl[name]) for name in self.MESSAGES}

  def test_the_defaults_are_what_was_hardcoded_before(self):
    # A bridge that never sets these must behave exactly as it did.
    state = SimulatorState()
    self.assertEqual(state.gear, "drive")
    self.assertFalse(state.parking_brake)
    self.assertEqual(state.steering_rate, 0.0)
    self.assertEqual(self.decode(state)["GEARBOX_AUTO"]["GEAR_SHIFTER"], 4)

  def test_every_gear_survives_the_round_trip(self):
    # carstate.py maps these through shifter_values, whose table is
    # VAL_ 401/419 GEAR_SHIFTER 1 "P" 2 "R" 3 "N" 4 "D" ...
    for gear, expected in (("park", 1), ("reverse", 2), ("neutral", 3), ("drive", 4)):
      state = SimulatorState()
      state.gear = gear
      self.assertEqual(self.decode(state)["GEARBOX_AUTO"]["GEAR_SHIFTER"], expected, gear)

  def test_reverse_is_distinguishable(self):
    # car_events.py raises reverseGear off this, which is what stops openpilot
    # engaging while the car is rolling backwards.
    state = SimulatorState()
    state.gear = "reverse"
    self.assertNotEqual(self.decode(state)["GEARBOX_AUTO"]["GEAR_SHIFTER"],
                        GEAR_SHIFTER_VALUES["drive"])

  def test_parking_brake(self):
    # carstate.py reads this off the SCM message for Bosch cars, NOT EPB_STATUS.
    state = SimulatorState()
    state.parking_brake = True
    self.assertEqual(self.decode(state)["SCM_FEEDBACK"]["PARKING_BRAKE_ON"], 1)
    state.parking_brake = False
    self.assertEqual(self.decode(state)["SCM_FEEDBACK"]["PARKING_BRAKE_ON"], 0)

  def test_parking_brake_does_not_disturb_the_blinkers(self):
    state = SimulatorState()
    state.parking_brake = True
    state.left_blinker = True
    decoded = self.decode(state)["SCM_FEEDBACK"]
    self.assertEqual(decoded["LEFT_BLINKER"], 1)
    self.assertEqual(decoded["RIGHT_BLINKER"], 0)
    self.assertEqual(decoded["PARKING_BRAKE_ON"], 1)

  def test_steering_rate_both_directions(self):
    for rate in (0.0, 42.0, -42.0, 500.0, -500.0):
      state = SimulatorState()
      state.steering_rate = rate
      decoded = self.decode(state)["STEERING_SENSORS"]["STEER_ANGLE_RATE"]
      # The DBC signal is scaled (-1,0) on some Honda variants, so check the
      # magnitude and that it is not stuck at zero, which is the old behaviour.
      self.assertAlmostEqual(abs(decoded), abs(rate), delta=1.0, msg=f"rate={rate}")

  def test_steering_angle_still_decodes_alongside_the_rate(self):
    state = SimulatorState()
    state.steering_angle = 123.4
    state.steering_rate = -60.0
    decoded = self.decode(state)["STEERING_SENSORS"]
    self.assertAlmostEqual(decoded["STEER_ANGLE"], 123.4, delta=0.5)


class TestSimulatedCarStillPacksEverything(unittest.TestCase):
  def test_gear_table_covers_every_state_the_bridge_can_report(self):
    # beamngd maps BeamNG's gearIndex onto these three; park is only reachable
    # if a future bridge reports it.
    self.assertEqual(set(GEAR_SHIFTER_VALUES), {"park", "reverse", "neutral", "drive"})

  def test_packer_accepts_the_full_message_set(self):
    # Catches a signal name that does not exist in the DBC, which make_can_msg
    # raises on -- the failure mode is beamngd dying at the first tick.
    car = SimulatedCar.__new__(SimulatedCar)   # no cereal sockets needed
    state = SimulatorState()
    state.valid = True
    state.gear = "reverse"
    state.parking_brake = True
    state.steering_rate = 12.5
    sent = []
    car.pm = type("FakePubMaster", (), {"send": lambda self, *a: sent.append(a)})()
    car.packer = CANPacker(DBC)
    SimulatedCar.send_can_messages(car, state)
    self.assertEqual(len(sent), 1, "send_can_messages did not publish")


if __name__ == "__main__":
  unittest.main(verbosity=2)
