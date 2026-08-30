-- openpilot camera: a rigidly mounted forward-facing camera for the
-- openpilot <-> BeamNG bridge.
--
-- openpilot's driving model was trained on a comma three bolted to the
-- windscreen. It assumes the camera is rigid with respect to the car and that
-- the intrinsics never change. Every comfort feature of the stock driver
-- camera (mouse look, seat adjustment, head bob, look-ahead yaw, horizon and
-- pitch stabilisation, FOV smoothing) violates that assumption, so none of
-- them exist here. Pose is a pure rigid transform of the vehicle pose and the
-- FOV is whatever it was set to, every frame, unconditionally.

local vecZ = vec3(0, 0, 1)
local cameraTuner = nil

local function effectiveConfig(veh, baseCfg)
  -- Load through BeamNG's extension manager so this camera and the Vue/Lua
  -- bridge share one module instance (and therefore one live working profile).
  cameraTuner = cameraTuner or rawget(_G, "beampilotCameraTuner")
  if not cameraTuner and extensions and type(extensions.load) == "function" then
    cameraTuner = extensions.load("beampilotCameraTuner")
  end
  if cameraTuner and type(cameraTuner.getEffectiveConfig) == "function" then
    return cameraTuner.getEffectiveConfig(veh, baseCfg)
  end
  return baseCfg
end

local C = {}
C.__index = C

