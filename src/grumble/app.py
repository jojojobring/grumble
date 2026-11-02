"""The windowed app: choose a game folder, run a session, and read what came out of it."""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import winsound
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from grumble import config, hotkey
from grumble.config import ConfigError, ProjectConfig
from grumble.review_ui import ReviewRoutes
from grumble.session import ActiveLock, Session, SessionError, _pid_alive, resolve_session, sessions_root, write_json
from grumble.setup_ui import SETUP_PAGE, Response, SetupApp, make_handler

APP_PAGE = Path(__file__).with_name("app_page.html")
DEFAULT_SESSION_HOTKEY = "f8"
_BUSY = ("recording", "transcribing", "proposing")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]+")
# Browsers slow timers in hidden windows to about once a minute, so allow for several misses.
_NO_WINDOW_EXIT_SECONDS = 180
_PICK_FOLDER_SCRIPT = """
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$owner = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true; ShowInTaskbar = $false; WindowState = 'Minimized' }
$owner.Show(); $owner.Activate()
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Choose your game''s project folder'
if ($dialog.ShowDialog($owner) -eq 'OK') { $dialog.SelectedPath }
$owner.Close()
"""


def project_summary(project: ProjectConfig) -> dict[str, Any]:
    return {
        "name": project.name,
        "folder": project.folder,
        "description": project.description,
        "vocabulary": project.vocabulary,
        "hotkey": project.hotkey,
        "repo": project.tracker.repo if project.tracker.type == "github" else None,
    }


def session_summary(session: Session) -> dict[str, Any] | None:
    try:
        manifest = session.read_manifest()
    except Exception:  # a damaged folder shouldn't hide the others
        return None
    if manifest.get("status") == "failed" and manifest.get("duration_seconds") is None:
        return None  # it never recorded anything
    summary = {
        "id": session.id,
        "project": manifest["project"],
        "started_at": manifest["started_at"],
        "duration": manifest.get("duration_seconds"),
        "has_transcript": (session.path / "transcript.txt").exists(),
        "issues": None,
    }
    try:
        proposals = json.loads((session.path / "proposals.json").read_text(encoding="utf-8"))
        summary["issues"] = len(proposals["new_issues"])
    except (OSError, ValueError, KeyError):
        pass
    return summary


