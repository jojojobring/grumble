"""proposals.json -> proposals.csv, for reading in a spreadsheet."""

import csv
import json
from typing import Any

from grumble.session import Session
from grumble.transcribe import format_time

_COLUMNS = ["Kind", "ID", "Title", "Type", "Severity", "Description", "Repro steps", "What you said", "Screenshots"]


def _cell(text: str) -> str:
    # Transcribed text must not run as a formula when the spreadsheet opens it.
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _quotes(excerpts: list[dict[str, Any]]) -> str:
    return "\n".join(f"[{format_time(e['start'])}] {e['text']}" for e in excerpts)


def proposal_rows(proposals: dict[str, Any]) -> list[list[str]]:
    rows = []
    for issue in proposals["new_issues"]:
        steps = "\n".join(f"{n}. {step}" for n, step in enumerate(issue["repro_steps"], 1))
        rows.append(
            ["Issue", issue["id"], issue["title"], issue["type"], issue["severity"], issue["description"],
             steps, _quotes(issue["excerpts"]), "\n".join(issue["screenshots"])]
        )  # fmt: skip
    for item in proposals["uncertain"]:
        rows.append(
            ["Uncertain", item["id"], item["summary"], "", "", item["why_uncertain"],
             "", _quotes(item["excerpts"]), "\n".join(item["screenshots"])]
        )  # fmt: skip
    for kind, excerpts in (("Discarded", proposals["discarded"]["excerpts"]), ("Unsorted", proposals["unaccounted"])):
        for excerpt in excerpts:
            rows.append([kind, f"line-{excerpt['segment_id']}", "", "", "", "", "", _quotes([excerpt]), ""])
    return [[_cell(cell) for cell in row] for row in rows]


def export_csv(session: Session) -> tuple[Any, int]:
    source = session.path / "proposals.json"
    if not source.exists():
        raise ValueError(f"Session {session.id} has no proposals. Run `grumble process {session.id}` first.")
    rows = proposal_rows(json.loads(source.read_text(encoding="utf-8")))
    target = session.path / "proposals.csv"
    # The byte order mark tells Excel this is UTF-8.
    with target.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(_COLUMNS)
        writer.writerows(rows)
    return target, len(rows)
