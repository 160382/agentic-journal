from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agentic_journal.config import journal_root, load_config
from agentic_journal.events import (
    MODEL_OPERATION_EVENT_TYPE,
    SEMANTIC_NOTE_EVENT_TYPE,
    SESSION_SUMMARY_EVENT_TYPE,
    TASK_BLOCKED_EVENT_TYPE,
    TASK_COMPLETED_CLAIM_EVENT_TYPE,
    normalize_event,
)
from agentic_journal.git_context import event_context
from agentic_journal.storage import write_event

SKIPPED_NOTE_REQUIRED = "skipped: note is required"
SKIPPED_SUMMARY_REQUIRED = "skipped: summary is required"
SKIPPED_TASK_OR_NOTE_REQUIRED = "skipped: task_id or note is required"
SKIPPED_REASON_REQUIRED = "skipped: reason is required"
SKIPPED_MODEL_OPERATION_METADATA_REQUIRED = "skipped: model operation metadata is required"

MAX_CATEGORY_LENGTH = 64

# How journal_note places the runtime metadata a client hook injects: identity
# of the author and turn goes to the event top level, execution conditions and
# measurements go to evidence. Unlisted runtime keys are dropped.
RUNTIME_TOP_LEVEL_KEYS = (
    "agent_id",
    "agent_type",
    "turn_id",
    "session_name",
    "session_name_source",
)
RUNTIME_EVIDENCE_KEYS = (
    "model",
    "permission_mode",
    "collaboration_mode",
    "effort",
    "usage_scope",
    "usage_status",
    "stats_error",
    "token_usage",
    "turn_elapsed_ms",
    "native_session_id",
    "tool_use_id",
    "injected_by",
)


def _clean_text(value: str | None) -> str:
    return value.strip() if isinstance(value, str) else ""


def _event_context(session_id: str | None = None, cwd: Path | None = None) -> dict:
    return {
        "session_id": session_id
        or os.environ.get("AGENTIC_JOURNAL_SESSION_ID")
        or os.environ.get("AGENT_JOURNAL_SESSION_ID"),
        **event_context(cwd or Path.cwd()),
    }


def journal_note(
    journal_home: str | Path | None = None,
    agent: str = "unknown",
    note: str = "",
    session_id: str | None = None,
    category: str = "",
    runtime: Mapping[str, Any] | None = None,
) -> str:
    note_text = _clean_text(note)
    if not note_text:
        return SKIPPED_NOTE_REQUIRED
    runtime = runtime if isinstance(runtime, Mapping) else {}
    semantic = {"note": note_text}
    # An over-long category is cut rather than rejected so the note survives.
    category_text = _clean_text(category)[:MAX_CATEGORY_LENGTH].rstrip()
    if category_text:
        semantic["category"] = category_text
    # One MCP server can serve agents in different directories, so the agent's
    # own cwd, when the runtime supplies it, decides the git context. The path
    # is used as given: whitespace at its ends is part of a valid directory name.
    runtime_cwd = runtime.get("cwd")
    event = normalize_event(
        {
            "event_type": SEMANTIC_NOTE_EVENT_TYPE,
            "agent": _clean_text(runtime.get("client")) or agent,
            **_event_context(session_id, Path(runtime_cwd) if isinstance(runtime_cwd, str) and runtime_cwd else None),
            **{key: runtime[key] for key in RUNTIME_TOP_LEVEL_KEYS if runtime.get(key) not in (None, "")},
            "semantic": semantic,
            "evidence": {key: runtime[key] for key in RUNTIME_EVIDENCE_KEYS if runtime.get(key) is not None},
        }
    )
    write_event(journal_home, event)
    return f"logged {event['event_id']}"


def journal_task_completed(
    journal_home: str | Path | None = None,
    agent: str = "unknown",
    task_id: str | None = None,
    note: str = "",
    session_id: str | None = None,
) -> str:
    task_text = _clean_text(task_id)
    note_text = _clean_text(note)
    if not task_text and not note_text:
        return SKIPPED_TASK_OR_NOTE_REQUIRED
    semantic = {"status": "completed_claimed"}
    if task_text:
        semantic["task_id"] = task_text
    if note_text:
        semantic["note"] = note_text
    event = normalize_event(
        {
            "event_type": TASK_COMPLETED_CLAIM_EVENT_TYPE,
            "agent": agent,
            **_event_context(session_id),
            "semantic": semantic,
        }
    )
    write_event(journal_home, event)
    return "logged"


