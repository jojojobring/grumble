"""Decide what happens to each proposal, and file the approved ones. Nothing is filed without approval.

Decisions and edits live in review.json, so proposals.json stays what Claude wrote.
"""

import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from grumble.github import GitHub, GitHubError, GitHubUnreachable
from grumble.session import Session, write_json
from grumble.transcribe import format_time

TYPES = ("bug", "feature", "ux", "balance")
SEVERITIES = ("low", "medium", "high", "critical")
_EDITABLE = ("pending", "approved", "rejected")
_TITLE_LIMIT = 256
_BODY_LIMIT = 60000  # GitHub allows 65536, and filing adds screenshots and a footer
_LIST_LAG_SECONDS = 120  # twice the 60 seconds GitHub's headers allow its issue list to be cached
_WAIT_MESSAGE = (
    "The connection dropped while filing this, so it may already be on GitHub. "
    "Wait two minutes and press File again. Grumble will check first, so it isn't filed twice."
)
_LABEL_COLORS = {
    "grumble": "6f42c1",
    "bug": "d73a4a",
    "feature": "a2eeef",
    "ux": "c5def5",
    "balance": "fbca04",
    "severity:low": "e4e669",
    "severity:medium": "fbca04",
    "severity:high": "d93f0b",
    "severity:critical": "b60205",
}


class ReviewError(Exception):
    pass


def _quotes(excerpts: list[dict[str, Any]]) -> str:
    lines = "\n".join(f"> [{format_time(e['start'])}] {e['text'].strip()}" for e in excerpts)
    return f"**What was said**\n\n{lines}" if lines else ""


def _issue_body(issue: dict[str, Any]) -> str:
    steps = "\n".join(f"{n}. {step}" for n, step in enumerate(issue["repro_steps"], 1))
    parts = [issue["description"], f"**Steps to reproduce**\n\n{steps}" if steps else "", _quotes(issue["excerpts"])]
    return "\n\n".join(part for part in parts if part)


def _new_review(proposals: dict[str, Any], previously_filed: list[dict[str, Any]]) -> dict[str, Any]:
    items = [
        {
            "id": issue["id"],
            "kind": "issue",
            "status": "pending",
            "title": issue["title"],
            "type": issue["type"],
            "severity": issue["severity"],
            "body": _issue_body(issue),
            "screenshots": issue["screenshots"],
            "include": issue["screenshots"],
        }
        for issue in proposals["new_issues"]
    ]
    for item in proposals["uncertain"]:
        parts = [f"Unsure because: {item['why_uncertain']}", _quotes(item["excerpts"])]
        items.append(
            {
                "id": item["id"],
                "kind": "uncertain",
                "status": "pending",
                "title": item["summary"][:_TITLE_LIMIT],
                "type": "bug",
                "severity": "low",
                "body": "\n\n".join(part for part in parts if part),
                "screenshots": item["screenshots"],
                "include": item["screenshots"],
            }
        )
    return {"proposals_created_at": proposals.get("created_at"), "items": items, "previously_filed": previously_filed}


def _review_path(session: Session) -> Path:
    return session.path / "review.json"


def load_review(session: Session) -> dict[str, Any]:
    """Read the review, starting afresh if the proposals were regenerated since it was made."""
    try:
        proposals = json.loads((session.path / "proposals.json").read_text(encoding="utf-8"))
    except OSError as error:
        raise ReviewError("This session has no proposed issues yet.") from error

    path = _review_path(session)
    previously_filed: list[dict[str, Any]] = []
    if path.exists():
        review = json.loads(path.read_text(encoding="utf-8"))
        if review["proposals_created_at"] == proposals.get("created_at"):
            return review
        shutil.copy2(path, session.path / f"review.{datetime.now():%Y%m%d-%H%M%S}.json")
        previously_filed = review["previously_filed"] + [
            {"title": item["title"], "number": item["number"], "url": item["url"]}
            for item in review["items"]
            if item["status"] == "filed"
        ]
    review = _new_review(proposals, previously_filed)
    write_json(path, review)
    return review


def _item(review: dict[str, Any], item_id: str) -> dict[str, Any]:
    for item in review["items"]:
        if item["id"] == item_id:
            return item
    raise ReviewError("That item no longer exists. Reload the page.")


