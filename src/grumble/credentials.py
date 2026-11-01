"""Find, check and save the Anthropic key and GitHub token. Values never leave this module's callers."""

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
from dotenv import dotenv_values, set_key

from grumble.config import home, load_project, projects_dir

ENV_PATH = home() / ".env"
ANTHROPIC_VAR = "ANTHROPIC_API_KEY"
GITHUB_VAR = "GITHUB_TOKEN"
_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_\-]{10,512}")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW


def is_plausible_secret(value: str) -> bool:
    """Reject anything that could break out of its line in .env, such as a stray newline."""
    return bool(_SECRET_PATTERN.fullmatch(value))


def source_of(name: str, env_path: Path = ENV_PATH) -> str | None:
    value = os.environ.get(name)
    if not value:
        return None
    return ".env file" if dotenv_values(env_path).get(name) == value else "system environment"


def save_secret(name: str, value: str, env_path: Path = ENV_PATH) -> bool:
    """Save to .env. Returns True if a different system variable will keep overriding it."""
    overridden = source_of(name, env_path) == "system environment" and os.environ[name] != value
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.touch(exist_ok=True)
    set_key(str(env_path), name, value)
    if not overridden:
        os.environ[name] = value
    return overridden


def check_anthropic(key: str) -> tuple[bool, str]:
    """Listing models proves the key works and costs nothing."""
    try:
        anthropic.Anthropic(api_key=key, max_retries=1, timeout=15).models.list(limit=1)
    except anthropic.AuthenticationError:
        return False, "Anthropic rejected this key."
    except anthropic.PermissionDeniedError:
        return False, "This key doesn't have permission to use the API."
    except anthropic.APIConnectionError:
        return False, "Couldn't reach Anthropic. Check your connection and try again."
    except anthropic.APIStatusError as error:
        return False, f"Anthropic returned error {error.status_code}. Try again shortly."
    return True, "Key works."


def gh_installed() -> bool:
    return shutil.which("gh") is not None


def gh_token() -> str | None:
    if not gh_installed():
        return None
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def find_github_token(env_path: Path = ENV_PATH) -> tuple[str | None, str | None]:
    """Return (token, where it came from). GITHUB_TOKEN wins over the GitHub CLI login."""
    if os.environ.get(GITHUB_VAR):
        return os.environ[GITHUB_VAR], source_of(GITHUB_VAR, env_path)
    token = gh_token()
    return token, "GitHub CLI" if token else None


def _github_get(path: str, token: str) -> Any:
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "grumble",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


@dataclass
class GitHubCheck:
    ok: bool
    detail: str
    login: str | None = None
    repos: list[dict[str, Any]] = field(default_factory=list)


def check_github(token: str, repos: list[str]) -> GitHubCheck:
    try:
        login = _github_get("/user", token)["login"]
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return GitHubCheck(False, "GitHub rejected this token.")
        return GitHubCheck(False, f"GitHub returned error {error.code}. Try again shortly.")
    except (OSError, ValueError, KeyError):
        return GitHubCheck(False, "Couldn't reach GitHub. Check your connection and try again.")

    results = []
    for repo in repos:
        try:
            data = _github_get(f"/repos/{repo}", token)
        except urllib.error.HTTPError as error:
            message = "not found, or this token can't see it" if error.code == 404 else f"GitHub error {error.code}"
            results.append({"repo": repo, "ok": False, "detail": message})
            continue
        except (OSError, ValueError):
            results.append({"repo": repo, "ok": False, "detail": "couldn't check"})
            continue
        # A fine-grained token can read a repo without this; pushing will report that case.
        if data.get("permissions", {}).get("push"):
            results.append({"repo": repo, "ok": True, "detail": "can file issues"})
        else:
            results.append({"repo": repo, "ok": False, "detail": "this account can't write to it"})
    return GitHubCheck(True, f"Connected as {login}.", login, results)


def tracked_repos(directory: Path | None = None) -> list[str]:
    """GitHub repos named by project configs. A broken config is ignored here."""
    directory = directory or projects_dir()
    repos = set()
    for path in directory.glob("*.yaml"):
        try:
            tracker = load_project(path.stem, directory).tracker
        except Exception:  # `grumble start` reports config errors properly
            continue
        if tracker.type == "github" and tracker.repo:
            repos.add(tracker.repo)
    return sorted(repos)
