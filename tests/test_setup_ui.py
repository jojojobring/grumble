import http.client
import json
import os
import threading
from http.server import ThreadingHTTPServer

import pytest
from dotenv import dotenv_values

from grumble import credentials, recorder
from grumble.config import create_project, load_project
from grumble.credentials import GitHubCheck
from grumble.setup_ui import SetupApp, make_handler

GOOD = "sk-ant-good-key-123456"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv(credentials.ANTHROPIC_VAR, raising=False)
    monkeypatch.delenv(credentials.GITHUB_VAR, raising=False)
    # save_secret writes to os.environ; monkeypatch restores it afterwards.
    monkeypatch.setenv(credentials.ANTHROPIC_VAR, "")
    monkeypatch.setenv(credentials.GITHUB_VAR, "")
    monkeypatch.setattr(credentials, "check_anthropic", lambda key: (key == GOOD, "Key works." if key == GOOD else "Anthropic rejected this key."))
    monkeypatch.setattr(credentials, "check_github", lambda token, repos: GitHubCheck(token == GOOD, "Connected as me." if token == GOOD else "GitHub rejected this token.", "me"))
    monkeypatch.setattr(credentials, "gh_installed", lambda: False)
    monkeypatch.setattr(credentials, "gh_token", lambda: None)
    return SetupApp(tmp_path / ".env")


@pytest.fixture
def server(app):
    instance = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    yield instance
    instance.shutdown()
    instance.server_close()