def journal_session_summary(
    journal_home: str | Path | None = None,
    agent: str = "unknown",
    session_id: str | None = None,
    task_id: str | None = None,
    summary: str = "",
    outcome: str = "unknown",
) -> str:
    summary_text = _clean_text(summary)
    if not summary_text:
        return SKIPPED_SUMMARY_REQUIRED
    semantic = {"summary": summary_text, "outcome": _clean_text(outcome) or "unknown"}
    task_text = _clean_text(task_id)
    if task_text:
        semantic["task_id"] = task_text
    event = normalize_event(
        {
            "event_type": SESSION_SUMMARY_EVENT_TYPE,
            "agent": agent,
            **_event_context(session_id),
            "semantic": semantic,
        }
    )
    write_event(journal_home, event)
    return "logged"


def _optional_int(value: int | None) -> int | None:
    return value if isinstance(value, int) else None


def journal_model_operation(
    journal_home: str | Path | None = None,
    agent: str = "unknown",
    session_id: str | None = None,
    provider: str = "",
    model: str = "",
    operation: str = "",
    source: str = "",
    status: str = "",
    duration_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    error_code: str = "",
) -> str:
    semantic = {
        key: value
        for key, value in {
            "provider": _clean_text(provider),
            "model": _clean_text(model),
            "operation": _clean_text(operation),
            "source": _clean_text(source),
            "status": _clean_text(status),
        }.items()
        if value
    }
    token_usage = {
        key: value
        for key, value in {
            "input_tokens": _optional_int(input_tokens),
            "output_tokens": _optional_int(output_tokens),
            "cached_input_tokens": _optional_int(cached_input_tokens),
            "cache_creation_input_tokens": _optional_int(cache_creation_input_tokens),
            "reasoning_tokens": _optional_int(reasoning_tokens),
        }.items()
        if value is not None
    }
    evidence = {}
    if token_usage:
        evidence["token_usage"] = token_usage
    error_text = _clean_text(error_code)
    if error_text:
        evidence["error_code"] = error_text
    if not semantic and not evidence and duration_ms is None:
        return SKIPPED_MODEL_OPERATION_METADATA_REQUIRED
    event = normalize_event(
        {
            "event_type": MODEL_OPERATION_EVENT_TYPE,
            "agent": agent,
            **_event_context(session_id),
            "duration_ms": duration_ms,
            "semantic": semantic,
            "evidence": evidence,
        }
    )
    write_event(journal_home, event)
    return "logged"


def journal_task_blocked(
    journal_home: str | Path | None = None,
    agent: str = "unknown",
    task_id: str | None = None,
    reason: str = "",
    session_id: str | None = None,
) -> str:
    reason_text = _clean_text(reason)
    if not reason_text:
        return SKIPPED_REASON_REQUIRED
    semantic = {"status": "blocked", "reason": reason_text}
    task_text = _clean_text(task_id)
    if task_text:
        semantic["task_id"] = task_text
    event = normalize_event(
        {
            "event_type": TASK_BLOCKED_EVENT_TYPE,
            "agent": agent,
            **_event_context(session_id),
            "semantic": semantic,
        }
    )
    write_event(journal_home, event)
    return "logged"


def journal_daily_report(journal_home: str | Path | None = None, date: str | None = None) -> str:
    # Import lazily so the lightweight semantic write helpers stay dependency-free.
    from agentic_journal.config import journal_root, secure_dir, secure_file
    from agentic_journal.report import render_daily_report

    root = Path(journal_home).expanduser() if journal_home else journal_root()
    report_date, markdown, _ = render_daily_report(root, date)
    report_dir = secure_dir(root / "reports")
    path = report_dir / f"{report_date}.md"
    path.write_text(markdown, encoding="utf-8")
    secure_file(path)
    return f"report: {path}"


