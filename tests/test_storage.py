import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from agentic_journal import storage
from agentic_journal.config import journal_root
from agentic_journal.storage import (
    DB_SCHEMA_VERSION,
    append_jsonl_event,
    init_db,
    insert_event,
    read_events_for_date,
    read_events_for_session,
    read_jsonl_events,
    record_event,
    write_event,
)

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def _event(event_id, ts="2026-05-31T10:00:00+03:00", **updates):
    raw = {
        "schema_version": 1,
        "event_id": event_id,
        "ts": ts,
        "event_type": "agent_start",
        "agent": "codex",
        "semantic": {},
        "evidence": {},
    }
    raw.update(updates)
    return raw


def test_journal_root_accepts_legacy_home_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_JOURNAL_HOME", raising=False)
    monkeypatch.setenv("AGENT_JOURNAL_HOME", str(tmp_path / "legacy-journal"))

    assert journal_root() == (tmp_path / "legacy-journal").resolve()


def test_append_jsonl_event_writes_by_date(tmp_path):
    root = tmp_path / "journal"
    event = {
        "schema_version": 1,
        "event_id": "e1",
        "ts": "2026-05-31T10:00:00+03:00",
        "event_type": "agent_start",
        "agent": "codex",
    }

    path = append_jsonl_event(root, event)

    assert path == root / "events" / "2026-05-31-unscoped.jsonl"
    assert list(read_jsonl_events(path)) == [event]


def test_session_jsonl_uses_human_name_and_updates_when_native_title_arrives(tmp_path):
    root = tmp_path / "journal"
    first = _event(
        "e1",
        session_id="s1",
        session_name="Первый запрос",
        session_name_source="prompt",
    )

    old_path = write_event(root, first)
    new_path = write_event(
        root,
        _event(
            "e2",
            session_id="s1",
            session_name="Доработка логирования",
            session_name_source="codex-thread",
        ),
    )

    assert old_path.name == "2026-05-31-первый-запрос.jsonl"
    assert new_path.name == "2026-05-31-доработка-логирования.jsonl"
    assert not old_path.exists()
    events = list(read_jsonl_events(new_path))
    assert [event["event_id"] for event in events] == ["e1", "e2"]
    assert [event["session_name"] for event in events] == ["Первый запрос", "Доработка логирования"]
    assert [event["session_name_source"] for event in events] == ["prompt", "codex-thread"]


def test_duplicate_old_native_event_does_not_roll_session_name_back(tmp_path):
    root = tmp_path / "journal"
    old = _event(
        "e1",
        session_id="s1",
        session_name="Old native name",
        session_name_source="codex-thread",
    )
    write_event(root, old)
    current = write_event(
        root,
        _event(
            "e2",
            session_id="s1",
            session_name="Current native name",
            session_name_source="codex-thread",
        ),
    )

    replay = record_event(root, old)

    assert not replay.inserted
    assert replay.path == current
    assert [event["event_id"] for event in read_jsonl_events(current)] == ["e1", "e2"]
    assert not (root / "events" / "2026-05-31-old-native-name.jsonl").exists()
    with closing(storage.connect(root)) as conn:
        row = conn.execute("SELECT session_name, file_slug FROM sessions").fetchone()
    assert tuple(row) == ("Current native name", "current-native-name")


def test_prompt_fallback_is_stable_until_higher_priority_name_arrives(tmp_path):
    root = tmp_path / "journal"
    first = write_event(
        root,
        _event("e1", session_id="s1", session_name="Первый prompt", session_name_source="prompt"),
    )
    second = write_event(
        root,
        _event("e2", session_id="s1", session_name="Второй prompt", session_name_source="prompt"),
    )

    assert first == second
    assert first.name == "2026-05-31-первый-prompt.jsonl"
    assert {event["session_name"] for event in read_jsonl_events(first)} == {"Первый prompt"}


