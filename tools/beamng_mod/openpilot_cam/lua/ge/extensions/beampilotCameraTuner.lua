-- Per-vehicle pose overrides for the rigid openpilot camera.
--
-- OPENPILOT_CAM remains the bridge/TUI-controlled base configuration.  This
-- module applies a sparse vehicle-model profile after that base, so a bridge
-- refresh cannot overwrite a pose that was deliberately tuned in game.  FOV,
-- placement mode, and all fields not present in the profile continue to track
-- the bridge configuration.

local M = {}

local logTag = "beampilotCameraTuner"
local saveDir = "/settings/beampilot"
local savePath = saveDir .. "/camera.json"
local saveVersion = 1
local commandAddress = "127.0.0.1"
local defaultCommandPort = 49157
local commandMagic = "BPC1"

local fieldOrder = {
  "offRight",
  "offFwd",
  "offUp",
  "wideHeight",
  "wideClearance",
  "pitch",
  "yaw",
}

local fieldSpecs = {
  offRight = {
    label = "Lateral offset", unit = "m", min = -2, max = 2, step = 0.01,
    help = "Positive moves toward the passenger side.", modes = "both",
  },
  offFwd = {
    label = "Forward offset", unit = "m", min = -3, max = 8, step = 0.01,
    help = "Position relative to the vehicle reference node.", modes = "fixed",
  },
  offUp = {
    label = "Height offset", unit = "m", min = -2, max = 5, step = 0.01,
    help = "Height relative to the vehicle reference node.", modes = "fixed",
  },
  wideHeight = {
    label = "Camera height", unit = "m", min = 0.2, max = 5, step = 0.01,
    help = "Height above the bottom of the vehicle bounds.", modes = "adaptive",
  },
  wideClearance = {
    label = "Front clearance", unit = "m", min = 0.02, max = 2, step = 0.01,
    help = "Distance ahead of the vehicle bounds.", modes = "adaptive",
  },
  pitch = {
    label = "Pitch", unit = "deg", min = -20, max = 20, step = 0.1,
    help = "Positive looks downward.", modes = "both",
  },
  yaw = {
    label = "Yaw", unit = "deg", min = -30, max = 30, step = 0.1,
    help = "Positive looks right.", modes = "both",
  },
}

local loaded = false
local savedProfiles = {}
local workingProfiles = {}
local effectiveCfg = {}
local cameraVehicle = nil
local cameraVehicleKey = nil
local reportedVehicleKey = nil
local missingVehicleWarned = false

local function isFinite(value)
  return type(value) == "number" and value == value and value > -math.huge and value < math.huge
end

local function clamp(value, minimum, maximum)
  return math.max(minimum, math.min(maximum, value))
end

local function copyProfile(profile)
  local copy = {}
  if type(profile) ~= "table" then return copy end
  for _, name in ipairs(fieldOrder) do
    if profile[name] ~= nil then copy[name] = profile[name] end
  end
  return copy
end

local function profilesEqual(a, b)
  for _, name in ipairs(fieldOrder) do
    if (a and a[name] or nil) ~= (b and b[name] or nil) then return false end
  end
  return true
end

local function profileIsEmpty(profile)
  for _, name in ipairs(fieldOrder) do
    if profile and profile[name] ~= nil then return false end
  end
  return true
end

local function sanitizeProfile(profile)
  local clean = {}
  if type(profile) ~= "table" then return clean end
  for _, name in ipairs(fieldOrder) do
    local spec = fieldSpecs[name]
    local value = tonumber(profile[name])
    if isFinite(value) then clean[name] = clamp(value, spec.min, spec.max) end
  end
  return clean
end

local function ensureLoaded()
  if loaded then return end
  loaded = true

  local ok, data = pcall(jsonReadFile, savePath)
  if not ok then
    log("W", logTag, "could not read " .. savePath .. ": " .. tostring(data))
    return
  end
  if type(data) ~= "table" then return end
  if data.version ~= nil and tonumber(data.version) ~= saveVersion then
    log("W", logTag, "ignoring unsupported camera profile version " .. tostring(data.version))
    return
  end
  if type(data.vehicles) ~= "table" then return end

  for key, profile in pairs(data.vehicles) do
    if type(key) == "string" then
      local clean = sanitizeProfile(profile)
      if not profileIsEmpty(clean) then savedProfiles[key] = clean end
    end
  end
end