def enabled_tool_names(available: Sequence[str]) -> set[str]:
    """Pick the tools to publish from ``[mcp] tools`` in the journal config.

    Without the key every tool is published. A value that is not a list of
    names is ignored with a warning, and unknown names are reported and skipped.
    """
    configured = load_config(journal_root()).get("mcp", {}).get("tools")
    if configured is None:
        return set(available)
    if not isinstance(configured, list) or not all(isinstance(name, str) for name in configured):
        print("agentic-journal-mcp: ignoring [mcp] tools: expected a list of tool names", file=sys.stderr)
        return set(available)
    unknown = sorted(set(configured) - set(available))
    if unknown:
        print(f"agentic-journal-mcp: ignoring unknown [mcp] tools: {', '.join(unknown)}", file=sys.stderr)
    return set(configured) & set(available)


def create_mcp_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("The 'mcp' package is required to run agentic-journal-mcp") from exc

    from mcp.types import ToolAnnotations

    server = FastMCP("agentic-journal")

    if os.environ.get("AGENTIC_JOURNAL_REQUIRE_HOOK") == "1":
        from mcp.server.fastmcp.exceptions import ToolError

        from agentic_journal.note_bridge import consume_receipt

        def journal_note_tool(note: str, category: str = "") -> str:
            """Record a concise work note through the client hook.

            Use `scope` for task boundaries, `finding` for research results,
            `decision` for choices, `action` for performed work, and `correction`
            for revised claims. Record a significant `hypothesis` before testing
            it, then `verification` with the outcome and limits; routine checks
            need no invented hypothesis. Use `checkpoint` for a substantial
            stage's result, actual state, evidence limits, and open next steps.
            It may include the final verification without a duplicate note.
            Pass only note and category; the hook adds metadata.
            """
            if not consume_receipt(note, category):
                raise ToolError("journal hook did not confirm this call; check the journal before retrying")
            return ""  # Keep event ids and runtime metadata out of the chat.
    else:
        def journal_note_tool(
            note: str,
            category: str = "",
            agent: str = "unknown",
            session_id: str = "",
            runtime: dict[str, Any] | None = None,
        ) -> str:
            """Record a semantic note; hooks supply runtime, agents leave it empty."""
            return journal_note(
                agent=agent,
                note=note,
                session_id=session_id or None,
                category=category,
                runtime=runtime,
            )

    def journal_session_summary_tool(
        agent: str = "unknown",
        session_id: str = "",
        task_id: str = "",
        summary: str = "",
        outcome: str = "unknown",
    ) -> str:
        return journal_session_summary(
            agent=agent,
            session_id=session_id or None,
            task_id=task_id or None,
            summary=summary,
            outcome=outcome,
        )

    def journal_task_completed_tool(
        agent: str = "unknown",
        task_id: str = "",
        note: str = "",
        session_id: str = "",
    ) -> str:
        return journal_task_completed(agent=agent, task_id=task_id, note=note, session_id=session_id or None)

    def journal_task_blocked_tool(
        agent: str = "unknown",
        task_id: str = "",
        reason: str = "",
        session_id: str = "",
    ) -> str:
        return journal_task_blocked(agent=agent, task_id=task_id, reason=reason, session_id=session_id or None)

    def journal_model_operation_tool(
        agent: str = "unknown",
        session_id: str = "",
        provider: str = "",
        model: str = "",
        operation: str = "",
        source: str = "",
        status: str = "",
        duration_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        cache_creation_input_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        error_code: str = "",
    ) -> str:
        return journal_model_operation(
            agent=agent,
            session_id=session_id or None,
            provider=provider,
            model=model,
            operation=operation,
            source=source,
            status=status,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            reasoning_tokens=reasoning_tokens,
            error_code=error_code,
        )

    def journal_daily_report_tool(date: str = "") -> str:
        return journal_daily_report(date=date or None)

    tools = [
        (
            "journal_note",
            journal_note_tool,
            ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
        ),
        ("journal_session_summary", journal_session_summary_tool, None),
        ("journal_task_completed", journal_task_completed_tool, None),
        ("journal_task_blocked", journal_task_blocked_tool, None),
        ("journal_model_operation", journal_model_operation_tool, None),
        ("journal_daily_report", journal_daily_report_tool, None),
    ]
    enabled = enabled_tool_names([name for name, _, _ in tools])
    for name, function, annotations in tools:
        if name in enabled:
            server.add_tool(function, name=name, annotations=annotations)
    return server


def main() -> None:
    create_mcp_server().run()
