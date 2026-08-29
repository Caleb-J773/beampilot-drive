#!/usr/bin/env python3
"""Regression tests for the once-per-vehicle steering measurement handshake."""
from types import SimpleNamespace
import unittest

from openpilot.common.beampilot_vehicle import (RATIO_CACHE_MIN_SAMPLES, VehicleGeometryReceiver,
                                                encode)
from openpilot.selfdrive.beamngd.beamngd import BeamNGBridge


class FakeRatioCache:
  def __init__(self, ratio=None):
    self.ratio = ratio

  def get(self, name, lock):
    return self.ratio


def bridge_with_geometry(name="etk800", values=None, samples=0, cached=None):
  bridge = BeamNGBridge.__new__(BeamNGBridge)
  bridge.geometry = SimpleNamespace(name=name, values=values or {}, samples=samples)
  bridge.ratio_cache = FakeRatioCache(cached)
  return bridge


class TestCalibrationHandshake(unittest.TestCase):
  def test_no_sweep_before_the_first_identity_packet(self):
    bridge = bridge_with_geometry(name="", values={})
    self.assertFalse(bridge.ratio_calibration_needed())
    self.assertEqual(bridge.ratio_calibration_key(), "")

  def test_cached_vehicle_does_not_sweep(self):
    bridge = bridge_with_geometry(values={"steerLockDeg": 510.0}, cached=14.7)
    self.assertTrue(bridge.ratio_is_known())
    self.assertFalse(bridge.ratio_calibration_needed())
    self.assertEqual(bridge.ratio_calibration_key(), "")

  def test_identified_uncached_vehicle_does_sweep(self):
    bridge = bridge_with_geometry(values={"steerLockDeg": 510.0})
    self.assertFalse(bridge.ratio_is_known())
    self.assertTrue(bridge.ratio_calibration_needed())
    self.assertEqual(bridge.ratio_calibration_key(), "etk800|510")

  def test_thin_live_ratio_does_not_cancel_the_sweep(self):
    bridge = bridge_with_geometry(values={"steerLockDeg": 510.0, "steerRatio": 14.7},
                                  samples=RATIO_CACHE_MIN_SAMPLES - 1)
    self.assertFalse(bridge.ratio_is_known())
    self.assertTrue(bridge.ratio_calibration_needed())

  def test_persistable_live_ratio_finishes_the_sweep(self):
    bridge = bridge_with_geometry(values={"steerLockDeg": 510.0, "steerRatio": 14.7},
                                  samples=RATIO_CACHE_MIN_SAMPLES)
    self.assertTrue(bridge.ratio_is_known())
    self.assertFalse(bridge.ratio_calibration_needed())

  def test_unstable_identity_is_not_swept_forever(self):
    bridge = bridge_with_geometry(name="table: 0x7fadead", values={"steerLockDeg": 510.0})
    self.assertFalse(bridge.ratio_calibration_needed())


class TestCacheThresholdDelivery(unittest.TestCase):
  def test_crossing_cache_threshold_is_an_update_even_if_values_are_steady(self):
    class PacketQueue:
      def __init__(self):
        self.packets = []

      def recv(self, _size):
        if not self.packets:
          raise BlockingIOError
        return self.packets.pop(0)

    receiver = VehicleGeometryReceiver.__new__(VehicleGeometryReceiver)
    receiver.sock = PacketQueue()
    receiver.name = ""
    receiver.values = {}
    receiver.samples = 0
    receiver.last_packet = 0.0
    receiver.updated = False
    values = {
      "wheelbase": 2.8,
      "centerToFront": 1.2,
      "trackWidth": 1.6,
      "mass": 1500.0,
      "rotationalInertia": 2500.0,
      "steerRatio": 14.7,
      "steerLockDeg": 510.0,
      "maxWheelAngleDeg": 34.7,
    }

    receiver.sock.packets.append(encode("etk800", values, RATIO_CACHE_MIN_SAMPLES - 1))
    self.assertTrue(receiver.update())
    receiver.sock.packets.append(encode("etk800", values, RATIO_CACHE_MIN_SAMPLES))
    self.assertTrue(receiver.update())


if __name__ == "__main__":
  unittest.main(verbosity=2)
