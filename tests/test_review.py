import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from grumble import review
from grumble.config import ProjectConfig, create_project
from grumble.github import GitHub, GitHubError
from grumble.review import ReviewError, export_approved, file_approved, load_review, save_item, screenshot_file
from grumble.review_ui import ReviewApp
from grumble.session import create_session, write_json
from grumble.setup_ui import make_handler
from tests.helpers import request


class FakeGitHub:
    """Just enough of GitHub's behavior, over real HTTP, to exercise the client."""

    def __init__(self):
        self.issues = []
        self.labels = {"bug"}
        self.files = {}
        self.branch = False
        self.commit_parents = None
        self.fail_next = {}  # (method, path suffix) -> status
        self.drop_after_create = False
        self.repo = {"private": True, "has_issues": True, "permissions": {"push": True}}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _reply(self, status, data):
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path.split("?")[0].removeprefix("/repos/me/game")
                for (method, suffix), status in list(fake.fail_next.items()):
                    if method == self.command and path.endswith(suffix):
                        del fake.fail_next[(method, suffix)]
                        return self._reply(status, {"message": "nope"})
                return self._route(self.command, path, body)

            def _route(self, method, path, body):
                if method == "GET" and path == "":
                    return self._reply(200, fake.repo)
                if method == "POST" and path == "/labels":
                    if body["name"] in fake.labels:
                        return self._reply(422, {"message": "already_exists"})
                    fake.labels.add(body["name"])
                    return self._reply(201, {})
                if method == "POST" and path == "/issues":
                    number = len(fake.issues) + 1
                    fake.issues.append({**body, "number": number, "html_url": f"https://github.com/me/game/issues/{number}"})
                    if fake.drop_after_create:
                        fake.drop_after_create = False
                        self.connection.close()
                        return None
                    return self._reply(201, fake.issues[-1])
                if method == "GET" and path == "/issues":
                    return self._reply(200, list(reversed(fake.issues)))
                if method == "GET" and path == "/git/ref/heads/grumble-assets":
                    return self._reply(200 if fake.branch else 404, {"message": "x"})
                if method == "POST" and path == "/git/trees":
                    return self._reply(201, {"sha": "tree"})
                if method == "POST" and path == "/git/commits":
                    fake.commit_parents = body["parents"]
                    return self._reply(201, {"sha": "commit"})
                if method == "POST" and path == "/git/refs":
                    fake.branch = True
                    return self._reply(201, {})
                if method == "PUT" and path.startswith("/contents/"):
                    name = path.removeprefix("/contents/")
                    if name in fake.files:
                        return self._reply(422, {"message": "sha wasn't supplied"})
                    fake.files[name] = body
                    return self._reply(201, {})
                return self._reply(404, {"message": f"unexpected {method} {path}"})

            do_GET = do_POST = do_PUT = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def github():
    fake = FakeGitHub()
    yield fake
    fake.close()


@pytest.fixture
def client(github):
    return GitHub("token", "me/game", api=github.url)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRUMBLE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GRUMBLE_PROJECTS", raising=False)
    monkeypatch.delenv("GRUMBLE_SESSIONS", raising=False)
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def excerpt(text="The goalie fell through.", start=12.0):
    return {"segment_id": 1, "start": start, "end": start + 2, "text": text}


def proposals(created_at="2026-11-01T10:00:00", issues=2):
    return {
        "session": "s",
        "model": "m",
        "created_at": created_at,
        "new_issues": [
            {
                "id": f"issue-{n}",
                "title": f"Issue {n}",
                "type": "bug",
                "description": f"Description {n}",
                "repro_steps": ["Do the thing"],
                "severity": "high",
                "excerpts": [excerpt()],
                "marker_ids": [1],
                "screenshots": [f"screenshots/marker_{n:03d}.jpg"],
            }
            for n in range(1, issues + 1)
        ],
        "updates_to_existing": [],
        "uncertain": [
            {"id": "uncertain-1", "summary": "Maybe slow", "why_uncertain": "Said once.", "excerpts": [], "marker_ids": [], "screenshots": []}
        ],
        "discarded": {"count": 1, "excerpts": [excerpt("swearing")]},
        "unaccounted": [],
        "warnings": [],
    }


