"""Hook-owned notes keep runtime in storage and out of MCP arguments."""

import asyncio
import io
import json
import os
import sys
import threading
import time

import pytest

from agentic_journal.cli import main
from agentic_journal.mcp_server import create_mcp_server
from agentic_journal.note_bridge import _ps_client, add_receipt, client_instance, consume_receipt
from agentic_journal.storage import connect, read_events_for_date


class Input:
    def __init__(self, value):
        self.buffer = io.BytesIO(json.dumps(value).encode())


def test_hook_note_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIC_JOURNAL_BRIDGE_INSTANCE", "client-1")
    monkeypatch.setenv("AGENTIC_JOURNAL_REQUIRE_HOOK", "1")
    payload = {"note": "Checked the source. Confirmed the path.", "category": "check",
               "session_id": "s1", "runtime": {"client": "codex", "agent_id": "agent-1",
                                                 "turn_id": "t1", "model": "test-model",
                                                 "token_usage": {"input_tokens": 42}}}
    monkeypatch.setattr(sys, "stdin", Input(payload))
    assert main(["note-bridge"]) == 0
    assert capsys.readouterr().out.startswith("logged ")

    server = create_mcp_server()
    tool = {item.name: item for item in asyncio.run(server.list_tools())}["journal_note"]
    assert set(tool.inputSchema["properties"]) == {"note", "category"}
    result = asyncio.run(server.call_tool("journal_note", {"note": payload["note"],
                                                          "category": "check"}))
    assert result[0][0].text == ""
    [event] = read_events_for_date(tmp_path, None)
    assert event["agent"] == "codex"
    assert (event["session_id"], event["agent_id"], event["turn_id"]) == ("s1", "agent-1", "t1")
    assert event["evidence"]["token_usage"]["input_tokens"] == 42
    assert event["semantic"]["note"] == payload["note"]
    assert not (tmp_path / "note-bridge").exists()
    with connect(tmp_path) as conn:
        assert conn.execute("SELECT count(*) FROM note_receipts").fetchone()[0] == 0


def test_missing_hook_fails_without_plain_event(tmp_path, monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError

    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIC_JOURNAL_BRIDGE_INSTANCE", "client-1")
    monkeypatch.setenv("AGENTIC_JOURNAL_REQUIRE_HOOK", "1")
    server = create_mcp_server()
    with pytest.raises(ToolError, match="hook did not confirm"):
        asyncio.run(server.call_tool("journal_note", {"note": "No hook"}))
    assert read_events_for_date(tmp_path, None) == []


def test_duplicate_text_receipts_are_one_use_and_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIC_JOURNAL_BRIDGE_INSTANCE", "client-1")
    first = client_instance()
    add_receipt(first, "same", "check", "event-1")
    add_receipt(first, "same", "check", "event-2")
    results = []
    threads = [threading.Thread(target=lambda: results.append(consume_receipt("same", "check")))
               for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True, True]

    monkeypatch.setenv("AGENTIC_JOURNAL_BRIDGE_INSTANCE", "client-2")
    assert not consume_receipt("same", "check")


def test_bridge_rejects_unidentified_client_before_write(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTIC_JOURNAL_BRIDGE_INSTANCE", raising=False)
    monkeypatch.setattr("agentic_journal.note_bridge.client_instance", lambda: (_ for _ in ()).throw(
        RuntimeError("no client")))
    monkeypatch.setattr(sys, "stdin", Input({"note": "n", "session_id": "s1",
                                            "runtime": {"client": "codex"}}))
    assert main(["note-bridge"]) == 1
    assert read_events_for_date(tmp_path, None) == []


@pytest.mark.parametrize("payload", [
    {"note": "", "session_id": "s1", "runtime": {"client": "codex"}},
    {"note": "valid", "runtime": {"client": "codex"}},
    {"note": "valid", "session_id": "s1", "runtime": {"client": "other"}},
])
def test_bridge_rejects_invalid_input_without_write(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIC_JOURNAL_BRIDGE_INSTANCE", "client-1")
    monkeypatch.setattr(sys, "stdin", Input(payload))
    assert main(["note-bridge"]) == 2
    assert read_events_for_date(tmp_path, None) == []


def test_two_same_notes_from_hooks_confirm_independently(tmp_path, monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError

    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIC_JOURNAL_BRIDGE_INSTANCE", "client-1")
    monkeypatch.setenv("AGENTIC_JOURNAL_REQUIRE_HOOK", "1")
    payload = {"note": "same", "category": "check", "session_id": "s1",
               "runtime": {"client": "codex"}}
    for _ in range(2):
        monkeypatch.setattr(sys, "stdin", Input(payload))
        assert main(["note-bridge"]) == 0
    server = create_mcp_server()
    for _ in range(2):
        result = asyncio.run(server.call_tool("journal_note", {"note": "same", "category": "check"}))
        assert result[0][0].text == ""
    with pytest.raises(ToolError, match="did not confirm"):
        asyncio.run(server.call_tool("journal_note", {"note": "same", "category": "check"}))
    assert not consume_receipt("same", "check")


def test_expired_sqlite_receipts_are_purged(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIC_JOURNAL_BRIDGE_INSTANCE", "client-1")
    instance = client_instance()
    add_receipt(instance, "old", "check", "event-old")
    with connect(tmp_path) as conn:
        conn.execute("UPDATE note_receipts SET created = ?", (time.time() - 31,))

    assert not consume_receipt("old", "check")
    with connect(tmp_path) as conn:
        assert conn.execute("SELECT count(*) FROM note_receipts").fetchone()[0] == 0


def test_legacy_receipt_files_are_collected_after_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    legacy = tmp_path / "note-bridge"
    legacy.mkdir()
    instance = "a" * 32
    for suffix, content in (("json", "[]"), ("lock", "")):
        path = legacy / f"{instance}.{suffix}"
        path.write_text(content, encoding="utf-8")
        os.utime(path, (time.time() - 31, time.time() - 31))

    add_receipt("new-instance", "new", "check", "event-new")

    assert not legacy.exists()


def test_posix_ps_fallback_finds_same_client_for_children(monkeypatch):
    class ProcessTable:
        stdout = ("80 1 /Applications/Claude Code/claude Mon Sep 14 10:00:00 2026\n"
                  "101 80 python3 Mon Sep 14 10:01:00 2026\n"
                  "102 80 python3 Mon Sep 14 10:02:00 2026\n")

    def fake_ps(command, **kwargs):
        assert command[:2] == ["/bin/ps", "-A"]
        assert kwargs["env"] == {"LC_ALL": "C", "TZ": "UTC"}
        return ProcessTable()

    monkeypatch.setattr("agentic_journal.note_bridge.subprocess.run", fake_ps)
    assert _ps_client(101) == _ps_client(102)
