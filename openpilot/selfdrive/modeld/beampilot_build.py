"""Build-time tinygrad device selection for beampilot's desktop Chestnut mode."""


def usbgpu_build_config(arch: str, tg_device: str, tg_flags: str) -> tuple[str, str]:
  """Return the big model's compiler flags and runtime input-queue device.

  Upstream's comma ARM path renders/warps on QCOM and runs the model on the
  Chestnut accessory's AMD GPU. On a PC, CHESTNUT is only the large-model
  selector: there may be no AMD device at all, so compile and queue on the
  backend/device already selected for the ordinary model.
  """
  common = "DEBUG=2 FLOAT16=1 JIT_BATCH_SIZE=0 GMMU=0 TC_OPT=0"
  if arch == "comma_arm64":
    return f"{common} DEV=AMD WARP_DEV={tg_device}", "AMD"
  return f"{common} {tg_flags} WARP_DEV={tg_device}", tg_device
