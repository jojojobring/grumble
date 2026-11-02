"""The local web server behind `grumble setup` and the app, and the setup page's actions."""

import json
import os
import secrets
import subprocess
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from grumble import config, credentials, recorder

SETUP_PAGE = Path(__file__).with_name("setup_page.html")
_MAX_BODY_BYTES = 8192

Response = tuple[int, dict[str, Any]]


class SetupApp:
    in_app = False
    max_body = _MAX_BODY_BYTES

    def __init__(self, env_path: Path = credentials.ENV_PATH):
        self.env_path = env_path
        self.token = secrets.token_urlsafe(32)
        self.finished = threading.Event()
        self._gh_login: subprocess.Popen | None = None

    def page(self, path: str) -> Path | str | None:
        """Return the page file for a path, a string to redirect to, or None."""
        return SETUP_PAGE if path == "/" else None

    def get_routes(self) -> dict[str, Callable[[], Response]]:
        return {
            "/api/status/anthropic": lambda: (200, self.anthropic_status()),
            "/api/status/github": lambda: (200, self.github_status()),
            "/api/status/mic": lambda: (200, self.mic_status()),
        }

    def post_routes(self) -> dict[str, Callable[[dict[str, Any]], Response]]:
        return {
            "/api/anthropic": lambda body: self.save_anthropic(_text(body)),
            "/api/github/token": lambda body: self.save_github(_text(body)),
            "/api/github/login": lambda _body: self.start_gh_login(),
            "/api/mic": lambda body: self.save_mic(_text(body)),
            "/api/mic/test": lambda body: self.test_mic(_text(body)),
            "/api/finish": lambda _body: self.finish(),
        }

    def get_dynamic(self, _path: str) -> Response | None:
        return None

    def finish(self) -> Response:
        self.finished.set()
        return 200, {}

    def anthropic_status(self) -> dict[str, Any]:
        key = os.environ.get(credentials.ANTHROPIC_VAR)
        if not key:
            return {"connected": False, "source": None, "detail": "No API key found."}
        ok, detail = credentials.check_anthropic(key)
        return {"connected": ok, "source": credentials.source_of(credentials.ANTHROPIC_VAR, self.env_path), "detail": detail}

    def github_status(self) -> dict[str, Any]:
        token, source = credentials.find_github_token(self.env_path)
        status: dict[str, Any] = {
            "gh_installed": credentials.gh_installed(),
            "login_running": self._gh_login is not None and self._gh_login.poll() is None,
            "repos": [],
        }
        if not token:
            return {**status, "connected": False, "source": None, "detail": "Not connected."}
        check = credentials.check_github(token, credentials.tracked_repos())
        return {**status, "connected": check.ok, "source": source, "detail": check.detail, "repos": check.repos}

    def save_anthropic(self, key: str) -> Response:
        if not credentials.is_plausible_secret(key):
            return 400, {"error": "That doesn't look like an API key. Copy the whole key and try again."}
        ok, detail = credentials.check_anthropic(key)
        if not ok:
            return 400, {"error": detail}
        overridden = credentials.save_secret(credentials.ANTHROPIC_VAR, key, self.env_path)
        return 200, {"overridden": overridden}

    def save_github(self, token: str) -> Response:
        if not credentials.is_plausible_secret(token):
            return 400, {"error": "That doesn't look like a GitHub token. Copy the whole token and try again."}
        check = credentials.check_github(token, [])
        if not check.ok:
            return 400, {"error": check.detail}
        overridden = credentials.save_secret(credentials.GITHUB_VAR, token, self.env_path)
        return 200, {"overridden": overridden}

    def mic_status(self) -> dict[str, Any]:
        return {
            "devices": [{"name": name, "is_default": is_default} for name, is_default in recorder.list_input_devices()],
            "selected": config.default_mic(),
            "overrides": config.projects_with_own_mic(),
        }

    def save_mic(self, name: str) -> Response:
        if name and name not in [device for device, _ in recorder.list_input_devices()]:
            return 400, {"error": "That mic isn't available. Plug it in and restart setup."}
        config.save_default_mic(name or None)
        return 200, {}

    def test_mic(self, name: str) -> Response:
        try:
            return 200, {"peak": recorder.measure_level(name or None)}
        except (RuntimeError, ValueError) as error:
            return 400, {"error": str(error)}

    def start_gh_login(self) -> Response:
        if not credentials.gh_installed():
            return 400, {"error": "The GitHub CLI isn't installed. Use a token instead."}
        if self._gh_login is None or self._gh_login.poll() is not None:
            self._gh_login = subprocess.Popen(
                ["gh", "auth", "login", "--hostname", "github.com", "--web", "--git-protocol", "https"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        return 200, {}


def _text(body: dict[str, Any]) -> str:
    return str(body.get("value", "")).strip()


def make_handler(app: SetupApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def _send(self, status: int, body: bytes, content_type: str, location: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'",
            )
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict[str, Any]) -> None:
            self._send(status, json.dumps(data).encode("utf-8"), "application/json")

        def _from_this_page(self) -> bool:
            """Stop other websites reaching this server through the browser."""
            port = self.server.server_address[1]
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in allowed:
                return False
            origin = self.headers.get("Origin")
            return origin is None or urlparse(origin).netloc in allowed

        def _authorized(self) -> bool:
            sent = self.headers.get("X-Grumble-Token", "")
            return self._from_this_page() and secrets.compare_digest(sent, app.token)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if not path.startswith("/api/"):
                if not self._from_this_page():
                    return self._json(403, {"error": "Forbidden"})
                page = app.page(path)
                if page is None:
                    return self._json(404, {"error": "Not found"})
                if isinstance(page, str):
                    return self._send(302, b"", "text/plain", location=page)
                text = page.read_text(encoding="utf-8").replace("__TOKEN__", app.token).replace("__IN_APP__", json.dumps(app.in_app))
                return self._send(200, text.encode("utf-8"), "text/html; charset=utf-8")
            if not self._authorized():
                return self._json(403, {"error": "Forbidden"})
            route = app.get_routes().get(path)
            result = route() if route else app.get_dynamic(path)
            if result is None:
                return self._json(404, {"error": "Not found"})
            self._json(*result)

        def do_POST(self) -> None:
            if not self._authorized():
                return self._json(403, {"error": "Forbidden"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._json(400, {"error": "Bad request"})
            if length > app.max_body:
                return self._json(413, {"error": "Too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "Bad request"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "Bad request"})
            route = app.post_routes().get(urlparse(self.path).path)
            if route is None:
                return self._json(404, {"error": "Not found"})
            self._json(*route(body))

    return Handler


def run_setup(open_browser: bool = True, port: int = 0) -> None:
    app = SetupApp()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Setup is open at {url}\nPress Ctrl+C here to close it.")
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
    print("Setup closed.")
