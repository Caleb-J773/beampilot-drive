-- beampilot bridge protocol.
--
-- Modeled on the stock lua/vehicle/protocols/outgauge.lua and motionSim.lua
-- (both bundled with BeamNG.drive), combined into one custom protocol so
-- openpilot gets everything it needs -- including electrics.values.steering_input,
-- which neither stock protocol exposes -- in a single UDP packet.
--
-- This file only OUTPUTS telemetry (via the protocols.lua framework's normal
-- ffi-struct + fillStruct mechanism, same as any other protocol). Control
-- commands flow the other way over a second, plain UDP socket that this file
-- owns and polls itself every tick. That socket is what lets openpilot's
-- steering/throttle/brake actually drive the car, via input.event(), the same
-- function BeamNG's own AI (lua/vehicle/ai.lua) and the stock
-- controller/inputOutputDemo.lua use.
local M = {}

local TELEMETRY_PORT = 49152 -- this protocol's outbound port (mod -> openpilot)
local CONTROL_PORT = 49153 -- inbound control port (openpilot -> mod)
local CONTROL_ADDRESS = "127.0.0.1"
local CONTROL_TIMEOUT = 0.5 -- seconds; if openpilot goes quiet (crash/pause) this long, release control

local controlSocket
local lastControlAt = nil -- os.clock() timestamp of the last applied control packet, nil = never/released
local cameraSelected = false
-- Tracks whether WE are currently the ones driving via input.event (i.e.
-- openpilot was engaged last we checked). beamngd sends a control packet
-- every tick regardless of engaged state, so "not engaged" is the normal,
-- constant, common case -- releaseControl() must only ever act on the EDGE
-- out of engagement, never on every not-engaged tick. Otherwise it fights
-- (and wins against) the player's own WASD/controller input forever.
local isControlling = false

local function init()
  -- init() runs once per spawned vehicle regardless of who (if anyone) is
  -- driving it -- BeamNG's own protocols.lua framework only gates the
  -- per-tick fillStruct call on playerInfo.firstPlayerSeated, not init().
  -- With many vehicles in a scene (traffic AI included), binding the control
  -- socket here means every one of them races for the same port. Only ever
  -- bind lazily from pollControl(), which fillStruct only reaches for the
  -- vehicle that's actually player-seated -- see ensureControlSocket below.
  if controlSocket then
    controlSocket:close()
    controlSocket = nil
  end
  cameraSelected = false
end

local function reset()
  lastControlAt = nil
  isControlling = false
  cameraSelected = false
end

local function getAddress()       return CONTROL_ADDRESS end
local function getPort()          return TELEMETRY_PORT end
local function getMaxUpdateRate() return 100 end
local function isPhysicsStepUsed() return true end -- accurate high-rate motion data, like motionSim

local function getStructDefinition()
  -- All fields are 4-byte types (char[4], float, int, unsigned) so this
  -- struct is tightly packed with no compiler padding -- the Python side's
  -- struct.Struct format string just lists the same fields in the same order.
  -- Stick to the plain C types the stock outgauge.lua/motionSim.lua structs
  -- use (int/unsigned/float/char) rather than stdint types.
  return [[
    char     format[4];   // fixed value "BPL1", lets the reader sanity-check framing
    float    speed;       // m/s, wheelspeed
    float    steeringInput; // -1..1, electrics.values.steering_input (raw commanded input, ~no dynamics)
    float    steeringWheelDeg; // degrees, electrics.values.steering -- the REAL post-dynamics
                             // steering wheel angle (already scaled by this car's actual
                             // steeringWheelLock), unlike steeringInput above which is
                             // essentially just an echo of the last commanded input value.
                             // Use this one for anything that needs a genuine measurement.
    float    throttle;    // 0..1
    float    brake;       // 0..1
    float    clutch;      // 0..1
    int      gear;        // electrics.values.gearIndex (reverse/neutral/1st... same convention as outgauge.lua)
    unsigned dashLights;  // bit0 leftBlinker, bit1 rightBlinker, bit2 parkingBrake, bit3 ignitionOn
    float    posX, posY, posZ;    // world position, metres
    float    velX, velY, velZ;    // world velocity, m/s
    float    accX, accY, accZ;    // vehicle-relative acceleration, m/s^2, gravity not included
    float    rollPos, pitchPos, yawPos; // radians
    float    rollVel, pitchVel, yawVel; // radians/s
  ]]
end

local DL_LEFT_BLINKER  = 1
local DL_RIGHT_BLINKER = 2
local DL_PARKING_BRAKE = 4
local DL_IGNITION_ON   = 8

