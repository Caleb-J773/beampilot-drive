import { lua } from "@/bridge"

const BUTTON_ID = "beampilot-camera-tuner"

export async function onLoad() {
  await lua.extensions.load("beampilotCameraTuner")
  await lua.extensions.ui_pause_actions.registerModButton({
    id: BUTTON_ID,
    tabId: "mods",
    label: "Beampilot Camera",
    icon: "wrench",
    componentName: "/ui/ui-vue/mods/beampilot_camera/CameraTuner.vue",
  })
}

export async function onUnload() {
  await lua.extensions.ui_pause_actions.unregisterModButton(BUTTON_ID)
}
