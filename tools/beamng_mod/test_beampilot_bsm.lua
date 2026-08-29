-- Tests the blind spot detection in beampilot_bridge's protocol, standalone.
--
--   luajit tools/beamng_mod/test_beampilot_bsm.lua
--
-- BeamNG is not running here, but its maths is: lua/common/mathlib.lua loads
-- under plain LuaJIT, so vec3 and overlapsOBB_OBB are the REAL ones the mod
-- will use in game. Everything else BeamNG normally provides (obj, mapmgr,
-- electrics, input, socket) is stubbed below, which is what makes it possible
-- to place a car exactly 3m off the left flank and assert which bit comes out.
--
-- Worth having because the two things most likely to be silently wrong -- which
-- way is "right", and where the zone's edges actually land -- are invisible by
-- inspection and awkward to check by driving around.
--
-- Set BEAMNG_DIR if your install is not in the usual Steam location.

local BEAMNG = os.getenv("BEAMNG_DIR")
  or (os.getenv("HOME") .. "/.local/share/Steam/steamapps/common/BeamNG.drive")

local mathlib = io.open(BEAMNG .. "/lua/common/mathlib.lua")
if not mathlib then
  print("SKIP: no BeamNG install at " .. BEAMNG .. " (set BEAMNG_DIR)")
  os.exit(0)
end
mathlib:close()
dofile(BEAMNG .. "/lua/common/mathlib.lua")   -- defines vec3, overlapsOBB_OBB
-- BeamNG preloads LuaJIT's table.clear/table.new extensions; plain luajit does not.
pcall(require, "table.clear")
pcall(require, "table.new")

-- --- the world -------------------------------------------------------------
-- Right-handed, z up. The ego sits at the origin facing +y, so +x is its right.
local EGO_LEN, EGO_WIDTH = 4.5, 1.85
local world = {
  egoVel = {0, 20, 0},          -- 20 m/s, comfortably above minSpeedMs
  vehicles = {},                 -- [id] = {pos={x,y,z}, vel={x,y,z}, len, width, height, yaw}
  collisions = {},
}

local function addVehicle(id, x, y, z, opts)
  opts = opts or {}
  world.vehicles[id] = {
    pos = {x, y, z},
    vel = opts.vel or world.egoVel,
    len = opts.len or 4.5,
    width = opts.width or 1.85,
    height = opts.height or 1.5,
    yaw = opts.yaw or 0,          -- radians, 0 = facing +y like the ego
  }
end

local function vehFwd(v)
  return vec3(math.sin(v.yaw), math.cos(v.yaw), 0)
end

-- --- the stubs -------------------------------------------------------------
objectId = 1
FILTER_DIRECT = 0

