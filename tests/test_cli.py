import json
import os
import subprocess
import sys
from pathlib import Path

from agentic_journal.cli import main
from agentic_journal.install import generate_wrapper_script
from agentic_journal.mcp_server import journal_task_blocked, journal_task_completed
from agentic_journal.storage import read_events_for_date, write_event


def test_main_help_exits_cleanly(capsys):
    exit_code = main(["--help"])

    assert exit_code == 0
    assert "agentic-journal" in capsys.readouterr().out


def test_event_command_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))

    exit_code = main(["event", "--type", "agent_start", "--agent", "codex"])

    assert exit_code == 0
    event_file = tmp_path / "events" / "2026-05-31.jsonl"
    if not event_file.exists():
        event_file = next((tmp_path / "events").glob("*.jsonl"))
    event = json.loads(event_file.read_text().splitlines()[0])
    assert event["event_type"] == "agent_start"
    assert event["agent"] == "codex"
    assert (tmp_path / "config.toml").exists()


def test_event_command_accepts_semantic_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))

    exit_code = main(
        [
            "event",
            "--type",
            "task_completed_claim",
            "--agent",
            "gemini",
            "--task",
            "TASK-1",
            "--note",
            "Smoke test completed",
        ]
    )

    assert exit_code == 0
    event_file = next((tmp_path / "events").glob("*.jsonl"))
    event = json.loads(event_file.read_text().splitlines()[0])
    assert event["semantic"]["task_id"] == "TASK-1"
    assert event["semantic"]["note"] == "Smoke test completed"


def test_event_command_writes_session_summary_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))

    exit_code = main(
        [
            "event",
            "--type",
            "session_summary",
            "--agent",
            "codex",
            "--session-id",
            "session-1",
            "--task",
            "TASK-8",
            "--summary",
            "Implemented session summary logging",
            "--outcome",
            "completed",
        ]
    )

    assert exit_code == 0
    event_file = next((tmp_path / "events").glob("*.jsonl"))
    event = json.loads(event_file.read_text().splitlines()[0])
    assert event["event_type"] == "session_summary"
    assert event["session_id"] == "session-1"
    assert event["semantic"]["task_id"] == "TASK-8"
    assert event["semantic"]["summary"] == "Implemented session summary logging"
    assert event["semantic"]["outcome"] == "completed"


def test_event_command_writes_model_operation_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))

    exit_code = main(
        [
            "event",
            "--type",
            "model_operation",
            "--agent",
            "cortex",
            "--session-id",
            "chat-1",
            "--provider",
            "claude",
            "--model",
            "claude-opus-4-8-thinking-high",
            "--operation",
            "chat",
            "--source",
            "/api/chat",
            "--status",
            "completed",
            "--duration-ms",
            "1234",
            "--input-tokens",
            "1200",
            "--output-tokens",
            "340",
            "--cached-input-tokens",
            "100",
            "--reasoning-tokens",
            "50",
            "--error-code",
            "rate_limit",
        ]
    )

    assert exit_code == 0
    event_file = next((tmp_path / "events").glob("*.jsonl"))
    event = json.loads(event_file.read_text().splitlines()[0])
    assert event["event_type"] == "model_operation"
    assert event["agent"] == "cortex"
    assert event["session_id"] == "chat-1"
    assert event["duration_ms"] == 1234
    assert event["semantic"] == {
        "provider": "claude",
        "model": "claude-opus-4-8-thinking-high",
        "operation": "chat",
        "source": "/api/chat",
        "status": "completed",
    }
    assert event["evidence"]["token_usage"] == {
        "input_tokens": 1200,
        "output_tokens": 340,
        "cached_input_tokens": 100,
        "reasoning_tokens": 50,
    }
    assert event["evidence"]["error_code"] == "rate_limit"


def test_status_command_prints_model_activity_count(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    main(
        [
            "event",
            "--type",
            "model_operation",
            "--agent",
            "cortex",
            "--provider",
            "claude",
            "--model",
            "claude-opus-4-8-thinking-high",
            "--operation",
            "workflow.step",
            "--status",
            "completed",
        ]
    )

    exit_code = main(["status", "--today"])

    assert exit_code == 0
    assert "Model activity: 1" in capsys.readouterr().out


def test_report_command_writes_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    main(["event", "--type", "task_completed_claim", "--agent", "codex", "--task", "TASK-1"])

    exit_code = main(["report", "--date", "2026-05-31"])

    assert exit_code == 0
    report_files = list((tmp_path / "reports").glob("*.md"))
    assert report_files
    assert "Completed Claimed" in report_files[0].read_text()


def test_report_command_accepts_explicit_write_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))

    exit_code = main(["report", "--date", "2026-05-31", "--write"])

    assert exit_code == 0
    assert (tmp_path / "reports" / "2026-05-31.md").exists()


