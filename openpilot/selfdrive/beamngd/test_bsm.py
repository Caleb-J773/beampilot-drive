#!/usr/bin/env python3
"""Tests for the BSM side channel between beamngd and card.

Run with: uv run python openpilot/selfdrive/beamngd/test_bsm.py

Deliberately no BeamNG and no openpilot processes: this covers the wire format
and the fail-safe behaviour, which are the parts that break silently. The
detection itself lives in Lua and needs the game.
"""
import time
import unittest

from openpilot.common import beampilot_bsm as bsm


class TestDashLightBits(unittest.TestCase):
  def test_ignores_the_bits_that_were_already_taken(self):
    # leftBlinker | rightBlinker | parkingBrake | ignitionOn, no BSM
    self.assertEqual(bsm.flags_from_dash_lights(0b1111), 0)

  def test_extracts_each_bsm_bit(self):
    self.assertEqual(bsm.flags_from_dash_lights(1 << 4), bsm.FLAG_LEFT)
    self.assertEqual(bsm.flags_from_dash_lights(1 << 5), bsm.FLAG_RIGHT)
    self.assertEqual(bsm.flags_from_dash_lights(1 << 6), bsm.FLAG_LEFT_APPROACHING)
    self.assertEqual(bsm.flags_from_dash_lights(1 << 7), bsm.FLAG_RIGHT_APPROACHING)

  def test_extracts_alongside_the_other_dash_lights(self):
    dash = 0b1001 | (1 << 5)  # left blinker + ignition + right blind spot
    self.assertEqual(bsm.flags_from_dash_lights(dash), bsm.FLAG_RIGHT)


class TestResolve(unittest.TestCase):
  def setUp(self):
    self.previous = bsm.BSM_APPROACHING
  def tearDown(self):
    bsm.BSM_APPROACHING = self.previous

  def test_occupied_maps_to_the_matching_side(self):
    bsm.BSM_APPROACHING = False
    self.assertEqual(bsm.resolve(bsm.FLAG_LEFT), (True, False))
    self.assertEqual(bsm.resolve(bsm.FLAG_RIGHT), (False, True))
    self.assertEqual(bsm.resolve(bsm.FLAG_LEFT | bsm.FLAG_RIGHT), (True, True))

  def test_approaching_counts_only_when_enabled(self):
    bsm.BSM_APPROACHING = False
    self.assertEqual(bsm.resolve(bsm.FLAG_LEFT_APPROACHING), (False, False))
    bsm.BSM_APPROACHING = True
    self.assertEqual(bsm.resolve(bsm.FLAG_LEFT_APPROACHING), (True, False))
    self.assertEqual(bsm.resolve(bsm.FLAG_RIGHT_APPROACHING), (False, True))

  def test_clear_is_clear(self):
    self.assertEqual(bsm.resolve(0), (False, False))


class TestWire(unittest.TestCase):
  """A sender and a receiver on a port nothing else is using."""
  PORT = 49254

  def setUp(self):
    # hold_seconds=0: the flicker hold is tested on its own below, and it would
    # otherwise smear one case's answer into the next.
    self.rx = bsm.BlindSpotReceiver(port=self.PORT, hold_seconds=0.0)
    self.tx = bsm.BlindSpotSender(port=self.PORT)
    self.previous = bsm.BSM_APPROACHING
    bsm.BSM_APPROACHING = True

  def tearDown(self):
    bsm.BSM_APPROACHING = self.previous
    self.tx.close()
    self.rx.close()

  def _deliver(self, flags):
    self.tx.send(flags)
    time.sleep(0.02)  # loopback, but the datagram still has to be queued
    return self.rx.update()

  def test_round_trip(self):
    self.assertEqual(self._deliver(bsm.FLAG_LEFT), (True, False))
    self.assertEqual(self._deliver(bsm.FLAG_RIGHT), (False, True))
    self.assertEqual(self._deliver(0), (False, False))

  def test_latest_packet_wins(self):
    for flags in (bsm.FLAG_LEFT, bsm.FLAG_RIGHT, bsm.FLAG_LEFT | bsm.FLAG_RIGHT):
      self.tx.send(flags)
    time.sleep(0.02)
    self.assertEqual(self.rx.update(), (True, True))

  def test_no_packets_at_all_reads_clear(self):
    # card can start before beamngd; nothing must latch on an empty socket.
    self.assertEqual(self.rx.update(), (False, False))

  def test_stale_data_fails_to_clear_not_to_blocked(self):
    self.rx.stale_seconds = 0.05
    self.assertEqual(self._deliver(bsm.FLAG_LEFT | bsm.FLAG_RIGHT), (True, True))
    time.sleep(0.08)
    self.assertEqual(self.rx.update(), (False, False))

  def test_garbage_is_ignored(self):
    self.assertEqual(self._deliver(bsm.FLAG_LEFT), (True, False))
    self.tx.sock.sendto(b"nonsense", ("127.0.0.1", self.PORT))
    self.tx.sock.sendto(bsm.PACKET.pack(b"XXXX", 0), ("127.0.0.1", self.PORT))
    time.sleep(0.02)
    self.assertEqual(self.rx.update(), (True, False))


