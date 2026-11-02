import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from grumble import config, recorder
from grumble.app import RunApp
from grumble.capture import CaptureResult
from grumble.config import ProjectConfig, detect_github_repo, load_project, project_for_folder, update_project
from grumble.hotkey import is_safe_global_hotkey
from grumble.session import ActiveLock, create_session
from grumble.setup_ui import make_handler
from tests.helpers import request


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRUMBLE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GRUMBLE_PROJECTS", raising=False)
    monkeypatch.delenv("GRUMBLE_SESSIONS", raising=False)
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture
def game(tmp_path):
    folder = tmp_path / "My Cool Game"
    folder.mkdir()
    return folder


@pytest.fixture
def app(home):
    return RunApp(home / ".env", use_hotkey=False)


@pytest.fixture
def server(app):
    instance = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    yield instance
    instance.shutdown()
    instance.server_close()


def call(server, app, path, body=None):
    method = "GET" if body is None else "POST"
    status, data = request(server, app, method, path, body)
    return status, json.loads(data)


def wait_for(app, stage):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if app.stage == stage:
            return
        time.sleep(0.02)
    raise AssertionError(f"stuck at {app.stage}, wanted {stage}")


def test_folder_creates_project_named_after_it(home, game):
    project = project_for_folder(game)
    assert project.name == "my-cool-game"
    assert project.folder == str(game.resolve())
    assert (home / "projects" / "my-cool-game.yaml").exists()


def test_same_folder_gives_same_project(home, game):
    assert project_for_folder(game).name == project_for_folder(game).name
    assert len(list((home / "projects").glob("*.yaml"))) == 1


def test_same_named_folders_stay_separate(home, tmp_path):
    one, two = tmp_path / "a" / "game", tmp_path / "b" / "game"
    one.mkdir(parents=True)
    two.mkdir(parents=True)
    assert project_for_folder(one).name == "game"
    assert project_for_folder(two).name == "game-2"