local logged = {}
function log(level, origin, msg) logged[#logged + 1] = level .. ": " .. msg end
function dumps(v) return tostring(v) end

-- A minimal JSON reader, just enough for the flat objects/numbers/booleans
-- beamngd's control packet contains. The game's own lua/common/json.lua cannot
-- be used here: it delegates to a native jsondecode binding that only exists
-- inside BeamNG. Parsing real JSON text still matters -- it is what proves
-- applyBsmConfig copes with the decoded shape, floats and all.
local function decodeJson(text)
  local pos = 1
  local function skip() pos = text:find("[^ \t\r\n]", pos) or #text + 1 end
  local parseValue
  local function parseString()
    local finish = text:find('"', pos + 1, true)
    local s = text:sub(pos + 1, finish - 1)
    pos = finish + 1
    return s
  end
  local function parseObject()
    local out = {}
    pos = pos + 1
    skip()
    if text:sub(pos, pos) == "}" then pos = pos + 1; return out end
    while true do
      skip()
      local key = parseString()
      skip()
      pos = pos + 1 -- ':'
      out[key] = parseValue()
      skip()
      local c = text:sub(pos, pos)
      pos = pos + 1
      if c == "}" then return out end
      assert(c == ",", "bad JSON near " .. pos)
    end
  end
  parseValue = function()
    skip()
    local c = text:sub(pos, pos)
    if c == "{" then return parseObject() end
    if c == '"' then return parseString() end
    if text:sub(pos, pos + 3) == "true" then pos = pos + 4; return true end
    if text:sub(pos, pos + 4) == "false" then pos = pos + 5; return false end
    if text:sub(pos, pos + 3) == "null" then pos = pos + 4; return nil end
    local finish = text:find("[^-+%d%.eE]", pos) or #text + 1
    local n = tonumber(text:sub(pos, finish - 1))
    pos = finish
    return n
  end
  return parseValue()
end

function jsonDecode(s) return decodeJson(s) end

input = {event = function() end}
electrics = {values = {watertemp = 90, wheelspeed = 20, steering_input = 0, steering = 0,
                       throttle = 0, brake = 0, clutch = 0, gearIndex = 1, ignitionLevel = 2}}
sensors = {ffiSensors = {sensorX = 0, sensorY = 0, sensorZ = 0}}

local pendingControl = nil  -- next control datagram the stub socket will hand over
local sentRadar = nil       -- raw bytes of the last radar datagram the mod sent
socket = {
  udp = function()
    return {
      settimeout = function() end,
      setsockname = function() return 1 end,
      close = function() end,
      receive = function()
        local p = pendingControl
        pendingControl = nil
        return p
      end,
      sendto = function(_, data) sentRadar = data end,
    }
  end,
}

mapmgr = {
  objectCollisionIds = world.collisions,
  getObjects = function()
    local out = {}
    for id, v in pairs(world.vehicles) do
      out[id] = {id = id, vel = vec3(v.vel[1], v.vel[2], v.vel[3]), pos = vec3(v.pos[1], v.pos[2], v.pos[3])}
    end
    -- The ego is in this table in game too; the scan has to skip it by id.
    out[objectId] = {id = objectId, vel = vec3(world.egoVel[1], world.egoVel[2], world.egoVel[3]), pos = vec3(0, 0, 0)}
    return out
  end,
}

obj = {
  getVelocityXYZ = function() return world.egoVel[1], world.egoVel[2], world.egoVel[3] end,
  getPositionXYZ = function() return 0, 0, 0 end,
  getCenterPosition = function() return vec3(0, 0, 0) end,
  getDirectionVector = function() return vec3(0, 1, 0) end,
  getDirectionVectorRight = function() return vec3(1, 0, 0) end,
  getDirectionVectorUp = function() return vec3(0, 0, 1) end,
  getInitialLength = function() return EGO_LEN end,
  getInitialWidth = function() return EGO_WIDTH end,
  getRollPitchYaw = function() return 0, 0, 0 end,
  getRollPitchYawAngularVelocity = function() return 0, 0, 0 end,
  queueGameEngineLua = function() end,
  getObjectCenterPosition = function(_, id)
    local v = world.vehicles[id]
    return v and vec3(v.pos[1], v.pos[2], v.pos[3]) or nil
  end,
  getObjectDirectionVector = function(_, id)
    local v = world.vehicles[id]
    return v and vehFwd(v) or nil
  end,
  getObjectDirectionVectorUp = function(_, id)
    return world.vehicles[id] and vec3(0, 0, 1) or nil
  end,
  getObjectInitialLength = function(_, id) return world.vehicles[id].len end,
  getObjectInitialWidth = function(_, id) return world.vehicles[id].width end,
  getObjectInitialHeight = function(_, id) return world.vehicles[id].height end,
}
-- The mod calls these with method syntax, so the receiver arrives as arg 1.
for name, fn in pairs(obj) do
  if name:match("^getObject") == nil then
    obj[name] = function(_, ...) return fn(...) end
  end
end

-- --- run -------------------------------------------------------------------
local M = dofile("tools/beamng_mod/beampilot_bridge/lua/vehicle/protocols/beampilot.lua")

local DL_BSM_LEFT, DL_BSM_RIGHT = 16, 32
local DL_BSM_LEFT_APPROACH, DL_BSM_RIGHT_APPROACH = 64, 128

-- One fillStruct at the BSM scan interval, so the scan definitely runs.
local function scan()
  local o = {}
  M.fillStruct(o, 1 / 15)
  local d = o.dashLights
  return {
    left = bit.band(d, DL_BSM_LEFT) ~= 0,
    right = bit.band(d, DL_BSM_RIGHT) ~= 0,
    leftApproach = bit.band(d, DL_BSM_LEFT_APPROACH) ~= 0,
    rightApproach = bit.band(d, DL_BSM_RIGHT_APPROACH) ~= 0,
  }
end

local failures, checks = 0, 0
local function check(name, got, want)
  checks = checks + 1
  for _, line in ipairs(logged) do
    if line:sub(1, 1) == "E" then failures = failures + 1; print("  ERROR " .. line) end
  end
  for i = #logged, 1, -1 do logged[i] = nil end
  local ok = true
  for _, k in ipairs({"left", "right", "leftApproach", "rightApproach"}) do
    if (got[k] or false) ~= (want[k] or false) then ok = false end
  end
  if ok then
    print("  ok    " .. name)
  else
    failures = failures + 1
    print(string.format("  FAIL  %s\n          got  L=%s R=%s La=%s Ra=%s\n          want L=%s R=%s La=%s Ra=%s",
      name, tostring(got.left), tostring(got.right), tostring(got.leftApproach), tostring(got.rightApproach),
      tostring(want.left or false), tostring(want.right or false),
      tostring(want.leftApproach or false), tostring(want.rightApproach or false)))
  end
end

local function reset()
  sentRadar = nil
  for k in pairs(world.vehicles) do world.vehicles[k] = nil end
  for i = #world.collisions, 1, -1 do world.collisions[i] = nil end
  world.egoVel = {0, 20, 0}
  pendingControl = nil
  M.reset()
end

print("beampilot BSM zone tests (ego " .. EGO_LEN .. "m x " .. EGO_WIDTH .. "m at the origin, facing +y)")

reset()
check("empty scene", scan(), {})

reset()
addVehicle(2, 3, -1, 0)
check("car 3m to the right, 1m back", scan(), {right = true})

reset()
addVehicle(2, -3, -1, 0)
check("car 3m to the left, 1m back", scan(), {left = true})

reset()
addVehicle(2, 3, -1, 0)
addVehicle(3, -3, -1, 0)
check("cars on both sides", scan(), {left = true, right = true})

reset()
addVehicle(2, 3, 20, 0)
check("car well ahead on the right", scan(), {})

reset()
addVehicle(2, 3, -30, 0)
check("car well behind on the right", scan(), {})

reset()
addVehicle(2, 10, -1, 0)
check("car two lanes over", scan(), {})

reset()
addVehicle(2, 0, -6, 0)
check("car directly behind, same lane", scan(), {})

reset()
addVehicle(2, 3, -1, 6)
check("car on an overpass above the right zone", scan(), {})

reset()
-- 12m back on the right, closing at 10 m/s: 10 * approachS(2.0) = 20m of
-- extra zone, so it should register as closing but not as occupying.
addVehicle(2, 3, -12, 0, {vel = {0, 30, 0}})
check("car closing fast from behind on the right", scan(), {rightApproach = true})

reset()
addVehicle(2, 3, -12, 0)   -- same distance, matched speed
check("car holding station 12m back on the right", scan(), {})

reset()
addVehicle(2, 3, -12, 0, {vel = {0, 10, 0}})
check("car falling behind on the right", scan(), {})

reset()
world.egoVel = {0, 0.5, 0}
addVehicle(2, 3, -1, 0)
check("stopped ego reports nothing", scan(), {})

reset()
addVehicle(2, 3, -1, 0)
world.collisions[1] = 2
check("vehicle in contact with us is ignored (towed trailer)", scan(), {})

reset()
-- A 16m trailer directly behind, wide enough to reach into both zones.
addVehicle(2, 0, -10, 0, {len = 16, width = 2.6})
check("long wide trailer behind reaches both zones", scan(), {left = true, right = true})

reset()
-- Config arriving over the control socket must actually take effect.
addVehicle(2, 3, -1, 0)
pendingControl = '{"engaged": false, "bsm": {"enabled": 0.0}}'
check("enabled=0 pushed from beamngd switches it off", scan(), {})

reset()
addVehicle(2, 6, -1, 0)
check("car 6m out is outside the default zone", scan(), {})
pendingControl = '{"engaged": false, "bsm": {"outerM": 8.0}}'
check("...but inside it once outerM is widened to 8m", scan(), {right = true})

reset()
addVehicle(2, 3, -1, 0)
check("defaults restored after the config is withdrawn", scan(), {right = true})

-- --- radar --------------------------------------------------------------
-- The packet is read back byte by byte rather than through the struct the mod
-- writes with, so the OFFSETS are checked independently of the cdef: a wrong
-- pragma pack would round-trip fine inside Lua and be unreadable from Python.
local ffi = require("ffi")

local function decodeRadar(bytes)
  if bytes == nil then return nil end
  if bytes:sub(1, 4) ~= "BPR1" then return nil, "bad magic " .. bytes:sub(1, 4) end
  local count = bytes:byte(5)
  if #bytes ~= 5 + count * 16 then
    return nil, string.format("length %d, expected %d for %d tracks", #bytes, 5 + count * 16, count)
  end
  local out = {}
  local buf = ffi.new("uint8_t[?]", #bytes, bytes)
  for i = 0, count - 1 do
    local base = 5 + i * 16
    local id = ffi.cast("uint32_t*", buf + base)[0]
    local f = ffi.cast("float*", buf + base + 4)
    out[i + 1] = {tonumber(id), f[0], f[1], f[2]}
  end
  return out
end

local function checkRadar(name, want)
  checks = checks + 1
  local got, err = decodeRadar(sentRadar)
  local problem = err
  if not problem and got == nil then problem = "no radar packet was sent" end
  if not problem and #got ~= #want then
    problem = string.format("%d tracks, wanted %d", #got, #want)
  end
  if not problem then
    for i, w in ipairs(want) do
      local g = got[i]
      if g[1] ~= w.id then problem = string.format("track %d id %s, wanted %s", i, g[1], w.id) break end
      for k, field in ipairs({"dRel", "yRel", "vRel"}) do
        if math.abs(g[k + 1] - w[field]) > (w.tol or 0.15) then
          problem = string.format("track %d %s = %.2f, wanted %.2f", i, field, g[k + 1], w[field])
          break
        end
      end
      if problem then break end
    end
  end
  if problem then
    failures = failures + 1
    print("  FAIL  " .. name .. "\n          " .. problem)
    if got then
      for i, g in ipairs(got) do
        print(string.format("          got track %d: id=%s dRel=%.2f yRel=%.2f vRel=%.2f", i, g[1], g[2], g[3], g[4]))
      end
    end
  else
    print("  ok    " .. name)
  end
end

print("\nradar tracks (dRel from OUR front bumper to their nearest surface, yRel LEFT positive)")

reset()
check("radar: no traffic, BSM unaffected", scan(), {})
checkRadar("radar: empty scene sends an empty packet", {})

reset()
-- 20m centre-to-centre, both 4.5m long: 20 - 2.25 - 2.25 = 15.5m of gap.
addVehicle(2, 0, 20, 0)
scan()
checkRadar("car 20m ahead in lane", {{id = 2, dRel = 15.5, yRel = 0.0, vRel = 0.0}})

reset()
addVehicle(2, -3.5, 30, 0)   -- one lane to the LEFT
scan()
checkRadar("car ahead one lane left reports yRel positive", {{id = 2, dRel = 25.5, yRel = 3.5, vRel = 0.0}})

reset()
addVehicle(2, 3.5, 30, 0)    -- one lane to the RIGHT
scan()
checkRadar("car ahead one lane right reports yRel negative", {{id = 2, dRel = 25.5, yRel = -3.5, vRel = 0.0}})

reset()
addVehicle(2, 0, 25, 0, {vel = {0, 15, 0}})   -- ego does 20, lead does 15
scan()
checkRadar("closing lead reports negative vRel", {{id = 2, dRel = 20.5, yRel = 0.0, vRel = -5.0}})

reset()
addVehicle(2, 0, -25, 0)
scan()
checkRadar("car behind is not a forward radar target", {})

reset()
addVehicle(2, 0, 400, 0)
scan()
checkRadar("car past the radar range", {})

reset()
addVehicle(2, 30, 30, 0)     -- way off to the side, outside the beam
scan()
checkRadar("car outside the beam cone", {})

reset()
addVehicle(2, 0, 40, 0)
addVehicle(3, 0, 15, 0)
addVehicle(4, -3.2, 25, 0)
scan()
checkRadar("multiple targets come back nearest first", {
  {id = 3, dRel = 10.5, yRel = 0.0, vRel = 0.0},
  {id = 4, dRel = 20.5, yRel = 3.2, vRel = 0.0},
  {id = 2, dRel = 35.5, yRel = 0.0, vRel = 0.0},
})

reset()
-- Rows are reused between scans, so a busy scan followed by a quiet one is the
-- case where leftovers leak back out as phantom vehicles.
addVehicle(2, 0, 15, 0)
addVehicle(3, 0, 30, 0)
addVehicle(4, 0, 45, 0)
addVehicle(5, 0, 60, 0)
scan()
checkRadar("four targets", {
  {id = 2, dRel = 10.5, yRel = 0.0, vRel = 0.0},
  {id = 3, dRel = 25.5, yRel = 0.0, vRel = 0.0},
  {id = 4, dRel = 40.5, yRel = 0.0, vRel = 0.0},
  {id = 5, dRel = 55.5, yRel = 0.0, vRel = 0.0},
})
world.vehicles[3], world.vehicles[4], world.vehicles[5] = nil, nil, nil
scan()
checkRadar("...and the three that left do not linger",
           {{id = 2, dRel = 10.5, yRel = 0.0, vRel = 0.0}})

reset()
addVehicle(2, 0, 20, 0)
pendingControl = '{"engaged": false, "radar": {"enabled": 0.0}}'
scan()
-- Disabled means SILENT, not "an empty packet": card's receiver times out after
-- half a second and reports no tracks, which is the same answer with no traffic
-- on the wire for it.
checks = checks + 1
if sentRadar == nil then
  print("  ok    enabled=0 pushed from beamngd stops the tracks entirely")
else
  failures = failures + 1
  print("  FAIL  enabled=0 pushed from beamngd stops the tracks entirely\n"
        .. "          still sent " .. #sentRadar .. " bytes")
end

reset()
-- A trailer in contact with us must not be reported as something to brake for.
addVehicle(2, 0, 8, 0)
world.collisions[1] = 2
scan()
checkRadar("vehicle touching us is not a radar target either", {})

-- Hand the last packet to the Python side so the wire format is checked across
-- both languages, not just against itself.
local dump = os.getenv("BEAMPILOT_RADAR_DUMP")
if dump then
  reset()
  addVehicle(2, 0, 20, 0, {vel = {0, 15, 0}})
  addVehicle(3, -3.5, 45, 0)
  scan()
  local fh = io.open(dump, "wb")
  if fh then
    fh:write(sentRadar or "")
    fh:close()
    print("\nwrote " .. #(sentRadar or "") .. " bytes of radar packet to " .. dump)
  end
end

print(string.format("\n%d checks, %d failures", checks, failures))
os.exit(failures == 0 and 0 or 1)
