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

local DL_BSM_LEFT           = 16
local DL_BSM_RIGHT          = 32
local DL_BSM_LEFT_APPROACH  = 64
local DL_BSM_RIGHT_APPROACH = 128

-- ---------------------------------------------------------------------------
-- Perception: what the simulator can see that a camera on a screen cannot.
--
-- mapmgr.getObjects() is the same table BeamNG's own AI (lua/vehicle/ai.lua)
-- and ACC (extensions/tech/ACC.lua) use for traffic awareness, refreshed every
-- graphics frame from the game engine through the "objUpdate" mailbox.
-- Per-object geometry comes from the same natives ai.lua's getObjectBoundingBox
-- uses. One walk over that table feeds two consumers:
--
--   BSM   -- blind spot monitoring. openpilot has always known what to do with
--            carState.leftBlindspot/rightBlindspot (refuse a lane change into
--            that side, and cancel one already under way); nothing was ever
--            setting them, because a real car reads them off rear-corner radar.
--   RADAR -- radar points for radard. The simulated Honda is BOSCH_RADARLESS,
--            so opendbc hands radard an EMPTY RadarData at 20Hz and lead
--            detection falls back on the camera -- the same camera the README
--            admits is fed wide-lens intrinsics for an image that is not wide.
--            Distance to the car in front is exactly what that gets wrong.
--
-- Both are ground truth. That is a deliberate trade: this sees through walls
-- and fog, which no real sensor does, but the goal is a car that follows and
-- changes lanes properly, not a faithful sensor model.
--
-- Every value below is overridable at runtime. beamngd puts "bsm" and "radar"
-- objects inside the control packet it already sends (see applyConfig), which
-- is how the BEAMPILOT_* variables in config_beampilot.sh reach vehicle Lua --
-- which cannot read that process's environment itself.

-- BSM zone geometry, all in metres, all measured from the ego's own body so it
-- fits whatever vehicle is spawned instead of assuming a sedan:
--
--                   frontM (behind the front bumper)
--                      |
--        +-------------v--------------------+   <- outerM out from the flank
--        |                                  |
--  [==== ego vehicle ====]                  |
--        |                                  |
--        +----------------------------------+   <- innerM out from the flank
--                                           ^
--                          rearM (behind the rear bumper)
--
-- Defaults roughly follow SAE J2802's blind spot zone: it starts about level
-- with the driver's shoulder, ends a few metres past the rear bumper, and is
-- one lane wide.
local BSM_DEFAULTS = {
  enabled = 1,
  frontM = 1.5,        -- forward edge, measured BEHIND the ego's front bumper
  rearM = 4.0,         -- rear edge, measured BEHIND the ego's rear bumper
  innerM = 0.2,        -- inner edge, measured out from the ego's flank
  outerM = 3.6,        -- outer edge, measured out from the ego's flank
  heightM = 2.0,       -- half-height, so a car on an overpass does not count
  approachS = 2.0,     -- extend the rear edge by (closing speed * this); 0 disables
  approachMaxM = 20.0, -- ...but never by more than this
  minSpeedMs = 1.4,    -- below this we are parking or stopped; report nothing
  rangeM = 60.0,       -- cheap distance pre-filter before the OBB test
  ignoreTouching = 1,  -- skip vehicles in contact with us (i.e. a towed trailer)
  rateHz = 20.0,       -- scan rate; fillStruct runs at 100Hz, this need not
  debug = 0,           -- log every state change
}

