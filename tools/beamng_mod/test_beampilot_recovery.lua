-- Tests the two ways beampilot_bridge used to get permanently stuck, standalone.
--
--   luajit tools/beamng_mod/test_beampilot_recovery.lua
--
-- Both bugs had the same shape: a one-shot action whose result was never
-- checked, latched as if it had succeeded.
--
--   control port  UDP 49153 is bound lazily by whichever vehicle is seated, and
--                 NOTHING ever releases it -- protocols.lua gates fillStruct on
--                 playerInfo.firstPlayerSeated and its onPlayersChanged returns
--                 early when unseated, never reaching protocol modules. Switch
--                 vehicles and the old one keeps the port for the session, so
--                 control packets go to the car nobody is driving.
--
--   camera        core_camera.setByName RETURNS NOTHING (camera.lua's setByName
--                 calls set() and discards it; the failing path below only
--                 log("E")s). The request was fired once and cameraSelected set
--                 true regardless, so losing the startup race meant the
--                 openpilot camera never came up and nothing retried.
--
-- Neither is visible by inspection in game: the first looks like "openpilot
-- just does not drive this car", the second like "the camera did not load".

local BEAMNG = os.getenv("BEAMNG_DIR")
  or (os.getenv("HOME") .. "/.local/share/Steam/steamapps/common/BeamNG.drive")

local mathlib = io.open(BEAMNG .. "/lua/common/mathlib.lua")
if not mathlib then
  print("SKIP: no BeamNG install at " .. BEAMNG .. " (set BEAMNG_DIR)")
  os.exit(0)
end
mathlib:close()
dofile(BEAMNG .. "/lua/common/mathlib.lua")
pcall(require, "table.clear")
pcall(require, "table.new")

-- --- stubs -----------------------------------------------------------------
local EGO_ID = 7

objectId = EGO_ID
FILTER_DIRECT = 0

local logged = {}
function log(level, _, msg) logged[#logged + 1] = level .. ": " .. tostring(msg) end
function dumps(v) return tostring(v) end
function jsonDecode(_, _) return nil end

-- A clock we control, so the retry intervals can be tested without sleeping.
local clock = 1000.0
os.clock = function() return clock end

-- socket.udp(), with a bind that fails while `portHeld` is true -- exactly what
-- LuaSocket does when another vehicle's VM already owns 49153.
local portHeld = true
local openSockets = 0
socket = {
  udp = function()
    openSockets = openSockets + 1
    local sock = {closed = false}
    function sock:settimeout() end
    function sock:setsockname()
      if portHeld then return nil, "address already in use" end
      return 1
    end
    function sock:receive() return nil end
    function sock:close()
      if not self.closed then
        self.closed = true
        openSockets = openSockets - 1
      end
    end
    return sock
  end,
}

local queued = {}
obj = {
  getId = function() return EGO_ID end,
  queueGameEngineLua = function(_, cmd) queued[#queued + 1] = cmd end,
  getPositionXYZ = function() return 0, 0, 0 end,
  getVelocityXYZ = function() return 0, 20, 0 end,
  getCenterPosition = function() return vec3(0, 0, 0) end,
  getDirectionVector = function() return vec3(0, 1, 0) end,
  getDirectionVectorRight = function() return vec3(1, 0, 0) end,
  getDirectionVectorUp = function() return vec3(0, 0, 1) end,
  getInitialLength = function() return 4.5 end,
  getInitialWidth = function() return 1.85 end,
  getRollPitchYaw = function() return 0, 0, 0 end,
  getRollPitchYawAngularVelocity = function() return 0, 0, 0 end,
}
for name, fn in pairs(obj) do
  if name ~= "queueGameEngineLua" then
    obj[name] = function(_, ...) return fn(...) end
  end
end

electrics = {values = {}}      -- watertemp nil = "still spawning", fillStruct returns early
input = {event = function() end}
sensors = {ffiSensors = {sensorX = 0, sensorY = 0, sensorZ = 0}}
mapmgr = {getObjects = function() return {} end}
v = {data = {nodes = nil}}

-- --- run -------------------------------------------------------------------
local M = dofile("tools/beamng_mod/beampilot_bridge/lua/vehicle/protocols/beampilot.lua")

local failures, checks = 0, 0
local function check(name, got, want)
  checks = checks + 1
  if got ~= want then
    failures = failures + 1
    print(string.format("  FAIL %s: expected %s, got %s", name, tostring(want), tostring(got)))
  else
    print("  ok   " .. name)
  end
end

local function tick()
  queued = {}
  M.fillStruct({}, 1 / 100)
end

local function queuedMatching(pattern)
  local n = 0
  for _, cmd in ipairs(queued) do
    if cmd:find(pattern, 1, true) then n = n + 1 end
  end
  return n
end

print("beampilot bridge recovery tests")

-- 1. The control port is held by another vehicle ---------------------------
print("\ncontrol port takeover")
tick()
check("asks the other vehicles to release the port",
  queuedMatching("beampilotBridge.releaseControlPort()") , 1)
check("excludes our own vehicle id from the broadcast",
  queued[1]:find("v:getId() ~= " .. EGO_ID, 1, true) ~= nil, true)

-- Rate limited: a bind failure must not queue a command every tick at 100Hz.
tick()
check("does not re-broadcast on the very next tick",
  queuedMatching("beampilotBridge.releaseControlPort()"), 0)
clock = clock + 1.5
tick()
check("re-broadcasts once the retry interval has passed",
  queuedMatching("beampilotBridge.releaseControlPort()"), 1)

-- Only one warning, however long the port stays held.
local warnings = 0
for _, line in ipairs(logged) do
  if line:find("is held by another vehicle", 1, true) then warnings = warnings + 1 end
end
check("warns once, not once per attempt", warnings, 1)

-- Every failed attempt must close its socket, or a 100Hz retry leaks fds.
check("leaks no sockets while the port is held", openSockets, 0)

-- 2. The other vehicle lets go ---------------------------------------------
print("\nport is released")
portHeld = false
clock = clock + 1.5
tick()
check("binds once the port is free", openSockets, 1)
check("stops broadcasting takeover requests",
  queuedMatching("beampilotBridge.releaseControlPort()"), 0)

-- 3. ...and hands it back when asked ---------------------------------------
print("\nreleaseControlPort")
M.releaseControlPort()
check("closes the control socket", openSockets, 0)

-- 4. Camera selection is verified, not assumed ------------------------------
-- Past the spawn guard now, so fillStruct reaches the camera block.
print("\ncamera selection")
electrics.values.watertemp = 80
clock = clock + 1.5
tick()
check("requests the openpilot camera", queuedMatching("core_camera.setByName(0, 'openpilot'"), 1)
check("asks GE to report the result back",
  queued[1]:find("beampilotBridge.onCameraResult", 1, true) ~= nil, true)

-- GE answers: still on some other camera. This is the case that used to latch.
M.onCameraResult("orbit")
clock = clock + 1.5
tick()
check("retries when GE reports a different camera",
  queuedMatching("core_camera.setByName(0, 'openpilot'"), 1)

-- GE answers: it took.
M.onCameraResult("openpilot")
clock = clock + 1.5
tick()
check("stops requesting once the camera is actually active",
  queuedMatching("core_camera.setByName(0, 'openpilot'"), 0)

-- A reset (respawn / reload) re-arms it.
M.reset()
clock = clock + 1.5
tick()
check("requests again after a vehicle reset",
  queuedMatching("core_camera.setByName(0, 'openpilot'"), 1)

print(string.format("\n%d checks, %d failures", checks, failures))
os.exit(failures == 0 and 0 or 1)
