#!/usr/bin/env python3
"""Install the BEAMPILOT opendbc platform into the opendbc submodule.

opendbc is a git submodule pointing at commaai/opendbc -- a repository we
cannot push to. So the BEAMPILOT platform lives HERE, in the repo that is
actually ours (tools/opendbc_beampilot_car/), and this installs it into the
submodule's package tree. Exactly the same trade the BeamNG mods make in
setup_beampilot.sh: source of truth in the repo, symlink into the place that
has to see it.

The alternative -- committing inside the submodule -- would work on one machine
and nowhere else. A fresh `git clone --recursive` fetches comma.ai's opendbc,
which has never heard of BEAMPILOT, and `config_beampilot.sh` would then point
FINGERPRINT at a platform that does not exist. `git submodule update` would
wipe it on this machine too.

Three things are needed, and only the first is ours alone:

  1. the package itself           -> symlinked, so repo edits apply live
  2. an entry in car/values.py    -> patched, marked, and idempotent
  3. an entry in torque_data/     -> patched, marked, and idempotent

Two and three are edits to comma.ai's files. They are small, marked with a
sentinel comment, and re-applied automatically -- because a submodule update
silently reverts them, and the symptom of that is openpilot refusing to start
with a KeyError on a car nobody can find.

Safe to run repeatedly. Run with --check to report without changing anything.
"""
import argparse
import os
import re
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(REPO, "tools", "opendbc_beampilot_car")
CAR_DIR = os.path.join(REPO, "opendbc_repo", "opendbc", "car")
TARGET = os.path.join(CAR_DIR, "beampilot")
VALUES = os.path.join(CAR_DIR, "values.py")
TORQUE = os.path.join(CAR_DIR, "torque_data", "override.toml")

MARK = "# beampilot: installed by tools/install_beampilot_car.py"

VALUES_IMPORT = f"from opendbc.car.beampilot.values import CAR as BEAMPILOT  {MARK}\n"
TORQUE_ENTRY = f"""
{MARK}
# Not a car. Steered by angle rather than torque as far as the real actuation
# goes, so the torque response factors are undefined the same way COMMA_BODY's
# are, and the lateral acceleration limit lives in the planner
# (BEAMPILOT_MAX_LAT_ACCEL) rather than in a measurement of how a real rack
# answers a real torque.
"BEAMPILOT" = [nan, 1000, nan]
"""

OK, FAIL, WARN = "✓", "✗", "!"


class Problem(Exception):
  pass


def _read(path: str) -> str:
  with open(path, encoding="utf-8") as f:
    return f.read()


def install_package(check: bool) -> str:
  """Symlink the package in, so an edit here takes effect without reinstalling."""
  if os.path.islink(TARGET):
    if os.path.realpath(TARGET) == os.path.realpath(SOURCE):
      return f"  {OK} package: already linked"
    if check:
      return f"  {FAIL} package: linked to the wrong place ({os.readlink(TARGET)})"
    os.unlink(TARGET)
  elif os.path.exists(TARGET):
    # A real directory here is almost certainly an older install that predates
    # the symlink, or a copy someone made. Move it rather than delete it: it
    # may hold edits that were never brought back into the repo.
    if check:
      return f"  {FAIL} package: a real directory, not a link (would be moved aside)"
    backup = TARGET + ".replaced"
    n = 0
    while os.path.exists(backup):
      n += 1
      backup = f"{TARGET}.replaced-{n}"
    shutil.move(TARGET, backup)
  if check:
    return f"  {FAIL} package: missing"
  os.symlink(SOURCE, TARGET)
  return f"  {OK} package: linked -> {os.path.relpath(SOURCE, REPO)}"


def install_values(check: bool) -> str:
  """Register the brand so car_helpers.load_interfaces() can find it."""
  text = _read(VALUES)
  if "BEAMPILOT" in text:
    return f"  {OK} values.py: already registered"
  if check:
    return f"  {FAIL} values.py: BEAMPILOT not registered"

  # Anchor on the first brand import rather than a line number, and fail loudly
  # if opendbc has restructured -- a patch that silently does nothing here
  # surfaces much later as an unfindable car.
  anchor = "from opendbc.car.body.values import CAR as BODY\n"
  if anchor not in text:
    raise Problem(f"{VALUES}: could not find the brand import block to patch")
  text = text.replace(anchor, VALUES_IMPORT + anchor, 1)

  # The Platform union is what BRANDS (and therefore PLATFORMS) is built from.
  union = re.search(r"^Platform = (.+)$", text, re.M)
  if not union:
    raise Problem(f"{VALUES}: could not find the `Platform = ...` union to patch")
  text = text.replace(union.group(0), f"Platform = BEAMPILOT | {union.group(1)}", 1)

  with open(VALUES, "w", encoding="utf-8") as f:
    f.write(text)
  return f"  {OK} values.py: registered BEAMPILOT"


def install_torque(check: bool) -> str:
  """get_std_params() looks every platform up here and KeyErrors without it."""
  text = _read(TORQUE)
  if "BEAMPILOT" in text:
    return f"  {OK} override.toml: already present"
  if check:
    return f"  {FAIL} override.toml: no BEAMPILOT torque entry"
  with open(TORQUE, "a", encoding="utf-8") as f:
    f.write(TORQUE_ENTRY)
  return f"  {OK} override.toml: added the BEAMPILOT entry"


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  ap.add_argument("--check", action="store_true",
                  help="report what is missing without changing anything")
  ap.add_argument("--quiet", action="store_true",
                  help="print nothing when everything is already in place")
  args = ap.parse_args()

  if not os.path.isdir(SOURCE):
    print(f"{FAIL} {SOURCE} is missing -- this is the source of truth, not the submodule copy")
    return 1
  if not os.path.isdir(CAR_DIR):
    print(f"{FAIL} {CAR_DIR} is missing -- run `git submodule update --init` first")
    return 1

  try:
    lines = [install_package(args.check),
             install_values(args.check),
             install_torque(args.check)]
  except Problem as e:
    print(f"{FAIL} {e}")
    print("  opendbc has changed shape. Patch it by hand, or open an issue.")
    return 1

  failed = [ln for ln in lines if FAIL in ln]
  if args.quiet and not failed and all(OK in ln and "already" in ln for ln in lines):
    return 0

  print("BEAMPILOT opendbc platform:")
  for line in lines:
    print(line)
  if failed and args.check:
    print(f"  {WARN} run: uv run python tools/install_beampilot_car.py")
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