def save_item(session: Session, item_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    review = load_review(session)
    item = _item(review, item_id)
    if item["status"] not in _EDITABLE:
        raise ReviewError("This one has already gone to GitHub, so it can't be changed here.")

    updated = dict(item)
    if "title" in changes:
        updated["title"] = str(changes["title"]).strip()
    if "body" in changes:
        updated["body"] = str(changes["body"])
    for field, allowed in (("type", TYPES), ("severity", SEVERITIES), ("status", _EDITABLE)):
        if field in changes:
            if changes[field] not in allowed:
                raise ReviewError(f"'{changes[field]}' isn't a valid {field}.")
            updated[field] = changes[field]
    if "include" in changes:
        # Only ever this item's own screenshots, whatever the request says.
        updated["include"] = [path for path in updated["screenshots"] if path in changes["include"]]

    if len(updated["title"]) > _TITLE_LIMIT:
        raise ReviewError(f"Titles can be up to {_TITLE_LIMIT} characters.")
    if len(updated["body"]) > _BODY_LIMIT:
        raise ReviewError(f"The description can be up to {_BODY_LIMIT} characters.")
    if updated["status"] == "approved" and not updated["title"]:
        raise ReviewError("Add a title before approving this.")

    updated.pop("error", None)
    item.clear()
    item.update(updated)
    write_json(_review_path(session), review)
    return item


def screenshot_file(session: Session, name: str) -> Path:
    """Find a session's screenshot by file name, refusing anything outside its folder."""
    path = (session.screenshots_dir / name).resolve()
    if path.parent != session.screenshots_dir.resolve() or not path.is_file():
        raise ReviewError("That screenshot doesn't exist.")
    return path


def _filing_marker(session: Session, review: dict[str, Any], item: dict[str, Any]) -> str:
    # Hidden in the issue, so a filing that lost its connection can be recognized rather than repeated.
    return f"<!-- grumble:{session.id}:{review['proposals_created_at']}:{item['id']} -->"


def _full_body(session: Session, review: dict[str, Any], item: dict[str, Any], links: list[str]) -> str:
    parts = [item["body"].strip()]
    if links:
        parts.append("**Screenshots**\n\n" + "\n\n".join(f"![Screenshot]({link})" for link in links))
    parts.append(f"<sub>Filed with Grumble from session {session.id}.</sub>")
    parts.append(_filing_marker(session, review, item))
    return "\n\n".join(part for part in parts if part)


def _labels(item: dict[str, Any]) -> list[str]:
    return ["grumble", item["type"], f"severity:{item['severity']}"]


def _mark_filed(item: dict[str, Any], issue: dict[str, Any]) -> None:
    item.update(status="filed", number=issue["number"], url=issue["url"])
    item.pop("error", None)
    item.pop("filing_at", None)


def _file_one(client: GitHub, session: Session, review: dict[str, Any], item: dict[str, Any]) -> None:
    marker = _filing_marker(session, review, item)
    if item["status"] == "filing":
        found = client.find_issue_containing(marker)
        if found:
            _mark_filed(item, found)
            return
        # GitHub's list can lag behind a new issue, so "not listed" means nothing until this has passed.
        if time.time() - item.get("filing_at", 0) < _LIST_LAG_SECONDS:
            raise ReviewError(_WAIT_MESSAGE)

    links = []
    if item["include"]:
        client.ensure_assets_branch()
        links = [client.upload_screenshot(session.id, screenshot_file(session, Path(path).name)) for path in item["include"]]

    # Saved first, so if the answer never arrives the next attempt knows to look before filing again.
    item.update(status="filing", filing_at=time.time())
    write_json(_review_path(session), review)
    try:
        issue = client.create_issue(item["title"], _full_body(session, review, item, links), _labels(item))
    except GitHubError:
        item["status"] = "approved"
        item.pop("filing_at")
        raise
    _mark_filed(item, issue)


def file_approved(session: Session, client: GitHub) -> dict[str, Any]:
    """File approved items in order, saving after each one. Stops at the first failure so a retry resumes."""
    review = load_review(session)
    queue = [item for item in review["items"] if item["status"] in ("approved", "filing")]
    if not queue:
        raise ReviewError("Approve at least one issue first.")

    current = queue[0]
    try:
        if not client.repo_info()["has_issues"]:
            raise ReviewError(f"Issues are turned off for {client.repo}. Turn them on in the repo's settings.")
        client.ensure_labels({name: _LABEL_COLORS[name] for item in queue for name in _labels(item)})
        for current in queue:
            current.pop("error", None)
            _file_one(client, session, review, current)
            write_json(_review_path(session), review)
    except GitHubUnreachable as error:
        if current["status"] == "filing":
            message = _WAIT_MESSAGE
        else:
            message = str(error)
        current["error"] = message
        write_json(_review_path(session), review)
        raise ReviewError(message) from error
    except (GitHubError, ReviewError) as error:
        current["error"] = str(error)
        write_json(_review_path(session), review)
        raise ReviewError(str(error)) from error
    return review


def export_approved(session: Session) -> Path:
    """Write approved issues to a file, for projects that don't use GitHub."""
    review = load_review(session)
    chosen = [item for item in review["items"] if item["status"] in ("approved", "filing", "filed")]
    if not chosen:
        raise ReviewError("Approve at least one issue first.")
    sections = []
    for item in chosen:
        filed = f"\n\nFiled as {item['url']}" if item["status"] == "filed" else ""
        shots = "".join(f"\n\n![Screenshot]({path})" for path in item["include"])
        sections.append(f"## {item['title']}\n\n*{item['type']}, {item['severity']} severity*{filed}\n\n{item['body'].strip()}{shots}")
    target = session.path / "approved.md"
    target.write_text(f"# Approved issues from session {session.id}\n\n" + "\n\n".join(sections) + "\n", encoding="utf-8")
    return target
