"""JSON schemas for the files later stages read back."""

import jsonschema

_NUMBER_OR_NULL = {"type": ["number", "null"]}
_STRING_OR_NULL = {"type": ["string", "null"]}

SESSION = {
    "type": "object",
    "required": ["id", "project", "status", "started_at", "sample_rate", "chunk_seconds"],
    "properties": {
        "id": {"type": "string"},
        "project": {"type": "string"},
        "status": {"enum": ["recording", "recorded", "failed"]},
        "started_at": {"type": "string"},
        "ended_at": _STRING_OR_NULL,
        "sample_rate": {"const": 16000},
        "chunk_seconds": {"type": "integer", "minimum": 1},
        "duration_seconds": _NUMBER_OR_NULL,
        "overflow_count": {"type": "integer", "minimum": 0},
        "device": _STRING_OR_NULL,
    },
}

MARKER = {
    "type": "object",
    "required": ["id", "t", "wall_time", "screenshot", "gamestate"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "integer", "minimum": 1},
        "t": {"type": "number", "minimum": 0},
        "wall_time": {"type": "string"},
        "window_title": _STRING_OR_NULL,
        "screenshot": _STRING_OR_NULL,
        "screenshot_warning": _STRING_OR_NULL,
        "gamestate": _STRING_OR_NULL,
        "gamestate_error": _STRING_OR_NULL,
    },
}

TRANSCRIPT = {
    "type": "object",
    "required": ["session", "model", "audio_seconds", "segments"],
    "properties": {
        "session": {"type": "string"},
        "model": {"type": "string"},
        "language": _STRING_OR_NULL,
        "initial_prompt": _STRING_OR_NULL,
        "created_at": {"type": "string"},
        "audio_seconds": {"type": "number", "minimum": 0},
        "speech_seconds": {"type": "number", "minimum": 0},
        "transcribe_seconds": {"type": "number", "minimum": 0},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["start", "end", "text"],
                "additionalProperties": False,
                "properties": {
                    "start": {"type": "number", "minimum": 0},
                    "end": {"type": "number", "minimum": 0},
                    "text": {"type": "string"},
                    "avg_logprob": {"type": "number"},
                    "no_speech_prob": {"type": "number"},
                },
            },
        },
    },
}


_STRINGS = {"type": "array", "items": {"type": "string"}}
_INTEGERS = {"type": "array", "items": {"type": "integer"}}
_EXCERPTS = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["segment_id", "start", "end", "text"],
        "additionalProperties": False,
        "properties": {
            "segment_id": {"type": "integer", "minimum": 1},
            "start": {"type": "number", "minimum": 0},
            "end": {"type": "number", "minimum": 0},
            "text": {"type": "string"},
        },
    },
}

PROPOSALS = {
    "type": "object",
    "required": ["session", "model", "new_issues", "updates_to_existing", "uncertain", "discarded", "unaccounted", "warnings"],
    "properties": {
        "session": {"type": "string"},
        "model": {"type": "string"},
        "created_at": {"type": "string"},
        "usage": {"type": "object"},
        "new_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "title", "type", "description", "repro_steps", "severity", "excerpts", "marker_ids", "screenshots"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string", "minLength": 1},
                    "type": {"enum": ["bug", "feature", "ux", "balance"]},
                    "description": {"type": "string"},
                    "repro_steps": _STRINGS,
                    "severity": {"enum": ["low", "medium", "high", "critical"]},
                    "excerpts": _EXCERPTS,
                    "marker_ids": _INTEGERS,
                    "screenshots": _STRINGS,
                },
            },
        },
        "updates_to_existing": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["issue_number", "comment"],
                "properties": {"issue_number": {"type": "integer"}, "comment": {"type": "string"}},
            },
        },
        "uncertain": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "summary", "why_uncertain", "excerpts", "marker_ids", "screenshots"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "summary": {"type": "string"},
                    "why_uncertain": {"type": "string"},
                    "excerpts": _EXCERPTS,
                    "marker_ids": _INTEGERS,
                    "screenshots": _STRINGS,
                },
            },
        },
        "discarded": {
            "type": "object",
            "required": ["count", "excerpts"],
            "additionalProperties": False,
            "properties": {"count": {"type": "integer", "minimum": 0}, "excerpts": _EXCERPTS},
        },
        "unaccounted": _EXCERPTS,
        "warnings": _STRINGS,
    },
}


def validate(data: object, schema: dict) -> None:
    """Raise jsonschema.ValidationError if data does not match schema."""
    jsonschema.validate(data, schema)