@pytest.fixture
def session(home):
    create_project("game", "me/game")
    created = create_session(ProjectConfig(name="game"))
    created.screenshots_dir.mkdir(parents=True, exist_ok=True)
    for n in (1, 2):
        (created.screenshots_dir / f"marker_{n:03d}.jpg").write_bytes(b"jpeg" + bytes([n]))
    write_json(created.path / "proposals.json", proposals())
    return created


def approve(session, *ids):
    for item_id in ids:
        save_item(session, item_id, {"status": "approved"})


def test_every_proposal_starts_undecided_with_a_readable_body(session):
    items = {item["id"]: item for item in load_review(session)["items"]}
    assert set(items) == {"issue-1", "issue-2", "uncertain-1"}
    assert all(item["status"] == "pending" for item in items.values())
    body = items["issue-1"]["body"]
    assert "Description 1" in body and "1. Do the thing" in body and "> [00:00:12] The goalie fell through." in body
    assert "Said once." in items["uncertain-1"]["body"]


def test_only_approved_items_are_filed(session, client, github):
    approve(session, "issue-1")
    save_item(session, "issue-2", {"status": "rejected"})
    file_approved(session, client)
    assert [issue["title"] for issue in github.issues] == ["Issue 1"]
    statuses = {item["id"]: item["status"] for item in load_review(session)["items"]}
    assert statuses == {"issue-1": "filed", "issue-2": "rejected", "uncertain-1": "pending"}


def test_nothing_approved_files_nothing(session, client, github):
    with pytest.raises(ReviewError, match="Approve"):
        file_approved(session, client)
    assert github.issues == []


def test_edits_reach_the_filed_issue(session, client, github):
    save_item(session, "uncertain-1", {"title": " Skating is slow ", "body": "My words", "type": "balance", "severity": "medium", "status": "approved"})
    file_approved(session, client)
    issue = github.issues[0]
    assert issue["title"] == "Skating is slow" and issue["body"].startswith("My words")
    assert issue["labels"] == ["grumble", "balance", "severity:medium"]
    assert {"grumble", "balance", "severity:medium"} <= github.labels


def test_filing_twice_does_not_duplicate(session, client, github):
    approve(session, "issue-1")
    file_approved(session, client)
    with pytest.raises(ReviewError):
        file_approved(session, client)
    assert len(github.issues) == 1


def test_screenshots_go_to_a_separate_branch_and_into_the_issue(session, client, github):
    approve(session, "issue-1")
    file_approved(session, client)
    assert github.commit_parents == []  # shares no history with the code
    assert list(github.files) == [f"{session.id}/marker_001.jpg"]
    assert github.files[f"{session.id}/marker_001.jpg"]["branch"] == "grumble-assets"
    assert f"blob/grumble-assets/{session.id}/marker_001.jpg?raw=true" in github.issues[0]["body"]


def test_left_out_screenshots_are_never_uploaded(session, client, github):
    save_item(session, "issue-1", {"include": [], "status": "approved"})
    file_approved(session, client)
    assert github.files == {} and not github.branch
    assert "Screenshot" not in github.issues[0]["body"]


def test_include_cannot_name_another_file(session):
    item = save_item(session, "issue-1", {"include": ["../../secret.txt", "screenshots/marker_001.jpg"]})
    assert item["include"] == ["screenshots/marker_001.jpg"]


@pytest.mark.parametrize("name", ["../session.json", "..\\session.json", "nope.jpg"])
def test_screenshot_lookup_stays_in_its_folder(session, name):
    with pytest.raises(ReviewError):
        screenshot_file(session, name)