@pytest.mark.parametrize(
    "url",
    ["git@github.com:me/game.git", "https://github.com/me/game.git", "https://github.com/me/game", "ssh://git@github.com/me/game.git"],
)
def test_github_remote_is_detected(game, url):
    (game / ".git").mkdir()
    (game / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n', encoding="utf-8")
    assert detect_github_repo(game) == "me/game"


def test_non_github_folder_has_no_repo(game):
    assert detect_github_repo(game) is None


def test_detected_repo_becomes_the_tracker(home, game):
    (game / ".git").mkdir()
    (game / ".git" / "config").write_text('[remote "origin"]\n\turl = git@github.com:me/game.git\n', encoding="utf-8")
    tracker = project_for_folder(game).tracker
    assert tracker.type == "github" and tracker.repo == "me/game"


def test_update_merges_sections_and_validates(home, game):
    name = project_for_folder(game).name
    update_project(name, {"audio": {"chunk_seconds": 10}})
    project = load_project(name)
    assert project.audio.chunk_seconds == 10 and project.folder
    with pytest.raises(config.ConfigError):
        update_project(name, {"hotkey": "hyper+f9"})
    assert load_project(name).hotkey == "f9"


@pytest.mark.parametrize(("key", "safe"), [("f8", True), ("ctrl+g", True), ("alt+1", True), ("g", False), ("1", False)])
def test_global_hotkey_safety(key, safe):
    assert is_safe_global_hotkey(key) is safe


def test_first_launch_goes_to_setup_then_app(server, app, home):
    status, _ = request(server, app, "GET", "/", token=None)
    assert status == 302
    assert request(server, app, "GET", "/setup", token=None)[0] == 200
    assert call(server, app, "/api/finish", {})[0] == 200
    status, page = request(server, app, "GET", "/", token=None)
    assert status == 200 and app.token.encode() in page
    assert not app.finished.is_set()


def test_add_and_configure_project_through_the_page(server, app, game):
    status, added = call(server, app, "/api/project/add", {"folder": str(game)})
    assert status == 200
    name = added["name"]
    status, _ = call(server, app, "/api/project/update", {"name": name, "description": "A game", "vocabulary": "Puck, Saucer Pass, ", "repo": "me/game"})
    assert status == 200
    project = load_project(name)
    assert project.description == "A game" and project.vocabulary == ["Puck", "Saucer Pass"]
    assert project.tracker.repo == "me/game"
    state = call(server, app, "/api/app")[1]
    assert state["selected"] == name and state["projects"][0]["folder"] == str(game.resolve())


@pytest.mark.parametrize("folder", ["", "relative/path", "C:/definitely/not/here"])
def test_bad_folder_is_refused(server, app, folder):
    assert call(server, app, "/api/project/add", {"folder": folder})[0] == 400


def test_start_without_project_explains(server, app):
    status, data = call(server, app, "/api/session/start", {})
    assert status == 400 and "folder" in data["error"]


def test_bad_project_details_are_refused(server, app, game):
    name = call(server, app, "/api/project/add", {"folder": str(game)})[1]["name"]
    assert call(server, app, "/api/project/update", {"name": name, "repo": "not a repo"})[0] == 400
    assert call(server, app, "/api/project/update", {"name": "../../evil", "description": "x"})[0] == 400
    assert call(server, app, "/api/project/update", {"name": name, "hotkey": "g"})[0] == 400


def test_marker_and_session_keys_cannot_match(server, app, game):
    name = call(server, app, "/api/project/add", {"folder": str(game)})[1]["name"]
    assert call(server, app, "/api/project/update", {"name": name, "hotkey": app.session_hotkey})[0] == 400
    assert call(server, app, "/api/hotkey", {"value": "f9"})[0] == 400


def test_session_hotkey_change_registers_and_saves(server, app, game, monkeypatch):
    registered = []
    monkeypatch.setattr(app, "_register_session_hotkey", lambda value: registered.append(value) or True)
    assert call(server, app, "/api/hotkey", {"value": "ctrl+shift+g"})[0] == 200
    assert registered == ["ctrl+shift+g"] and app.session_hotkey == "ctrl+shift+g"


def test_session_hotkey_failure_restores_the_old_key(server, app, monkeypatch):
    registered = []

    def register(value):
        registered.append(value)
        app.hotkey_error = "taken" if value == "f7" else None
        return value != "f7"

    monkeypatch.setattr(app, "_register_session_hotkey", register)
    status, data = call(server, app, "/api/hotkey", {"value": "f7"})
    assert status == 400 and data["error"] == "taken"
    assert registered == ["f7", "f8"] and app.session_hotkey == "f8"


@pytest.fixture
def fake_pipeline(monkeypatch):
    """Replace hardware and models, leaving the app's own sequencing real."""
    release = threading.Event()

    def capture(project, on_status=None, **_kwargs):
        session = create_session(project)
        on_status({"session": session.id, "elapsed": 1, "level": 0.5, "markers": 2, "mic": "Test mic"})
        release.wait(5)
        session.update_manifest(status="recorded", duration_seconds=60)
        return CaptureResult(session, 60.0, 2)

    def transcribe(session, _project):
        transcript = {"session": session.id, "model": "m", "audio_seconds": 1, "segments": []}
        (session.path / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")
        (session.path / "transcript.txt").write_text("[00:00:01] hello\n", encoding="utf-8")

    monkeypatch.setattr("grumble.capture.run_capture", capture)
    monkeypatch.setattr("grumble.transcribe.transcribe_session", transcribe)
    return release


def test_full_session_runs_from_button_to_results(server, app, game, fake_pipeline):
    call(server, app, "/api/project/add", {"folder": str(game)})
    assert call(server, app, "/api/session/start", {})[0] == 200
    wait_for(app, "recording")
    deadline = time.monotonic() + 5
    while "mic" not in call(server, app, "/api/app")[1]["state"] and time.monotonic() < deadline:
        time.sleep(0.02)
    state = call(server, app, "/api/app")[1]["state"]
    assert state["mic"] == "Test mic" and state["markers"] == 2
    assert call(server, app, "/api/session/start", {})[0] == 409
    assert call(server, app, "/api/quit", {})[0] == 409

    fake_pipeline.set()
    wait_for(app, "done")
    data = call(server, app, "/api/app")[1]
    session_id = data["state"]["session"]
    assert data["sessions"][0]["id"] == session_id and data["sessions"][0]["issues"] == 0
    details = call(server, app, f"/api/session/{session_id}")[1]
    assert details["proposals"]["new_issues"] == [] and "hello" in details["transcript"]


def test_hotkey_toggles_session(app, game, fake_pipeline, monkeypatch):
    project_for_folder(game)
    stops = []
    monkeypatch.setattr(ActiveLock, "request_stop", lambda self: stops.append(True) or fake_pipeline.set() or "s")
    assert app.toggle_session()[0] == 200
    wait_for(app, "recording")
    assert app.toggle_session()[0] == 200
    assert stops == [True]
    wait_for(app, "done")
    fake_pipeline.clear()


def test_missing_key_still_leaves_the_transcript(server, app, game, monkeypatch, fake_pipeline):
    fake_pipeline.set()
    call(server, app, "/api/project/add", {"folder": str(game)})

    from grumble.synthesize import SynthesisError

    def fail(*_args):
        raise SynthesisError("No Claude API key found.")

    monkeypatch.setattr("grumble.synthesize.process_session", fail)
    call(server, app, "/api/session/start", {})
    wait_for(app, "done")
    state = call(server, app, "/api/app")[1]["state"]
    assert "No Claude API key" in state["note"] and "transcript" in state["note"]


def test_capture_failure_is_shown(server, app, game, monkeypatch):
    call(server, app, "/api/project/add", {"folder": str(game)})

    def fail(*_args, **_kwargs):
        raise RuntimeError("Could not open mic")

    monkeypatch.setattr("grumble.capture.run_capture", fail)
    call(server, app, "/api/session/start", {})
    wait_for(app, "error")
    assert call(server, app, "/api/app")[1]["state"]["error"] == "Could not open mic"


def test_transcription_failure_can_be_rerun(server, app, game, monkeypatch, fake_pipeline):
    fake_pipeline.set()
    call(server, app, "/api/project/add", {"folder": str(game)})
    good = __import__("grumble.transcribe", fromlist=["x"]).transcribe_session

    def fail(*_args):
        raise RuntimeError("out of memory")

    monkeypatch.setattr("grumble.transcribe.transcribe_session", fail)
    call(server, app, "/api/session/start", {})
    wait_for(app, "error")
    assert "out of memory" in call(server, app, "/api/app")[1]["state"]["error"]
    session_id = call(server, app, "/api/app")[1]["state"]["session"]

    monkeypatch.setattr("grumble.transcribe.transcribe_session", good)
    assert call(server, app, "/api/session/rerun", {"id": session_id})[0] == 200
    wait_for(app, "done")


@pytest.mark.parametrize("session_id", ["../..", "..%2f", "C:\\Windows", "nope"])
def test_session_ids_cannot_escape(server, app, session_id):
    assert call(server, app, "/api/session/open", {"id": session_id, "what": "folder"})[0] == 404
    assert call(server, app, "/api/session/rerun", {"id": session_id})[0] == 404


def test_open_only_allows_known_targets(server, app, home, monkeypatch):
    session = create_session(ProjectConfig(name="x"))
    opened = []
    monkeypatch.setattr("grumble.app.os.startfile", lambda path: opened.append(Path(path)), raising=False)
    assert call(server, app, "/api/session/open", {"id": session.id, "what": "calc.exe"})[0] == 400
    assert call(server, app, "/api/session/open", {"id": session.id, "what": "folder"})[0] == 200
    assert opened == [session.path]


def test_mic_opens_from_a_background_thread():
    if not recorder.list_input_devices():
        pytest.skip("no mic on this machine")
    outcome = []

    def listen():
        try:
            outcome.append(recorder.measure_level(None, seconds=0.1))
        except RuntimeError as error:
            outcome.append(error)

    thread = threading.Thread(target=listen)
    thread.start()
    thread.join()
    assert isinstance(outcome[0], float), outcome[0]


def test_api_requires_the_page_token(server, app, game):
    status, _ = request(server, app, "POST", "/api/session/start", {}, token="wrong")
    assert status == 403
    status, _ = request(server, app, "POST", "/api/project/add", {"folder": str(game)}, token=None)
    assert status == 403
    status, _ = request(server, app, "POST", "/api/quit", {}, headers={"Origin": "https://evil.example"})
    assert status == 403
    assert not app.finished.is_set()
