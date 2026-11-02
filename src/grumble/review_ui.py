"""The review page's routes, used by the app and by `grumble review`."""

import base64
import json
import os
import re
import threading
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from grumble import config, credentials, review
from grumble.config import ConfigError
from grumble.github import GitHub, GitHubError, GitHubUnreachable
from grumble.review import ReviewError
from grumble.session import Session, SessionError, resolve_session
from grumble.setup_ui import Response, SetupApp, make_handler

REVIEW_PAGE = Path(__file__).with_name("review_page.html")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]+")
_SHOT = re.compile(r"/api/review/([A-Za-z0-9_-]+)/shot/([A-Za-z0-9_.-]+)")
_TARGET = re.compile(r"/api/review/([A-Za-z0-9_-]+)/target")
_STATE = re.compile(r"/api/review/([A-Za-z0-9_-]+)")
_PAGE = re.compile(r"/review/[A-Za-z0-9_-]+")


def _find_session(session_id: str) -> Session | None:
    if not _SESSION_ID.fullmatch(session_id):
        return None
    try:
        return resolve_session(session_id)
    except SessionError:
        return None


class ReviewRoutes:
    max_body = 262144  # an edited issue can be long
    # Saving and filing both rewrite review.json, so they take turns.
    _review_lock = threading.Lock()

    def page(self, path: str) -> Path | str | None:
        return REVIEW_PAGE if _PAGE.fullmatch(path) else super().page(path)

    def post_routes(self):
        return {
            **super().post_routes(),
            "/api/review/save": self.review_save,
            "/api/review/file": self.review_file,
            "/api/review/export": self.review_export,
        }

    def get_dynamic(self, path: str) -> Response | None:
        if match := _SHOT.fullmatch(path):
            return self.review_screenshot(match.group(1), match.group(2))
        if match := _TARGET.fullmatch(path):
            return self.review_target(match.group(1))
        if match := _STATE.fullmatch(path):
            return self.review_state(match.group(1))
        return super().get_dynamic(path)

    def _project(self, session: Session) -> config.ProjectConfig:
        return config.load_project(session.read_manifest()["project"])

    def _client(self, token: str, repo: str) -> GitHub:
        return GitHub(token, repo)

    def review_state(self, session_id: str) -> Response:
        session = _find_session(session_id)
        if session is None:
            return 404, {"error": "That session no longer exists."}
        with self._review_lock:
            try:
                state = review.load_review(session)
            except ReviewError as error:
                return 400, {"error": str(error)}
        proposals = json.loads((session.path / "proposals.json").read_text(encoding="utf-8"))
        return 200, {
            **state,
            "session": session.id,
            "project": session.read_manifest()["project"],
            "discarded": proposals["discarded"]["excerpts"],
            "unsorted": proposals["unaccounted"],
            "warnings": proposals["warnings"],
            "types": review.TYPES,
            "severities": review.SEVERITIES,
        }

    def review_target(self, session_id: str) -> Response:
        """Say where filing would go. Kept apart so the page still opens when GitHub can't be reached."""
        session = _find_session(session_id)
        if session is None:
            return 404, {"error": "That session no longer exists."}
        try:
            project = self._project(session)
        except ConfigError as error:
            return 400, {"error": str(error)}
        repo = project.tracker.repo if project.tracker.type == "github" else None
        if not repo:
            return 200, {"repo": None, "ready": False, "problem": "no_repo"}
        token, _ = credentials.find_github_token(self.env_path)
        if not token:
            return 200, {"repo": repo, "ready": False, "problem": "no_token"}
        try:
            info = self._client(token, repo).repo_info()
        except (GitHubError, GitHubUnreachable) as error:
            return 200, {"repo": repo, "ready": False, "problem": "other", "detail": str(error)}
        if not info["has_issues"]:
            return 200, {"repo": repo, "ready": False, "problem": "other", "detail": f"Issues are turned off for {repo}."}
        if not info["can_push"]:
            return 200, {"repo": repo, "ready": False, "problem": "other", "detail": f"Your GitHub account can't write to {repo}."}
        return 200, {"repo": repo, "ready": True, "private": info["private"]}

    def review_screenshot(self, session_id: str, name: str) -> Response:
        session = _find_session(session_id)
        if session is None:
            return 404, {"error": "That session no longer exists."}
        try:
            data = review.screenshot_file(session, name).read_bytes()
        except (ReviewError, OSError) as error:
            return 404, {"error": str(error)}
        return 200, {"data": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}

    def review_save(self, body: dict[str, Any]) -> Response:
        session = _find_session(str(body.get("session", "")))
        if session is None:
            return 404, {"error": "That session no longer exists."}
        if not self._review_lock.acquire(blocking=False):
            return 409, {"error": "Grumble is filing issues. Try again when it finishes."}
        try:
            return 200, review.save_item(session, str(body.get("item", "")), body.get("changes") or {})
        except ReviewError as error:
            return 400, {"error": str(error)}
        finally:
            self._review_lock.release()

    def review_file(self, body: dict[str, Any]) -> Response:
        session = _find_session(str(body.get("session", "")))
        if session is None:
            return 404, {"error": "That session no longer exists."}
        try:
            project = self._project(session)
        except ConfigError as error:
            return 400, {"error": str(error)}
        repo = project.tracker.repo if project.tracker.type == "github" else None
        if not repo:
            return 400, {"error": "This game has no GitHub repo yet. Add one in its settings."}
        token, _ = credentials.find_github_token(self.env_path)
        if not token:
            return 400, {"error": "GitHub isn't connected yet. Connect it in Setup."}
        if not self._review_lock.acquire(blocking=False):
            return 409, {"error": "Grumble is already filing issues."}
        try:
            review.file_approved(session, self._client(token, repo))
            return 200, {}
        except ReviewError as error:
            return 502, {"error": str(error)}
        finally:
            self._review_lock.release()

    def review_export(self, body: dict[str, Any]) -> Response:
        session = _find_session(str(body.get("session", "")))
        if session is None:
            return 404, {"error": "That session no longer exists."}
        with self._review_lock:
            try:
                target = review.export_approved(session)
            except ReviewError as error:
                return 400, {"error": str(error)}
        try:
            os.startfile(target)
        except OSError:
            pass  # the file is still written, and the reply says where
        return 200, {"path": str(target)}


class ReviewApp(ReviewRoutes, SetupApp):
    """`grumble review`: the review page by itself."""


def run_review(session: Session, open_browser: bool = True, port: int = 0) -> None:
    app = ReviewApp()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{server.server_address[1]}/review/{session.id}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Review is open at {url}\nPress Ctrl+C here to close it.")
    if open_browser:
        webbrowser.open(url)
    try:
        # A short wait keeps Ctrl+C responsive on Windows.
        while not app.finished.wait(0.25):
            pass
    except KeyboardInterrupt:
        pass
    server.shutdown()
    server.server_close()
    print("Review closed.")