def test_failure_keeps_approval_and_retry_finishes(session, client, github):
    approve(session, "issue-1", "issue-2")
    # The first issue files, then the second is refused.
    original_create = client.create_issue
    calls = []

    def create(*args):
        calls.append(args)
        if len(calls) == 2:
            raise GitHubError(403, "refused")
        return original_create(*args)

    client.create_issue = create
    with pytest.raises(ReviewError, match="refused"):
        file_approved(session, client)
    items = {item["id"]: item for item in load_review(session)["items"]}
    assert items["issue-1"]["status"] == "filed"
    assert items["issue-2"]["status"] == "approved" and items["issue-2"]["error"] == "refused"

    client.create_issue = original_create
    file_approved(session, client)
    assert [issue["title"] for issue in github.issues] == ["Issue 1", "Issue 2"]


def test_lost_connection_after_filing_does_not_duplicate(session, client, github):
    approve(session, "issue-1")
    github.drop_after_create = True
    with pytest.raises(ReviewError, match="connection dropped"):
        file_approved(session, client)
    assert load_review(session)["items"][0]["status"] == "filing"
    assert "error" in load_review(session)["items"][0]
    with pytest.raises(ReviewError, match="already gone"):
        save_item(session, "issue-1", {"title": "Changed"})

    file_approved(session, client)
    assert len(github.issues) == 1
    item = load_review(session)["items"][0]
    assert item["status"] == "filed" and item["number"] == 1


def test_lost_connection_before_filing_still_files_on_retry(session, client, github):
    approve(session, "issue-1")
    review_data = load_review(session)
    review_data["items"][0]["status"] = "filing"
    write_json(session.path / "review.json", review_data)
    file_approved(session, client)
    assert len(github.issues) == 1


def _mark_filing(session, seconds_ago):
    state = load_review(session)
    state["items"][0].update(status="filing", filing_at=time.time() - seconds_ago)
    write_json(session.path / "review.json", state)


def test_issue_missing_from_a_lagging_list_is_not_filed_again(session, client, github):
    # On real GitHub the list missed an issue filed seconds earlier, which made a duplicate.
    approve(session, "issue-1")
    _mark_filing(session, seconds_ago=5)
    with pytest.raises(ReviewError, match="Wait two minutes"):
        file_approved(session, client)
    assert github.issues == []
    assert load_review(session)["items"][0]["status"] == "filing"


def test_issue_still_missing_after_the_lag_is_filed(session, client, github):
    approve(session, "issue-1")
    _mark_filing(session, seconds_ago=121)
    file_approved(session, client)
    assert len(github.issues) == 1
    assert "filing_at" not in load_review(session)["items"][0]


def test_issues_turned_off_is_explained(session, client, github):
    github.repo["has_issues"] = False
    approve(session, "issue-1")
    with pytest.raises(ReviewError, match="turned off"):
        file_approved(session, client)
    assert github.issues == []
    assert load_review(session)["items"][0]["status"] == "approved"


def test_empty_repo_is_explained(session, client, github):
    github.fail_next[("POST", "/git/trees")] = 409
    approve(session, "issue-1")
    with pytest.raises(ReviewError, match="no commits"):
        file_approved(session, client)
    assert github.issues == []


def test_reprocessing_starts_over_but_remembers_what_was_filed(session, client, github):
    approve(session, "issue-1")
    file_approved(session, client)
    write_json(session.path / "proposals.json", proposals(created_at="2026-11-01T11:00:00", issues=1))
    state = load_review(session)
    assert state["items"][0]["status"] == "pending"
    assert state["previously_filed"][0]["number"] == 1
    assert list(session.path.glob("review.*.json"))


@pytest.mark.parametrize(
    "changes",
    [{"status": "filed"}, {"type": "exploit"}, {"severity": "x"}, {"title": "", "status": "approved"}, {"title": "x" * 257}, {"body": "x" * 60001}],
)
def test_bad_changes_are_refused(session, changes):
    with pytest.raises(ReviewError):
        save_item(session, "issue-1", changes)
    assert load_review(session)["items"][0]["status"] == "pending"


def test_no_proposals_is_explained(home):
    with pytest.raises(ReviewError, match="no proposed"):
        load_review(create_session(ProjectConfig(name="game")))