class RunApp(ReviewRoutes, SetupApp):
    in_app = True

    def __init__(self, env_path: Path | None = None, use_hotkey: bool = True):
        from grumble import credentials

        super().__init__(env_path or credentials.ENV_PATH)
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"stage": "idle"}
        self._session_hotkey: hotkey.Hotkey | None = None
        self.hotkey_error: str | None = None
        self.last_ping = time.monotonic()
        if use_hotkey:
            self._register_session_hotkey(self.session_hotkey)

    @property
    def session_hotkey(self) -> str:
        return config.get_setting("session_hotkey", DEFAULT_SESSION_HOTKEY)

    def page(self, path: str) -> Path | str | None:
        if path == "/":
            return APP_PAGE if config.get_setting("setup_done") else "/setup"
        return SETUP_PAGE if path == "/setup" else super().page(path)

    def get_routes(self):
        return {**super().get_routes(), "/api/app": lambda: (200, self.app_state())}

    def post_routes(self):
        return {
            **super().post_routes(),
            "/api/ping": lambda _body: self.ping(),
            "/api/quit": lambda _body: self.quit(),
            "/api/folder/pick": lambda _body: self.pick_folder(),
            "/api/project/add": lambda body: self.add_project(str(body.get("folder", ""))),
            "/api/project/select": lambda body: self.select_project(str(body.get("name", ""))),
            "/api/project/update": self.update_project,
            "/api/hotkey": lambda body: self.set_session_hotkey(str(body.get("value", ""))),
            "/api/session/start": lambda _body: self.toggle_session(start_only=True),
            "/api/session/stop": lambda _body: self.stop_session(),
            "/api/session/rerun": lambda body: self.rerun(str(body.get("id", ""))),
            "/api/session/open": lambda body: self.open_session_file(str(body.get("id", "")), str(body.get("what", ""))),
        }

    def get_dynamic(self, path: str) -> Response | None:
        match = re.fullmatch(r"/api/session/([A-Za-z0-9_-]+)", path)
        return self.session_details(match.group(1)) if match else super().get_dynamic(path)

    # In the app, setup's "Continue" moves on to the main page instead of exiting.
    def finish(self) -> Response:
        config.save_setting("setup_done", True)
        return 200, {}

    def ping(self) -> Response:
        self.last_ping = time.monotonic()
        return 200, {}

    def quit(self) -> Response:
        if self.stage in _BUSY:
            return 409, {"error": "Finish the current session first."}
        self.finished.set()
        return 200, {}

    @property
    def stage(self) -> str:
        with self._lock:
            return self._state["stage"]

    def _set_state(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)

    def app_state(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        projects = config.list_projects()
        selected = config.get_setting("last_project")
        if selected not in [project.name for project in projects]:
            selected = projects[0].name if projects else None
        return {
            "state": state,
            "projects": [project_summary(project) for project in projects],
            "selected": selected,
            "session_hotkey": self.session_hotkey,
            "hotkey_error": self.hotkey_error,
            "has_claude_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "sessions": self._sessions(selected),
        }

    def _sessions(self, project: str | None) -> list[dict[str, Any]]:
        root = sessions_root()
        if not project or not root.exists():
            return []
        paths = sorted((p for p in root.iterdir() if (p / "session.json").exists()), reverse=True)
        summaries = (session_summary(Session(path)) for path in paths)
        return [item for item in summaries if item and item["project"] == project][:20]

    def pick_folder(self) -> Response:
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", _PICK_FOLDER_SCRIPT],
                capture_output=True,
                encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as error:
            return 500, {"error": f"Couldn't open the folder picker: {error}. Type the folder's path instead."}
        return 200, {"folder": result.stdout.strip().splitlines()[-1] if result.stdout.strip() else None}

    def add_project(self, folder: str) -> Response:
        path = Path(folder.strip().strip('"'))
        if not folder.strip() or not path.is_absolute() or not path.is_dir():
            return 400, {"error": "That folder doesn't exist. Check the path and try again."}
        try:
            project = config.project_for_folder(path)
        except ConfigError as error:
            return 400, {"error": str(error)}
        config.save_setting("last_project", project.name)
        return 200, {"name": project.name}

    def select_project(self, name: str) -> Response:
        if name not in [project.name for project in config.list_projects()]:
            return 400, {"error": "That project no longer exists."}
        config.save_setting("last_project", name)
        return 200, {}

    def update_project(self, body: dict[str, Any]) -> Response:
        name = str(body.get("name", ""))
        changes: dict[str, Any] = {}
        if "description" in body:
            changes["description"] = str(body["description"]).strip()
        if "vocabulary" in body:
            changes["vocabulary"] = [word.strip() for word in str(body["vocabulary"]).split(",") if word.strip()]
        if "hotkey" in body:
            value = str(body["hotkey"]).strip().lower()
            error = self._hotkey_problem(value)
            if error:
                return 400, {"error": error}
            if value == self.session_hotkey:
                return 400, {"error": "That key already starts and stops sessions. Choose a different one."}
            changes["hotkey"] = value
        if "repo" in body:
            repo = str(body["repo"]).strip()
            if repo and not config.REPO_NAME.fullmatch(repo):
                return 400, {"error": "The GitHub repo must look like owner/name."}
            changes["tracker"] = {"type": "github", "repo": repo} if repo else {"type": "local", "repo": None}
        try:
            config.update_project(name, changes)
        except ConfigError as error:
            return 400, {"error": str(error)}
        return 200, {}

    @staticmethod
    def _hotkey_problem(value: str) -> str | None:
        try:
            safe = hotkey.is_safe_global_hotkey(value)
        except ValueError:
            return "That key can't be used. Try a function key, or a combination such as Ctrl+Shift+G."
        return None if safe else "A single letter or number would stop working everywhere else. Add Ctrl, Alt or Shift."

    def set_session_hotkey(self, value: str) -> Response:
        value = value.strip().lower()
        error = self._hotkey_problem(value)
        if error:
            return 400, {"error": error}
        if self.stage in _BUSY:
            return 409, {"error": "Finish the current session first."}
        if value in [project.hotkey for project in config.list_projects()]:
            return 400, {"error": "A project uses that key for markers. Choose a different one."}
        previous = self.session_hotkey
        if value != previous:
            if not self._register_session_hotkey(value):
                problem = self.hotkey_error
                self._register_session_hotkey(previous)
                return 400, {"error": problem}
            config.save_setting("session_hotkey", value)
        return 200, {}

    def _register_session_hotkey(self, value: str) -> bool:
        if self._session_hotkey:
            self._session_hotkey.stop()
        candidate = hotkey.Hotkey(value, self._on_session_hotkey)
        try:
            candidate.start()
        except RuntimeError as error:
            self._session_hotkey = None
            self.hotkey_error = str(error)
            return False
        self._session_hotkey = candidate
        self.hotkey_error = None
        return True

    def _on_session_hotkey(self) -> None:
        status, _ = self.toggle_session()
        winsound.MessageBeep(winsound.MB_OK if status == 200 else winsound.MB_ICONHAND)

    def toggle_session(self, start_only: bool = False) -> Response:
        if self.stage == "recording":
            return (409, {"error": "A session is already recording."}) if start_only else self.stop_session()
        return self.start_session()

    def start_session(self) -> Response:
        name = self.app_state()["selected"]
        if not name:
            self._set_state(stage="error", error="Choose your game folder first.")
            return 400, {"error": "Choose your game folder first."}
        try:
            project = config.load_project(name)
        except ConfigError as error:
            self._set_state(stage="error", error=str(error))  # the hotkey has only this to show it
            return 400, {"error": str(error)}
        with self._lock:
            if self._state["stage"] in _BUSY:
                return 409, {"error": "Grumble is still working on the last session."}
            self._state = {"stage": "recording", "project": name, "elapsed": 0, "level": 0.0, "markers": 0}
        threading.Thread(target=self._run_session, args=(project,), name="session", daemon=True).start()
        return 200, {}

    def stop_session(self) -> Response:
        if self.stage != "recording":
            return 409, {"error": "Nothing is recording."}
        try:
            ActiveLock().request_stop()
        except SessionError as error:
            return 409, {"error": str(error)}
        return 200, {}

    def _run_session(self, project: ProjectConfig) -> None:
        from grumble.capture import run_capture

        try:
            result = run_capture(project, on_status=lambda status: self._set_state(**status))
        except Exception as error:
            traceback.print_exc()
            self._set_state(stage="error", error=str(error))
            return
        self._process(result.session, project)

    def rerun(self, session_id: str) -> Response:
        session = self._find_session(session_id)
        if session is None:
            return 404, {"error": "That session no longer exists."}
        try:
            project = config.load_project(session.read_manifest()["project"])
        except ConfigError as error:
            return 400, {"error": str(error)}
        with self._lock:
            if self._state["stage"] in _BUSY:
                return 409, {"error": "Grumble is still working on the last session."}
            self._state = {"stage": "transcribing", "project": project.name, "session": session_id}
        threading.Thread(target=self._process, args=(session, project), name="session", daemon=True).start()
        return 200, {}

    def _process(self, session: Session, project: ProjectConfig) -> None:
        from grumble.export import export_csv
        from grumble.synthesize import SynthesisError, process_session
        from grumble.transcribe import transcribe_session

        self._set_state(stage="transcribing", session=session.id)
        try:
            transcribe_session(session, project)
        except Exception as error:
            traceback.print_exc()
            self._set_state(stage="error", error=f"Transcription failed: {error}. Your recording is saved, so you can try again.")
            return

        self._set_state(stage="proposing")
        try:
            process_session(session, project)
            export_csv(session)
        except SynthesisError as error:
            self._set_state(stage="done", note=f"Your transcript is ready, but issues couldn't be proposed. {error}")
            return
        except Exception as error:
            traceback.print_exc()
            self._set_state(stage="done", note=f"Your transcript is ready, but issues couldn't be proposed: {error}")
            return
        self._set_state(stage="done", note=None)

    @staticmethod
    def _find_session(session_id: str) -> Session | None:
        if not _SESSION_ID.fullmatch(session_id):
            return None
        try:
            return resolve_session(session_id)
        except SessionError:
            return None

    def session_details(self, session_id: str) -> Response:
        session = self._find_session(session_id)
        if session is None:
            return 404, {"error": "That session no longer exists."}
        details: dict[str, Any] = {"id": session.id, "proposals": None, "transcript": None}
        try:
            details["proposals"] = json.loads((session.path / "proposals.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:
            details["transcript"] = (session.path / "transcript.txt").read_text(encoding="utf-8")
        except OSError:
            pass
        return 200, details

    def open_session_file(self, session_id: str, what: str) -> Response:
        session = self._find_session(session_id)
        if session is None:
            return 404, {"error": "That session no longer exists."}
        if what == "csv":
            from grumble.export import export_csv

            try:
                target, _ = export_csv(session)
            except ValueError as error:
                return 400, {"error": str(error)}
        elif what == "folder":
            target = session.path
        else:
            return 400, {"error": "Bad request"}
        try:
            os.startfile(target)
        except OSError as error:
            return 500, {"error": f"Couldn't open it: {error}"}
        return 200, {}

    def close(self) -> None:
        if self._session_hotkey:
            self._session_hotkey.stop()


def _open_window(url: str) -> None:
    """Prefer Edge's app mode, which looks like a normal window with no tabs or address bar."""
    candidates = [
        Path(os.environ.get(variable, "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        for variable in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")
    ]
    edge = next((path for path in candidates if path.is_file()), None) or shutil.which("msedge")
    if edge:
        try:
            subprocess.Popen([str(edge), f"--app={url}", "--window-size=860,900"])
            return
        except OSError:
            pass
    webbrowser.open(url)


def _send_output_to_log() -> None:
    """A windowed app has no console, so printing would otherwise crash."""
    config.home().mkdir(parents=True, exist_ok=True)
    log = open(config.home() / "grumble.log", "w", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115
    sys.stdout = log
    sys.stderr = log


def main() -> None:
    instance_path = config.home() / "app.json"
    try:
        running = json.loads(instance_path.read_text(encoding="utf-8"))
        if _pid_alive(running["pid"]):
            _open_window(running["url"])
            return
    except (OSError, ValueError, KeyError):
        pass

    _send_output_to_log()
    load_dotenv(config.home() / ".env")
    app = RunApp()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    write_json(instance_path, {"pid": os.getpid(), "url": url})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if "--no-window" not in sys.argv:
        _open_window(url)

    # Without a tray icon, closing the window has to end the app, or its hotkey would stay taken.
    while not app.finished.wait(5):
        window_gone = time.monotonic() - app.last_ping > _NO_WINDOW_EXIT_SECONDS
        if window_gone and app.stage not in _BUSY:
            break

    app.close()
    server.shutdown()
    server.server_close()
    instance_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
