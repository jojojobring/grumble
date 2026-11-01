"""Per-project YAML config, validated into dataclasses."""

import dataclasses
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from grumble.hotkey import parse_hotkey

_TEMPLATE = Path(__file__).with_name("project_template.yaml")
_PROJECT_NAME = re.compile(r"[A-Za-z0-9_-]+")
_REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def home() -> Path:
    """Where a user's projects, sessions and .env live, separate from the installed code."""
    configured = os.environ.get("GRUMBLE_HOME")
    if configured:
        return Path(configured)
    return Path(os.environ.get("APPDATA", Path.home())) / "Grumble"


class ConfigError(Exception):
    pass


@dataclass
class TrackerConfig:
    type: str = "local"
    repo: str | None = None


@dataclass
class AudioConfig:
    device: str | None = None
    chunk_seconds: int = 30


@dataclass
class VadConfig:
    threshold: float = 0.5
    min_speech_ms: int = 250
    min_silence_ms: int = 800
    speech_pad_ms: int = 300
    max_speech_seconds: float = 30.0


@dataclass
class TranscribeConfig:
    model: str = "auto"
    device: str = "auto"
    compute_type: str = "auto"
    language: str = "en"
    beam_size: int = 5


@dataclass
class GameStateConfig:
    type: str = "none"
    url: str | None = None
    path: str | None = None
    timeout_seconds: float = 0.5


@dataclass
class ProjectConfig:
    name: str
    description: str = ""
    hotkey: str = "f9"
    vocabulary: list[str] = field(default_factory=list)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    gamestate: GameStateConfig = field(default_factory=GameStateConfig)
    marker_beep: bool = True


_SECTIONS = {
    "tracker": TrackerConfig,
    "audio": AudioConfig,
    "vad": VadConfig,
    "transcribe": TranscribeConfig,
    "gamestate": GameStateConfig,
}


def _check_keys(cls: type, data: dict[str, Any], where: str) -> None:
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {sorted(unknown)}")


def parse_config(data: Any, where: str) -> ProjectConfig:
    if not isinstance(data, dict) or not data.get("name"):
        raise ConfigError(f"{where}: must be a mapping with a 'name'")
    _check_keys(ProjectConfig, data, where)

    values = dict(data)
    for key, cls in _SECTIONS.items():
        section = values.get(key) or {}
        if not isinstance(section, dict):
            raise ConfigError(f"{where}: '{key}' must be a mapping")
        _check_keys(cls, section, f"{where}: {key}")
        values[key] = cls(**section)
    values["vocabulary"] = [str(word) for word in values.get("vocabulary") or []]
    config = ProjectConfig(**values)

    try:
        parse_hotkey(config.hotkey)
    except ValueError as error:
        raise ConfigError(f"{where}: {error}") from error
    if config.gamestate.type not in ("none", "http", "file"):
        raise ConfigError(f"{where}: gamestate.type must be none, http or file")
    if config.gamestate.type == "http" and not config.gamestate.url:
        raise ConfigError(f"{where}: gamestate.type http needs a url")
    if config.gamestate.type == "file" and not config.gamestate.path:
        raise ConfigError(f"{where}: gamestate.type file needs a path")
    if config.tracker.type not in ("local", "github"):
        raise ConfigError(f"{where}: tracker.type must be local or github")
    if config.tracker.type == "github" and not config.tracker.repo:
        raise ConfigError(f"{where}: tracker.type github needs a repo")
    if config.audio.chunk_seconds < 1:
        raise ConfigError(f"{where}: audio.chunk_seconds must be at least 1")
    return config


def projects_dir() -> Path:
    return Path(os.environ.get("GRUMBLE_PROJECTS", home() / "projects"))


def create_project(name: str, repo: str | None = None, directory: Path | None = None) -> Path:
    if not _PROJECT_NAME.fullmatch(name):
        raise ConfigError("Project names can only use letters, numbers, - and _.")
    if repo and not _REPO_NAME.fullmatch(repo):
        raise ConfigError("The repo must look like owner/name.")
    directory = directory or projects_dir()
    path = directory / f"{name}.yaml"
    if path.exists():
        raise ConfigError(f"Project '{name}' already exists at {path}")

    tracker = f"type: github\n  repo: {repo}" if repo else "type: local # switch to github and add repo: owner/name"
    text = _TEMPLATE.read_text(encoding="utf-8").replace("__NAME__", name).replace("__TRACKER__", tracker)
    parse_config(yaml.safe_load(text), str(path))
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def load_project(name: str, directory: Path | None = None) -> ProjectConfig:
    path = (directory or projects_dir()) / f"{name}.yaml"
    if not path.exists():
        raise ConfigError(f"No project config at {path}. Create one with `grumble init {name}`.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ConfigError(f"{path}: {error}") from error
    config = parse_config(data, str(path))
    if config.audio.device is None:
        config.audio.device = default_mic()
    return config


def _read_settings() -> dict[str, Any]:
    try:
        data = yaml.safe_load((home() / "settings.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def default_mic() -> str | None:
    """The mic for projects that don't name their own. None means the Windows default."""
    return _read_settings().get("mic") or None


def save_default_mic(name: str | None) -> None:
    home().mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump({**_read_settings(), "mic": name})
    (home() / "settings.yaml").write_text(text, encoding="utf-8")


def projects_with_own_mic(directory: Path | None = None) -> list[dict[str, str]]:
    directory = directory or projects_dir()
    result = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            device = (yaml.safe_load(path.read_text(encoding="utf-8")).get("audio") or {}).get("device")
        except (OSError, yaml.YAMLError, AttributeError):
            continue
        if device:
            result.append({"project": path.stem, "device": str(device)})
    return result
