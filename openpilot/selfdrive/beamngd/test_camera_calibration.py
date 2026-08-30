#!/usr/bin/env python3
"""Tests for the BeamNG UI -> beamngd camera calibration command."""
import json
import unittest

from openpilot.selfdrive.beamngd.beamngd import (CAMERA_COMMAND_MAGIC, CAMERA_RESET_COMMAND,
                                                 handle_camera_command)


class FakeParams:
  def __init__(self):
    self.removed: list[str] = []
    self.writes: list[tuple[str, bool, bool]] = []

  def remove(self, key: str):
    self.removed.append(key)

  def put_bool(self, key: str, value: bool, block: bool = False):
    self.writes.append((key, value, block))


def request(command=CAMERA_RESET_COMMAND, magic=CAMERA_COMMAND_MAGIC) -> bytes:
  return json.dumps({"magic": magic, "command": command}).encode()


class TestCameraCalibrationCommand(unittest.TestCase):
  def test_reset_clears_only_extrinsics_and_cycles_onroad(self):
    params = FakeParams()
    response = handle_camera_command(request(), params, engaged=False)
    self.assertTrue(response["ok"])
    self.assertEqual(params.removed, ["CalibrationParams"])
    self.assertEqual(params.writes, [("OnroadCycleRequested", True, True)])

  def test_reset_is_refused_while_engaged(self):
    params = FakeParams()
    response = handle_camera_command(request(), params, engaged=True)
    self.assertFalse(response["ok"])
    self.assertIn("Disengage", response["error"])
    self.assertEqual(params.removed, [])
    self.assertEqual(params.writes, [])

  def test_invalid_and_unknown_packets_do_nothing(self):
    for payload in (b"not json", request(magic="wrong"), request(command="steeringCalibration")):
      params = FakeParams()
      response = handle_camera_command(payload, params, engaged=False)
      self.assertFalse(response["ok"])
      self.assertEqual(params.removed, [])
      self.assertEqual(params.writes, [])


if __name__ == "__main__":
  unittest.main(verbosity=2)