def test_status_command_prints_today_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    main(["event", "--type", "agent_start", "--agent", "codex"])

    exit_code = main(["status", "--date", "2026-05-31"])

    assert exit_code == 0
    assert "Raw events:" in capsys.readouterr().out


def test_doctor_command_prints_setup_and_coverage(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    main(["event", "--type", "agent_start", "--agent", "codex", "--session-id", "doctor-1"])
    main(
        [
            "event",
            "--type",
            "session_summary",
            "--agent",
            "codex",
            "--session-id",
            "doctor-1",
            "--summary",
            "Doctor smoke",
            "--outcome",
            "completed",
        ]
    )

    exit_code = main(["doctor", "--date", "2026-05-31"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agentic Journal Doctor" in output
    assert "Session summaries:" in output
    assert "codex:" in output


def test_web_help_exits_cleanly(capsys):
    exit_code = main(["web", "--help"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "--host" in output
    assert "--port" in output
    assert "--date" in output
    assert "--token" in output


def test_web_command_without_explicit_date_uses_dynamic_default(monkeypatch):
    calls = {}

    def fake_run_web_server(root, host, port, default_date, refresh_ms=2000, api_token=None):
        calls["default_date"] = default_date
        calls["refresh_ms"] = refresh_ms

    monkeypatch.setattr("agentic_journal.cli.run_web_server", fake_run_web_server)

    exit_code = main(["web", "--today", "--refresh-ms", "3000"])

    assert exit_code == 0
    assert calls["default_date"] is None
    assert calls["refresh_ms"] == 3000


def test_web_command_with_explicit_date_keeps_that_date(monkeypatch):
    calls = {}

    def fake_run_web_server(root, host, port, default_date, refresh_ms=2000, api_token=None):
        calls["default_date"] = default_date

    monkeypatch.setattr("agentic_journal.cli.run_web_server", fake_run_web_server)

    exit_code = main(["web", "--date", "2026-06-01"])

    assert exit_code == 0
    assert calls["default_date"] == "2026-06-01"


def _stored_event(event_id, ts="2026-05-31T10:00:00+03:00", **updates):
    event = {
        "schema_version": 1,
        "event_id": event_id,
        "ts": ts,
        "event_type": "agent_start",
        "agent": "codex",
        "semantic": {},
        "evidence": {},
        "files_changed": [],
    }
    event.update(updates)
    return event


def _write_cortex_project_config(project):
    config_path = project / ".agentic-journal.toml"
    config_path.write_text(
        "\n".join(
            [
                "[project]",
                'id = "cortex"',
                'path = "."',
                "",
                "[mirror]",
                'path = "Agentbase/.agentic-journal"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def test_mirror_sync_backfills_matching_project_events(tmp_path, capsys):
    global_root = tmp_path / "global"
    project = tmp_path / "cortex"
    agentbase = project / "Agentbase"
    codebase = project / "Codebase" / "Cortex"
    other = tmp_path / "other"
    agentbase.mkdir(parents=True)
    codebase.mkdir(parents=True)
    other.mkdir()
    write_event(global_root, _stored_event("agentbase", cwd=str(agentbase)))
    write_event(global_root, _stored_event("codebase", cwd=str(codebase), repo=str(codebase)))
    write_event(global_root, _stored_event("other", cwd=str(other)))
    config_path = _write_cortex_project_config(project)

    exit_code = main(["mirror", "sync", "--root", str(global_root), "--config", str(config_path)])

    assert exit_code == 0
    assert "scanned=3 matched=2 inserted=2 duplicates=0" in capsys.readouterr().out
    mirror_root = agentbase / ".agentic-journal"
    assert [event["event_id"] for event in read_events_for_date(mirror_root, "2026-05-31")] == [
        "agentbase",
        "codebase",
    ]


def test_status_command_reads_explicit_root(tmp_path, capsys):
    mirror_root = tmp_path / "mirror"
    write_event(mirror_root, _stored_event("mirror-event"))

    exit_code = main(["status", "--root", str(mirror_root), "--date", "2026-05-31"])

    assert exit_code == 0
    assert "Raw events: 1" in capsys.readouterr().out


def test_report_command_reads_and_writes_explicit_root(tmp_path):
    mirror_root = tmp_path / "mirror"
    write_event(mirror_root, _stored_event("mirror-event"))

    exit_code = main(["report", "--root", str(mirror_root), "--date", "2026-05-31"])

    assert exit_code == 0
    assert (mirror_root / "reports" / "2026-05-31.md").exists()


def test_web_command_passes_explicit_root(monkeypatch, tmp_path):
    calls = {}

    def fake_run_web_server(root, host, port, default_date, refresh_ms=2000, api_token=None):
        calls["root"] = root

    monkeypatch.setattr("agentic_journal.cli.run_web_server", fake_run_web_server)

    exit_code = main(["web", "--root", str(tmp_path / "mirror"), "--date", "2026-06-01"])

    assert exit_code == 0
    assert calls["root"] == tmp_path / "mirror"


def test_guard_session_end_writes_risky_fallback_when_semantic_entry_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    main(["event", "--type", "agent_start", "--agent", "claude", "--session-id", "session-1"])
    main(["event", "--type", "agent_end", "--agent", "claude", "--session-id", "session-1"])

    exit_code = main(["guard", "session-end", "--agent", "claude", "--session-id", "session-1"])
    second_exit_code = main(["guard", "session-end", "--agent", "claude", "--session-id", "session-1"])

    assert exit_code == 0
    assert second_exit_code == 0
    assert "guarded" in capsys.readouterr().out
    event_file = next((tmp_path / "events").glob("*.jsonl"))
    events = [json.loads(line) for line in event_file.read_text().splitlines()]
    guard_events = [event for event in events if event["event_type"] == "verification"]
    assert len(guard_events) == 1
    assert guard_events[0]["evidence"]["verification_status"] == "failed"
    assert guard_events[0]["semantic"]["status"] == "journal_missing"


def test_guard_session_end_skips_when_semantic_entry_exists(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    main(["event", "--type", "agent_start", "--agent", "claude", "--session-id", "session-2"])
    main(
        [
            "event",
            "--type",
            "task_completed_claim",
            "--agent",
            "claude",
            "--session-id",
            "session-2",
            "--task",
            "TASK-5",
        ]
    )

    exit_code = main(["guard", "session-end", "--agent", "claude", "--session-id", "session-2"])

    assert exit_code == 0
    assert "semantic journal entry exists" in capsys.readouterr().out
    event_file = next((tmp_path / "events").glob("*.jsonl"))
    events = [json.loads(line) for line in event_file.read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["agent_start", "task_completed_claim"]


def test_guard_session_end_requires_session_outcome_not_generic_note(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    main(["event", "--type", "agent_start", "--agent", "claude", "--session-id", "session-3"])
    main(["event", "--type", "semantic_note", "--agent", "claude", "--session-id", "session-3", "--note", "FYI"])

    exit_code = main(["guard", "session-end", "--agent", "claude", "--session-id", "session-3"])

    assert exit_code == 0
    assert "guarded" in capsys.readouterr().out
    event_file = next((tmp_path / "events").glob("*.jsonl"))
    events = [json.loads(line) for line in event_file.read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["agent_start", "semantic_note", "verification"]


def test_guard_session_end_records_changed_files_as_objective_fallback_context(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add tracked"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(journal))
    monkeypatch.chdir(repo)
    main(["event", "--type", "agent_start", "--agent", "claude", "--session-id", "session-files"])

    exit_code = main(["guard", "session-end", "--agent", "claude", "--session-id", "session-files"])

    assert exit_code == 0
    event_file = next((journal / "events").glob("*.jsonl"))
    events = [json.loads(line) for line in event_file.read_text().splitlines()]
    fallback = [event for event in events if event["event_type"] == "verification"][-1]
    assert fallback["files_changed"] == ["tracked.txt"]


def test_guard_session_end_skips_when_session_summary_exists(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    main(["event", "--type", "agent_start", "--agent", "claude", "--session-id", "session-4"])
    main(
        [
            "event",
            "--type",
            "session_summary",
            "--agent",
            "claude",
            "--session-id",
            "session-4",
            "--summary",
            "Reviewed MCP setup",
            "--outcome",
            "completed",
        ]
    )

    exit_code = main(["guard", "session-end", "--agent", "claude", "--session-id", "session-4"])

    assert exit_code == 0
    assert "semantic journal entry exists" in capsys.readouterr().out
    event_file = next((tmp_path / "events").glob("*.jsonl"))
    events = [json.loads(line) for line in event_file.read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["agent_start", "session_summary"]


def test_guard_session_end_skips_when_mcp_task_completed_uses_session_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIC_JOURNAL_SESSION_ID", "session-5")
    main(["event", "--type", "agent_start", "--agent", "claude", "--session-id", "session-5"])
    journal_task_completed(agent="claude", task_id="TASK-5", note="Done")

    exit_code = main(["guard", "session-end", "--agent", "claude", "--session-id", "session-5"])

    assert exit_code == 0
    assert "semantic journal entry exists" in capsys.readouterr().out
    event_file = next((tmp_path / "events").glob("*.jsonl"))
    events = [json.loads(line) for line in event_file.read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["agent_start", "task_completed_claim"]


def test_guard_session_end_skips_when_mcp_task_blocked_uses_session_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIC_JOURNAL_SESSION_ID", "session-6")
    main(["event", "--type", "agent_start", "--agent", "gemini", "--session-id", "session-6"])
    journal_task_blocked(agent="gemini", task_id="TASK-6", reason="Missing key")

    exit_code = main(["guard", "session-end", "--agent", "gemini", "--session-id", "session-6"])

    assert exit_code == 0
    assert "semantic journal entry exists" in capsys.readouterr().out
    event_file = next((tmp_path / "events").glob("*.jsonl"))
    events = [json.loads(line) for line in event_file.read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["agent_start", "task_blocked"]


def test_wrapper_preserves_exit_code_and_logs_events(tmp_path):
    fake_bin = tmp_path / "fake-agent"
    fake_bin.write_text("#!/usr/bin/env sh\nexit 7\n")
    fake_bin.chmod(0o755)
    wrapper = tmp_path / "codex"
    wrapper.write_text(generate_wrapper_script("codex", str(fake_bin)), encoding="utf-8")
    wrapper.chmod(0o755)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    agentic_journal = shim_dir / "agentic-journal"
    agentic_journal.write_text(
        f'#!/usr/bin/env sh\nPYTHONPATH="{Path("src").resolve()}" "{sys.executable}" -m agentic_journal.cli "$@"\n',
        encoding="utf-8",
    )
    agentic_journal.chmod(0o755)

    env = os.environ.copy()
    env["AGENTIC_JOURNAL_HOME"] = str(tmp_path / "journal")
    env["PATH"] = f"{shim_dir}:{env['PATH']}"

    result = subprocess.run([str(wrapper)], env=env, check=False)

    assert result.returncode == 7
    event_file = next((tmp_path / "journal" / "events").glob("*.jsonl"))
    event_types = [json.loads(line)["event_type"] for line in event_file.read_text().splitlines()]
    assert event_types == ["agent_start", "agent_end", "verification"]


def test_git_commit_event_records_head_commit_and_committed_files(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("hello\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add tracked"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(journal))
    monkeypatch.chdir(repo)

    exit_code = main(["event", "--type", "git_commit", "--agent", "git"])

    assert exit_code == 0
    event_file = next((journal / "events").glob("*.jsonl"))
    event = json.loads(event_file.read_text().splitlines()[0])
    assert event["commit"] == head
    assert event["files_changed"] == ["tracked.txt"]


def _run_ingest(monkeypatch, payload, *args):
    import io

    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))
    return main(["ingest", *args])


def _enable_prompts(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text("[privacy]\nlog_prompts = true\n", encoding="utf-8")


def test_ingest_stores_event_and_reports_seq(tmp_path, monkeypatch, capsys):
    _enable_prompts(tmp_path)
    event = {
        "event_type": "user_message",
        "event_id": "msg-1",
        "agent": "claude",
        "session_id": "s1",
        "turn_id": "prompt-1",
        "semantic": {"text": "  keep me\r\n", "origin": "claude:cli"},
    }

    assert _run_ingest(monkeypatch, event, "--root", str(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out) == {"event_id": "msg-1", "inserted": True, "seq": 1}
    assert _run_ingest(monkeypatch, event, "--root", str(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out) == {"event_id": "msg-1", "inserted": False, "seq": 1}

    [stored] = read_events_for_date(tmp_path, None)
    assert stored["semantic"]["text"] == "  keep me\r\n"
    assert stored["turn_id"] == "prompt-1"


def test_ingest_uses_journal_home_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_JOURNAL_HOME", str(tmp_path))

    assert _run_ingest(monkeypatch, {"event_type": "agent_start", "agent": "codex"}) == 0

    assert json.loads(capsys.readouterr().out)["seq"] == 1
    assert len(read_events_for_date(tmp_path, None)) == 1


def test_ingest_rejects_invalid_input_with_exit_code_2(tmp_path, monkeypatch, capsys):
    root = str(tmp_path)
    cases = [
        b"{not json",
        b"[1, 2]",
        json.dumps({"event_type": "unknown", "agent": "codex"}).encode(),
        json.dumps({"event_type": "agent_start", "ts": "../../etc/passwd"}).encode(),
        json.dumps({"event_type": "user_message", "agent": "claude", "semantic": {"text": "hi"}}).encode(),
    ]

    for payload in cases:
        assert _run_ingest(monkeypatch, payload, "--root", root) == 2, payload
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "agentic-journal ingest: rejected:" in captured.err

    assert read_events_for_date(tmp_path, None) == []


def test_ingest_reports_storage_failure_with_exit_code_1(tmp_path, monkeypatch, capsys):
    def fail_record(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("agentic_journal.cli.record_event", fail_record)

    assert _run_ingest(monkeypatch, {"event_type": "agent_start", "agent": "codex"}, "--root", str(tmp_path)) == 1
    assert "storage error: disk full" in capsys.readouterr().err


def _track_event(root, event_id, event_type="semantic_note", agent="claude", session_id="s1", **updates):
    from agentic_journal.events import normalize_event
    from agentic_journal.storage import record_event

    semantic = {"text": event_id} if event_type == "user_message" else {"note": event_id}
    raw = {"event_id": event_id, "event_type": event_type, "agent": agent, "session_id": session_id, "semantic": semantic}
    raw.update(updates)
    record_event(root, normalize_event(raw))


def test_events_prints_one_track_in_write_order(tmp_path, capsys):
    _enable_prompts(tmp_path)
    _track_event(tmp_path, "main-msg", "user_message", ts="2026-09-14T12:00:00+03:00")
    _track_event(tmp_path, "sub-note", agent_id="a1")
    _track_event(tmp_path, "main-note", ts="2026-09-14T11:00:00+03:00")
    _track_event(tmp_path, "main-turn", "model_operation")
    _track_event(tmp_path, "other-session", session_id="s2")
    _track_event(tmp_path, "other-client", agent="codex")

    assert main(["events", "--root", str(tmp_path), "--agent", "claude", "--session-id", "s1", "--main",
                 "--type", "user_message", "--type", "semantic_note"]) == 0
    assert [json.loads(line)["event_id"] for line in capsys.readouterr().out.splitlines()] == ["main-msg", "main-note"]

    assert main(["events", "--root", str(tmp_path), "--agent", "claude", "--session-id", "s1", "--agent-id", "a1"]) == 0
    assert [json.loads(line)["event_id"] for line in capsys.readouterr().out.splitlines()] == ["sub-note"]

    assert main(["events", "--root", str(tmp_path), "--agent", "claude", "--session-id", "s1", "--main"]) == 0
    assert [json.loads(line)["event_id"] for line in capsys.readouterr().out.splitlines()] == [
        "main-msg",
        "main-note",
        "main-turn",
    ]


def test_events_requires_exactly_one_track_selector(tmp_path, capsys):
    base = ["events", "--root", str(tmp_path), "--agent", "claude", "--session-id", "s1"]

    assert main(base) == 2
    assert main([*base, "--main", "--agent-id", "a1"]) == 2
    capsys.readouterr()


def test_events_output_keeps_user_text_bytes(tmp_path):
    _enable_prompts(tmp_path)
    text = "tab\there\r\nline sep\u2028end \U0001f468\u200d\U0001f469"
    _track_event(tmp_path, "msg", "user_message", semantic={"text": text})
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "LC_ALL": "C", "PYTHONIOENCODING": "ascii"}

    result = subprocess.run(
        [sys.executable, "-m", "agentic_journal.cli", "events", "--root", str(tmp_path), "--agent", "claude",
         "--session-id", "s1", "--main"],
        env=env,
        capture_output=True,
        check=True,
    )

    [line] = result.stdout.split(b"\n")[:-1]
    assert json.loads(line.decode("utf-8"))["semantic"]["text"] == text


def test_ingest_does_not_import_web_report_or_mcp(tmp_path):
    code = (
        "import sys\n"
        "from agentic_journal.cli import main\n"
        "code = main(['ingest', '--root', sys.argv[1]])\n"
        "heavy = ['agentic_journal.web', 'agentic_journal.report', 'agentic_journal.mcp_server', 'http.server', 'mcp']\n"
        "print(sorted(name for name in heavy if name in sys.modules), file=sys.stderr)\n"
        "sys.exit(code)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}

    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        input=b'{"event_type": "agent_start", "agent": "codex"}',
        env=env,
        capture_output=True,
        check=True,
    )

    assert result.stderr.decode().strip().splitlines()[-1] == "[]"