def request(server, app, method, path, value=None, token="auto", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    all_headers = dict(headers or {})
    if token == "auto":
        all_headers["X-Grumble-Token"] = app.token
    elif token:
        all_headers["X-Grumble-Token"] = token
    body = json.dumps({"value": value}) if value is not None else None
    connection.request(method, path, body=body, headers=all_headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


def test_page_has_token_and_no_secrets(server, app):
    os.environ[credentials.ANTHROPIC_VAR] = GOOD
    status, page = request(server, app, "GET", "/", token=None)
    assert status == 200
    assert app.token.encode() in page and b"__TOKEN__" not in page
    assert GOOD.encode() not in page


def test_status_never_returns_the_key(server, app):
    os.environ[credentials.ANTHROPIC_VAR] = GOOD
    status, data = request(server, app, "GET", "/api/status/anthropic")
    assert status == 200 and json.loads(data)["connected"] is True
    assert GOOD.encode() not in data


def test_good_key_is_saved_and_used(server, app):
    status, _ = request(server, app, "POST", "/api/anthropic", GOOD)
    assert status == 200
    assert dotenv_values(app.env_path)[credentials.ANTHROPIC_VAR] == GOOD
    assert os.environ[credentials.ANTHROPIC_VAR] == GOOD
    assert json.loads(request(server, app, "GET", "/api/status/anthropic")[1])["source"] == ".env file"


@pytest.mark.parametrize("key", ["sk-ant-wrong-key-1234", "short", "has spaces in the key", "sk-ant-good\nEVIL=1", ""])
def test_bad_key_is_not_saved(server, app, key):
    status, _ = request(server, app, "POST", "/api/anthropic", key)
    assert status == 400
    assert not app.env_path.exists()


def test_github_token_is_saved_and_found(server, app):
    assert request(server, app, "POST", "/api/github/token", GOOD)[0] == 200
    assert dotenv_values(app.env_path)[credentials.GITHUB_VAR] == GOOD
    status = json.loads(request(server, app, "GET", "/api/status/github")[1])
    assert status["connected"] and status["source"] == ".env file"


def test_saving_warns_when_system_variable_wins(server, app):
    os.environ[credentials.ANTHROPIC_VAR] = "system-value-1234567"
    status, data = request(server, app, "POST", "/api/anthropic", GOOD)
    assert status == 200 and json.loads(data)["overridden"] is True
    assert os.environ[credentials.ANTHROPIC_VAR] == "system-value-1234567"
    assert dotenv_values(app.env_path)[credentials.ANTHROPIC_VAR] == GOOD


def test_saving_keeps_other_settings(server, app):
    app.env_path.write_text("GRUMBLE_MODEL=claude-sonnet-5-5\n", encoding="utf-8")
    request(server, app, "POST", "/api/anthropic", GOOD)
    assert dotenv_values(app.env_path)["GRUMBLE_MODEL"] == "claude-sonnet-5-5"


@pytest.mark.parametrize("token", [None, "wrong"])
@pytest.mark.parametrize(("method", "path"), [("GET", "/api/status/anthropic"), ("POST", "/api/anthropic"), ("POST", "/api/finish")])
def test_api_requires_the_page_token(server, app, method, path, token):
    value = GOOD if method == "POST" else None
    assert request(server, app, method, path, value, token=token)[0] == 403
    assert not app.env_path.exists() and not app.finished.is_set()


def test_other_websites_are_refused(server, app):
    assert request(server, app, "POST", "/api/anthropic", GOOD, headers={"Origin": "https://evil.example"})[0] == 403
    assert not app.env_path.exists()


def test_rebound_hostname_is_refused(server, app):
    assert request(server, app, "GET", "/", token=None, headers={"Host": "evil.example"})[0] == 403


def test_oversized_body_is_refused(server, app):
    assert request(server, app, "POST", "/api/anthropic", "x" * 10000)[0] == 413


def test_finish_stops_setup(server, app):
    assert request(server, app, "POST", "/api/finish", "")[0] == 200
    assert app.finished.is_set()


def test_login_without_gh_explains(server, app):
    status, data = request(server, app, "POST", "/api/github/login", "")
    assert status == 400 and b"token" in data


def test_gh_login_token_is_used_when_no_variable(app, monkeypatch):
    monkeypatch.setattr(credentials, "gh_token", lambda: "gho_token")
    assert credentials.find_github_token(app.env_path) == ("gho_token", "GitHub CLI")


@pytest.fixture
def mics(tmp_path, monkeypatch):
    monkeypatch.setenv("GRUMBLE_HOME", str(tmp_path))
    monkeypatch.delenv("GRUMBLE_PROJECTS", raising=False)
    monkeypatch.setattr(recorder, "list_input_devices", lambda: [("Webcam", True), ("Headset", False)])
    return tmp_path


def test_mic_choice_becomes_the_default_for_projects(server, app, mics):
    assert request(server, app, "POST", "/api/mic", "Headset")[0] == 200
    assert json.loads(request(server, app, "GET", "/api/status/mic")[1])["selected"] == "Headset"
    create_project("game")
    assert load_project("game").audio.device == "Headset"


def test_project_mic_beats_the_default_and_is_reported(server, app, mics):
    request(server, app, "POST", "/api/mic", "Headset")
    (mics / "projects").mkdir()
    (mics / "projects" / "game.yaml").write_text("name: game\naudio: {device: Webcam}\n", encoding="utf-8")
    assert load_project("game").audio.device == "Webcam"
    status = json.loads(request(server, app, "GET", "/api/status/mic")[1])
    assert status["overrides"] == [{"project": "game", "device": "Webcam"}]


def test_mic_can_go_back_to_windows_default(server, app, mics):
    request(server, app, "POST", "/api/mic", "Headset")
    assert request(server, app, "POST", "/api/mic", "")[0] == 200
    assert json.loads(request(server, app, "GET", "/api/status/mic")[1])["selected"] is None


def test_unknown_mic_is_refused(server, app, mics):
    assert request(server, app, "POST", "/api/mic", "Nope")[0] == 400
    assert not (mics / "settings.yaml").exists()


def test_mic_test_reports_level_and_errors(server, app, mics, monkeypatch):
    monkeypatch.setattr(recorder, "measure_level", lambda name: 0.5)
    status, data = request(server, app, "POST", "/api/mic/test", "Headset")
    assert status == 200 and json.loads(data)["peak"] == 0.5

    def fail(_name):
        raise RuntimeError("Could not open mic")

    monkeypatch.setattr(recorder, "measure_level", fail)
    assert request(server, app, "POST", "/api/mic/test", "Headset")[0] == 400


def test_tracked_repos_reads_github_projects(tmp_path):
    (tmp_path / "a.yaml").write_text("name: a\ntracker: {type: github, repo: me/a}\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("name: b\ntracker: {type: local}\n", encoding="utf-8")
    (tmp_path / "broken.yaml").write_text("nope: [", encoding="utf-8")
    assert credentials.tracked_repos(tmp_path) == ["me/a"]
