import traceback
import openpilot.cereal.messaging as messaging

from opendbc.can.packer import CANPacker
from opendbc.can.parser import CANParser
from opendbc.car.honda.values import HondaSafetyFlags
from openpilot.common.params import Params
from openpilot.selfdrive.pandad.pandad_api_impl import can_list_to_can_capnp
from openpilot.tools.sim.lib.common import SimulatorState


class SimulatedCar:
  """Simulates a honda civic 2022 (panda state + can messages) to OpenPilot"""
  packer = CANPacker("honda_bosch_radarless_generated")

  def __init__(self):
    self.pm = messaging.PubMaster(['can', 'pandaStates'])
    self.sm = messaging.SubMaster(['carControl', 'controlsState', 'carParams', 'selfdriveState'])
    self.cp = self.get_car_can_parser()
    self.idx = 0
    self.params = Params()
    self.obd_multiplexing = False

  @staticmethod
  def get_car_can_parser():
    dbc_f = 'honda_bosch_radarless_generated'
    checks = []
    return CANParser(dbc_f, checks, 0)

  def send_can_messages(self, simulator_state: SimulatorState):
    if not simulator_state.valid:
      return

    msg = []

    # *** powertrain bus ***

    speed = simulator_state.speed * 3.6 # convert m/s to kph
    msg.append(self.packer.make_can_msg("ENGINE_DATA", 0, {"XMISSION_SPEED": speed}))
    msg.append(self.packer.make_can_msg("WHEEL_SPEEDS", 0, {
      "WHEEL_SPEED_FL": speed,
      "WHEEL_SPEED_FR": speed,
      "WHEEL_SPEED_RL": speed,
      "WHEEL_SPEED_RR": speed
    }))

    msg.append(self.packer.make_can_msg("SCM_BUTTONS", 0, {"CRUISE_BUTTONS": simulator_state.cruise_button}))

    msg.append(self.packer.make_can_msg("GEARBOX_AUTO", 0, {"GEAR_SHIFTER": 4}))
    msg.append(self.packer.make_can_msg("GAS_PEDAL_2", 0, {}))
    msg.append(self.packer.make_can_msg("SEATBELT_STATUS", 0, {"SEATBELT_DRIVER_LATCHED": 1}))
    msg.append(self.packer.make_can_msg("STEER_STATUS", 0, {"STEER_TORQUE_SENSOR": simulator_state.user_torque}))
    msg.append(self.packer.make_can_msg("STEERING_SENSORS", 0, {"STEER_ANGLE": simulator_state.steering_angle}))
    msg.append(self.packer.make_can_msg("VSA_STATUS", 0, {}))
    msg.append(self.packer.make_can_msg("STANDSTILL", 0, {"WHEELS_MOVING": 1 if simulator_state.speed >= 1.0 else 0}))
    msg.append(self.packer.make_can_msg("STEER_MOTOR_TORQUE", 0, {}))
    msg.append(self.packer.make_can_msg("EPB_STATUS", 0, {}))
    msg.append(self.packer.make_can_msg("DOORS_STATUS", 0, {}))
    msg.append(self.packer.make_can_msg("CRUISE", 0, {"CRUISE_SPEED_PCM": simulator_state.cruise_speed * 3.6}))
    msg.append(self.packer.make_can_msg("CRUISE_FAULT_STATUS", 0, {}))
    msg.append(self.packer.make_can_msg("SCM_FEEDBACK", 0,
                                    {
                                      "MAIN_ON": 1,
                                      "LEFT_BLINKER": simulator_state.left_blinker,
                                      "RIGHT_BLINKER": simulator_state.right_blinker
                                    }))
    msg.append(self.packer.make_can_msg("POWERTRAIN_DATA", 0,
                                    {
                                    "ACC_STATUS": int(simulator_state.is_engaged),
                                    "PEDAL_GAS": simulator_state.user_gas,
                                    "BRAKE_PRESSED": simulator_state.user_brake > 0
                                    }))
    msg.append(self.packer.make_can_msg("CAR_SPEED", 0, {}))

    # *** cam bus ***
    msg.append(self.packer.make_can_msg("STEERING_CONTROL", 2, {}))
    # For BOSCH_RADARLESS Hondas (this car), carstate.py's cruiseState.speed
    # comes from THIS message's CRUISE_SPEED field, not CRUISE_SPEED_PCM above
    # (that one's only read for non-Bosch Hondas) -- see
    # carstate.py: `if self.CP.flags & HondaFlags.BOSCH: ... acc_hud =
    # cp_cam.vl["ACC_HUD"] if BOSCH_RADARLESS else cp.vl["ACC_HUD"]`. Values
    # above 160 are treated as a transient PCM "pulse" and ignored (real
    # speeds are always well under that), so no special-casing needed here.
    msg.append(self.packer.make_can_msg("ACC_HUD", 2, {"CRUISE_SPEED": simulator_state.cruise_speed * 3.6}))
    msg.append(self.packer.make_can_msg("LKAS_HUD", 2, {}))

    self.pm.send('can', can_list_to_can_capnp(msg))

  def send_panda_state(self, simulator_state):
    self.sm.update(0)

    if self.params.get_bool("ObdMultiplexingEnabled") != self.obd_multiplexing:
      self.obd_multiplexing = not self.obd_multiplexing
      self.params.put_bool("ObdMultiplexingChanged", True, block=True)

    # Mirror back whatever the car interface actually computed (selfdrived
    # compares pandaStates[i].safetyModel/safetyParam against
    # CP.safetyConfigs[i] and disables with "Controls Mismatch" on any
    # difference). Hardcoding RADARLESS|BOSCH_LONG here only matches cars
    # where openpilotLongitudinalControl is on; e.g. HONDA_CIVIC_2022 without
    # alpha_long has openpilotLongitudinalControl=False, so BOSCH_LONG is
    # never actually set and the hardcoded value silently mismatched forever.
    safety_configs = self.sm["carParams"].safetyConfigs
    if safety_configs:
      safety_model = safety_configs[0].safetyModel
      safety_param = safety_configs[0].safetyParam
    else:
      # carParams not published yet (first tick or two) -- fall back to a
      # reasonable default; this self-corrects within a couple of ticks.
      safety_model = 'hondaBosch'
      safety_param = HondaSafetyFlags.RADARLESS.value

    dat = messaging.new_message('pandaStates', 1)
    dat.valid = True
    dat.pandaStates[0] = {
      'ignitionLine': simulator_state.ignition,
      'pandaType': "blackPanda",
      'controlsAllowed': True,
      'safetyModel': safety_model,
      'alternativeExperience': self.sm["carParams"].alternativeExperience,
      'safetyParam': safety_param,
    }
    self.pm.send('pandaStates', dat)

  def update(self, simulator_state: SimulatorState):
    try:
      self.send_can_messages(simulator_state)

      # 10Hz, matching SERVICE_LIST['pandaStates'].frequency -- this used to be
      # every 50 ticks (2Hz), 5x slower than expected, which measured as a
      # persistent pandaStates rate shortfall.
      if self.idx % 10 == 0:
        self.send_panda_state(simulator_state)

      self.idx += 1
    except Exception:
      traceback.print_exc()
      raise
