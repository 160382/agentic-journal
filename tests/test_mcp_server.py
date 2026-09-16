import asyncio
import json
import subprocess

import pytest

import agentic_journal.mcp_server as mcp_server
from agentic_journal.mcp_server import (
    create_mcp_server,
    journal_note,
    journal_session_summary,
    journal_task_blocked,
    journal_task_completed,
)
from agentic_journal.storage import read_events_for_date


def _init_git_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "tracked.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "add tracked"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def test_journal_note_writes_semantic_note(tmp_path):
    result = journal_note(journal_home=tmp_path, agent="codex", note="Investigated TASK-1")

    events = read_events_for_date(tmp_path, None)
    assert result == f"logged {events[0]['event_id']}"
    assert events[0]["event_type"] == "semantic_note"
    assert events[0]["semantic"]["note"] == "Investigated TASK-1"


def test_journal_note_accepts_hook_owned_session_identity(tmp_path):
    journal_note(
        journal_home=tmp_path,
        note="Investigated",
        session_id="s1",
        runtime={
            "client": "codex",
            "session_name": "Logging engine",
            "session_name_source": "codex-thread",
        },
    )

    [event] = read_events_for_date(tmp_path, None)
    assert event["session_name"] == "Logging engine"
    assert event["session_name_source"] == "codex-thread"


def test_journal_note_skips_blank_note(tmp_path):
    result = journal_note(journal_home=tmp_path, agent="codex", note="   ")

    assert result == "skipped: note is required"
    assert read_events_for_date(tmp_path, None) == []


def test_journal_task_completed_writes_claim(tmp_path):
    result = journal_task_completed(journal_home=tmp_path, agent="claude", task_id="TASK-2", note="Done")

    assert result == "logged"
    events = read_events_for_date(tmp_path, None)
    assert events[0]["event_type"] == "task_completed_claim"
    assert events[0]["semantic"]["task_id"] == "TASK-2"


def test_journal_task_completed_skips_without_task_or_note(tmp_path):
    result = journal_task_completed(journal_home=tmp_path, agent="claude", task_id="", note="  ")

    assert result == "skipped: task_id or note is required"
    assert read_events_for_date(tmp_path, None) == []


def test_journal_task_blocked_writes_blocked_event(tmp_path):
    result = journal_task_blocked(journal_home=tmp_path, agent="gemini", task_id="TASK-3", reason="Missing key")

    assert result == "logged"
    events = read_events_for_date(tmp_path, None)
    assert events[0]["event_type"] == "task_blocked"
    assert events[0]["semantic"]["status"] == "blocked"


def test_journal_task_blocked_skips_blank_reason(tmp_path):
    result = journal_task_blocked(journal_home=tmp_path, agent="gemini", task_id="TASK-3", reason=" ")

    assert result == "skipped: reason is required"
    assert read_events_for_date(tmp_path, None) == []


def test_journal_session_summary_writes_outcome_event(tmp_path):
    result = journal_session_summary(
        journal_home=tmp_path,
        agent="codex",
        session_id="session-1",
        task_id="TASK-8",
        summary="Implemented session summary logging",
        outcome="completed",
    )

    assert result == "logged"
    events = read_events_for_date(tmp_path, None)
    assert events[0]["event_type"] == "session_summary"
    assert events[0]["session_id"] == "session-1"
    assert events[0]["semantic"]["task_id"] == "TASK-8"
    assert events[0]["semantic"]["summary"] == "Implemented session summary logging"
    assert events[0]["semantic"]["outcome"] == "completed"


def test_journal_session_summary_skips_blank_summary(tmp_path):
    result = journal_session_summary(
        journal_home=tmp_path,
        agent="codex",
        session_id="session-1",
        summary=" ",
        outcome="completed",
    )

    assert result == "skipped: summary is required"
    assert read_events_for_date(tmp_path, None) == []


def test_journal_model_operation_writes_activity_event(tmp_path):
    result = mcp_server.journal_model_operation(
        journal_home=tmp_path,
        agent="cortex",
        session_id="chat-1",
        provider="claude",
        model="claude-opus-4-8-thinking-high",
        operation="chat",
        source="/api/chat",
        status="completed",
        duration_ms=1234,
        input_tokens=1200,
        output_tokens=340,
        cached_input_tokens=100,
        reasoning_tokens=50,
        error_code="rate_limit",
    )

    assert result == "logged"
    event = read_events_for_date(tmp_path, None)[0]
    assert event["event_type"] == "model_operation"
    assert event["semantic"] == {
        "provider": "claude",
        "model": "claude-opus-4-8-thinking-high",
        "operation": "chat",
        "source": "/api/chat",
        "status": "completed",
    }
    assert event["evidence"]["token_usage"]["input_tokens"] == 1200
    assert event["evidence"]["error_code"] == "rate_limit"


