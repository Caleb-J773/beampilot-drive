# beamngd packs Honda Bosch radarless frames, so Honda's parser is the correct
# one -- it is the format actually on the wire. It resolves its DBC through
# honda.values.DBC, which beampilot.values registers BEAMPILOT in.
from opendbc.car.honda.carstate import CarState

assert CarState  # re-export
