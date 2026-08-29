#!/usr/bin/env python3
"""The setup screen has to tell the truth about what is on and what is off.

Run with: uv run python tools/test_beampilot_tui.py

Every setting in beampilot_tui.py carries a `default` string, hand-written next
to the label. The value the code ACTUALLY uses when the variable is unset lives
somewhere else entirely -- in an env_bool()/env_float() call in whichever daemon
reads it. Nothing tied the two together, and they drifted:

  BEAMPILOT_AUTO_LANE_CHANGE   screen said 1     code used False
  BEAMPILOT_MAX_LAT_ACCEL      screen said 5.0   code used 3.0
  BEAMPILOT_ACCEL_SCALE        screen said 2.0   code used 1.0

which matters because the screen falls back to that string for any key not in
the config file. A wrong one shows a feature as enabled while it runs disabled.

The second half of the file covers the other way the screen used to lie: saving
wrote out every setting it knew about, at whatever the default was that day.
Those lines then outranked the code permanently -- change a default in the
source and the config still pinned the old number, with the screen reading it
back and presenting it as current.
"""
import ast
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("TERM", "xterm")

from tools import beampilot_tui as tui

ENV_READERS = ("env_bool", "env_float", "env_int", "env_str")

# Settings that are not read through common/beampilot_env.py: openpilot's own
# variables, and the ones the launcher scripts interpret themselves.
NOT_ENV_BACKED = {
  "BEAMPILOT_GPU", "BEAMPILOT_GPU_INDEX", "BEAMPILOT_CALIBRATION",
  "BEAMPILOT_CAPTURE_BACKEND", "BEAMPILOT_CAM_WINDOW", "BEAMPILOT_CAM_MONITOR",
  "BEAMPILOT_CAM_REGION", "BEAMPILOT_IGNORE_COMM_ISSUE", "BEAMPILOT_LAUNCH_DELAY",
  "BEAMPILOT_PERSONALITY", "BIG", "SCALE", "CHESTNUT", "FINGERPRINT", "BLOCK",
}


def code_defaults() -> dict[str, tuple[str, str]]:
  """{KEY: (default as a string, where it is read)} by reading the source.

  Static parse rather than an import: these live at module scope in daemons
  that pull in cereal, the model, and a camera stack, and half of them would
  latch the environment this test is running under anyway.
  """
  found: dict[str, tuple[str, str]] = {}
  for root, dirs, files in os.walk(REPO):
    dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "third_party", "node_modules")]
    for fn in files:
      if not fn.endswith(".py") or fn.startswith("test_") or fn == "beampilot_env.py":
        continue
      path = os.path.join(root, fn)
      try:
        tree = ast.parse(open(path, encoding="utf-8", errors="ignore").read())
      except (SyntaxError, ValueError):
        continue
      for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ENV_READERS and len(node.args) == 2):
          continue
        key, dflt = node.args
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
          continue
        if not key.value.startswith("BEAMPILOT_"):
          continue
        where = f"{os.path.relpath(path, REPO)}:{node.lineno}"
        if isinstance(dflt, ast.Constant):
          v = dflt.value
          if node.func.id == "env_bool" or isinstance(v, bool):
            text = "1" if v else "0"
          elif isinstance(v, float):
            text = f"{v:g}"
          else:
            text = str(v)
        else:
          text = "<derived>"
        found.setdefault(key.value, (text, where))
  return found