def test_journal_model_operation_skips_without_metadata(tmp_path):
    result = mcp_server.journal_model_operation(journal_home=tmp_path, agent="cortex")

    assert result == "skipped: model operation metadata is required"
    assert read_events_for_date(tmp_path, None) == []


def test_mcp_tools_attach_session_and_git_context(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    head = _init_git_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AGENTIC_JOURNAL_SESSION_ID", "session-env")

    journal_note(journal_home=tmp_path / "journal", agent="codex", note="Investigated")
    journal_task_completed(journal_home=tmp_path / "journal", agent="claude", task_id="TASK-2", note="Done")
    journal_task_blocked(journal_home=tmp_path / "journal", agent="gemini", task_id="TASK-3", reason="Missing key")
    journal_session_summary(
        journal_home=tmp_path / "journal",
        agent="codex",
        task_id="TASK-8",
        summary="Implemented session summary logging",
        outcome="completed",
    )
    mcp_server.journal_model_operation(
        journal_home=tmp_path / "journal",
        agent="cortex",
        provider="claude",
        model="claude-opus-4-8-thinking-high",
        operation="chat",
        status="completed",
    )

    events = read_events_for_date(tmp_path / "journal", None)
    assert [event["session_id"] for event in events] == ["session-env"] * 5
    assert {event["repo"] for event in events} == {str(repo)}
    assert {event["branch"] for event in events} == {"main"}
    assert {event["commit"] for event in events} == {head}
    assert {event["cwd"] for event in events} == {str(repo)}


def test_mcp_tools_accept_legacy_session_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_JOURNAL_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENT_JOURNAL_SESSION_ID", "legacy-session")

    journal_note(journal_home=tmp_path, agent="codex", note="legacy session")

    event = read_events_for_date(tmp_path, None)[0]
    assert event["session_id"] == "legacy-session"


def test_create_mcp_server_has_expected_name_or_clear_dependency_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    try:
        server = create_mcp_server()
    except RuntimeError as exc:
        assert "mcp" in str(exc).lower()
    else:
        assert getattr(server, "name", None) == "agentic-journal"


def test_create_mcp_server_registers_expected_tool_names(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    try:
        server = create_mcp_server()
    except RuntimeError:
        return

    assert {
        "journal_note",
        "journal_session_summary",
        "journal_task_completed",
        "journal_task_blocked",
        "journal_model_operation",
        "journal_daily_report",
    }.issubset(set(server._tool_manager._tools))


def _full_runtime(cwd):
    return {
        "client": "claude",
        "agent_id": "a3b0",
        "agent_type": "general-purpose",
        "turn_id": "prompt-1",
        "cwd": str(cwd),
        "model": "claude-opus-5",
        "permission_mode": "plan",
        "collaboration_mode": "plan",
        "effort": "xhigh",
        "usage_scope": "turn_to_date",
        "usage_status": "partial",
        "stats_error": "transcript unavailable",
        "token_usage": {
            "input_tokens": 12,
            "output_tokens": 3410,
            "cache_creation_input_tokens": 20411,
            "cache_read_input_tokens": 381220,
            "cache_write_input_tokens": 7,
            "reasoning_output_tokens": 5,
            "total_tokens": 405055,
        },
        "turn_elapsed_ms": 84120,
        "native_session_id": "native-1",
        "tool_use_id": "toolu_1",
        "injected_by": "journal_hook",
        "unexpected": "dropped-value",
    }


def test_journal_note_places_runtime_metadata(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    head = _init_git_repo(repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    runtime = _full_runtime(repo)

    result = journal_note(
        journal_home=tmp_path / "journal",
        agent="unknown",
        note="  Found that reviewer tools are fixed.  ",
        session_id="track-1",
        category="observation",
        runtime=runtime,
    )

    [event] = read_events_for_date(tmp_path / "journal", None)
    assert result == f"logged {event['event_id']}"
    assert (event["agent"], event["session_id"]) == ("claude", "track-1")
    assert (event["agent_id"], event["agent_type"], event["turn_id"]) == ("a3b0", "general-purpose", "prompt-1")
    assert (event["cwd"], event["repo"], event["branch"], event["commit"]) == (str(repo), str(repo), "main", head)
    assert event["semantic"] == {"note": "Found that reviewer tools are fixed.", "category": "observation"}
    assert event["evidence"] == {key: runtime[key] for key in mcp_server.RUNTIME_EVIDENCE_KEYS}
    assert "dropped-value" not in json.dumps(event)


def test_journal_note_uses_runtime_cwd_verbatim(tmp_path, monkeypatch):
    repo = tmp_path / " repo with edge spaces "
    head = _init_git_repo(repo)
    monkeypatch.chdir(tmp_path)

    journal_note(journal_home=tmp_path / "journal", agent="codex", note="n", runtime={"cwd": str(repo)})

    [event] = read_events_for_date(tmp_path / "journal", None)
    assert (event["cwd"], event["repo"], event["commit"]) == (str(repo), str(repo), head)


def test_journal_note_truncates_long_category(tmp_path):
    journal_note(journal_home=tmp_path, agent="codex", note="n", category="  " + "c" * 65 + "  ")

    [event] = read_events_for_date(tmp_path, None)
    assert event["semantic"]["category"] == "c" * 64


def test_journal_note_without_runtime_keeps_plain_event(tmp_path):
    journal_note(journal_home=tmp_path, agent="codex", note="n", category="   ")

    [event] = read_events_for_date(tmp_path, None)
    assert event["agent"] == "codex"
    assert event["semantic"] == {"note": "n"}
    assert event["evidence"] == {}
    assert not {"agent_id", "agent_type", "turn_id"} & set(event)


def test_journal_note_tool_schema_and_annotations(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    server = create_mcp_server()
    tool = {tool.name: tool for tool in asyncio.run(server.list_tools())}["journal_note"]

    assert tool.inputSchema["required"] == ["note"]
    assert {"note", "category", "agent", "session_id", "runtime"} <= set(tool.inputSchema["properties"])
    annotations = tool.annotations
    assert (annotations.readOnlyHint, annotations.destructiveHint) == (False, False)
    assert (annotations.idempotentHint, annotations.openWorldHint) == (False, False)


def test_journal_note_tool_passes_category_and_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    server = create_mcp_server()

    asyncio.run(
        server.call_tool(
            "journal_note",
            {"note": "Ran tests.", "category": "action", "session_id": "track-2", "runtime": {"client": "codex", "turn_id": "t-1"}},
        )
    )

    [event] = read_events_for_date(tmp_path, None)
    assert (event["agent"], event["session_id"], event["turn_id"]) == ("codex", "track-2", "t-1")
    assert event["semantic"] == {"note": "Ran tests.", "category": "action"}


def test_journal_note_tool_reports_storage_failure_as_tool_error(tmp_path, monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError

    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mcp_server, "write_event", fail_write)
    server = create_mcp_server()

    with pytest.raises(ToolError, match="disk full"):
        asyncio.run(server.call_tool("journal_note", {"note": "n"}))


ALL_TOOL_NAMES = {
    "journal_note",
    "journal_session_summary",
    "journal_task_completed",
    "journal_task_blocked",
    "journal_model_operation",
    "journal_daily_report",
}


def _server_with_config(tmp_path, monkeypatch, config_text):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(config_text, encoding="utf-8")
    return create_mcp_server()


def test_mcp_tools_config_limits_published_tools(tmp_path, monkeypatch):
    server = _server_with_config(tmp_path, monkeypatch, '[mcp]\ntools = ["journal_note"]\n')

    assert set(server._tool_manager._tools) == {"journal_note"}
    [tool] = asyncio.run(server.list_tools())
    assert tool.annotations.destructiveHint is False


def test_mcp_without_tools_config_publishes_every_tool(tmp_path, monkeypatch):
    server = _server_with_config(tmp_path, monkeypatch, "[privacy]\nlog_prompts = false\n")

    assert set(server._tool_manager._tools) == ALL_TOOL_NAMES


def test_mcp_tools_config_reports_unknown_names(tmp_path, monkeypatch, capsys):
    server = _server_with_config(tmp_path, monkeypatch, '[mcp]\ntools = ["journal_note", "journal_delete"]\n')

    assert set(server._tool_manager._tools) == {"journal_note"}
    assert "unknown [mcp] tools: journal_delete" in capsys.readouterr().err


def test_mcp_tools_config_of_wrong_type_publishes_every_tool(tmp_path, monkeypatch, capsys):
    server = _server_with_config(tmp_path, monkeypatch, '[mcp]\ntools = "journal_note"\n')

    assert set(server._tool_manager._tools) == ALL_TOOL_NAMES
    assert "expected a list of tool names" in capsys.readouterr().err