local function vehicleKey(veh)
  if not veh then return nil end
  local ok, key = pcall(function()
    return veh.JBeam or veh:getJBeamFilename()
  end)
  if not ok then return nil end
  if key == nil or tostring(key) == "" then return nil end
  return tostring(key)
end

local function playerVehicle()
  local veh = nil
  if type(getPlayerVehicle) == "function" then
    local ok, result = pcall(getPlayerVehicle, 0)
    if ok then veh = result end
  end
  if not veh and be and type(be.getPlayerVehicle) == "function" then
    local ok, result = pcall(be.getPlayerVehicle, be, 0)
    if ok then veh = result end
  end
  if not veh and be and type(be.getPlayerVehicleID) == "function" and type(getObjectByID) == "function" then
    local ok, vehicleId = pcall(be.getPlayerVehicleID, be, 0)
    if ok and vehicleId and vehicleId >= 0 then
      local objectOk, result = pcall(getObjectByID, vehicleId)
      if objectOk then veh = result end
    end
  end
  if vehicleKey(veh) then return veh end
  if vehicleKey(cameraVehicle) then return cameraVehicle end
  return nil
end

local function profileFor(key)
  ensureLoaded()
  if not key then return nil end
  if workingProfiles[key] == nil then
    workingProfiles[key] = copyProfile(savedProfiles[key])
  end
  return workingProfiles[key]
end

local function baseConfig()
  local cfg = rawget(_G, "OPENPILOT_CAM")
  if type(cfg) == "table" then return cfg end
  return {}
end

local function effectiveFor(veh, base)
  local key = vehicleKey(veh)
  if key then
    -- data.veh is the authoritative object supplied to the active camera.
    -- Player-slot lookups can be nil under BeamMP even while this object is
    -- being rendered, so retain it for pause-menu calls made outside update().
    cameraVehicle = veh
    cameraVehicleKey = key
    missingVehicleWarned = false
    if reportedVehicleKey ~= key then
      log("I", logTag, "camera tuner attached to vehicle model " .. key)
      reportedVehicleKey = key
    end
  else
    key = cameraVehicleKey
  end
  local profile = profileFor(key)
  for key in pairs(effectiveCfg) do effectiveCfg[key] = nil end
  if type(base) == "table" then
    for key, value in pairs(base) do effectiveCfg[key] = value end
  end
  if profile then
    for _, name in ipairs(fieldOrder) do
      if profile[name] ~= nil then effectiveCfg[name] = profile[name] end
    end
  end
  return effectiveCfg
end

local function displayName(veh, key)
  if not veh or not key then return "No vehicle" end
  if core_vehicles and type(core_vehicles.getModel) == "function" then
    local ok, info = pcall(core_vehicles.getModel, key)
    local model = ok and type(info) == "table" and info.model or nil
    if type(model) == "table" then
      local brand = tostring(model.Brand or "")
      local name = tostring(model.Name or "")
      local joined = (brand .. " " .. name):match("^%s*(.-)%s*$")
      if joined ~= "" then return joined end
    end
  end
  return key
end

local function state(message, errorMessage)
  local veh = playerVehicle()
  local key = vehicleKey(veh) or cameraVehicleKey
  if not key then
    if not missingVehicleWarned then
      log("W", logTag, "no player-slot or active-camera vehicle is available to the tuner")
      missingVehicleWarned = true
    end
    return {available = false, message = message, error = errorMessage}
  end

  local base = baseConfig()
  local profile = profileFor(key)
  local effective = effectiveFor(veh, base)
  local adaptive = tonumber(effective.autoPlace) ~= 0
  local saved = savedProfiles[key]
  local fields = {}
  for _, name in ipairs(fieldOrder) do
    local spec = fieldSpecs[name]
    -- What a per-field revert should land on: the saved override if there is
    -- one for this field, otherwise the TUI/base value -- so reverting one
    -- ruined field never touches any other field's live edit.
    local savedVal = saved and saved[name]
    fields[#fields + 1] = {
      name = name,
      label = spec.label,
      unit = spec.unit,
      min = spec.min,
      max = spec.max,
      step = spec.step,
      help = spec.help,
      visible = spec.modes == "both" or (adaptive and spec.modes == "adaptive") or
        (not adaptive and spec.modes == "fixed"),
      value = tonumber(effective[name]) or 0,
      overridden = profile[name] ~= nil,
      origValue = savedVal ~= nil and savedVal or (tonumber(base[name]) or 0),
    }
  end

  return {
    available = true,
    vehicleKey = key,
    vehicleName = displayName(veh, key),
    mode = adaptive and "Adaptive vehicle-front placement" or "Fixed-offset placement",
    autoPlace = adaptive,
    fov = tonumber(effective.fov) or 0,
    measuredHeight = tonumber(base.measuredHeight) or -1,
    fields = fields,
    dirty = not profilesEqual(profile, savedProfiles[key]),
    hasSavedProfile = not profileIsEmpty(savedProfiles[key]),
    message = message,
    error = errorMessage,
  }
