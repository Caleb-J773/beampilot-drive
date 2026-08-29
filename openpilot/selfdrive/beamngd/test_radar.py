#!/usr/bin/env python3
"""Tests for the ground-truth radar channel: mod -> card -> radarTracks.

Run with: uv run python openpilot/selfdrive/beamngd/test_radar.py

The wire format is defined in Python but WRITTEN in Lua, by hand, through a
LuaJIT ffi struct. Nothing in either language checks the other, so the last
test here runs the Lua harness for real and decodes what it produced -- a
disagreement about struct padding would otherwise show up as silently absent
lead cars in the game and nowhere else.
"""
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path

from openpilot.common import beampilot_radar as radar

REPO = Path(__file__).parents[3]
LUA_TEST = REPO / "tools/beamng_mod/test_beampilot_bsm.lua"


class TestWireFormat(unittest.TestCase):
  def test_round_trip(self):
    tracks = [(2, 15.5, 0.0, -5.0), (7, 40.25, 3.5, 1.25)]
    self.assertEqual(radar.decode(radar.encode(tracks)), tracks)

  def test_empty(self):
    self.assertEqual(radar.decode(radar.encode([])), [])

  def test_packet_size_is_exactly_header_plus_tracks(self):
    self.assertEqual(len(radar.encode([])), radar.HEADER.size)
    self.assertEqual(len(radar.encode([(1, 2.0, 3.0, 4.0)])),
                     radar.HEADER.size + radar.TRACK.size)
    # No padding between the 5-byte header and the first 4-byte-aligned field:
    # the Lua side has to declare #pragma pack(1) to match this.
    self.assertEqual(radar.HEADER.size, 5)
    self.assertEqual(radar.TRACK.size, 16)

  def test_more_tracks_than_the_cap_are_dropped_not_wrapped(self):
    encoded = radar.encode([(i, float(i), 0.0, 0.0) for i in range(radar.MAX_TRACKS + 10)])
    self.assertEqual(len(radar.decode(encoded)), radar.MAX_TRACKS)

  def test_garbage_is_rejected(self):
    self.assertIsNone(radar.decode(b""))
    self.assertIsNone(radar.decode(b"nonsense"))
    self.assertIsNone(radar.decode(b"XXXX\x00"))                        # wrong magic
    self.assertIsNone(radar.decode(radar.MAGIC + bytes([200])))         # count over the cap
    self.assertIsNone(radar.decode(radar.MAGIC + bytes([2]) + b"\x00" * 16))  # truncated


class TestReceiver(unittest.TestCase):
  PORT = 49256

  def setUp(self):
    self.rx = radar.RadarReceiver(port=self.PORT)
    import socket
    self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

  def tearDown(self):
    self.tx.close()
    self.rx.close()

  def _deliver(self, tracks):
    self.tx.sendto(radar.encode(tracks), ("127.0.0.1", self.PORT))
    time.sleep(0.02)
    return self.rx.update()

  def test_nothing_received_is_no_tracks(self):
    self.assertEqual(self.rx.update(), [])

  def test_round_trip_over_the_socket(self):
    self.assertEqual(self._deliver([(3, 12.0, -1.0, -2.0)]), [(3, 12.0, -1.0, -2.0)])

  def test_latest_packet_wins(self):
    self.tx.sendto(radar.encode([(1, 5.0, 0.0, 0.0)]), ("127.0.0.1", self.PORT))
    self.tx.sendto(radar.encode([(2, 9.0, 0.0, 0.0)]), ("127.0.0.1", self.PORT))
    time.sleep(0.02)
    self.assertEqual(self.rx.update(), [(2, 9.0, 0.0, 0.0)])

  def test_a_dead_feed_clears_rather_than_freezing_a_lead(self):
    # A frozen lead is worse than none: the planner would go on braking for a
    # car that is no longer in front of us.
    self.rx.stale_seconds = 0.05
    self.assertTrue(self._deliver([(1, 8.0, 0.0, 0.0)]))
    time.sleep(0.08)
    self.assertEqual(self.rx.update(), [])

  def test_garbage_does_not_disturb_the_last_good_tracks(self):
    self.assertTrue(self._deliver([(1, 8.0, 0.0, 0.0)]))
    self.tx.sendto(b"nonsense", ("127.0.0.1", self.PORT))
    time.sleep(0.02)
    self.assertEqual(self.rx.update(), [(1, 8.0, 0.0, 0.0)])


