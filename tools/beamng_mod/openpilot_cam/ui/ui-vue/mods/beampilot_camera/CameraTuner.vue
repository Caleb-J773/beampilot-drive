<template>
  <div ref="rootRef" class="camera-tuner">
    <BngGroupPanel title="Beampilot Camera">
      <template v-if="loading">
        <p>Loading camera settings…</p>
      </template>

      <template v-else-if="!cameraState.available">
        <p>{{ cameraState.error ? "Camera tuner error:" : "Spawn a vehicle before tuning the camera." }}</p>
        <p v-if="cameraState.error" class="message error">{{ cameraState.error }}</p>
        <BngButton @click="refresh">Refresh</BngButton>
      </template>

      <template v-else>
        <div class="vehicle-heading">
          <div>
            <div class="vehicle-name">{{ cameraState.vehicleName }}</div>
            <div class="vehicle-key">Profile key: {{ cameraState.vehicleKey }}</div>
          </div>
          <span class="mode-pill">{{ cameraState.mode }}</span>
        </div>

        <div class="readouts">
          <span>Vertical FOV: {{ format(cameraState.fov, 2) }}°</span>
          <span v-if="cameraState.measuredHeight > 0">
            Measured road height: {{ format(cameraState.measuredHeight, 2) }} m
          </span>
        </div>

        <BngRow
          v-for="field in visibleFields"
          :key="field.name"
          class="camera-field"
          vertical
          :tooltip="field.help"
        >
          <template #label>
            <span>{{ field.label }}</span>
            <span v-if="field.overridden" class="override-badge">vehicle override</span>
          </template>
          <BngSlider
            v-model="field.value"
            :min="field.min"
            :max="field.max"
            :step="field.step"
            :unit="field.unit"
            :debounce="0"
            :orig-value="field.origValue"
            with-input
            with-reset
            @valueChanged="value => updateField(field.name, value)"
          />
        </BngRow>

        <div v-if="cameraState.error" class="message error">{{ cameraState.error }}</div>
        <div v-else-if="cameraState.message" class="message">{{ cameraState.message }}</div>

        <div class="actions">
          <BngButton @click="activateCamera">Activate camera</BngButton>
          <BngButton :disabled="!cameraState.dirty" @click="save">Save for this vehicle</BngButton>
          <BngButton :disabled="!cameraState.dirty" @click="revert">Revert</BngButton>
          <BngButton @click="reset">Use TUI/default pose</BngButton>
          <BngButton @click="confirmOrResetCalibration">
            {{ calibrationConfirm ? "Confirm calibration reset" : "Reset openpilot calibration" }}
          </BngButton>
        </div>

        <p class="save-hint">Not saved until you click Save.</p>
        <p class="save-hint">Calibration reset briefly disengages; drive straight above 15 mph after.</p>
      </template>
    </BngGroupPanel>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue"
import { runRaw, serialize } from "@/bridge/libs/Lua.js"
import { BngButton, BngGroupPanel, BngRow, BngSlider } from "@/common/components/base"
import { setFocus } from "@/services/uiNavFocus"

defineOptions({ name: "BeampilotCameraTuner" })

const rootRef = ref(null)
const loading = ref(true)
const calibrationConfirm = ref(false)
let calibrationConfirmTimer = null
let unavailableRefreshTimer = null
const cameraState = reactive({
  available: false,
  fields: [],
})

const visibleFields = computed(() => (cameraState.fields || []).filter(field => field.visible))

function applyState(next) {
  for (const key of Object.keys(cameraState)) delete cameraState[key]
  Object.assign(cameraState, next || { available: false, fields: [] })
  if (!cameraState.fields) cameraState.fields = []
}

// beampilotCameraTuner is a mod-defined GE extension, not one of BeamNG's own
// (LuaFunctionSignatures.js only declares built-in extensions like
// ui_pause_actions, so `lua.extensions.beampilotCameraTuner` is always
// undefined). extensions.load() registers a loaded extension as a global of
// the same name, so call it through runRaw instead -- see Garage.vue /
// TodControl.vue in BeamNG's own ui-vue source for the same pattern.
async function invoke(method, ...args) {
  try {
    const argList = args.map(arg => serialize(arg)).join(",")
    applyState(await runRaw(`beampilotCameraTuner.${method}(${argList})`))
  } catch (error) {
    cameraState.error = String(error)
  } finally {
    loading.value = false
  }
}

function refresh() {
  return invoke("getState")
}

function updateField(name, value) {
  return invoke("setValue", name, value)
}

function activateCamera() {
  return invoke("activateCamera")
}

function save() {
  return invoke("save")
}

function revert() {
  return invoke("revert")
}

function reset() {
  return invoke("reset")
}

function confirmOrResetCalibration() {
  if (!calibrationConfirm.value) {
    calibrationConfirm.value = true
    cameraState.message = "Press again to reset openpilot camera calibration"
    clearTimeout(calibrationConfirmTimer)
    calibrationConfirmTimer = setTimeout(() => {
      calibrationConfirm.value = false
    }, 8000)
    return
  }
  calibrationConfirm.value = false
  clearTimeout(calibrationConfirmTimer)
  return invoke("resetCalibration")
}

function format(value, digits) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(digits) : "—"
}

function focusEntry() {
  const target = rootRef.value?.querySelector("button:not([disabled]), [bng-nav-item]:not([disabled]), [tabindex='0']")
  if (!target) return false
  return setFocus(target) || (target.focus(), document.activeElement === target)
}

defineExpose({ focusEntry })
onMounted(() => {
  refresh()
  unavailableRefreshTimer = setInterval(() => {
    if (!loading.value && !cameraState.available) refresh()
  }, 750)
})
onBeforeUnmount(() => {
  clearTimeout(calibrationConfirmTimer)
  clearInterval(unavailableRefreshTimer)
})
</script>

<style lang="scss" scoped>
.camera-tuner {
  height: 100%;
  padding: 0.5rem 0.75rem;
  overflow: auto;
}

.vehicle-heading,
.actions,
.readouts {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.65rem;
}

.vehicle-heading {
  justify-content: space-between;
  margin-bottom: 0.7rem;
}

.vehicle-name {
  font-size: 1.3rem;
  font-weight: 700;
}

.vehicle-key,
.save-hint {
  opacity: 0.72;
  font-size: 0.9rem;
}

.mode-pill,
.override-badge {
  padding: 0.15rem 0.45rem;
  border-radius: 0.35rem;
  background: rgba(255, 255, 255, 0.13);
  font-size: 0.8rem;
}

.override-badge {
  margin-left: 0.45rem;
  color: #ffb52e;
}

.readouts {
  margin-bottom: 0.8rem;
  opacity: 0.85;
}

.camera-field {
  margin-bottom: 0.45rem;
}

.actions {
  margin-top: 1rem;
}

.message {
  margin-top: 0.75rem;
  color: #b8e6a2;
}

.message.error {
  color: #ff9a8f;
}

.save-hint {
  margin-bottom: 0;
}
</style>