def test_equal_session_slugs_get_stable_hash_suffix(tmp_path):
    root = tmp_path / "journal"
    first = write_event(
        root,
        _event("e1", session_id="s1", session_name="Same title", session_name_source="codex-thread"),
    )
    second = write_event(
        root,
        _event("e2", session_id="s2", session_name="Same title", session_name_source="codex-thread"),
    )

    assert first.name == "2026-05-31-same-title.jsonl"
    assert second.name.startswith("2026-05-31-same-title-")
    assert second.name.endswith(".jsonl")
    assert first != second


def test_session_spanning_midnight_gets_one_file_per_date(tmp_path):
    root = tmp_path / "journal"
    first = write_event(
        root,
        _event("e1", ts="2026-05-31T23:59:00+03:00", session_id="s1",
               session_name="Night work", session_name_source="codex-thread"),
    )
    second = write_event(
        root,
        _event("e2", ts="2026-06-01T00:01:00+03:00", session_id="s1",
               session_name="Night work", session_name_source="codex-thread"),
    )

    assert first.name == "2026-05-31-night-work.jsonl"
    assert second.name == "2026-06-01-night-work.jsonl"


def test_sqlite_storage_uses_wal_and_reads_by_date(tmp_path):
    root = tmp_path / "journal"
    db_path = init_db(root)
    event = {
        "schema_version": 1,
        "event_id": "e1",
        "ts": "2026-05-31T10:00:00+03:00",
        "event_type": "agent_start",
        "agent": "codex",
        "semantic": {},
        "evidence": {},
    }

    insert_event(root, event)
    insert_event(root, event)

    events = read_events_for_date(root, "2026-05-31")
    assert db_path.exists()
    assert len(events) == 1
    assert events[0]["event_id"] == "e1"


def test_write_event_keeps_jsonl_mirror_idempotent(tmp_path):
    from agentic_journal.storage import write_event

    root = tmp_path / "journal"
    event = {
        "schema_version": 1,
        "event_id": "e1",
        "ts": "2026-05-31T10:00:00+03:00",
        "event_type": "agent_start",
        "agent": "codex",
        "semantic": {},
        "evidence": {},
    }

    path = write_event(root, event)
    second_path = write_event(root, event)

    assert second_path == path
    assert list(read_jsonl_events(path)) == [event]
    assert len(read_events_for_date(root, "2026-05-31")) == 1


def test_write_event_rolls_back_sqlite_when_jsonl_append_fails(tmp_path, monkeypatch):
    root = tmp_path / "journal"

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "append_jsonl_event", boom)

    with pytest.raises(OSError):
        write_event(root, _event("e1"))

    # The SQLite row must be rolled back so a retry re-attempts both writes
    # instead of permanently skipping the JSONL mirror line.
    assert read_events_for_date(root, "2026-05-31") == []


def test_write_event_preserves_original_append_error_when_rollback_fails(tmp_path, monkeypatch):
    root = tmp_path / "journal"

    def append_boom(*args, **kwargs):
        raise OSError("jsonl append failed")

    def delete_boom(*args, **kwargs):
        raise OSError("rollback failed")

    monkeypatch.setattr(storage, "append_jsonl_event", append_boom)
    monkeypatch.setattr(storage, "delete_event", delete_boom)

    with pytest.raises(OSError, match="jsonl append failed"):
        write_event(root, _event("e1"))


def test_failed_session_rename_restores_registry_and_old_jsonl(tmp_path, monkeypatch):
    root = tmp_path / "journal"
    old_path = write_event(
        root,
        _event("e1", session_id="s1", session_name="Prompt name", session_name_source="prompt"),
    )

    def boom(*args, **kwargs):
        raise OSError("snapshot failed")

    monkeypatch.setattr(storage, "_write_jsonl_snapshot", boom)
    with pytest.raises(OSError, match="snapshot failed"):
        write_event(
            root,
            _event("e2", session_id="s1", session_name="Native name",
                   session_name_source="codex-thread"),
        )

    assert old_path.exists()
    assert not (root / "events" / "2026-05-31-native-name.jsonl").exists()
    assert [event["event_id"] for event in read_events_for_session(root, "s1")] == ["e1"]
    with closing(storage.connect(root)) as conn:
        row = conn.execute("SELECT session_name, file_slug FROM sessions").fetchone()
    assert tuple(row) == ("Prompt name", "prompt-name")