class TestTelemetryPacket(unittest.TestCase):
  """The bits as the mod actually packs them, through beamngd's own decoder.

  Guards the one thing that would break silently if either side's bit
  numbering drifted: the mod would keep sending telemetry, beamngd would keep
  accepting it, and BSM would just never fire.
  """

  @staticmethod
  def _packet(dash_lights: int) -> bytes:
    from openpilot.selfdrive.beamngd.beamngd import TELEMETRY_MAGIC, TELEMETRY_STRUCT
    return TELEMETRY_STRUCT.pack(TELEMETRY_MAGIC, 20.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1,
                                 dash_lights, *([0.0] * 15))

  def _decode(self, dash_lights: int):
    from openpilot.selfdrive.beamngd.beamngd import parse_telemetry
    telemetry = parse_telemetry(self._packet(dash_lights))
    self.assertIsNotNone(telemetry)
    return bsm.flags_from_dash_lights(telemetry.dash_lights)

  def test_lua_dl_bsm_bit_values(self):
    # These four literals are DL_BSM_* in beampilot.lua. If they change there,
    # they have to change here.
    self.assertEqual(self._decode(16), bsm.FLAG_LEFT)
    self.assertEqual(self._decode(32), bsm.FLAG_RIGHT)
    self.assertEqual(self._decode(64), bsm.FLAG_LEFT_APPROACHING)
    self.assertEqual(self._decode(128), bsm.FLAG_RIGHT_APPROACHING)

  def test_blinkers_and_ignition_still_decode_alongside(self):
    from openpilot.selfdrive.beamngd.beamngd import (DL_IGNITION_ON, DL_LEFT_BLINKER,
                                                     parse_telemetry)
    dash = DL_LEFT_BLINKER | DL_IGNITION_ON | 32
    telemetry = parse_telemetry(self._packet(dash))
    self.assertTrue(telemetry.dash_lights & DL_LEFT_BLINKER)
    self.assertTrue(telemetry.dash_lights & DL_IGNITION_ON)
    self.assertEqual(bsm.flags_from_dash_lights(telemetry.dash_lights), bsm.FLAG_RIGHT)


class TestHold(unittest.TestCase):
  """A car hovering on the zone boundary must not strobe the output.

  Downstream this is not cosmetic: desire_helper.py cancels a lane change that
  is already under way on this flag, so a single dropped scan would abort the
  manoeuvre and then immediately restart it.
  """
  PORT = 49255
  HOLD = 0.3

  def setUp(self):
    self.rx = bsm.BlindSpotReceiver(port=self.PORT, hold_seconds=self.HOLD)
    self.tx = bsm.BlindSpotSender(port=self.PORT)
    self.previous = bsm.BSM_APPROACHING
    bsm.BSM_APPROACHING = True

  def tearDown(self):
    bsm.BSM_APPROACHING = self.previous
    self.tx.close()
    self.rx.close()

  def _deliver(self, flags):
    self.tx.send(flags)
    time.sleep(0.02)
    return self.rx.update()

  def test_a_single_dropped_scan_does_not_clear_it(self):
    self.assertEqual(self._deliver(bsm.FLAG_LEFT), (True, False))
    self.assertEqual(self._deliver(0), (True, False), "cleared during the hold window")

  def test_it_does_clear_once_the_hold_expires(self):
    self.assertEqual(self._deliver(bsm.FLAG_LEFT), (True, False))
    self.tx.send(0)
    time.sleep(self.HOLD + 0.05)
    self.assertEqual(self.rx.update(), (False, False))

  def test_each_side_holds_independently(self):
    self.assertEqual(self._deliver(bsm.FLAG_LEFT), (True, False))
    self.assertEqual(self._deliver(bsm.FLAG_RIGHT), (True, True))
    time.sleep(self.HOLD + 0.05)
    self.tx.send(bsm.FLAG_RIGHT)
    time.sleep(0.02)
    self.assertEqual(self.rx.update(), (False, True))

  def test_a_dead_feed_clears_immediately_despite_the_hold(self):
    # Holding a warning we can no longer confirm would block lane changes for
    # the rest of the drive, so staleness has to win over the hold.
    self.rx.stale_seconds = 0.05
    self.assertEqual(self._deliver(bsm.FLAG_LEFT | bsm.FLAG_RIGHT), (True, True))
    time.sleep(0.08)
    self.assertEqual(self.rx.update(), (False, False))


class TestLuaConfig(unittest.TestCase):
  def test_keys_match_the_lua_defaults_table(self):
    """Every key beamngd pushes must exist in BSM_DEFAULTS in the mod.

    The mod resets any key it is not sent back to its own default, so a name
    that drifts here does not error -- it silently stops taking effect.
    """
    import re
    from pathlib import Path
    lua = Path(__file__).parents[3] / "tools/beamng_mod/beampilot_bridge/lua/vehicle/protocols/beampilot.lua"
    body = lua.read_text().split("local BSM_DEFAULTS = {", 1)[1].split("}", 1)[0]
    lua_keys = set(re.findall(r"^\s*(\w+)\s*=", body, re.MULTILINE))
    self.assertEqual(set(bsm.lua_config()), lua_keys)

  def test_every_value_is_a_number(self):
    # It is JSON-encoded into the control packet and read by a Lua table that
    # only accepts numbers and booleans.
    for key, value in bsm.lua_config().items():
      self.assertIsInstance(value, float, key)

  def test_disabling_approaching_zeroes_the_horizon(self):
    previous = bsm.BSM_APPROACHING
    try:
      bsm.BSM_APPROACHING = False
      self.assertEqual(bsm.lua_config()["approachS"], 0.0)
    finally:
      bsm.BSM_APPROACHING = previous


if __name__ == "__main__":
  unittest.main(verbosity=2)
