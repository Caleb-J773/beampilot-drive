-- Standalone tests for the per-vehicle camera profile layer. No BeamNG needed.

local function copy(value)
  if type(value) ~= "table" then return value end
  local result = {}
  for key, item in pairs(value) do result[key] = copy(item) end
  return result
end

local function assertEqual(actual, expected, message)
  if actual ~= expected then
    error(string.format("%s: expected %s, got %s", message, tostring(expected), tostring(actual)), 2)
  end
end

local disk = {
  version = 1,
  vehicles = {
    etk800 = {offRight = 0.12},
  },
}
local lastWrite = nil
local activeVehicle = {JBeam = "etk800"}

function jsonReadFile(path)
  assertEqual(path, "/settings/beampilot/camera.json", "profile read path")
  return copy(disk)
end

function jsonWriteFile(path, data, pretty)
  assertEqual(path, "/settings/beampilot/camera.json", "profile write path")
  assertEqual(pretty, true, "pretty JSON")
  lastWrite = copy(data)
  return true
end

FS = {
  directoryCreate = function(_, path, recursive)
    assertEqual(path, "/settings/beampilot", "profile directory")
    assertEqual(recursive, true, "recursive directory creation")
    return true
  end,
}

function getPlayerVehicle(_)
  return activeVehicle
end

function log(_, _, _) end

function jsonEncode(_)
  return "calibration-request"
end

function jsonDecode(data, _)
  if data == "calibration-ok" then
    return {ok = true, message = "calibration reset"}
  end
  return {ok = false, error = "calibration refused"}
end

local calibrationReply = "calibration-ok"
socket = {
  udp = function()
    return {
      settimeout = function(_, seconds)
        assertEqual(seconds, 0.35, "calibration reply timeout")
      end,
      sendto = function(_, data, address, port)
        assertEqual(data, "calibration-request", "calibration request body")
        assertEqual(address, "127.0.0.1", "calibration command address")
        assertEqual(port, 49157, "calibration command port")
        return #data
      end,
      receivefrom = function()
        return calibrationReply, "127.0.0.1", 49157
      end,
      close = function() end,
    }
  end,
}

core_vehicles = {
  getModel = function(key)
    return {model = {Brand = "Test", Name = key}}
  end,
}

core_camera = {
  setByName = function(player, name, reset)
    assertEqual(player, 0, "camera player")
    assertEqual(name, "openpilot", "camera name")
    assertEqual(reset, false, "camera reset")
    return true
  end,
}

OPENPILOT_CAM = {
  fov = 25.698296,
  offRight = 0,
  offFwd = 0.55,
  offUp = 0.85,
  pitch = 0,
  yaw = 0,
  autoPlace = 0,
  wideHeight = 1.22,
  wideClearance = 0.15,
  commandPort = 49157,
}

local tuner = dofile("tools/beamng_mod/openpilot_cam/lua/ge/extensions/beampilotCameraTuner.lua")

-- A saved field wins over the base, while all other values still follow it.
local effective = tuner.getEffectiveConfig(activeVehicle, OPENPILOT_CAM)
assertEqual(effective.offRight, 0.12, "saved lateral override")
assertEqual(effective.offFwd, 0.55, "unmodified base field")
assertEqual(effective.fov, 25.698296, "base FOV")

-- BeamMP can leave GE's normal player-slot getter empty even though the camera
-- receives data.veh every frame. The tuner must retain that authoritative
-- camera vehicle so the pause panel still knows what is being tuned.
local cameraVehicleOnly = activeVehicle
activeVehicle = nil
local cameraState = tuner.getState()
assertEqual(cameraState.available, true, "camera data vehicle survives missing player slot")
assertEqual(cameraState.vehicleKey, "etk800", "camera data vehicle key")
activeVehicle = cameraVehicleOnly

-- This simulates the bridge's periodic TUI/environment refresh. Sparse saved
-- fields must remain fixed, while uncustomized values and FOV update live.
OPENPILOT_CAM.offRight = -0.4
OPENPILOT_CAM.offFwd = 0.8
OPENPILOT_CAM.fov = 93.619537
effective = tuner.getEffectiveConfig(activeVehicle, OPENPILOT_CAM)
assertEqual(effective.offRight, 0.12, "bridge refresh cannot replace vehicle override")
assertEqual(effective.offFwd, 0.8, "bridge refresh updates uncustomized pose")
assertEqual(effective.fov, 93.619537, "bridge refresh retains FOV authority")