-- If openpilot stops sending control packets (crash, pause, bridge restart)
-- while it was engaged, don't leave the last throttle/steering command
-- latched forever -- input.event values persist until overwritten, so a
-- watchdog has to actively release control back to neutral. Only fires (and
-- only touches input.event) if we were actually driving.
local function releaseControl()
  if isControlling then
    input.event("steering", 0, FILTER_DIRECT)
    input.event("throttle", 0, FILTER_DIRECT)
    input.event("brake", 0, FILTER_DIRECT)
    isControlling = false
  end
  lastControlAt = nil
end

local function ensureControlSocket()
  if controlSocket then return end
  local sock = socket.udp()
  sock:settimeout(0)
  local ok, err = sock:setsockname(CONTROL_ADDRESS, CONTROL_PORT)
  if not ok then
    log("E", "", "beampilot: failed to bind control socket on "..CONTROL_ADDRESS..":"..CONTROL_PORT..": "..dumps(err))
    sock:close()
    return
  end
  controlSocket = sock
end

local function pollControl()
  ensureControlSocket()
  if not controlSocket then return end
  local gotPacket = false
  while true do
    local data = controlSocket:receive()
    if not data then break end
    gotPacket = true
    local ok, msg = pcall(jsonDecode, data, "beampilot control packet")
    if ok and type(msg) == "table" and msg.engaged then
      if msg.steering ~= nil then input.event("steering", msg.steering, FILTER_DIRECT) end
      if msg.throttle ~= nil then input.event("throttle", msg.throttle, FILTER_DIRECT) end
      if msg.brake ~= nil then input.event("brake", msg.brake, FILTER_DIRECT) end
      isControlling = true
      lastControlAt = os.clock()
    else
      -- not engaged (the normal, constant case) or malformed -- only actually
      -- touches input.event if we were previously driving (see isControlling)
      releaseControl()
    end
  end
  if not gotPacket and lastControlAt and (os.clock() - lastControlAt) > CONTROL_TIMEOUT then
    log("W", "", "beampilot: control packets stopped arriving, releasing control")
    releaseControl()
  end
end

local function fillStruct(o, dtSim)
  pollControl()

  if not electrics.values.watertemp then
    -- vehicle not fully initialized yet, skip this tick (same guard outgauge.lua uses)
    return
  end

  if not cameraSelected then
    -- Auto-select the openpilot_cam mod's rigidly-mounted, FOV-matched camera
    -- (mods/unpacked/openpilot_cam) so no manual camera switch is needed.
    -- Vehicle Lua can't touch GE-side state directly; queueGameEngineLua is
    -- the normal cross-VM bridge for this (see lua/vehicle/bullettime.lua and
    -- others for the same pattern).
    obj:queueGameEngineLua("core_camera.setByName(0, 'openpilot', false)")
    cameraSelected = true
  end

  o.format = "BPL1"
  o.speed = electrics.values.wheelspeed or 0
  o.steeringInput = electrics.values.steering_input or 0
  o.steeringWheelDeg = electrics.values.steering or 0
  o.throttle = electrics.values.throttle or 0
  o.brake = electrics.values.brake or 0
  o.clutch = electrics.values.clutch or 0
  o.gear = electrics.values.gearIndex or 0

  local dashLights = 0
  if (electrics.values.signal_L or 0) ~= 0 then dashLights = bit.bor(dashLights, DL_LEFT_BLINKER) end
  if (electrics.values.signal_R or 0) ~= 0 then dashLights = bit.bor(dashLights, DL_RIGHT_BLINKER) end
  if (electrics.values.parkingbrake or 0) ~= 0 then dashLights = bit.bor(dashLights, DL_PARKING_BRAKE) end
  if (electrics.values.ignitionLevel or 0) > 0 then dashLights = bit.bor(dashLights, DL_IGNITION_ON) end
  o.dashLights = dashLights

  local posX, posY, posZ = obj:getPositionXYZ()
  o.posX, o.posY, o.posZ = posX, posY, posZ

  local velX, velY, velZ = obj:getVelocityXYZ()
  o.velX, o.velY, o.velZ = velX, velY, velZ

  local ffisensors = sensors.ffiSensors
  o.accX = -ffisensors.sensorX
  o.accY = -ffisensors.sensorY
  o.accZ = -ffisensors.sensorZ

  local roll, pitch, yaw = obj:getRollPitchYaw()
  -- negate roll/yaw to match the sign convention motionSim.lua already uses
  o.rollPos, o.pitchPos, o.yawPos = -roll, pitch, -yaw

  local rollVel, pitchVel, yawVel = obj:getRollPitchYawAngularVelocity()
  o.rollVel, o.pitchVel, o.yawVel = rollVel, pitchVel, yawVel
end

M.init = init
M.reset = reset
M.getAddress = getAddress
M.getPort = getPort
M.getMaxUpdateRate = getMaxUpdateRate
M.getStructDefinition = getStructDefinition
M.fillStruct = fillStruct
M.isPhysicsStepUsed = isPhysicsStepUsed

return M
