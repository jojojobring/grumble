import pytest

from grumble.config import ConfigError, TranscribeConfig, create_project, home, load_project, parse_config
from grumble.hotkey import parse_hotkey
from grumble.transcribe import resolve_settings


def test_defaults_fill_missing_settings():
    config = parse_config({"name": "x"}, "test")
    assert config.hotkey == "f9"
    assert config.audio.chunk_seconds == 30
    assert config.gamestate.type == "none"


def test_init_creates_a_loadable_local_project(tmp_path):
    create_project("my-game", directory=tmp_path / "new")
    config = load_project("my-game", tmp_path / "new")
    assert config.name == "my-game" and config.tracker.type == "local"


def test_init_with_repo_uses_github(tmp_path):
    create_project("my-game", "me/game", tmp_path)
    tracker = load_project("my-game", tmp_path).tracker
    assert tracker.type == "github" and tracker.repo == "me/game"


def test_init_never_overwrites(tmp_path):
    create_project("my-game", directory=tmp_path)
    with pytest.raises(ConfigError, match="already exists"):
        create_project("my-game", directory=tmp_path)


@pytest.mark.parametrize(("name", "repo"), [("../evil", None), ("a b", None), ("ok", "not-a-repo"), ("ok", "a/b\nc: d")])
def test_init_rejects_bad_names(tmp_path, name, repo):
    with pytest.raises(ConfigError):
        create_project(name, repo, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_home_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("GRUMBLE_HOME", str(tmp_path))
    assert home() == tmp_path


def test_auto_settings_match_the_hardware():
    gpu = resolve_settings(TranscribeConfig(), True)
    assert (gpu.device, gpu.compute_type, gpu.model) == ("cuda", "float16", "large-v3-turbo")
    cpu = resolve_settings(TranscribeConfig(), False)
    assert (cpu.device, cpu.compute_type, cpu.model) == ("cpu", "int8", "small.en")
    assert resolve_settings(TranscribeConfig(language="de"), False).model == "small"


def test_explicit_settings_beat_auto():
    settings = resolve_settings(TranscribeConfig(model="tiny", device="cpu"), True)
    assert (settings.device, settings.compute_type, settings.model) == ("cpu", "int8", "tiny")


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"name": "x", "vocabluary": []},
        {"name": "x", "audio": {"devcie": "mic"}},
        {"name": "x", "hotkey": "hyper+f9"},
        {"name": "x", "gamestate": {"type": "http"}},
        {"name": "x", "gamestate": {"type": "carrier pigeon"}},
        {"name": "x", "tracker": {"type": "github"}},
        {"name": "x", "audio": {"chunk_seconds": 0}},
    ],
)
def test_bad_config_is_rejected(data):
    with pytest.raises(ConfigError):
        parse_config(data, "test")


def test_missing_project_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="No project config"):
        load_project("nope", tmp_path)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("f9", (0, 0x78)), ("F1", (0, 0x70)), ("ctrl+f9", (2, 0x78)), ("ctrl+shift+m", (6, ord("M")))],
)
def test_parse_hotkey(text, expected):
    assert parse_hotkey(text) == expected
