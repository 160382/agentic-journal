"""Hook-owned notes keep runtime in storage and out of MCP arguments."""

import asyncio
import io
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from agentic_journal import note_bridge
from agentic_journal.cli import main
from agentic_journal.mcp_server import create_mcp_server
from agentic_journal.note_bridge import _ps_client, add_receipt, client_instance, consume_receipt
from agentic_journal.storage import connect, init_db, read_events_for_date


def _stored_event(root, note="stored"):
    from agentic_journal.mcp_server import journal_note

    return journal_note(journal_home=root, agent="codex", note=note).removeprefix("logged ")


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
    [event] = read_events_for_date(tmp_path, None)
    assert re.fullmatch(r"journal ✓ · check · seq 1 · \d+ ms", result[0][0].text)
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
    add_receipt(first, "same", "check", _stored_event(tmp_path, "one"))
    add_receipt(first, "same", "check", _stored_event(tmp_path, "two"))
    results = []
    threads = [threading.Thread(target=lambda: results.append(consume_receipt("same", "check")))
               for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(result is not None for result in results) == [False, True, True]

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
    texts = [asyncio.run(server.call_tool("journal_note", {"note": "same", "category": "check"}))[0][0].text
             for _ in range(2)]
    assert [re.sub(r" · \d+ ms$", "", text) for text in texts] == ["journal ✓ · check · seq 1",
                                                                  "journal ✓ · check · seq 2"]
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


def test_receipt_expiry_is_checked_after_write_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIC_JOURNAL_BRIDGE_INSTANCE", "client-1")
    instance = client_instance()
    add_receipt(instance, "old", "check", "event-old")
    with connect(tmp_path) as conn:
        conn.execute("UPDATE note_receipts SET created = 70")

    original_transaction = note_bridge._immediate_transaction
    state = {"locked": False}

    @contextmanager
    def tracked_transaction(conn):
        with original_transaction(conn):
            state["locked"] = True
            yield

    def locked_time():
        assert state["locked"]
        return 101

    monkeypatch.setattr(note_bridge, "_immediate_transaction", tracked_transaction)
    monkeypatch.setattr(note_bridge.time, "time", locked_time)

    assert not consume_receipt("old", "check")


def test_legacy_cleanup_errors_do_not_affect_sqlite_receipts(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIC_JOURNAL_BRIDGE_INSTANCE", "client-1")
    (tmp_path / "note-bridge").write_text("not a directory", encoding="utf-8")
    instance = client_instance()

    add_receipt(instance, "note", "check", _stored_event(tmp_path))

    assert consume_receipt("note", "check")
    assert not consume_receipt("note", "check")


def test_legacy_entry_stat_errors_do_not_affect_sqlite_receipts(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIC_JOURNAL_BRIDGE_INSTANCE", "client-1")
    legacy = tmp_path / "note-bridge"
    legacy.mkdir()
    (legacy / f"{'a' * 32}.json").write_text("[]", encoding="utf-8")
    real_is_file = Path.is_file

    def fail_legacy_stat(path):
        if path.parent == legacy:
            raise PermissionError("legacy entry is unreadable")
        return real_is_file(path)

    monkeypatch.setattr(Path, "is_file", fail_legacy_stat)
    instance = client_instance()

    add_receipt(instance, "note", "check", _stored_event(tmp_path))

    assert consume_receipt("note", "check")
    assert not consume_receipt("note", "check")


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


@pytest.mark.parametrize("client", ["agy", "opencode"])
def test_posix_ps_fallback_finds_agy_and_opencode_clients(monkeypatch, client):
    class ProcessTable:
        stdout = (f"80 1 /usr/bin/{client} Mon Sep 14 10:00:00 2026\n"
                  "101 80 python3 Mon Sep 14 10:01:00 2026\n"
                  "102 80 agentic-journal-mcp Mon Sep 14 10:02:00 2026\n"
                  "90 1 /usr/bin/other Mon Sep 14 10:03:00 2026\n"
                  "103 90 python3 Mon Sep 14 10:04:00 2026\n")

    monkeypatch.setattr("agentic_journal.note_bridge.subprocess.run", lambda command, **kwargs: ProcessTable())
    assert _ps_client(101) == _ps_client(102)
    with pytest.raises(RuntimeError, match="cannot identify"):
        _ps_client(103)


def _fake_proc_stat(monkeypatch, table):
    """Serve /proc/<pid>/stat lines for a fake process table of pid -> (comm, ppid, start)."""
    real_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        parts = path.parts
        if len(parts) == 4 and parts[:2] == ("/", "proc") and parts[3] == "stat":
            comm, ppid, start = table[int(parts[2])]
            fields = ["S", str(ppid), *["0"] * 17, str(start), "0", "0"]
            return f"{parts[2]} ({comm}) " + " ".join(fields) + "\n"
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


@pytest.mark.parametrize("client", ["agy", "opencode"])
def test_proc_client_resolves_agy_and_opencode_ancestor_by_comm(monkeypatch, client):
    _fake_proc_stat(monkeypatch, {
        1: ("systemd", 0, 1),
        80: (client, 1, 5000),
        101: ("bash", 80, 5100),
        102: ("python3", 101, 5101),
        103: ("agentic-journal", 102, 5102),
        201: ("agentic-journal", 80, 5200),
        90: (client, 1, 6000),
        301: ("python3", 90, 6100),
        400: ("other", 1, 7000),
        401: ("python3", 400, 7100),
    })

    hook_side = note_bridge._proc_client(103)
    assert hook_side == note_bridge._proc_client(201)
    assert hook_side != note_bridge._proc_client(301)
    with pytest.raises(RuntimeError, match="cannot identify"):
        note_bridge._proc_client(401)


@pytest.mark.parametrize("client", ["agy", "opencode"])
def test_agy_and_opencode_hook_notes_are_queued_and_consumed(hook_profile, monkeypatch, client):
    payload = {"note": f"Note from {client}", "category": "check", "session_id": f"{client}-session",
               "runtime": {"client": client, "turn_id": "t1"}}
    monkeypatch.setattr(sys, "stdin", Input(payload))
    assert main(["note-bridge"]) == 0

    [event] = read_events_for_date(hook_profile, None)
    assert event["agent"] == client
    assert event["session_id"] == f"{client}-session"
    assert event["semantic"]["note"] == payload["note"]
    with connect(hook_profile) as conn:
        assert conn.execute("SELECT count(*) FROM note_receipts").fetchone()[0] == 1

    confirmation = consume_receipt(payload["note"], "check")
    assert confirmation is not None
    assert re.sub(r" · \d+ ms$", "", confirmation.render()) == "journal ✓ · check · seq 1"
    assert consume_receipt(payload["note"], "check") is None


def _bridge_and_confirm(monkeypatch, note, category, **runtime):
    monkeypatch.setattr(sys, "stdin", Input({"note": note, "category": category, "session_id": "session-7f3a",
                                            "runtime": {"client": "claude", **runtime}}))
    assert main(["note-bridge"]) == 0
    server = create_mcp_server()
    return asyncio.run(server.call_tool("journal_note", {"note": note, "category": category}))[0][0].text


@pytest.fixture
def hook_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIC_JOURNAL_BRIDGE_INSTANCE", "client-1")
    monkeypatch.setenv("AGENTIC_JOURNAL_REQUIRE_HOOK", "1")
    return tmp_path


def test_confirmation_shows_no_note_ids_or_runtime(hook_profile, monkeypatch):
    note = "Нашёл причину: /home/user/secret-project падает на API_KEY=abc"
    text = _bridge_and_confirm(monkeypatch, note, "finding", agent_id="agent-9c1", turn_id="turn-55",
                               cwd="/home/user/secret-project", model="model-x",
                               token_usage={"input_tokens": 7})

    [event] = read_events_for_date(hook_profile, None)
    assert re.fullmatch(r"journal ✓ · finding · seq 1 · \d+ ms", text)
    for leaked in ("Нашёл", "secret-project", event["event_id"], "session-7f3a", "agent-9c1", "turn-55",
                   "model-x", "claude", "subagent", "main"):
        assert leaked not in text


@pytest.mark.parametrize(("category", "shown"), [
    ("", "uncategorized"),
    ("   ", "uncategorized"),
    ("проверка-гипотезы", "проверка-гипотезы"),
    ("two\nlines\t here", "two lines here"),
    ("x" * 80, "x" * 64),
    ("API_KEY=abc123", "API_KEY=[REDACTED]"),
])
def test_confirmation_shows_stored_category_on_one_line(hook_profile, monkeypatch, category, shown):
    text = _bridge_and_confirm(monkeypatch, "note", category)

    assert text.split(" · ")[:2] == ["journal ✓", shown]
    assert "\n" not in text


def test_confirmation_latency_runs_from_event_time_to_consumption(hook_profile, monkeypatch):
    monkeypatch.setattr(sys, "stdin", Input({"note": "timed", "category": "action", "session_id": "s1",
                                            "runtime": {"client": "codex"}}))
    assert main(["note-bridge"]) == 0
    [event] = read_events_for_date(hook_profile, None)
    started = datetime.fromisoformat(event["ts"]).timestamp()
    monkeypatch.setattr(note_bridge.time, "time", lambda: started + 0.0234)

    confirmation = consume_receipt("timed", "action")

    assert confirmation.render() == "journal ✓ · action · seq 1 · 23 ms"


def test_confirmation_latency_is_never_negative(hook_profile, monkeypatch):
    monkeypatch.setattr(sys, "stdin", Input({"note": "skew", "session_id": "s1", "runtime": {"client": "codex"}}))
    assert main(["note-bridge"]) == 0
    [event] = read_events_for_date(hook_profile, None)
    monkeypatch.setattr(note_bridge.time, "time", lambda: datetime.fromisoformat(event["ts"]).timestamp() - 5)

    assert consume_receipt("skew", "").render() == "journal ✓ · uncategorized · seq 1 · 0 ms"


def test_receipt_without_stored_event_does_not_confirm(hook_profile):
    from mcp.server.fastmcp.exceptions import ToolError

    add_receipt(client_instance(), "orphan", "check", "missing-event")

    with pytest.raises(ToolError, match="did not confirm"):
        asyncio.run(create_mcp_server().call_tool("journal_note", {"note": "orphan", "category": "check"}))
    assert consume_receipt("orphan", "check") is None


def test_confirmation_latency_covers_git_context_collection(hook_profile, monkeypatch):
    def slow_git_context(cwd, **kwargs):
        time.sleep(0.2)
        return {"cwd": str(cwd)}

    monkeypatch.setattr("agentic_journal.mcp_server.event_context", slow_git_context)

    text = _bridge_and_confirm(monkeypatch, "slow repo", "action")

    assert int(re.fullmatch(r"journal ✓ · action · seq 1 · (\d+) ms", text).group(1)) >= 200


def test_bridge_storage_failure_leaves_no_receipt(hook_profile, monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("agentic_journal.mcp_server.write_event", fail_write)
    monkeypatch.setattr(sys, "stdin", Input({"note": "lost", "session_id": "s1", "runtime": {"client": "codex"}}))

    assert main(["note-bridge"]) == 1
    init_db(hook_profile)
    with connect(hook_profile) as conn:
        assert conn.execute("SELECT count(*) FROM note_receipts").fetchone()[0] == 0
    with pytest.raises(ToolError, match="did not confirm"):
        asyncio.run(create_mcp_server().call_tool("journal_note", {"note": "lost"}))