end

function M.getEffectiveConfig(veh, base)
  return effectiveFor(veh, base)
end

function M.getState()
  return state()
end

function M.setValue(name, rawValue)
  local spec = fieldSpecs[name]
  local value = tonumber(rawValue)
  if not spec or not isFinite(value) then return state(nil, "Invalid camera value") end

  local veh = playerVehicle()
  local key = vehicleKey(veh)
  if not key then return state(nil, "Spawn a vehicle before tuning the camera") end

  value = clamp(value, spec.min, spec.max)
  local profile = profileFor(key)
  local baseValue = tonumber(baseConfig()[name])
  if isFinite(baseValue) and math.abs(value - baseValue) < 0.000001 then
    profile[name] = nil
  else
    profile[name] = value
  end
  return state()
end

function M.save()
  local key = vehicleKey(playerVehicle())
  if not key then return state(nil, "Spawn a vehicle before saving") end

  local profile = profileFor(key)
  if profileIsEmpty(profile) then
    savedProfiles[key] = nil
  else
    savedProfiles[key] = copyProfile(profile)
  end

  local okDir, dirError = pcall(function() FS:directoryCreate(saveDir, true) end)
  if not okDir then return state(nil, "Could not create settings directory: " .. tostring(dirError)) end
  local okWrite, result = pcall(jsonWriteFile, savePath, {
    version = saveVersion,
    vehicles = savedProfiles,
  }, true)
  if not okWrite or result == false then
    return state(nil, "Could not save camera profile: " .. tostring(result))
  end
  return state("Saved camera pose for " .. key)
end

function M.revert()
  local key = vehicleKey(playerVehicle())
  if not key then return state(nil, "Spawn a vehicle before reverting") end
  workingProfiles[key] = copyProfile(savedProfiles[key])
  return state("Restored the last saved pose")
end

function M.reset()
  local key = vehicleKey(playerVehicle())
  if not key then return state(nil, "Spawn a vehicle before resetting") end
  workingProfiles[key] = {}
  return state("Using TUI/default pose; save to remove this vehicle override")
end

function M.activateCamera()
  if not core_camera or type(core_camera.setByName) ~= "function" then
    return state(nil, "BeamNG camera service is not available")
  end
  local ok, result = pcall(core_camera.setByName, 0, "openpilot", false)
  if not ok or result == false then return state(nil, "Could not activate the openpilot camera") end
  return state("Openpilot camera activated")
end

function M.onVehicleSwitched(_, _, player)
  if player == 0 then
    cameraVehicle = nil
    cameraVehicleKey = nil
    reportedVehicleKey = nil
  end
end

function M.resetCalibration()
  if not socket or type(socket.udp) ~= "function" then
    return state(nil, "BeamNG UDP service is not available")
  end
  local port = tonumber(baseConfig().commandPort) or defaultCommandPort
  port = math.floor(clamp(port, 1024, 65535))
  local sock = socket.udp()
  if not sock then return state(nil, "Could not create calibration command socket") end
  sock:settimeout(0.35)

  local request = jsonEncode({magic = commandMagic, command = "resetCalibration"})
  local sent, sendError = sock:sendto(request, commandAddress, port)
  if not sent then
    sock:close()
    return state(nil, "Could not send calibration request: " .. tostring(sendError))
  end

  local data, receiveError = sock:receivefrom()
  sock:close()
  if not data then
    return state(nil, "No reply from beamngd; make sure beampilot is running (" .. tostring(receiveError) .. ")")
  end
  local ok, response = pcall(jsonDecode, data, "beampilot camera calibration response")
  if not ok or type(response) ~= "table" then return state(nil, "Invalid reply from beamngd") end
  if response.ok then return state(response.message or "Camera calibration reset") end
  return state(nil, response.error or "Camera calibration reset was refused")
end

return M
