"""The parts of the GitHub API that filing issues needs."""

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ASSETS_BRANCH = "grumble-assets"
_ASSETS_README = "Screenshots attached to issues filed by Grumble. Nothing here is part of the project's code.\n"
_RECENT_ISSUES = 100


class GitHubError(Exception):
    """GitHub answered with an error, so the request definitely took no effect."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class GitHubUnreachable(Exception):
    """No answer came back, so a request that was sent may or may not have worked."""


class GitHub:
    def __init__(self, token: str, repo: str, api: str = "https://api.github.com"):
        self._token = token
        self.repo = repo
        self._api = api

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            f"{self._api}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "grumble",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise GitHubError(error.code, self._explain(error)) from error
        except (OSError, ValueError) as error:
            raise GitHubUnreachable(f"Couldn't reach GitHub: {error}") from error

    def _explain(self, error: urllib.error.HTTPError) -> str:
        try:
            detail = json.load(error).get("message", "")
        except (OSError, ValueError, AttributeError):
            detail = ""
        if error.code == 401:
            return "GitHub rejected your token. Reconnect it in Setup."
        if error.code == 403:
            return f"GitHub refused this. Your token may lack write access to {self.repo}, or you may have hit a rate limit. ({detail})"
        if error.code == 404:
            return f"GitHub can't find {self.repo}, or your token can't see it."
        if error.code == 409:
            return f"{self.repo} has no commits yet. Add a first commit, then try again."
        if error.code == 410:
            return f"Issues are turned off for {self.repo}."
        return f"GitHub returned error {error.code}. {detail}"

    def repo_info(self) -> dict[str, Any]:
        data = self._call("GET", f"/repos/{self.repo}")
        return {
            "private": bool(data.get("private")),
            "can_push": bool(data.get("permissions", {}).get("push")),
            "has_issues": bool(data.get("has_issues")),
        }

    def ensure_labels(self, labels: dict[str, str]) -> None:
        """Create any missing labels, given as name to color. Existing ones keep their color."""
        for name, color in labels.items():
            try:
                self._call("POST", f"/repos/{self.repo}/labels", {"name": name, "color": color})
            except GitHubError as error:
                if error.status != 422:  # 422 means it already exists
                    raise

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        data = self._call("POST", f"/repos/{self.repo}/issues", {"title": title, "body": body, "labels": labels})
        return {"number": data["number"], "url": data["html_url"]}

    def find_issue_containing(self, text: str) -> dict[str, Any] | None:
        issues = self._call("GET", f"/repos/{self.repo}/issues?state=all&sort=created&direction=desc&per_page={_RECENT_ISSUES}")
        for issue in issues:
            if text in (issue.get("body") or ""):
                return {"number": issue["number"], "url": issue["html_url"]}
        return None

    def ensure_assets_branch(self) -> None:
        """Create the screenshot branch with no shared history, so it never mixes with the code."""
        try:
            self._call("GET", f"/repos/{self.repo}/git/ref/heads/{ASSETS_BRANCH}")
            return
        except GitHubError as error:
            if error.status != 404:
                raise
        tree = self._call(
            "POST",
            f"/repos/{self.repo}/git/trees",
            {"tree": [{"path": "README.md", "mode": "100644", "type": "blob", "content": _ASSETS_README}]},
        )
        commit = self._call("POST", f"/repos/{self.repo}/git/commits", {"message": "Add Grumble screenshot branch", "tree": tree["sha"], "parents": []})
        self._call("POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{ASSETS_BRANCH}", "sha": commit["sha"]})

    def upload_screenshot(self, session_id: str, file: Path) -> str:
        """Commit a screenshot to the branch and return a link that displays it in an issue."""
        path = f"{session_id}/{file.name}"
        content = base64.b64encode(file.read_bytes()).decode("ascii")
        try:
            self._call(
                "PUT",
                f"/repos/{self.repo}/contents/{path}",
                {"message": f"Add {path}", "content": content, "branch": ASSETS_BRANCH},
            )
        except GitHubError as error:
            if error.status != 422:  # 422 means an earlier attempt already uploaded this file
                raise
        return f"https://github.com/{self.repo}/blob/{ASSETS_BRANCH}/{path}?raw=true"
