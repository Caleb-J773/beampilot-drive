import unittest

from openpilot.selfdrive.modeld.beampilot_build import usbgpu_build_config


class TestChestnutBuildDevice(unittest.TestCase):
  def test_desktop_cuda_uses_selected_backend_and_visibility(self):
    flags, queue = usbgpu_build_config(
      "x86_64", "CUDA",
      "CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=1 DEV=CUDA",
    )

    assert "DEV=CUDA" in flags
    assert "CUDA_VISIBLE_DEVICES=1" in flags
    assert "WARP_DEV=CUDA" in flags
    assert "DEV=AMD" not in flags
    assert queue == "CUDA"

  def test_desktop_nv_keeps_selected_physical_device(self):
    flags, queue = usbgpu_build_config("x86_64", ":2+NV", "DEV=:2+NV")

    assert "DEV=:2+NV" in flags
    assert "WARP_DEV=:2+NV" in flags
    assert queue == ":2+NV"

  def test_comma_hardware_keeps_real_chestnut_split(self):
    flags, queue = usbgpu_build_config("comma_arm64", "QCOM", "DEV=QCOM")

    assert "DEV=AMD" in flags
    assert "WARP_DEV=QCOM" in flags
    assert queue == "AMD"