def test_approved_issues_can_be_saved_without_github(session):
    approve(session, "issue-1")
    save_item(session, "issue-2", {"status": "rejected"})
    text = export_approved(session).read_text(encoding="utf-8")
    assert "Issue 1" in text and "Issue 2" not in text and "Maybe slow" not in text


# The page's routes, through the real local server.


@pytest.fixture
def app(home, github, monkeypatch):
    app = ReviewApp(home / ".env")
    monkeypatch.setattr(app, "_client", lambda token, repo: GitHub(token, repo, api=github.url))
    monkeypatch.setattr("grumble.credentials.find_github_token", lambda _env: ("token", "test"))
    return app


@pytest.fixture
def server(app):
    instance = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    yield instance
    instance.shutdown()
    instance.server_close()


def call(server, app, path, body=None):
    status, data = request(server, app, "GET" if body is None else "POST", path, body)
    return status, json.loads(data)


def test_page_flow_reviews_and_files(server, app, session, github):
    assert request(server, app, "GET", f"/review/{session.id}", token=None)[0] == 200
    state = call(server, app, f"/api/review/{session.id}")[1]
    assert len(state["items"]) == 3 and state["discarded"][0]["text"] == "swearing"
    target = call(server, app, f"/api/review/{session.id}/target")[1]
    assert target == {"repo": "me/game", "ready": True, "private": True}

    assert call(server, app, "/api/review/file", {"session": session.id})[0] == 502
    assert github.issues == []
    assert call(server, app, "/api/review/save", {"session": session.id, "item": "issue-1", "changes": {"status": "approved"}})[0] == 200
    assert call(server, app, "/api/review/file", {"session": session.id})[0] == 200
    assert len(github.issues) == 1
    assert call(server, app, f"/api/review/{session.id}")[1]["items"][0]["url"].endswith("/issues/1")


def test_long_edit_fits_through_the_server(server, app, session):
    body = {"session": session.id, "item": "issue-1", "changes": {"body": "x" * 50000}}
    assert call(server, app, "/api/review/save", body)[0] == 200


def test_screenshot_route(server, app, session):
    status, data = call(server, app, f"/api/review/{session.id}/shot/marker_001.jpg")
    assert status == 200 and data["data"].startswith("data:image/jpeg;base64,")
    assert call(server, app, f"/api/review/{session.id}/shot/nope.jpg")[0] == 404


def test_public_repo_is_flagged(server, app, session, github):
    github.repo["private"] = False
    assert call(server, app, f"/api/review/{session.id}/target")[1]["private"] is False


def test_no_write_access_blocks_filing(server, app, session, github):
    github.repo["permissions"]["push"] = False
    target = call(server, app, f"/api/review/{session.id}/target")[1]
    assert target["ready"] is False and "can't write" in target["detail"]


def test_local_project_has_nowhere_to_file(server, app, home, github):
    create_project("local-game", None)
    session = create_session(ProjectConfig(name="local-game"))
    write_json(session.path / "proposals.json", proposals())
    assert call(server, app, f"/api/review/{session.id}/target")[1]["problem"] == "no_repo"
    call(server, app, "/api/review/save", {"session": session.id, "item": "issue-1", "changes": {"status": "approved"}})
    status, data = call(server, app, "/api/review/file", {"session": session.id})
    assert status == 400 and "no GitHub repo" in data["error"]
    assert github.issues == []


def test_filing_needs_the_page_token(server, app, session, github):
    review.save_item(session, "issue-1", {"status": "approved"})
    assert request(server, app, "POST", "/api/review/file", {"session": session.id}, token="wrong")[0] == 403
    assert request(server, app, "POST", "/api/review/file", {"session": session.id}, headers={"Origin": "https://evil.example"})[0] == 403
    assert github.issues == []


@pytest.mark.parametrize("session_id", ["..", "C:\\Windows", "nope"])
def test_review_session_ids_cannot_escape(server, app, session_id):
    assert call(server, app, "/api/review/file", {"session": session_id})[0] == 404
    assert call(server, app, "/api/review/save", {"session": session_id, "item": "x"})[0] == 404
