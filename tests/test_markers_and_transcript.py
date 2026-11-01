import json

import jsonschema
import pytest
from PIL import Image

from grumble import schemas
from grumble.config import GameStateConfig
from grumble.gamestate import fetch_gamestate
from grumble.markers import is_black, pick_monitor
from grumble.transcribe import build_initial_prompt, format_time, render_text

MONITORS = [
    {"left": -1920, "top": 0, "width": 3840, "height": 1080},
    {"left": 0, "top": 0, "width": 1920, "height": 1080},
    {"left": -1920, "top": 0, "width": 1920, "height": 1080},
]


def test_pick_monitor_uses_window_centre():
    assert pick_monitor(MONITORS, (-1800, 100, -200, 900)) is MONITORS[2]
    assert pick_monitor(MONITORS, (100, 100, 900, 900)) is MONITORS[1]


def test_pick_monitor_falls_back_to_primary():
    assert pick_monitor(MONITORS, None) is MONITORS[1]
    assert pick_monitor(MONITORS, (9000, 9000, 9100, 9100)) is MONITORS[1]


def test_black_frame_detection():
    assert is_black(Image.new("RGB", (640, 360), (0, 0, 0)))
    assert not is_black(Image.new("RGB", (640, 360), (40, 80, 20)))


def test_gamestate_none_is_silent():
    assert fetch_gamestate(GameStateConfig()) == (None, None)


def test_gamestate_reads_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"level": 3}), encoding="utf-8")
    assert fetch_gamestate(GameStateConfig(type="file", path=str(path))) == ({"level": 3}, None)


def test_gamestate_failures_are_reported_not_raised(tmp_path):
    missing = GameStateConfig(type="file", path=str(tmp_path / "none.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    invalid = GameStateConfig(type="file", path=str(bad))
    unreachable = GameStateConfig(type="http", url="http://127.0.0.1:9/state", timeout_seconds=0.2)
    for config in (missing, invalid, unreachable):
        state, error = fetch_gamestate(config)
        assert state is None and error


def transcript(**changes):
    return {
        "session": "s",
        "model": "m",
        "audio_seconds": 10,
        "segments": [{"start": 1.0, "end": 2.0, "text": "hi"}],
        **changes,
    }


def test_transcript_schema():
    schemas.validate(transcript(), schemas.TRANSCRIPT)
    with pytest.raises(jsonschema.ValidationError):
        schemas.validate(transcript(segments=[{"start": 1.0}]), schemas.TRANSCRIPT)


def test_prompt_and_time_format():
    assert build_initial_prompt([]) is None
    assert build_initial_prompt(["Saucer Pass", "Burst"]) == "Glossary: Saucer Pass, Burst."
    assert format_time(3725.9) == "01:02:05"


def test_render_text_places_markers_in_order():
    text = render_text(
        [{"start": 1.0, "text": "first"}, {"start": 5.0, "text": "second"}],
        [{"id": 1, "t": 3.0, "screenshot": "screenshots/marker_001.jpg"}],
    )
    lines = text.splitlines()
    assert "first" in lines[0]
    assert "MARKER 1" in lines[1] and "marker_001.jpg" in lines[1]
    assert "second" in lines[2]