def test_init_db_tracks_schema_user_version(tmp_path):
    root = tmp_path / "journal"

    init_db(root)

    with closing(storage.connect(root)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == DB_SCHEMA_VERSION == 3


def test_read_events_skips_future_schema_versions(tmp_path):
    root = tmp_path / "journal"
    future = _event("future", schema_version=999)
    current = _event("current")

    write_event(root, future)
    write_event(root, current)

    events = read_events_for_date(root, "2026-05-31")

    assert [event["event_id"] for event in events] == ["current"]


def test_write_event_appends_multiple_events_in_order(tmp_path):
    root = tmp_path / "journal"
    path = write_event(root, _event("e1", ts="2026-05-31T10:00:00+03:00"))
    write_event(root, _event("e2", ts="2026-05-31T11:00:00+03:00"))

    ids = [event["event_id"] for event in read_jsonl_events(path)]
    assert ids == ["e1", "e2"]


def test_read_events_for_session_filters_by_session_across_dates(tmp_path):
    root = tmp_path / "journal"
    write_event(root, _event("a1", ts="2026-05-31T23:59:00+03:00", session_id="s1"))
    write_event(root, _event("a2", ts="2026-06-01T00:01:00+03:00", session_id="s1"))
    write_event(root, _event("b1", ts="2026-06-01T00:02:00+03:00", session_id="s2"))

    events = read_events_for_session(root, "s1")

    assert [event["event_id"] for event in events] == ["a1", "a2"]


def test_write_event_secures_db_and_wal_sidecar_permissions(tmp_path):
    import glob
    import os
    import stat

    root = tmp_path / "journal"
    write_event(root, _event("e1"))

    db_files = glob.glob(str(root / "agentic-journal.db*"))
    assert any(name.endswith("agentic-journal.db") for name in db_files)
    for path in [*db_files, str(root / storage.WRITE_LOCK_FILENAME)]:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, (os.path.basename(path), oct(mode))


def test_read_jsonl_events_skips_corrupt_lines(tmp_path):
    path = tmp_path / "2026-05-31.jsonl"
    good = '{"event_id": "ok", "event_type": "agent_start"}'
    path.write_text(good + "\n{ this is not json\n", encoding="utf-8")

    events = list(read_jsonl_events(path))

    assert events == [{"event_id": "ok", "event_type": "agent_start"}]


def _write_project_config(project):
    config_path = project / ".agentic-journal.toml"
    config_path.write_text(
        "\n".join(
            [
                "[project]",
                'id = "cortex"',
                'path = "."',
                "",
                "[mirror]",
                "enabled = true",
                'path = "Agentbase/.agentic-journal"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def test_write_event_mirrors_matching_project_event(tmp_path):
    global_root = tmp_path / "global"
    project = tmp_path / "cortex"
    agentbase = project / "Agentbase"
    agentbase.mkdir(parents=True)
    _write_project_config(project)
    event = _event("cortex-1", cwd=str(agentbase), repo=None)

    write_event(global_root, event)

    mirror_root = agentbase / ".agentic-journal"
    assert [item["event_id"] for item in read_events_for_date(global_root, "2026-05-31")] == ["cortex-1"]
    assert [item["event_id"] for item in read_events_for_date(mirror_root, "2026-05-31")] == ["cortex-1"]


def test_write_event_mirrors_child_repo_project_event(tmp_path):
    global_root = tmp_path / "global"
    project = tmp_path / "cortex"
    codebase = project / "Codebase" / "Cortex"
    agentbase = project / "Agentbase"
    codebase.mkdir(parents=True)
    agentbase.mkdir(parents=True)
    _write_project_config(project)
    event = _event("cortex-2", cwd=str(codebase), repo=str(codebase))

    write_event(global_root, event)

    mirror_root = agentbase / ".agentic-journal"
    assert [item["event_id"] for item in read_events_for_date(mirror_root, "2026-05-31")] == ["cortex-2"]


def test_write_event_does_not_mirror_non_matching_project_event(tmp_path):
    global_root = tmp_path / "global"
    project = tmp_path / "cortex"
    other = tmp_path / "other"
    (project / "Agentbase").mkdir(parents=True)
    other.mkdir()
    _write_project_config(project)

    write_event(global_root, _event("other-1", cwd=str(other), repo=None))

    mirror_root = project / "Agentbase" / ".agentic-journal"
    assert read_events_for_date(mirror_root, "2026-05-31") == []


def test_write_event_keeps_project_mirror_idempotent(tmp_path):
    global_root = tmp_path / "global"
    project = tmp_path / "cortex"
    agentbase = project / "Agentbase"
    agentbase.mkdir(parents=True)
    _write_project_config(project)
    event = _event("cortex-duplicate", cwd=str(agentbase), repo=None)

    write_event(global_root, event)
    write_event(global_root, event)

    mirror_root = agentbase / ".agentic-journal"
    assert [item["event_id"] for item in read_events_for_date(mirror_root, "2026-05-31")] == ["cortex-duplicate"]
    assert list(read_jsonl_events(mirror_root / "events" / "2026-05-31-unscoped.jsonl")) == [event]


def test_project_mirror_append_failure_does_not_fail_global_write(tmp_path, monkeypatch, capsys):
    global_root = tmp_path / "global"
    project = tmp_path / "cortex"
    agentbase = project / "Agentbase"
    agentbase.mkdir(parents=True)
    _write_project_config(project)
    real_append = storage.append_jsonl_event

    def fail_mirror_append(root, event, **kwargs):
        if Path(root) == agentbase / ".agentic-journal":
            raise OSError("mirror unavailable")
        return real_append(root, event, **kwargs)

    monkeypatch.setattr(storage, "append_jsonl_event", fail_mirror_append)

    write_event(global_root, _event("global-survives", cwd=str(agentbase), repo=None))

    assert [item["event_id"] for item in read_events_for_date(global_root, "2026-05-31")] == ["global-survives"]
    assert "failed to mirror Agentic Journal event" in capsys.readouterr().err


_CONCURRENT_WRITER = """
import sys
import time
from pathlib import Path

from agentic_journal.storage import write_event

root, worker, count, size, start_flag = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), Path(sys.argv[5])
while not start_flag.exists():
    time.sleep(0.005)
for index in range(count):
    write_event(
        root,
        {
            "schema_version": 1,
            "event_id": f"{worker}-{index}",
            "ts": "2026-05-31T10:00:00.000000+03:00",
            "event_type": "semantic_note",
            "agent": worker,
            "semantic": {"note": worker[-1] * size},
            "evidence": {},
        },
    )
"""


def test_concurrent_processes_keep_jsonl_lines_whole_and_ordered_by_seq(tmp_path):
    root = tmp_path / "journal"
    start_flag = tmp_path / "start"
    workers, count, size = 6, 20, 70_000
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", _CONCURRENT_WRITER, str(root), f"w{worker}", str(count), str(size), str(start_flag)],
            env=env,
            cwd=tmp_path,
        )
        for worker in range(workers)
    ]
    time.sleep(0.5)
    start_flag.touch()
    assert [process.wait(timeout=120) for process in processes] == [0] * workers

    raw_lines = (root / "events" / "2026-05-31-unscoped.jsonl").read_bytes().split(b"\n")
    assert raw_lines[-1] == b""
    jsonl_ids = [json.loads(line)["event_id"] for line in raw_lines[:-1]]
    with closing(storage.connect(root)) as conn:
        rows = conn.execute("SELECT event_id, seq FROM events ORDER BY seq").fetchall()

    total = workers * count
    assert len(jsonl_ids) == total
    assert [row["seq"] for row in rows] == list(range(1, total + 1))
    assert jsonl_ids == [row["event_id"] for row in rows]


def test_init_db_migrates_version_1_database(tmp_path):
    root = tmp_path / "journal"
    root.mkdir()
    late = _event("late", ts="2026-05-31T11:00:00+03:00", agent_id="agent-1", turn_id="turn-1")
    early = _event("early", ts="2026-05-31T10:00:00+03:00")
    with closing(sqlite3.connect(root / "agentic-journal.db")) as conn:
        storage._migrate_to_1(conn)
        for event in (late, early):
            conn.execute(
                "INSERT INTO events (event_id, schema_version, ts, event_type, agent, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
                (event["event_id"], 1, event["ts"], event["event_type"], event["agent"], json.dumps(event)),
            )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

    init_db(root)

    with closing(storage.connect(root)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        rows = conn.execute("SELECT event_id, seq, agent_id, turn_id FROM events ORDER BY seq").fetchall()
    assert version == DB_SCHEMA_VERSION
    assert [tuple(row) for row in rows] == [("early", 1, None, None), ("late", 2, "agent-1", "turn-1")]
    assert record_event(root, _event("next")).seq == 3


def test_version_2_migration_keeps_legacy_jsonl_and_only_new_events_use_new_layout(tmp_path):
    root = tmp_path / "journal"
    events_dir = root / "events"
    events_dir.mkdir(parents=True)
    legacy = events_dir / "2026-05-31.jsonl"
    legacy.write_text('{"event_id":"legacy"}\n', encoding="utf-8")
    with closing(sqlite3.connect(root / "agentic-journal.db")) as conn:
        conn.row_factory = sqlite3.Row
        storage._migrate_to_1(conn)
        storage._migrate_to_2(conn)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()

    path = write_event(root, _event("new"))

    assert legacy.read_text(encoding="utf-8") == '{"event_id":"legacy"}\n'
    assert path == events_dir / "2026-05-31-unscoped.jsonl"
    with closing(storage.connect(root)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
        assert conn.execute("SELECT mirror_layout FROM events WHERE event_id = 'new'").fetchone()[0] == 2


def test_record_event_reports_seq_for_new_and_duplicate_events(tmp_path):
    root = tmp_path / "journal"

    first = record_event(root, _event("e1"))
    second = record_event(root, _event("e2"))
    duplicate = record_event(root, _event("e1"))

    assert (first.inserted, first.seq) == (True, 1)
    assert (second.inserted, second.seq) == (True, 2)
    assert (duplicate.inserted, duplicate.seq) == (False, 1)
    assert duplicate.path == first.path


def test_insert_event_stores_track_columns(tmp_path):
    root = tmp_path / "journal"

    insert_event(root, _event("e1", session_id="s1", agent_id="agent-1", turn_id="turn-1"))

    with closing(storage.connect(root)) as conn:
        row = conn.execute("SELECT seq, session_id, agent_id, turn_id FROM events").fetchone()
    assert tuple(row) == (1, "s1", "agent-1", "turn-1")


def test_reads_follow_write_order_not_ts(tmp_path):
    root = tmp_path / "journal"
    write_event(root, _event("written-first", ts="2026-05-31T11:00:00+03:00", session_id="s1"))
    write_event(root, _event("written-second", ts="2026-05-31T10:00:00+03:00", session_id="s1"))

    expected = ["written-first", "written-second"]
    assert [event["event_id"] for event in read_events_for_date(root, "2026-05-31")] == expected
    assert [event["event_id"] for event in read_events_for_session(root, "s1")] == expected


_CONCURRENT_READER = """
import sys
import time
from pathlib import Path

from agentic_journal.storage import read_events_for_date

root, start_flag = sys.argv[1], Path(sys.argv[2])
while not start_flag.exists():
    time.sleep(0.001)
read_events_for_date(root, None)
"""


def test_concurrent_readers_initialize_fresh_database(tmp_path):
    root = tmp_path / "journal"
    start_flag = tmp_path / "start"
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", _CONCURRENT_READER, str(root), str(start_flag)],
            env=env,
            cwd=tmp_path,
            stderr=subprocess.PIPE,
        )
        for _ in range(12)
    ]
    time.sleep(0.5)
    start_flag.touch()
    results = [(process.wait(timeout=120), process.stderr.read().decode()) for process in processes]

    assert [code for code, _ in results] == [0] * len(processes), [err for code, err in results if code]
    with closing(storage.connect(root)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
