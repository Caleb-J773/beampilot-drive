# No radar ECU. Ground-truth radar reaches openpilot a different way entirely:
# the mod sends tracks straight to card.py, which fills in the RadarData this
# would otherwise leave empty. See openpilot/common/beampilot_radar.py.
from opendbc.car.interfaces import RadarInterfaceBase as RadarInterface

assert RadarInterface  # re-export
