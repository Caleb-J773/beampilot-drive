import time

from openpilot.cereal import log
import openpilot.cereal.messaging as messaging

from openpilot.common.realtime import DT_DMON
from openpilot.tools.sim.lib.camerad import Camerad

from typing import TYPE_CHECKING
if TYPE_CHECKING:
  from openpilot.tools.sim.lib.common import World, SimulatorState


class SimulatedSensors:
  """Simulates the C3 sensors (acc, gyro, gps, peripherals, dm state, cameras) to OpenPilot"""

  def __init__(self, dual_camera=False, create_camera=True):
    self.pm = messaging.PubMaster(['accelerometer', 'gyroscope', 'gpsLocationExternal', 'driverStateV2', 'driverMonitoringState', 'peripheralState'])
    # create_camera=False lets a bridge that owns its own camera pipeline (a
    # separate process publishing wideRoadCameraState/narrowRoadCameraState,
    # e.g. beampilot's beamcamd) use this class without a second, conflicting
    # VisionIpcServer("camerad")/PubMaster for those same channels.
    self.camerad = Camerad(dual_camera=dual_camera) if create_camera else None
    self.last_perp_update = 0
    self.last_dmon_update = 0

  def send_imu_message(self, simulator_state: 'SimulatorState', count: int = 5):
    # count is how many duplicate samples to emit per call: the default of 5
    # assumes a ~20Hz caller (5 x 20 = 100Hz, roughly the 104Hz accelerometer/
    # gyroscope rate SERVICE_LIST expects). A caller already ticking at 100Hz
    # should pass count=1 -- otherwise it publishes at 500Hz, ~5x the expected
    # rate, which wastes CPU serializing capnp messages and shrinks locationd's
    # fixed-size (512 checkpoint) EKF rewind buffer to ~1s of history, right at
    # the edge of the CAM_ODO_POSE_DELAY rewinds cameraOdometry needs.
    for _ in range(count):
      dat = messaging.new_message('accelerometer', valid=True)
      dat.accelerometer.timestamp = dat.logMonoTime  # TODO: use the IMU timestamp
      dat.accelerometer.init('acceleration')
      dat.accelerometer.acceleration.v = [simulator_state.imu.accelerometer.x, simulator_state.imu.accelerometer.y, simulator_state.imu.accelerometer.z]
      self.pm.send('accelerometer', dat)

      dat = messaging.new_message('gyroscope', valid=True)
      dat.gyroscope.timestamp = dat.logMonoTime  # TODO: use the IMU timestamp
      dat.gyroscope.init('gyroUncalibrated')
      dat.gyroscope.gyroUncalibrated.v = [simulator_state.imu.gyroscope.x, simulator_state.imu.gyroscope.y, simulator_state.imu.gyroscope.z]
      self.pm.send('gyroscope', dat)

  def send_gps_message(self, simulator_state: 'SimulatorState', count: int = 10):
    # As with send_imu_message: the default of 10 assumes a ~20Hz caller. A
    # 100Hz caller must pass count=1 AND rate-limit calls to ~10Hz to match the
    # 10Hz gpsLocationExternal rate SERVICE_LIST expects -- at 100Hz x 10 this
    # publishes 1000 messages/sec, 100x the expected rate.
    if not simulator_state.valid:
      return

    # transform from vel to NED
    velNED = [
      -simulator_state.velocity.y,
      simulator_state.velocity.x,
      simulator_state.velocity.z,
    ]

    for _ in range(count):
      dat = messaging.new_message('gpsLocationExternal', valid=True)
      dat.gpsLocationExternal = {
        "unixTimestampMillis": int(time.time() * 1000),  # noqa: TID251
        "flags": 1,  # valid fix
        "horizontalAccuracy": 1.0,
        "verticalAccuracy": 1.0,
        "speedAccuracy": 0.1,
        "bearingAccuracyDeg": 0.1,
        "vNED": velNED,
        "bearingDeg": simulator_state.imu.bearing,
        "latitude": simulator_state.gps.latitude,
        "longitude": simulator_state.gps.longitude,
        "altitude": simulator_state.gps.altitude,
        "speed": simulator_state.speed,
        "source": log.GpsLocationData.SensorSource.ublox,
      }

      self.pm.send('gpsLocationExternal', dat)

  def send_peripheral_state(self):
    dat = messaging.new_message('peripheralState')
    dat.valid = True
    dat.peripheralState = {
      'pandaType': log.PandaState.PandaType.blackPanda,
      'voltage': 12000,
      'current': 5678,
      'fanSpeedRpm': 1000
    }
    self.pm.send('peripheralState', dat)

  def send_fake_driver_monitoring(self):
    # dmonitoringmodeld output
    dat = messaging.new_message('driverStateV2')
    dat.driverStateV2.leftDriverData.faceOrientation = [0., 0., 0.]
    dat.driverStateV2.leftDriverData.faceProb = 1.0
    dat.driverStateV2.rightDriverData.faceOrientation = [0., 0., 0.]
    dat.driverStateV2.rightDriverData.faceProb = 1.0
    self.pm.send('driverStateV2', dat)

    # dmonitoringd output
    dat = messaging.new_message('driverMonitoringState', valid=True)
    dm = dat.driverMonitoringState
    dm.alertLevel = log.DriverMonitoringState.AlertLevel.none
    dm.activePolicy = log.DriverMonitoringState.MonitoringPolicy.vision
    dm.visionPolicyState.faceDetected = True
    dm.visionPolicyState.isDistracted = False
    dm.visionPolicyState.awarenessPercent = 100
    self.pm.send('driverMonitoringState', dat)

  def send_camera_images(self, world: 'World'):
    world.image_lock.acquire()
    yuv = self.camerad.rgb_to_yuv(world.road_image)
    self.camerad.cam_send_yuv_road(yuv)

    if world.dual_camera:
      yuv = self.camerad.rgb_to_yuv(world.wide_road_image)
      self.camerad.cam_send_yuv_wide_road(yuv)

  def update(self, simulator_state: 'SimulatorState', world: 'World'):
    now = time.monotonic()
    self.send_imu_message(simulator_state)
    self.send_gps_message(simulator_state)

    if (now - self.last_dmon_update) > DT_DMON/2:
      self.send_fake_driver_monitoring()
      self.last_dmon_update = now

    if (now - self.last_perp_update) > 0.25:
      self.send_peripheral_state()
      self.last_perp_update = now
