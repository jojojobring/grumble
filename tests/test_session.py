import json
import os
import subprocess
import sys
from datetime import datetime

import jsonschema
import pytest

from grumble.config import ProjectConfig
from grumble.session import ActiveLock, SessionError, create_session, resolve_session

NOW = datetime(2026, 11, 1, 12, 0, 0)


def marker(marker_id=1):
    return {"id": marker_id, "t": 1.5, "wall_time": "now", "screenshot": None, "gamestate": None}


def test_create_session_builds_layout(tmp_path):
    session = create_session(ProjectConfig(name="x"), tmp_path, NOW)
    assert session.id == "2026-11-01_12-00-00"
    assert session.audio_dir.is_dir() and session.screenshots_dir.is_dir() and session.gamestate_dir.is_dir()
    assert session.read_manifest()["status"] == "recording"


def test_same_second_sessions_do_not_collide(tmp_path):
    first = create_session(ProjectConfig(name="x"), tmp_path, NOW)
    second = create_session(ProjectConfig(name="x"), tmp_path, NOW)
    assert first.path != second.path


def test_manifest_update_persists_and_validates(tmp_path):
    session = create_session(ProjectConfig(name="x"), tmp_path, NOW)
    session.update_manifest(status="recorded", duration_seconds=12.5)
    assert session.read_manifest()["duration_seconds"] == 12.5
    with pytest.raises(jsonschema.ValidationError):
        session.update_manifest(status="exploded")
    assert session.read_manifest()["status"] == "recorded"


def test_markers_round_trip_in_order(tmp_path):
    session = create_session(ProjectConfig(name="x"), tmp_path, NOW)
    assert session.read_markers() == []
    session.append_marker(marker(1))
    session.append_marker(marker(2))
    assert [item["id"] for item in session.read_markers()] == [1, 2]


def test_invalid_marker_is_rejected(tmp_path):
    session = create_session(ProjectConfig(name="x"), tmp_path, NOW)
    with pytest.raises(jsonschema.ValidationError):
        session.append_marker({**marker(), "t": -1})
    with pytest.raises(jsonschema.ValidationError):
        session.append_marker({**marker(), "typo": True})


def test_resolve_latest_and_by_name(tmp_path):
    create_session(ProjectConfig(name="x"), tmp_path, NOW)
    newer = create_session(ProjectConfig(name="x"), tmp_path, datetime(2026, 11, 2))
    assert resolve_session("latest", tmp_path).path == newer.path
    assert resolve_session(newer.id, tmp_path).path == newer.path
    with pytest.raises(SessionError):
        resolve_session("missing", tmp_path)


def test_resolve_latest_with_no_sessions(tmp_path):
    with pytest.raises(SessionError):
        resolve_session("latest", tmp_path / "none")


def test_lock_blocks_second_capture_and_carries_stop(tmp_path):
    lock = ActiveLock(tmp_path)
    with pytest.raises(SessionError):
        lock.request_stop()

    lock.acquire("one")
    with pytest.raises(SessionError, match="already recording"):
        lock.acquire("two")
    assert not lock.stop_requested()
    assert lock.request_stop() == "one"
    assert lock.stop_requested()

    lock.release()
    assert lock.read() is None and not lock.stop_requested()


def test_lock_from_dead_process_is_ignored(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    lock = ActiveLock(tmp_path)
    lock.lock_path.write_text(json.dumps({"pid": process.pid, "session": "old"}), encoding="utf-8")
    assert lock.read() is None
    lock.acquire("new")
    assert lock.read() == {"pid": os.getpid(), "session": "new"}