class TestLuaConfig(unittest.TestCase):
  def test_keys_match_the_lua_defaults_table(self):
    """A key that drifts does not error -- the mod resets it to its own
    default, so the setting silently stops taking effect."""
    import re
    lua = REPO / "tools/beamng_mod/beampilot_bridge/lua/vehicle/protocols/beampilot.lua"
    body = lua.read_text().split("local RADAR_DEFAULTS = {", 1)[1].split("}", 1)[0]
    self.assertEqual(set(radar.lua_config()), set(re.findall(r"^\s*(\w+)\s*=", body, re.MULTILINE)))

  def test_every_value_is_a_number(self):
    for key, value in radar.lua_config().items():
      self.assertIsInstance(value, float, key)

  def test_the_mods_track_cap_matches_ours(self):
    lua = REPO / "tools/beamng_mod/beampilot_bridge/lua/vehicle/protocols/beampilot.lua"
    import re
    found = re.search(r"local RADAR_MAX_TRACKS = (\d+)", lua.read_text())
    self.assertIsNotNone(found)
    self.assertEqual(int(found.group(1)), radar.MAX_TRACKS)


class TestAgainstTheRealLuaEncoder(unittest.TestCase):
  """Decode a packet the mod's own code actually produced.

  Skipped without luajit or a BeamNG install, because the Lua harness loads the
  game's real mathlib for vec3 and the OBB maths.
  """

  def test_python_can_decode_what_lua_wrote(self):
    if shutil.which("luajit") is None:
      self.skipTest("luajit not installed")
    beamng = os.environ.get("BEAMNG_DIR") or os.path.expanduser(
      "~/.local/share/Steam/steamapps/common/BeamNG.drive")
    if not os.path.exists(os.path.join(beamng, "lua/common/mathlib.lua")):
      self.skipTest(f"no BeamNG install at {beamng} (set BEAMNG_DIR)")

    dump = Path("/tmp") / f"beampilot_radar_{os.getpid()}.bin"
    env = {**os.environ, "BEAMPILOT_RADAR_DUMP": str(dump)}
    result = subprocess.run(["luajit", str(LUA_TEST)], cwd=REPO, env=env,
                            capture_output=True, text=True, timeout=120)
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertTrue(dump.exists(), "the Lua harness wrote no packet")
    try:
      tracks = radar.decode(dump.read_bytes())
    finally:
      dump.unlink(missing_ok=True)

    self.assertIsNotNone(tracks, "Python could not decode the packet Lua wrote")
    # The harness puts a lead 20m away closing at 5m/s and a car 45m away one
    # lane to the left; both vehicles are 4.5m long, so the gaps lose 2.25m at
    # each end. yRel is LEFT positive.
    self.assertEqual(len(tracks), 2, tracks)
    (id1, d1, y1, v1), (id2, d2, y2, v2) = tracks
    self.assertEqual(id1, 2)
    self.assertAlmostEqual(d1, 15.5, delta=0.15)
    self.assertAlmostEqual(y1, 0.0, delta=0.15)
    self.assertAlmostEqual(v1, -5.0, delta=0.15)
    self.assertEqual(id2, 3)
    self.assertAlmostEqual(d2, 40.5, delta=0.15)
    self.assertAlmostEqual(y2, 3.5, delta=0.15)


if __name__ == "__main__":
  unittest.main(verbosity=2)