-- Live editing is sparse and persists only for this JBeam model key.
local state = tuner.setValue("pitch", 2.5)
assertEqual(state.dirty, true, "edit marks profile dirty")
effective = tuner.getEffectiveConfig(activeVehicle, OPENPILOT_CAM)
assertEqual(effective.pitch, 2.5, "live pitch edit")
tuner.save()
assertEqual(lastWrite.version, 1, "save version")
assertEqual(lastWrite.vehicles.etk800.offRight, 0.12, "existing sparse field saved")
assertEqual(lastWrite.vehicles.etk800.pitch, 2.5, "new sparse field saved")
assertEqual(lastWrite.vehicles.etk800.fov, nil, "FOV is never persisted")
assertEqual(lastWrite.vehicles.etk800.offFwd, nil, "untouched pose field is not frozen")

-- Per-field revert (the slider's own reset button) must land each field on
-- its OWN saved value if there is one, base otherwise -- never on the whole
-- profile's last-saved snapshot, or reverting one ruined field would also
-- discard a sibling field's still-good live edit.
local function fieldByName(fields, name)
  for _, field in ipairs(fields) do
    if field.name == name then return field end
  end
  return nil
end
state = tuner.getState()
assertEqual(fieldByName(state.fields, "offRight").origValue, 0.12, "revert target: saved override")
assertEqual(fieldByName(state.fields, "pitch").origValue, 2.5, "revert target: saved override")
assertEqual(fieldByName(state.fields, "offFwd").origValue, 0.8, "revert target: base value when never saved")

activeVehicle = {JBeam = "pickup"}
effective = tuner.getEffectiveConfig(activeVehicle, OPENPILOT_CAM)
assertEqual(effective.offRight, -0.4, "profiles are isolated by vehicle model")
tuner.setValue("yaw", -3)
tuner.save()
assertEqual(lastWrite.vehicles.pickup.yaw, -3, "second vehicle profile saved")
assertEqual(lastWrite.vehicles.etk800.pitch, 2.5, "first vehicle profile retained")

-- Reverting restores the disk-backed value; reset then save deletes only the
-- current vehicle profile and hands authority back to the TUI/base config.
tuner.setValue("yaw", 7)
state = tuner.revert()
assertEqual(state.dirty, false, "revert clears dirty state")
effective = tuner.getEffectiveConfig(activeVehicle, OPENPILOT_CAM)
assertEqual(effective.yaw, -3, "revert restores saved value")
state = tuner.reset()
assertEqual(state.dirty, true, "reset is pending until saved")
effective = tuner.getEffectiveConfig(activeVehicle, OPENPILOT_CAM)
assertEqual(effective.yaw, 0, "reset restores base immediately")
tuner.save()
assertEqual(lastWrite.vehicles.pickup, nil, "saving reset removes current profile")
assertEqual(lastWrite.vehicles.etk800.offRight, 0.12, "reset does not remove other profiles")

-- Matching the current base removes a field from the sparse override. Invalid
-- fields and non-finite values are rejected instead of corrupting the file.
activeVehicle = {JBeam = "etk800"}
state = tuner.setValue("offRight", OPENPILOT_CAM.offRight)
assertEqual(state.dirty, true, "removing a saved field is dirty")
tuner.save()
assertEqual(lastWrite.vehicles.etk800.offRight, nil, "base-matching value removes override")
state = tuner.setValue("notAField", 1)
assertEqual(state.error, "Invalid camera value", "unknown field rejected")
state = tuner.setValue("pitch", 0 / 0)
assertEqual(state.error, "Invalid camera value", "NaN rejected")

state = tuner.activateCamera()
assertEqual(state.error, nil, "camera activation succeeds")

state = tuner.resetCalibration()
assertEqual(state.error, nil, "camera calibration request succeeds")
assertEqual(state.message, "calibration reset", "camera calibration response shown")
calibrationReply = "calibration-refused"
state = tuner.resetCalibration()
assertEqual(state.error, "calibration refused", "camera calibration refusal shown")

print("beampilot camera tuner tests passed")