-- Forward radar. Points are reported the way car.capnp defines a RadarPoint:
-- dRel in metres from OUR front bumper to the nearest surface of the target,
-- yRel in metres with LEFT positive (radard compares it against -leadsV3.y, and
-- the model's device frame is y-right), vRel in m/s along our heading with
-- negative meaning closing.
local RADAR_DEFAULTS = {
  enabled = 0,         -- off unless asked for; see beampilot_radar.py
  port = 49155,        -- straight to card.py, which owns radarTracks
  rangeM = 110.0,      -- shorter than real radar reaches, on purpose (below)
  halfWidthM = 3.0,    -- beam half-width at the bumper...
  spread = 0.07,       -- ...growing by this much per metre of range
  minDRelM = 0.5,      -- a forward radar cannot see behind its own bumper
  maxTracks = 12,      -- capped again by MAX_TRACKS in beampilot_radar.py
  rateHz = 20.0,       -- DT_MDL; radard is driven by the model's rate
  -- How much of the cheating to give back. mapmgr knows where every vehicle is,
  -- through hills and buildings, with exact velocities -- which is not a radar,
  -- it is omniscience, and openpilot behaves unrealistically well on it.
  oncoming = 0,        -- report vehicles travelling towards us. Off: an
                       -- approaching car is not a lead, and treating one as
                       -- such is a hard-braking event for no reason.
  occlusion = 1,       -- drop anything with no line of sight (static geometry
                       -- only, so a car does not hide the car behind it --
                       -- radar does see under and around one).
  noiseM = 0.12,       -- range noise, metres. Real radar is not exact.
  noiseMs = 0.06,      -- range-rate noise, m/s.
  debug = 0,
}

local RADAR_MAX_TRACKS = 24 -- must match MAX_TRACKS in beampilot_radar.py

local bsm, radar = {}, {}
local function resetPerceptionConfig()
  for k, v in pairs(BSM_DEFAULTS) do bsm[k] = v end
  for k, v in pairs(RADAR_DEFAULTS) do radar[k] = v end
end
resetPerceptionConfig()

local bsmLeft, bsmRight = false, false
local bsmLeftApproach, bsmRightApproach = false, false
local scanAccum = 0

-- Preallocated: a 20Hz scan should not churn the GC. The two BSM side zones
-- differ only in where their centre sits -- the half-extent vectors are
-- identical, since the sign of a half-extent means nothing to an OBB test --
-- so X/Y/Z are shared between them.
local zoneX, zoneY, zoneZ = vec3(), vec3(), vec3()
local zoneLC, zoneRC = vec3(), vec3()
local zoneEC, zoneEX = vec3(), vec3()   -- a zone stretched for a closing vehicle
local tgtX, tgtY, tgtZ = vec3(), vec3(), vec3()
local egoFwd, egoRight, egoUp, egoVel = vec3(), vec3(), vec3(), vec3()
local relPos, relVel = vec3(), vec3()
local bsmTouching = {}
local radarTracks = {}    -- reused list of {id, dRel, yRel, vRel} rows
local radarCount = 0
local rayFrom, rayDir = vec3(), vec3()
local tgtFwdWorld, tgtVel = vec3(), vec3()

-- A cheap deterministic jitter, so a track's reported range does not sit on the
-- exact truth. math.random would make the stream unreproducible run to run,
-- which is worse for debugging than a repeatable wobble.
local noisePhase = 0
local function jitter(scale)
  if scale <= 0 then return 0 end
  noisePhase = (noisePhase * 1103515245 + 12345) % 2147483648
  return ((noisePhase / 2147483648) * 2 - 1) * scale
end

-- Line of sight against static geometry. ai.lua:4189 uses the same idiom: the
-- ray reports the distance to the first hit, so anything >= the target's own
-- distance means nothing solid is in the way. Raised off the ground or every
-- ray terminates on the road surface immediately.
local function hasLineOfSight(egoCentre, egoUpVec, tgtC, distance)
  rayFrom:setAddScaled(egoCentre, egoUpVec, 0.5)
  rayDir:setSub2(tgtC, rayFrom)
  local len = rayDir:length()
  if len < 1e-3 then return true end
  rayDir:setScaled(1 / len)
  local ok, hit = pcall(function() return obj:castRayStatic(rayFrom, rayDir, len) end)
  if not ok or type(hit) ~= "number" then return true end -- no ray, no filtering
  return hit >= len - 0.5
end

-- The radar packet is built with ffi rather than assembled by hand: LuaJIT has
-- no string.pack (that is Lua 5.3), and BeamNG's own protocols.lua already
-- describes its wire formats as ffi structs. pack(1) is required -- the natural
-- layout would pad the 5-byte header out to 8 and the Python side unpacks with
-- '<' , which does not pad.
local radarFfi, radarPacket, radarSocket
do
  local ok, ffi = pcall(require, "ffi")
  if ok then
    radarFfi = ffi
    pcall(ffi.cdef, [[
      #pragma pack(push, 1)
      typedef struct { uint32_t trackId; float dRel; float yRel; float vRel; } beampilot_radar_track_t;
      typedef struct {
        char magic[4];
        uint8_t count;
        beampilot_radar_track_t tracks[24];
      } beampilot_radar_packet_t;
      #pragma pack(pop)
    ]])
    local made, packet = pcall(ffi.new, "beampilot_radar_packet_t")
    if made then
      radarPacket = packet
      ffi.copy(radarPacket.magic, "BPR1", 4)
    else
      radarFfi = nil
    end
  end
end

local function applyConfig(target, defaults, cfg)
  for k, default in pairs(defaults) do
    local v = cfg[k]
    if type(v) == "number" then
      target[k] = v
    elseif type(v) == "boolean" then
      target[k] = v and 1 or 0
    else
      target[k] = default -- absent means "back to the default", not "keep the old one"
    end
  end
end

local function applyBsmConfig(cfg) applyConfig(bsm, BSM_DEFAULTS, cfg) end
local function applyRadarConfig(cfg) applyConfig(radar, RADAR_DEFAULTS, cfg) end

-- Fills in one BSM zone's centre and forward half-extent. Positive `side` is
-- the ego's right. halfLen/halfWidth are the ego's own half extents, so the
-- zone hugs whatever vehicle is actually spawned.
local function bsmBuildZone(centre, halfX, egoCentre, halfLen, halfWidth, side, extraRear)
  local rear = bsm.rearM + extraRear
  -- Along the ego's forward axis the zone spans from (halfLen - frontM) ahead
  -- of the ego's centre back to -(halfLen + rear) behind it.
  local longCentre = -(bsm.frontM + rear) * 0.5
  local longHalf = (2 * halfLen - bsm.frontM + rear) * 0.5
  if longHalf <= 0 then return false end -- frontM swallowed the whole zone
  halfX:setScaled2(egoFwd, longHalf)
  centre:setAddScaled(egoCentre, egoFwd, longCentre)
  centre:setAddScaled(centre, egoRight, side * (halfWidth + (bsm.innerM + bsm.outerM) * 0.5))
  return true
end

local function radarSend()
  if not radarFfi or not radarPacket then return end
  if not radarSocket then
    local sock = socket.udp()
    if not sock then return end
    sock:settimeout(0)
    radarSocket = sock
  end
  local count = math.min(radarCount, RADAR_MAX_TRACKS)
  radarPacket.count = count
  for i = 1, count do
    local t = radarTracks[i]
    local slot = radarPacket.tracks[i - 1]
    slot.trackId = t[1] % 4294967296
    slot.dRel, slot.yRel, slot.vRel = t[2], t[3], t[4]
  end
  -- 5-byte header plus 16 bytes per track; sending the whole fixed array every
  -- time would waste most of a 400-byte datagram at 20Hz for nothing.
  local bytes = radarFfi.string(radarPacket, 5 + count * 16)
  radarSocket:sendto(bytes, CONTROL_ADDRESS, radar.port)
end

local function perceptionScan()
  local left, right, leftApproach, rightApproach = false, false, false, false
  radarCount = 0

  local wantBsm = bsm.enabled ~= 0
  local wantRadar = radar.enabled ~= 0 and radarFfi ~= nil
  local objects = (wantBsm or wantRadar) and mapmgr.getObjects() or nil

  if objects and next(objects) then
    egoVel:set(obj:getVelocityXYZ())
    -- Below a walking pace we are parking, and a blind spot warning is noise.
    -- Radar has no such threshold: stop-and-go is exactly when the lead matters.
    local bsmActive = wantBsm and egoVel:length() >= bsm.minSpeedMs

    if bsmActive or wantRadar then
      local egoCentre = obj:getCenterPosition()
      egoFwd:set(obj:getDirectionVector()); egoFwd:normalize()
      egoRight:set(obj:getDirectionVectorRight()); egoRight:normalize()
      -- Derived rather than read from getDirectionVectorUp so all three axes
      -- are guaranteed mutually perpendicular, which the OBB test assumes. The
      -- sign does not matter: the zone is symmetric about the ego's own plane.
      egoUp:setCross(egoFwd, egoRight); egoUp:normalize()

      local halfLen = obj:getInitialLength() * 0.5
      local halfWidth = obj:getInitialWidth() * 0.5

      local zoneOk = false
      if bsmActive then
        zoneY:setScaled2(egoRight, (bsm.outerM - bsm.innerM) * 0.5)
        zoneZ:setScaled2(egoUp, bsm.heightM)
        zoneOk = bsmBuildZone(zoneRC, zoneX, egoCentre, halfLen, halfWidth, 1, 0)
             and bsmBuildZone(zoneLC, zoneX, egoCentre, halfLen, halfWidth, -1, 0)
      end

      -- A coupled trailer is a separate vehicle sitting permanently across both
      -- blind spot zones. There is no "is this attached to me" query in vehicle
      -- Lua, but anything physically touching us is either towed or already a
      -- collision, and in neither case is a blind spot warning the useful
      -- signal. Radar skips it for the same reason: braking for your own
      -- trailer is not helpful.
      table.clear(bsmTouching)
      if bsm.ignoreTouching ~= 0 then
        for _, id in ipairs(mapmgr.objectCollisionIds) do bsmTouching[id] = true end
      end

      local bsmRangeSq = bsm.rangeM * bsm.rangeM
      local maxRange = math.max(bsm.rangeM, radar.rangeM)
      local maxRangeSq = maxRange * maxRange

      for id, o in pairs(objects) do
        if id ~= objectId and not bsmTouching[id] then
          local tgtC = obj:getObjectCenterPosition(id)
          local tgtFwd = tgtC and obj:getObjectDirectionVector(id)
          local tgtUp = tgtFwd and obj:getObjectDirectionVectorUp(id)
          if tgtUp then
            relPos:setSub2(tgtC, egoCentre)
            if relPos:squaredLength() <= maxRangeSq then
              -- Same construction as ai.lua's getObjectBoundingBox.
              tgtX:setScaled2(tgtFwd, obj:getObjectInitialLength(id) * 0.5)
              tgtY:setCross(tgtUp, tgtFwd)
              tgtY:setScaled(obj:getObjectInitialWidth(id) * 0.5 / math.max(tgtY:length(), 1e-30))
              tgtZ:setScaled2(tgtUp, obj:getObjectInitialHeight(id) * 0.5)

              if bsmActive and zoneOk and relPos:squaredLength() <= bsmRangeSq then
                -- Both sides, not just the one the target's centre sits on:
                -- something wide or badly angled -- a trailer, a car halfway
                -- through its own lane change -- can have its centre on one
                -- side while its body reaches into the zone on the other.
                local inLeft = overlapsOBB_OBB(zoneLC, zoneX, zoneY, zoneZ, tgtC, tgtX, tgtY, tgtZ)
                local inRight = overlapsOBB_OBB(zoneRC, zoneX, zoneY, zoneZ, tgtC, tgtX, tgtY, tgtZ)
                left = left or inLeft
                right = right or inRight

                -- A car still short of the zone but closing fast will be
                -- alongside by the time a lane change finishes -- exactly what
                -- a real blind spot warning is for. Stretch the rear edge by
                -- how far it closes in approachS seconds and retest.
                if bsm.approachS > 0 and not (inLeft and inRight)
                   and relPos:dot(egoFwd) < 0 and o.vel then
                  relVel:setSub2(o.vel, egoVel)
                  local closing = relVel:dot(egoFwd)
                  if closing > 0.5 then
                    local extra = math.min(bsm.approachMaxM, closing * bsm.approachS)
                    if not inLeft and bsmBuildZone(zoneEC, zoneEX, egoCentre, halfLen, halfWidth, -1, extra) then
                      leftApproach = leftApproach or overlapsOBB_OBB(zoneEC, zoneEX, zoneY, zoneZ, tgtC, tgtX, tgtY, tgtZ)
                    end
                    if not inRight and bsmBuildZone(zoneEC, zoneEX, egoCentre, halfLen, halfWidth, 1, extra) then
                      rightApproach = rightApproach or overlapsOBB_OBB(zoneEC, zoneEX, zoneY, zoneZ, tgtC, tgtX, tgtY, tgtZ)
                    end
                  end
                end
              end

              if wantRadar then
                -- dRel is defined in car.capnp as metres "from the front bumper
                -- of the car", so subtract our own half-length and however much
                -- of the target sticks out along OUR forward axis -- which is
                -- its full oriented extent, not half its length, for anything
                -- sitting at an angle.
                local extentFwd = math.abs(tgtX:dot(egoFwd)) + math.abs(tgtY:dot(egoFwd))
                                + math.abs(tgtZ:dot(egoFwd))
                local dRel = relPos:dot(egoFwd) - halfLen - extentFwd
                -- LEFT positive: radard matches yRel against -leadsV3.y, and
                -- the model's device frame is x-forward, y-RIGHT.
                local yRel = -relPos:dot(egoRight)
                if dRel >= radar.minDRelM and dRel <= radar.rangeM
                   and math.abs(yRel) <= radar.halfWidthM + radar.spread * dRel then
                  local vRel = 0
                  if o.vel then
                    relVel:setSub2(o.vel, egoVel)
                    vRel = relVel:dot(egoFwd)
                  end

                  -- Oncoming traffic is not a lead. Treating one as such is a
                  -- hard-braking event for a car that is going to pass on the
                  -- other side, and with the lane-width in-path test that is
                  -- exactly what a narrow road produces. A vehicle facing the
                  -- other way but NOT MOVING is kept: that is a broken-down car
                  -- in our lane, which very much is something to brake for.
                  local keep = true
                  if radar.oncoming == 0 and o.vel then
                    tgtVel:set(o.vel)
                    tgtFwdWorld:set(tgtFwd)
                    if tgtVel:length() > 2.0 and tgtFwdWorld:dot(egoFwd) < -0.2 then
                      keep = false
                    end
                  end

                  -- ...and neither is a car on the far side of a hill.
                  if keep and radar.occlusion ~= 0
                     and not hasLineOfSight(egoCentre, egoUp, tgtC, dRel) then
                    keep = false
                  end

                  if keep then
                    radarCount = radarCount + 1
                    local row = radarTracks[radarCount]
                    local nd = dRel + jitter(radar.noiseM)
                    local nv = vRel + jitter(radar.noiseMs)
                    if row then
                      row[1], row[2], row[3], row[4] = id, nd, yRel, nv
                    else
                      radarTracks[radarCount] = {id, nd, yRel, nv}
                    end
                  end
                end
              end
            end
          end
        end
      end
    end
  end

  if bsm.debug ~= 0 and (left ~= bsmLeft or right ~= bsmRight
                         or leftApproach ~= bsmLeftApproach or rightApproach ~= bsmRightApproach) then
    log("I", "", string.format("beampilot BSM: left=%s%s right=%s%s",
        tostring(left), leftApproach and " (closing)" or "",
        tostring(right), rightApproach and " (closing)" or ""))
  end
  bsmLeft, bsmRight = left, right
  bsmLeftApproach, bsmRightApproach = leftApproach, rightApproach

  if radar.enabled ~= 0 then
    -- Nearest first, then truncate: if there are more vehicles in the beam than
    -- the track budget, the far ones are the ones nothing downstream would have
    -- acted on anyway.
    --
    -- Insertion sort over the ACTIVE PREFIX, not table.sort over the list. The
    -- row tables are reused between scans to keep the GC out of a 20Hz loop, so
    -- the array still holds rows from whenever traffic was heaviest -- and
    -- table.sort would sort those in among the live ones and report vehicles
    -- that are no longer there. Cheap regardless: maxTracks is a dozen.
    for i = 2, radarCount do
      local row = radarTracks[i]
      local j = i - 1
      while j >= 1 and radarTracks[j][2] > row[2] do
        radarTracks[j + 1] = radarTracks[j]
        j = j - 1
      end
      radarTracks[j + 1] = row
    end
    if radarCount > radar.maxTracks then radarCount = math.floor(radar.maxTracks) end
    if radar.debug ~= 0 and radarCount > 0 then
      log("I", "", string.format("beampilot radar: %d track(s), nearest %.1fm at %+.1f m/s",
          radarCount, radarTracks[1][2], radarTracks[1][4]))
    end
    radarSend()
  end
end

-- fillStruct runs every physics step (100Hz). Scanning every vehicle that often
-- buys nothing -- a blind spot does not appear in 10ms, and radard consumes at
-- the model's 20Hz -- so run at the higher of the two configured rates and hold
-- the last answer in between. Driven by dtSim rather than a wall clock, so
-- pausing the game freezes the state instead of ageing it.
local function perceptionUpdate(dtSim)
  scanAccum = scanAccum + (dtSim or 0)
  local rateHz = math.max(bsm.rateHz, radar.enabled ~= 0 and radar.rateHz or 0, 1)
  local interval = 1 / rateHz
  if scanAccum < interval then return end
  scanAccum = math.min(scanAccum - interval, interval) -- never build up a backlog
  local ok, err = pcall(perceptionScan)
  if not ok then
    -- One bad frame (a vehicle despawning mid-scan, say) must not take the
    -- whole telemetry protocol down with it.
    log("E", "", "beampilot perception scan failed: " .. tostring(err))
    bsmLeft, bsmRight, bsmLeftApproach, bsmRightApproach = false, false, false, false
    radarCount = 0
  end
end

-- openpilot asks for the blinker to be switched off once a lane change is
-- finished; without it the signal stays on, since nothing in the game cancels
-- an indicator that was never physically stalked. Only ever toggles a signal
-- that is actually on, so beamngd can repeat the request for a few ticks
-- against UDP loss without flipping it back on again.
local function cancelSignal(side)
  if (electrics.values.hazard_enabled or 0) ~= 0 then return end -- do not break the hazards
  if side == "left" and (electrics.values.signal_left_input or 0) == 1 then
    electrics.toggle_left_signal()
  elseif side == "right" and (electrics.values.signal_right_input or 0) == 1 then
    electrics.toggle_right_signal()
  end
end
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
  bsmLeft, bsmRight = false, false
  bsmLeftApproach, bsmRightApproach = false, false
  radarCount = 0
  scanAccum = 0
  -- A reload or respawn drops whatever beamngd last pushed down, so go back to
  -- the built-in defaults and wait for the next config broadcast rather than
  -- carrying another vehicle's tuning over.
  resetPerceptionConfig()
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
    unsigned dashLights;  // bit0 leftBlinker, bit1 rightBlinker, bit2 parkingBrake, bit3 ignitionOn,
                          // bit4 blind spot left occupied, bit5 right occupied,
                          // bit6 left closing fast, bit7 right closing fast
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
    local decoded = ok and type(msg) == "table"
    -- Config and one-shot commands first, and outside the engaged check:
    -- perception has to keep working (and stay tunable) while openpilot is
    -- only watching, not driving, and a signal cancel is not an actuation.
    -- beamngd re-sends the config periodically rather than every tick, so a
    -- vehicle reload picks the settings back up on its own.
    if decoded and type(msg.bsm) == "table" then applyBsmConfig(msg.bsm) end
    if decoded and type(msg.radar) == "table" then applyRadarConfig(msg.radar) end
    if decoded and type(msg.cancelSignal) == "string" then cancelSignal(msg.cancelSignal) end
    if decoded and msg.engaged then
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

  -- After the init guard on purpose: obj's geometry queries are not meaningful
  -- until the vehicle is up, and this tick would not write dashLights anyway.
  perceptionUpdate(dtSim)

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
  -- BSM rides in spare dashLights bits rather than new struct fields on
  -- purpose: the struct's size is the wire format, and parse_telemetry in
  -- beamngd.py rejects any packet whose length does not match exactly. Adding
  -- fields would mean an updated beamngd paired with a not-yet-reinstalled mod
  -- (or the reverse) sees NO telemetry at all instead of just no BSM.
  if bsmLeft then dashLights = bit.bor(dashLights, DL_BSM_LEFT) end
  if bsmRight then dashLights = bit.bor(dashLights, DL_BSM_RIGHT) end
  if bsmLeftApproach then dashLights = bit.bor(dashLights, DL_BSM_LEFT_APPROACH) end
  if bsmRightApproach then dashLights = bit.bor(dashLights, DL_BSM_RIGHT_APPROACH) end
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