class DefaultsMatchTheCode(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.code = code_defaults()
    cls.settings = {s.key: s for sec in tui.build_sections() for s in sec.settings}

  def test_the_scan_found_the_settings_at_all(self):
    # If the walk breaks, every other check here passes vacuously.
    self.assertGreater(len(self.code), 30, "source scan found almost nothing")
    self.assertIn("BEAMPILOT_REPORT_GEAR", self.code)

  def test_every_default_matches_what_the_code_does_when_unset(self):
    wrong = []
    for key, s in sorted(self.settings.items()):
      if key in NOT_ENV_BACKED or key not in self.code:
        continue
      want, where = self.code[key]
      if want == "<derived>":
        continue   # covered by test_derived_defaults_track_what_they_derive_from
      got = s.default
      same = got == want
      if not same and s.numeric:
        try:
          same = float(got) == float(want)
        except ValueError:
          same = False
      if not same:
        wrong.append(f"    {key}: screen says {got!r}, {where} uses {want!r}")
    self.assertFalse(wrong, "TUI defaults disagree with the code:\n" + "\n".join(wrong))

  def test_a_setting_exists_for_the_features_that_can_be_switched_off(self):
    # These are the ones a driver turns off mid-session; a knob with no row is
    # a knob nobody finds.
    for key in ("BEAMPILOT_REPORT_GEAR", "BEAMPILOT_RADAR", "BEAMPILOT_BSM",
                "BEAMPILOT_CURVE_SLOWDOWN", "BEAMPILOT_RADAR_LEADS",
                "BEAMPILOT_BSM_INDICATOR", "BEAMPILOT_RADAR_INDICATOR",
                "BEAMPILOT_SIGNAL_AUTO_CANCEL", "BEAMPILOT_LANE_CHANGE_ABORT",
                "BEAMPILOT_CAMERA_MODE"):
      self.assertIn(key, self.settings, f"{key} has no row in the setup screen")

  def test_on_off_settings_offer_both(self):
    for key, s in sorted(self.settings.items()):
      if key in NOT_ENV_BACKED or key not in self.code:
        continue
      if self.code[key][0] in ("0", "1") and not s.numeric:
        self.assertEqual(sorted(s.choices or []), ["0", "1"], key)
        self.assertIn(s.default, ("0", "1"), key)

  def test_derived_defaults_track_what_they_derive_from(self):
    s = self.settings["BEAMPILOT_CURVE_LAT_ACCEL"]
    self.assertIsNotNone(s.derive, "cornering accel derives from the lateral limit")
    # 0.7x the lateral limit, per beampilot_curve.py
    self.assertEqual(s.default_for({}), "2.1")                                # unset -> ISO 3.0
    self.assertEqual(s.default_for({"BEAMPILOT_MAX_LAT_ACCEL": "5.0"}), "3.5")
    self.assertEqual(s.default_for({"BEAMPILOT_MAX_LAT_ACCEL": "junk"}), "2.1")

  def test_numeric_settings_compare_by_value_not_by_spelling(self):
    s = self.settings["BEAMPILOT_BSM_REAR_M"]
    self.assertTrue(s.is_default("4", {}))
    self.assertTrue(s.is_default("4.0", {}))
    self.assertFalse(s.is_default("4.5", {}))

  def test_no_duplicate_or_empty_keys(self):
    keys = [s.key for sec in tui.build_sections() for s in sec.settings]
    self.assertEqual(len(keys), len(set(keys)), "a key appears in two sections")
    self.assertTrue(all(keys))


class SavingDoesNotPinDefaults(unittest.TestCase):
  """write_config's contract: unset has to stay unset."""

  def setUp(self):
    self.tmp = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    self.addCleanup(os.unlink, self.tmp.name)
    self._real = tui.CONFIG
    tui.CONFIG = self.tmp.name
    self.addCleanup(setattr, tui, "CONFIG", self._real)

  def write(self, text):
    with open(self.tmp.name, "w") as f:
      f.write(text)

  def read(self):
    with open(self.tmp.name) as f:
      return f.read()

  def test_a_value_left_at_the_default_is_not_written(self):
    self.write("export FOO=1\n")
    tui.write_config({"BEAMPILOT_RADAR": "0"}, {"BEAMPILOT_RADAR": "0"})
    self.assertNotIn("BEAMPILOT_RADAR", self.read())

  def test_a_value_that_differs_is_written(self):
    self.write("export FOO=1\n")
    tui.write_config({"BEAMPILOT_RADAR": "1"}, {"BEAMPILOT_RADAR": "0"})
    self.assertIn('export BEAMPILOT_RADAR="1"', self.read())

  def test_turning_something_back_off_removes_the_line_it_added(self):
    # The bug in full: switch a feature on, save, switch it off, save. The
    # second save used to leave the "1" behind or replace it with a pinned "0".
    self.write("export FOO=1\n")
    tui.write_config({"BEAMPILOT_RADAR": "1"}, {"BEAMPILOT_RADAR": "0"})
    tui.write_config({"BEAMPILOT_RADAR": "0"}, {"BEAMPILOT_RADAR": "0"})
    self.assertNotIn("BEAMPILOT_RADAR", self.read())
    self.assertNotIn(tui.TUI_BLOCK_MARKER, self.read())

  def test_the_hand_written_part_of_the_file_keeps_its_defaults(self):
    # Explicit in the shipped config, with the comment that explains it: that
    # line is documentation and stays even though it matches the default.
    self.write('# how the gear is reported\nexport BEAMPILOT_REPORT_GEAR="1"    # keep\n')
    tui.write_config({"BEAMPILOT_REPORT_GEAR": "1"}, {"BEAMPILOT_REPORT_GEAR": "1"})
    out = self.read()
    self.assertIn('export BEAMPILOT_REPORT_GEAR="1"', out)
    self.assertIn("# keep", out)
    self.assertIn("# how the gear is reported", out)

  def test_a_hand_written_line_still_takes_a_new_value(self):
    self.write('export BEAMPILOT_REPORT_GEAR="1"    # units\n')
    tui.write_config({"BEAMPILOT_REPORT_GEAR": "0"}, {"BEAMPILOT_REPORT_GEAR": "1"})
    out = self.read()
    self.assertIn('export BEAMPILOT_REPORT_GEAR="0"', out)
    self.assertIn("# units", out)

  def test_unrelated_lines_survive(self):
    self.write("# a comment\nexport SOMETHING_ELSE=2\n\nexport BEAMPILOT_RADAR=\"1\"\n")
    tui.write_config({"BEAMPILOT_RADAR": "0"}, {"BEAMPILOT_RADAR": "0"})
    out = self.read()
    self.assertIn("# a comment", out)
    self.assertIn("export SOMETHING_ELSE=2", out)

  def test_read_config_takes_the_last_word_the_way_bash_does(self):
    self.write('export BEAMPILOT_RADAR="1"\nexport BEAMPILOT_RADAR="0"\n')
    self.assertEqual(tui.read_config()["BEAMPILOT_RADAR"], "0")

  def test_a_commented_out_line_reads_as_unset(self):
    self.write('# export BEAMPILOT_RADAR="1"\n')
    self.assertNotIn("BEAMPILOT_RADAR", tui.read_config())

  def test_round_trip_of_the_real_config_changes_nothing_it_should_not(self):
    with open(self._real) as f:
      original = f.read()
    self.write(original)
    sections = tui.build_sections()
    on_disk = tui.read_config()
    defaults = {s.key: s.default_for(on_disk) for sec in sections for s in sec.settings}
    values = {s.key: on_disk.get(s.key, defaults[s.key]) for sec in sections for s in sec.settings}
    values.pop("BEAMPILOT_GPU")
    tui.write_config(values, defaults)
    after = tui.read_config()
    for key, want in on_disk.items():
      if key in ("USE_NV", "USE_AMD"):
        continue
      if key in defaults and want == defaults[key]:
        continue    # a materialised default is allowed to be pruned away
      self.assertEqual(after.get(key), want, f"{key} changed on a no-op save")


class TheShippedConfigAgreesWithItself(unittest.TestCase):
  def test_no_appended_block_pins_a_value_the_code_already_defaults_to(self):
    """A pinned default is invisible until a default changes, then it wins."""
    sections = tui.build_sections()
    on_disk = tui.read_config()
    defaults = {s.key: s.default_for(on_disk) for sec in sections for s in sec.settings}
    numeric = {s.key for sec in sections for s in sec.settings if s.numeric}
    pinned = []
    in_block = False
    with open(tui.CONFIG) as f:
      for lineno, line in enumerate(f, 1):
        if line.strip().startswith(tui.TUI_BLOCK_MARKER):
          in_block = True
          continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
          continue
        if not stripped.startswith("export "):
          in_block = False
          continue
        if not in_block:
          continue
        key, _, raw = stripped[len("export "):].partition("=")
        val = raw.split("#")[0].strip().strip('"').strip("'")
        if key not in defaults or key in tui.NEVER_PRUNE:
          continue
        same = val == defaults[key]
        if not same and key in numeric:
          try:
            same = float(val) == float(defaults[key])
          except ValueError:
            same = False
        if same:
          pinned.append(f"    {tui.CONFIG}:{lineno}  {key}={val!r} is already the default")
    self.assertFalse(pinned, "config pins values the code already defaults to:\n" + "\n".join(pinned))


class TheReadmeAgreesToo(unittest.TestCase):
  """The settings tables in the README are the third copy of these numbers."""

  ROW = __import__("re").compile(r'^\|\s*`(BEAMPILOT_[A-Z0-9_]+)`\s*\|\s*`?([^|`]*)`?\s*\|')

  def test_documented_defaults_match_the_code(self):
    code = code_defaults()
    wrong = []
    with open(os.path.join(REPO, "README.md")) as f:
      for lineno, line in enumerate(f, 1):
        m = self.ROW.match(line.strip())
        if not m:
          continue
        key, doc = m.group(1), m.group(2).strip().strip("`")
        if key not in code or code[key][0] == "<derived>":
          continue
        want = code[key][0]
        same = doc == want
        if not same:
          try:
            same = float(doc) == float(want)
          except ValueError:
            same = False
        if not same:
          wrong.append(f"    README.md:{lineno}  {key}: documented {doc!r}, {code[key][1]} uses {want!r}")
    self.assertFalse(wrong, "README documents the wrong defaults:\n" + "\n".join(wrong))


if __name__ == "__main__":
  unittest.main(verbosity=2)
