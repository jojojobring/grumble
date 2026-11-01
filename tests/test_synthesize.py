import json

import jsonschema
import pytest
from PIL import Image

from grumble import schemas
from grumble.config import ProjectConfig
from grumble.session import create_session
from grumble.synthesize import (
    OUTPUT_SCHEMA,
    SynthesisError,
    build_content,
    build_proposals,
    model_name,
    near_markers,
    process_session,
    render_transcript,
)

SEGMENTS = [
    {"start": 1.0, "end": 2.0, "text": "wow nice"},
    {"start": 50.0, "end": 52.0, "text": "the goalie clips through the net"},
    {"start": 60.0, "end": 62.0, "text": "goalie is in the net again"},
    {"start": 90.0, "end": 91.0, "text": "something about the uh"},
]
MARKERS = [{"id": 1, "t": 55.0, "screenshot": "screenshots/marker_001.jpg"}]


def output(**changes):
    return {
        "new_issues": [
            {
                "title": "Goalie clips through net",
                "type": "bug",
                "description": "d",
                "repro_steps": [],
                "severity": "high",
                "segment_ids": [3, 2],
                "marker_ids": [1],
            }
        ],
        "uncertain": [{"summary": "s", "why_uncertain": "cut off", "segment_ids": [4], "marker_ids": []}],
        "discarded_segment_ids": [1],
        **changes,
    }


def build(data=None):
    return build_proposals(data or output(), "session", SEGMENTS, MARKERS, "model", {"input_tokens": 1})


def test_proposals_use_real_quotes_and_paths():
    issue = build()["new_issues"][0]
    assert [e["text"] for e in issue["excerpts"]] == ["the goalie clips through the net", "goalie is in the net again"]
    assert issue["excerpts"][0]["start"] == 50.0
    assert issue["screenshots"] == ["screenshots/marker_001.jpg"]
    assert issue["id"] == "issue-1"


def test_discarded_lines_are_kept_with_count():
    discarded = build()["discarded"]
    assert discarded["count"] == 1
    assert discarded["excerpts"][0]["text"] == "wow nice"


def test_line_cited_and_discarded_stays_with_the_item():
    result = build(output(discarded_segment_ids=[1, 2]))
    assert result["discarded"]["count"] == 1


def test_unsorted_lines_are_surfaced_not_lost():
    result = build(output(discarded_segment_ids=[]))
    assert [e["segment_id"] for e in result["unaccounted"]] == [1]
    assert any("unsorted" in warning for warning in result["warnings"])


def test_invented_ids_are_dropped_with_warnings():
    data = output()
    data["new_issues"][0]["segment_ids"] = [2, 99]
    data["new_issues"][0]["marker_ids"] = [7]
    result = build(data)
    issue = result["new_issues"][0]
    assert [e["segment_id"] for e in issue["excerpts"]] == [2]
    assert issue["screenshots"] == [] and issue["marker_ids"] == []
    assert any("[99]" in warning for warning in result["warnings"])
    assert any("[7]" in warning for warning in result["warnings"])


def test_output_schema_rejects_unknown_type():
    data = output()
    data["new_issues"][0]["type"] = "rant"
    with pytest.raises(jsonschema.ValidationError):
        schemas.validate(data, OUTPUT_SCHEMA)


def test_output_schema_fits_structured_output_rules():
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for forbidden in ("minimum", "maximum", "minLength", "maxLength"):
                assert forbidden not in node
            for value in node.values():
                check(value)

    check(OUTPUT_SCHEMA)


def test_near_markers_window():
    assert near_markers(30.0, MARKERS) == [1]  # 25s before
    assert near_markers(65.0, MARKERS) == [1]  # 10s after
    assert near_markers(10.0, MARKERS) == []
    assert near_markers(80.0, MARKERS) == []


def test_render_transcript_numbers_and_tags_lines():
    lines = render_transcript(SEGMENTS, MARKERS).splitlines()
    assert lines[0] == "#1 [00:00:01] wow nice"
    assert lines[1].startswith("#2 [00:00:50] (near marker 1)")


def make_session(tmp_path, segments):
    session = create_session(ProjectConfig(name="x"), tmp_path)
    transcript = {"session": session.id, "model": "m", "audio_seconds": 1, "segments": segments}
    (session.path / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")
    return session, transcript


def marker(marker_id, screenshot=None, warning=None, gamestate=None):
    return {
        "id": marker_id,
        "t": 1.0,
        "wall_time": "now",
        "screenshot": screenshot,
        "screenshot_warning": warning,
        "gamestate": gamestate,
    }


def test_content_sends_good_screenshots_only_and_resizes(tmp_path):
    session, transcript = make_session(tmp_path, SEGMENTS)
    Image.new("RGB", (3840, 2160), (40, 80, 20)).save(session.screenshots_dir / "a.jpg")
    (session.gamestate_dir / "a.json").write_text('{"level": 3}', encoding="utf-8")
    markers = [marker(1, "screenshots/a.jpg", gamestate="gamestate/a.json"), marker(2, "screenshots/b.jpg", "black frame")]

    content = build_content(session, ProjectConfig(name="x", description="A hockey game", vocabulary=["puck"]), transcript, markers)

    assert sum(block["type"] == "image" for block in content) == 1
    assert '{"level": 3}' in content[0]["text"]
    assert content[-1]["type"] == "text"
    assert "A hockey game" in content[-1]["text"] and "puck" in content[-1]["text"]


def test_process_without_transcript_explains_what_to_do(tmp_path):
    session = create_session(ProjectConfig(name="x"), tmp_path)
    with pytest.raises(SynthesisError, match="grumble transcribe"):
        process_session(session, ProjectConfig(name="x"))


def test_process_empty_transcript_makes_no_api_call(tmp_path, monkeypatch):
    session, _ = make_session(tmp_path, [])
    monkeypatch.setattr("grumble.synthesize.call_model", lambda *_: pytest.fail("called the API"))
    result = process_session(session, ProjectConfig(name="x"))
    assert result["new_issues"] == [] and result["discarded"]["count"] == 0


def test_rerun_keeps_the_previous_proposals(tmp_path, monkeypatch):
    session, _ = make_session(tmp_path, SEGMENTS)
    monkeypatch.setattr("grumble.synthesize.call_model", lambda *_: (output(new_issues=[], uncertain=[], discarded_segment_ids=[1, 2, 3, 4]), {}))
    process_session(session, ProjectConfig(name="x"))
    (session.path / "proposals.json").write_text('{"first": "run"}', encoding="utf-8")
    process_session(session, ProjectConfig(name="x"))

    backups = list(session.path.glob("proposals.*.json"))
    assert len(backups) == 1 and json.loads(backups[0].read_text(encoding="utf-8")) == {"first": "run"}
    schemas.validate(json.loads((session.path / "proposals.json").read_text(encoding="utf-8")), schemas.PROPOSALS)


def test_model_name_priority(monkeypatch):
    monkeypatch.delenv("GRUMBLE_MODEL", raising=False)
    assert model_name() == "claude-opus-5-5"
    monkeypatch.setenv("GRUMBLE_MODEL", "from-env")
    assert model_name() == "from-env"
    assert model_name("from-flag") == "from-flag"
