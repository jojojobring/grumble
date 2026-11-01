"""Check capture on real hardware, without a game.

    python scripts/manual_capture_test.py --project my-game --seconds 20

Talk during the run and press the hotkey once or twice. Add --inject to have the
script press a throwaway hotkey itself. Add --transcribe to run Whisper as well.
"""

import argparse
import ctypes
import dataclasses
import sys
import threading
import time

import soundfile as sf

from grumble.capture import run_capture
from grumble.config import load_project
from grumble.recorder import SAMPLE_RATE
from grumble.transcribe import transcribe_session

_INJECT_HOTKEY = "ctrl+alt+shift+f11"
_VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "f11": 0x7A}


def _press_hotkey(after_seconds: float) -> None:
    time.sleep(after_seconds)
    keys = [_VK[name] for name in _INJECT_HOTKEY.split("+")]
    user32 = ctypes.WinDLL("user32")
    for key in keys:
        user32.keybd_event(key, 0, 0, 0)
    for key in reversed(keys):
        user32.keybd_event(key, 0, 2, 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--device")
    parser.add_argument("--inject", action="store_true")
    parser.add_argument("--transcribe", action="store_true")
    args = parser.parse_args()

    config = load_project(args.project)
    if args.device:
        config = dataclasses.replace(config, audio=dataclasses.replace(config.audio, device=args.device))
    if args.inject:
        config = dataclasses.replace(config, hotkey=_INJECT_HOTKEY)
        threading.Thread(target=_press_hotkey, args=(args.seconds / 2,), daemon=True).start()

    result = run_capture(config, max_seconds=args.seconds)
    session = result.session

    chunks = sorted(session.audio_dir.glob("*.flac"))
    frames = sum(sf.info(path).frames for path in chunks)
    markers = session.read_markers()
    manifest = session.read_manifest()
    failures = []
    if abs(frames / SAMPLE_RATE - args.seconds) > 2:
        failures.append(f"audio is {frames / SAMPLE_RATE:.1f}s, expected about {args.seconds}s")
    if manifest["status"] != "recorded":
        failures.append(f"status is {manifest['status']}")
    if args.inject and len(markers) != 1:
        failures.append(f"expected 1 marker, got {len(markers)}")
    for marker in markers:
        if not marker["screenshot"] or not (session.path / marker["screenshot"]).exists():
            failures.append(f"marker {marker['id']} has no screenshot")

    print(f"session:   {session.path}")
    print(f"device:    {manifest['device']}")
    print(f"audio:     {frames / SAMPLE_RATE:.1f}s in {len(chunks)} chunk(s), {manifest['overflow_count']} overflow(s)")
    for marker in markers:
        print(f"marker {marker['id']}:  t={marker['t']}s {marker['screenshot']} {marker.get('screenshot_warning') or ''}")

    if args.transcribe:
        transcribe_session(session, config)
        print((session.path / "transcript.txt").read_text(encoding="utf-8"))

    for failure in failures:
        print(f"FAIL: {failure}")
    print("PASS" if not failures else "FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
