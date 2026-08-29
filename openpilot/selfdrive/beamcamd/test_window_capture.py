"""Tests for the KWin/Wayland window-detection path.

This path cannot be exercised on the machine it was written on (X11/GNOME), and
it cannot be exercised in CI either, so the compositor is simulated instead: the
one shell-out helper every KWin call goes through is replaced with a fake that
returns realistic dbus-send and journalctl output. That covers the parsing and,
more importantly, the degraded cases -- the rule there is that an uncertain
answer must come back as None, never as a confident wrong one, because a wrong
window means beamcamd silently captures the wrong pixels.

Run directly (`python -m openpilot.selfdrive.beamcamd.test_window_capture`) or
under pytest.
"""
from openpilot.selfdrive.beamcamd import window_capture as wc

GAME = ('{"id":"{d290f1ee-6c54-4b01-90e6-d701748f0851}","caption":"BeamNG.drive - 0.39.4.0.20972'
        + ' - RELEASE - Vulkan","cls":"beamng.drive.x64","rname":"beamng.drive","pid":12345,'
        + '"x":0,"y":0,"width":2560,"height":1440,"normal":true,"minimized":false}')
KONSOLE = ('{"id":"{aaaaaaaa-0000-0000-0000-000000000001}","caption":"~/beampilot : nvim",'
           + '"cls":"konsole","rname":"konsole","pid":222,"x":100,"y":100,'
           + '"width":1200,"height":800,"normal":true,"minimized":false}')


class FakeKWin:
  """Stands in for _run(), answering as a KDE Plasma session would."""

  def __init__(self, has_kwin=True, load_id="3", journal=True, windows=(GAME, KONSOLE)):
    self.has_kwin, self.load_id, self.journal, self.windows = has_kwin, load_id, journal, windows
    self.marker = None

  def __call__(self, argv, timeout=5.0):
    joined = " ".join(argv)
    # unloadScript must be tested before loadScript -- it contains it.
    if "unloadScript" in joined or "Script.stop" in joined:
      return "method return\n"
    if "ListNames" in joined:
      return "org.kde.KWin\norg.kde.plasmashell\n" if self.has_kwin else "org.gnome.Shell\n"
    if "loadScript" in joined:
      path = next(a for a in argv if a.startswith("string:/"))[len("string:"):]
      with open(path) as fh:
        self.marker = fh.read().split('var M = "')[1].split('"')[0]
      return ("method return time=1.0 sender=:1.4 -> destination=:1.8 serial=12 reply_serial=2\n"
              + f"   int32 {self.load_id}\n")
    if "Script.run" in joined:
      return "method return time=1.0 sender=:1.4 -> destination=:1.8 serial=13 reply_serial=3\n"
    if argv[0] == "journalctl":
      if not self.journal:
        return ""
      lines = []
      for w in self.windows:
        # KWin logs each line twice: once via console.info, once via print().
        lines += [f"{self.marker} {w}"] * 2
      lines.append(f'{self.marker} {{"probe_done":true}}')
      return "\n".join(lines) + "\n"
    return "method return\n"


def _with(fake, fn):
  original = wc._run
  wc._run = fake
  try:
    return fn()
  finally:
    wc._run = original


def test_parses_windows_and_dedupes_double_emit():
  wins = _with(FakeKWin(), wc.kwin_windows)
  assert len(wins) == 2, wins
  game = next(w for w in wins if "BeamNG" in w["caption"])
  assert (game["width"], game["height"], game["x"], game["y"]) == (2560, 1440, 0, 0)
  # the probe_done sentinel has no "id" and must not become a window
  assert all("probe_done" not in str(w) for w in wins)


def test_matching_is_case_insensitive_over_caption_class_and_name():
  assert len(_with(FakeKWin(), lambda: wc.kwin_matches("beamng"))) == 1
  assert len(_with(FakeKWin(), lambda: wc.kwin_matches("KONSOLE"))) == 1
  # a compositor that answered but has no match is [] -- a real, negative answer
  assert _with(FakeKWin(), lambda: wc.kwin_matches("nothinghere")) == []


def test_uncertain_answers_are_none_never_a_guess():
  assert _with(FakeKWin(has_kwin=False), wc.kwin_windows) is None      # not KDE
  assert _with(FakeKWin(load_id="-1"), wc.kwin_windows) is None        # KWin refused the script
  assert _with(FakeKWin(journal=False), wc.kwin_windows) is None       # journal unreadable
  # ...but "KWin answered, and there are genuinely no windows" is [], not None
  assert _with(FakeKWin(windows=()), wc.kwin_windows) == []


def test_explains_a_native_wayland_window():
  original = wc.candidates
  wc.candidates = lambda match="beamng": []   # X11 sees nothing
  try:
    msg = _with(FakeKWin(), lambda: wc.explain_not_found("beamng"))
  finally:
    wc.candidates = original
  assert "NATIVE WAYLAND" in msg      # names the actual cause
  assert "BeamNG.drive" in msg        # quotes the window it found
  assert "XWayland" in msg            # and gives a concrete fix


def test_explains_a_missing_xdotool_without_blaming_wayland():
  original = wc.have_xdotool
  wc.have_xdotool = lambda: False
  try:
    msg = wc.explain_not_found("beamng")
  finally:
    wc.have_xdotool = original
  assert "xdotool is not installed" in msg
  assert "apt install xdotool" in msg


if __name__ == "__main__":
  tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
  failed = 0
  for t in tests:
    try:
      t()
      print(f"  PASS  {t.__name__}")
    except AssertionError as e:
      failed += 1
      print(f"  FAIL  {t.__name__}: {e}")
  print(f"\n{len(tests) - failed}/{len(tests)} passed")
  raise SystemExit(1 if failed else 0)
