from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from agentic_journal.security import redact_value

SCHEMA_VERSION = 1

AGENT_START_EVENT_TYPE = "agent_start"
AGENT_END_EVENT_TYPE = "agent_end"
GIT_COMMIT_EVENT_TYPE = "git_commit"
VERIFICATION_EVENT_TYPE = "verification"
SEMANTIC_NOTE_EVENT_TYPE = "semantic_note"
SESSION_SUMMARY_EVENT_TYPE = "session_summary"
TASK_COMPLETED_CLAIM_EVENT_TYPE = "task_completed_claim"
TASK_BLOCKED_EVENT_TYPE = "task_blocked"
MODEL_OPERATION_EVENT_TYPE = "model_operation"

ALLOWED_EVENT_TYPES = {
    AGENT_START_EVENT_TYPE,
    AGENT_END_EVENT_TYPE,
    GIT_COMMIT_EVENT_TYPE,
    VERIFICATION_EVENT_TYPE,
    SEMANTIC_NOTE_EVENT_TYPE,
    SESSION_SUMMARY_EVENT_TYPE,
    TASK_COMPLETED_CLAIM_EVENT_TYPE,
    TASK_BLOCKED_EVENT_TYPE,
    MODEL_OPERATION_EVENT_TYPE,
}

# Single source of truth for the event-type subsets used across the codebase.
# Importers (cli, report, web, diagnostics) must read these instead of
# re-declaring their own literal sets, so a new event type only has to be
# classified in one place.
SESSION_OUTCOME_EVENT_TYPES = {SESSION_SUMMARY_EVENT_TYPE, TASK_COMPLETED_CLAIM_EVENT_TYPE, TASK_BLOCKED_EVENT_TYPE}
SESSION_LIFECYCLE_EVENT_TYPES = {AGENT_START_EVENT_TYPE, AGENT_END_EVENT_TYPE, VERIFICATION_EVENT_TYPE}
SESSION_EVENT_TYPES = SESSION_OUTCOME_EVENT_TYPES | SESSION_LIFECYCLE_EVENT_TYPES
SESSION_VIEW_EVENT_TYPES = SESSION_LIFECYCLE_EVENT_TYPES | {SESSION_SUMMARY_EVENT_TYPE}

JOURNAL_MISSING_STATUS = "journal_missing"

# Free-text semantic fields are bounded so an accidental paste of a file body or
# transcript cannot bloat the journal. Pattern-based redaction cannot strip
# non-secret sensitive content, so a hard length cap is the defense.
MAX_SEMANTIC_TEXT = 4000
_FREE_TEXT_KEYS = ("summary", "note", "reason")


def _validate_ts(ts: str) -> str:
    """Reject a caller-supplied timestamp that is not ISO-parseable.

    The timestamp drives both the JSONL filename (``ts[:10]``) and the date a
    report can surface the event under, so a malformed value would mis-file the
    event or, with path separators, escape the events directory.
    """
    try:
        datetime.fromisoformat(ts)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid ts (expected ISO 8601): {ts!r}") from exc
    return ts


def _cap_free_text(semantic: dict[str, Any]) -> dict[str, Any]:
    capped = dict(semantic)
    for key in _FREE_TEXT_KEYS:
        value = capped.get(key)
        if isinstance(value, str) and len(value) > MAX_SEMANTIC_TEXT:
            capped[key] = value[:MAX_SEMANTIC_TEXT] + "…[truncated]"
    return capped


def normalize_event(raw: dict[str, Any]) -> dict[str, Any]:
    event = redact_value(dict(raw))
    event_type = event.get("event_type")
    if event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError(f"Unsupported event_type: {event_type!r}")

    provided_ts = event.get("ts")
    ts = _validate_ts(provided_ts) if provided_ts else datetime.now().astimezone().isoformat(timespec="microseconds")

    normalized: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event.get("event_id") or str(uuid4()),
        "ts": ts,
        "event_type": event_type,
        "agent": event.get("agent"),
        "session_id": event.get("session_id"),
        "cwd": event.get("cwd"),
        "repo": event.get("repo"),
        "branch": event.get("branch"),
        "commit": event.get("commit"),
        "command": event.get("command"),
        "exit_code": event.get("exit_code"),
        "duration_ms": event.get("duration_ms"),
        "files_changed": event.get("files_changed") or [],
        "semantic": _cap_free_text(event.get("semantic") or {}),
        "evidence": event.get("evidence") or {},
    }

    return {key: value for key, value in normalized.items() if value is not None}
