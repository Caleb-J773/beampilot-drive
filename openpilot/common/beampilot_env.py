"""Typed environment-variable helpers for beampilot's tunable settings.

Every beampilot knob is read through here so that:
  - defaults live next to the code that uses them, not in a config file that
    can drift out of sync,
  - an unset environment behaves exactly like unmodified openpilot,
  - a malformed value falls back to the default instead of crashing a daemon
    mid-drive,
  - tools/beampilot_tui.py has one place to discover what is settable.

config_beampilot.sh is the intended place to set these; it is sourced by both
setup_beampilot.sh and launch_beampilot.sh.
"""
import os

TRUE_VALUES = ("1", "true", "yes", "on")
FALSE_VALUES = ("0", "false", "no", "off")


def env_float(name: str, default: float) -> float:
  raw = os.environ.get(name)
  if raw is None:
    return default
  try:
    return float(raw)
  except ValueError:
    return default


def env_int(name: str, default: int) -> int:
  raw = os.environ.get(name)
  if raw is None:
    return default
  try:
    return int(raw)
  except ValueError:
    return default


def env_str(name: str, default: str) -> str:
  raw = os.environ.get(name)
  return default if raw is None else raw


def env_bool(name: str, default: bool) -> bool:
  raw = os.environ.get(name)
  if raw is None:
    return default
  low = raw.strip().lower()
  if low in TRUE_VALUES:
    return True
  if low in FALSE_VALUES:
    return False
  return default
