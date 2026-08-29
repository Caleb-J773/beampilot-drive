# Reused for the same reason as CarState: the messages are Honda Bosch
# radarless. Note that beamngd does NOT use this path to steer -- it reads
# controlsState.desiredCurvature directly and sends BeamNG a -1..1 position, so
# the torque/PID output here never reaches the car. It still runs, because
# openpilot's own state machine expects a CarController to exist and to publish
# carOutput.
from opendbc.car.honda.carcontroller import CarController

assert CarController  # re-export