-- Runtime configuration. The bridge overwrites this table via run_lua; the
-- defaults are only a starting point and are expected to be tuned per vehicle.
local defaultCfg = {
  fov     = 25.698296, -- VERTICAL degrees (BeamNG's fov is vertical).
                       -- Narrow raw frame: 1208px high, focal length 2648px.
                       -- In BEAMPILOT_CAMERA_MODE=wide_crop, beamngd sends
                       -- 93.619537 degrees (focal length 567px) to the bridge,
                       -- which overwrites this live through OPENPILOT_CAM.
  offRight = 0.0,  -- metres, +ve moves the camera toward the passenger side
  offFwd   = 0.55, -- metres, +ve moves the camera toward the windscreen
  offUp    = 0.85, -- metres, +ve moves the camera up from the vehicle origin
  pitch    = 0.0,  -- degrees, +ve looks down
  yaw      = 0.0,  -- degrees, +ve looks right
  rightSign = 1.0, -- flips the lateral basis if the cross product is handed the other way
  autoPlace = 0,   -- wide mode: use this vehicle's OOBB instead of fixed offsets
  wideHeight = 1.22,    -- metres above the bottom of the vehicle OOBB
  wideClearance = 0.15, -- metres ahead of the front of the vehicle OOBB
  commandPort = 49157, -- GE camera tuner -> beamngd calibration commands
  measuredHeight = -1, -- metres above the road, filled in by the ray below
}

function C:init()
  -- NOT isGlobal. Global cameras (free, steadycam) live in the "world" group
  -- and camera.lua actively exits them back to the vehicle cameras, which
  -- silently reverted this mode to orbit about a second after being set.
  -- Every constructor in cameraModes/ is instantiated per-vehicle into
  -- vdata.cameras[<filename>], so a plain vehicle camera named "openpilot" is
  -- already selectable on every car without registering anything.
  self.icon = "simobject_camera"
  self.pos = vec3()
  self.rot = quat()
  self.autoVehicleId = nil
  self.autoFront = nil
  self.autoBottom = nil
  -- Published so the bridge can read/modify it with run_lua.
  rawset(_G, "OPENPILOT_CAM", rawget(_G, "OPENPILOT_CAM") or deepcopy(defaultCfg))
end

function C:cfg()
  local c = rawget(_G, "OPENPILOT_CAM")
  if type(c) ~= "table" then
    c = deepcopy(defaultCfg)
    rawset(_G, "OPENPILOT_CAM", c)
  end
  -- Fill in anything the bridge left out so a partial table can't break us.
  for k, v in pairs(defaultCfg) do
    if c[k] == nil then c[k] = v end
  end
  return c
end

-- Allow the in-game camera UI / core_camera.setFOV to drive our FOV too, so
-- the value reported by get_camera_state always matches what we render.
function C:setFOV(fovDeg)
  local f = tonumber(fovDeg)
  if f then self:cfg().fov = f end
end

function C:onCameraChanged(focused) end
function C:reset()
  self.autoVehicleId = nil
  self.autoFront = nil
  self.autoBottom = nil
end
function C:setRefNodes(centerNodeID, leftNodeID, backNodeID)
  self.refNodes = self.refNodes or {}
  self.refNodes.ref = centerNodeID
  self.refNodes.left = leftNodeID
  self.refNodes.back = backNodeID
end

local fwd, up, right, camPos = vec3(), vec3(), vec3(), vec3()
local qyaw, qpitch, qrot = quat(), quat(), quat()
local down = vec3(0, 0, -1)
local rayTick = 0

-- BeamNG's reference node is in a different place on every JBeam.  A fixed
-- offFwd/offUp can consequently land inside a windscreen, dashboard, bonnet,
-- or even a bus cabin.  Measure the spawn OOBB in the vehicle's own forward/up
-- basis once per spawned vehicle.  Putting the wide lens just beyond its front
-- plane leaves all bodywork behind a <180-degree lens while retaining the
-- model's expected camera height above the road.  getSpawnWorldOOBB describes
-- the undeformed spawn shape, so a crash cannot make the camera wander.
local function measureVehicleBounds(veh, origin, vehicleFwd, vehicleUp)
  local box = veh:getSpawnWorldOOBB()
  if not box then return nil, nil end

  local front = -math.huge
  local bottom = math.huge
  for i = 0, 7 do
    local rel = box:getPoint(i) - origin
    front = math.max(front, rel:dot(vehicleFwd))
    bottom = math.min(bottom, rel:dot(vehicleUp))
  end
  if front == -math.huge or bottom == math.huge then return nil, nil end
  return front, bottom
end

local function updateAdaptiveAnchor(self, veh, origin, vehicleFwd, vehicleUp)
  local vehicleId = veh:getId()
  if self.autoVehicleId == vehicleId and self.autoFront and self.autoBottom then return true end

  local ok, front, bottom = pcall(measureVehicleBounds, veh, origin, vehicleFwd, vehicleUp)
  self.autoVehicleId = vehicleId
  if not ok or not front or not bottom then
    self.autoFront = nil
    self.autoBottom = nil
    log("W", "openpilotCamera", "could not measure vehicle bounds; using legacy camera offsets")
    return false
  end

  self.autoFront = front
  self.autoBottom = bottom
  log("I", "openpilotCamera", string.format(
    "vehicle %d adaptive anchor: front %.2fm, bottom %.2fm from reference node",
    vehicleId, front, bottom))
  return true
end

-- Height of the camera above the road, by downward ray against static
-- collision (terrain and road meshes; vehicles are excluded, which is what we
-- want -- we are measuring the road, not our own bodywork).
--
-- openpilot's model was trained with the camera ~1.22m above the road and
-- modeld only ever applies calibrationd's *rotation*, never a height
-- correction, so a camera at the wrong height permanently misjudges how far
-- away everything is. BeamNG's veh:getPosition() is the vehicle reference
-- node, whose height above the ground is a per-jbeam accident, so the offset
-- cannot be hardcoded -- it has to be measured on whatever car is spawned.
local function measureHeight(pos)
  local ok, d = pcall(castRayStatic, pos, down, 20)
  if ok and type(d) == 'number' and d > 0 and d < 20 then
    return d
  end
  return nil
end

function C:update(data)
  local baseCfg = self:cfg()
  local veh = data.veh or (be and be:getPlayerVehicle(0))
  local cfg = effectiveConfig(veh, baseCfg)

  if not veh then
    -- No vehicle: hold the last pose rather than snapping to the origin.
    data.res.pos:set(self.pos)
    data.res.rot:set(self.rot)
    data.res.fov = cfg.fov
    return
  end

  -- World-space orthonormal basis of the vehicle. getDirectionVector* are
  -- already normalised and, unlike node positions, stay well defined while the
  -- vehicle is being deformed.
  fwd:set(veh:getDirectionVector())
  up:set(veh:getDirectionVectorUp())
  fwd:normalize()
  up:normalize()
  right:setCross(fwd, up)
  right:normalize()
  right:setScaled(cfg.rightSign)

  -- Re-orthogonalise up so a deformed vehicle can't shear the camera basis.
  up:setCross(right, fwd)
  up:normalize()

  local p = veh:getPosition()
  camPos:set(p)
  if cfg.autoPlace ~= 0 and updateAdaptiveAnchor(self, veh, p, fwd, up) then
    camPos:setAdd(right * cfg.offRight)
    camPos:setAdd(fwd * (self.autoFront + cfg.wideClearance))
    camPos:setAdd(up * (self.autoBottom + cfg.wideHeight))
  else
    camPos:setAdd(right * cfg.offRight)
    camPos:setAdd(fwd * cfg.offFwd)
    camPos:setAdd(up * cfg.offUp)
  end

  qrot:setFromDir(fwd, up)
  if cfg.yaw ~= 0 then
    qyaw:setFromAxisAngle(up, math.rad(-cfg.yaw))
    qrot:setMul2(qyaw, qrot)
  end
  if cfg.pitch ~= 0 then
    qpitch:setFromAxisAngle(right, math.rad(-cfg.pitch))
    qrot:setMul2(qpitch, qrot)
  end

  self.pos:set(camPos)
  self.rot:set(qrot)

  data.res.pos:set(camPos)
  data.res.rot:set(qrot)
  data.res.fov = cfg.fov          -- set every frame: never smoothed, never saved

  -- Raycasting every frame is wasted work; the ride height only moves with the
  -- suspension. ~4Hz is plenty for the bridge to trim offUp at startup.
  rayTick = rayTick + 1
  if rayTick >= 15 then
    rayTick = 0
    local h = measureHeight(camPos)
    if h then baseCfg.measuredHeight = h end
  end
end

-- DO NOT CHANGE CLASS IMPLEMENTATION BELOW

return function(...)
  local o = ... or {}
  setmetatable(o, C)
  o:init()
  return o
end
