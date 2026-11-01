import csv
import json

import pytest

from grumble.config import ProjectConfig
from grumble.export import export_csv
from grumble.session import create_session

EXCERPT = {"segment_id": 1, "start": 65.0, "end": 66.0, "text": "the puck’s stuck"}
PROPOSALS = {
    "new_issues": [
        {
            "id": "issue-1",
            "title": "=SUM(A1)",
            "type": "bug",
            "severity": "high",
            "description": "d",
            "repro_steps": ["one", "two"],
            "excerpts": [EXCERPT],
            "screenshots": ["screenshots/a.jpg"],
        }
    ],
    "uncertain": [{"id": "uncertain-1", "summary": "s", "why_uncertain": "w", "excerpts": [], "screenshots": []}],
    "discarded": {"count": 1, "excerpts": [{**EXCERPT, "segment_id": 2}]},
    "unaccounted": [{**EXCERPT, "segment_id": 3}],
}


def test_export_writes_every_kind_for_excel(tmp_path):
    session = create_session(ProjectConfig(name="x"), tmp_path)
    (session.path / "proposals.json").write_text(json.dumps(PROPOSALS), encoding="utf-8")

    path, count = export_csv(session)

    assert count == 4
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))
    assert [row["Kind"] for row in rows] == ["Issue", "Uncertain", "Discarded", "Unsorted"]
    assert rows[0]["Title"] == "'=SUM(A1)"
    assert rows[0]["Repro steps"] == "1. one\n2. two"
    assert rows[0]["What you said"] == "[00:01:05] the puck’s stuck"


def test_export_without_proposals_explains_what_to_do(tmp_path):
    session = create_session(ProjectConfig(name="x"), tmp_path)
    with pytest.raises(ValueError, match="grumble process"):
        export_csv(session)
