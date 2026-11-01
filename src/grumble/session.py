"""Session folders on disk, and the lock that lets `grumble stop` find a capture."""

import ctypes
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from grumble import schemas
from grumble.config import ProjectConfig, home

SAMPLE_RATE = 16000


class SessionError(Exception):
    pass


def sessions_root() -> Path:
    return Path(os.environ.get("GRUMBLE_SESSIONS", home() / "sessions"))


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def write_json(path: Path, data: Any) -> None:
    """Write through a temp file so a crash cannot leave half a JSON file."""
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


@dataclass
class Session:
    path: Path

    @property
    def id(self) -> str:
        return self.path.name

    @property
    def audio_dir(self) -> Path:
        return self.path / "audio"

    @property
    def screenshots_dir(self) -> Path:
        return self.path / "screenshots"

    @property
    def gamestate_dir(self) -> Path:
        return self.path / "gamestate"

    @property
    def markers_path(self) -> Path:
        return self.path / "markers.jsonl"

    def read_manifest(self) -> dict[str, Any]:
        data = json.loads((self.path / "session.json").read_text(encoding="utf-8"))
        schemas.validate(data, schemas.SESSION)
        return data

    def update_manifest(self, **changes: Any) -> None:
        data = {**self.read_manifest(), **changes}
        schemas.validate(data, schemas.SESSION)
        write_json(self.path / "session.json", data)

    def append_marker(self, marker: dict[str, Any]) -> None:
        schemas.validate(marker, schemas.MARKER)
        with self.markers_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(marker, ensure_ascii=False) + "\n")

    def read_markers(self) -> list[dict[str, Any]]:
        if not self.markers_path.exists():
            return []
        markers = []
        for line in self.markers_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                marker = json.loads(line)
                schemas.validate(marker, schemas.MARKER)
                markers.append(marker)
        return markers


def create_session(config: ProjectConfig, root: Path | None = None, now: datetime | None = None) -> Session:
    root = root or sessions_root()
    root.mkdir(parents=True, exist_ok=True)
    moment = now or datetime.now()
    path = root / moment.strftime("%Y-%m-%d_%H-%M-%S")
    suffix = 1
    while path.exists():
        suffix += 1
        path = root / f"{moment.strftime('%Y-%m-%d_%H-%M-%S')}_{suffix}"

    session = Session(path)
    for directory in (path, session.audio_dir, session.screenshots_dir, session.gamestate_dir):
        directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": session.id,
        "project": config.name,
        "status": "recording",
        "started_at": now_iso(),
        "ended_at": None,
        "sample_rate": SAMPLE_RATE,
        "chunk_seconds": config.audio.chunk_seconds,
        "duration_seconds": None,
        "overflow_count": 0,
        "device": None,
        "config": asdict(config),
    }
    schemas.validate(manifest, schemas.SESSION)
    write_json(path / "session.json", manifest)
    return session


def resolve_session(name: str, root: Path | None = None) -> Session:
    root = root or sessions_root()
    if name == "latest":
        candidates = sorted(p for p in root.glob("*") if (p / "session.json").exists()) if root.exists() else []
        if not candidates:
            raise SessionError(f"No sessions in {root}")
        return Session(candidates[-1])
    path = Path(name) if Path(name).is_absolute() else root / name
    if not (path / "session.json").exists():
        raise SessionError(f"No session at {path}")
    return Session(path)


def _pid_alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    query_limited_information = 0x1000
    still_active = 259

    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    try:
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


class ActiveLock:
    """Marks the one running capture. A lock left by a dead process is ignored."""

    def __init__(self, root: Path | None = None):
        self.root = root or sessions_root()
        self.lock_path = self.root / ".active.json"
        self.stop_path = self.root / ".stop"

    def read(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return data if _pid_alive(data["pid"]) else None

    def acquire(self, session_id: str) -> None:
        active = self.read()
        if active:
            raise SessionError(f"Session {active['session']} is already recording (pid {active['pid']})")
        self.root.mkdir(parents=True, exist_ok=True)
        self.stop_path.unlink(missing_ok=True)
        write_json(self.lock_path, {"pid": os.getpid(), "session": session_id})

    def release(self) -> None:
        self.lock_path.unlink(missing_ok=True)
        self.stop_path.unlink(missing_ok=True)

    def request_stop(self) -> str:
        active = self.read()
        if not active:
            raise SessionError("No session is recording")
        self.stop_path.touch()
        return active["session"]

    def stop_requested(self) -> bool:
        return self.stop_path.exists()
