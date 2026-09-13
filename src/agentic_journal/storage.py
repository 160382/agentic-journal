from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms have no flock
    fcntl = None

from agentic_journal.config import FILE_MODE, ensure_config, journal_root, secure_dir, secure_file
from agentic_journal.events import SCHEMA_VERSION
from agentic_journal.project_config import discover_project_mirror_configs, event_matches_project

# Layout version of the SQLite database, tracked in PRAGMA user_version. It is
# independent of the event SCHEMA_VERSION: index columns can change without
# changing the event payload.
DB_SCHEMA_VERSION = 2
WRITE_LOCK_FILENAME = ".write.lock"
BUSY_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class StoredEvent:
    path: Path
    inserted: bool
    seq: int


def _date_from_ts(ts: str) -> str:
    date = ts[:10]
    if "/" in date or "\\" in date or ".." in date:
        raise ValueError(f"Unsafe ts for date routing: {ts!r}")
    return date


def _root_path(root: str | Path | None) -> Path:
    return Path(root).expanduser() if root else journal_root()


def append_jsonl_event(root: str | Path, event: dict[str, Any]) -> Path:
    root_path = Path(root).expanduser()
    date = _date_from_ts(event["ts"])
    event_dir = secure_dir(root_path / "events")
    path = event_dir / f"{date}.jsonl"
    # The whole line goes out through O_APPEND writes of one buffer: buffered
    # text IO would split a long line into several writes that another process
    # could interleave.
    line = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
    try:
        view = memoryview(line)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)
    secure_file(path)
    return path


def read_jsonl_events(path: str | Path) -> Iterable[dict[str, Any]]:
    jsonl_path = Path(path)
    if not jsonl_path.exists():
        return []
    events = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            # Skip a torn or corrupt line (e.g. after a crash mid-append) rather
            # than failing the whole read; the SQLite store is the primary path.
            continue
    return events


def db_file(root: str | Path | None = None) -> Path:
    return Path(root).expanduser() / "agentic-journal.db" if root else journal_root() / "agentic-journal.db"


def connect(root: str | Path | None = None) -> sqlite3.Connection:
    path = db_file(root)
    secure_dir(path.parent)
    # Autocommit mode: writers open explicit BEGIN IMMEDIATE transactions, so
    # seq allocation and migrations take the database write lock up front.
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _immediate_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _migrate_to_1(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
          event_id TEXT PRIMARY KEY,
          schema_version INTEGER NOT NULL,
          ts TEXT NOT NULL,
          event_type TEXT NOT NULL,
          agent TEXT,
          session_id TEXT,
          cwd TEXT,
          repo TEXT,
          branch TEXT,
          commit_hash TEXT,
          exit_code INTEGER,
          duration_ms INTEGER,
          raw_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_repo ON events(repo)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id)")


def _migrate_to_2(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE events ADD COLUMN seq INTEGER")
    conn.execute("ALTER TABLE events ADD COLUMN agent_id TEXT")
    conn.execute("ALTER TABLE events ADD COLUMN turn_id TEXT")
    # Rows written before seq existed keep the order readers used to apply.
    rows = conn.execute("SELECT event_id, raw_json FROM events ORDER BY ts, event_id").fetchall()
    for seq, row in enumerate(rows, start=1):
        event = json.loads(row["raw_json"])
        conn.execute(
            "UPDATE events SET seq = ?, agent_id = ?, turn_id = ? WHERE event_id = ?",
            (seq, event.get("agent_id"), event.get("turn_id"), row["event_id"]),
        )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_seq ON events(seq)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_track ON events(session_id, agent_id, seq)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_turn_id ON events(turn_id)")


MIGRATIONS = {
    1: _migrate_to_1,
    2: _migrate_to_2,
}


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    if _user_version(conn) >= DB_SCHEMA_VERSION:
        return
    with _immediate_transaction(conn):
        # Re-read under the write lock: every reader and writer runs init_db, so
        # another process may have migrated since the unlocked check above.
        for version in range(_user_version(conn) + 1, DB_SCHEMA_VERSION + 1):
            MIGRATIONS[version](conn)
            conn.execute(f"PRAGMA user_version = {version}")


def _db_ready(conn: sqlite3.Connection) -> bool:
    return conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal" and _user_version(conn) >= DB_SCHEMA_VERSION


def init_db(root: str | Path | None = None) -> Path:
    path = db_file(root)
    ensure_config(path.parent)
    with closing(connect(root)) as conn:
        if not _db_ready(conn):
            # Switching a fresh database to WAL fails with "database is locked"
            # when another process does the same, and SQLite does not retry it
            # through the busy timeout; readers race writers here too, so the
            # one-time setup runs under the write lock.
            with _write_lock(path.parent):
                conn.execute("PRAGMA journal_mode=WAL")
                _apply_migrations(conn)
    secure_file(path)
    # WAL mode creates `-wal` / `-shm` sidecars that hold the freshest, not-yet
    # checkpointed event data; restrict them to the owner as well.
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            secure_file(sidecar)
    return path


def _insert_row(conn: sqlite3.Connection, event: dict[str, Any]) -> tuple[bool, int]:
    with _immediate_transaction(conn):
        existing = conn.execute("SELECT seq FROM events WHERE event_id = ?", (event["event_id"],)).fetchone()
        if existing is not None:
            return False, existing["seq"]
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events").fetchone()[0]
        conn.execute(
            """
            INSERT INTO events (
              event_id, schema_version, ts, event_type, agent, session_id, cwd,
              repo, branch, commit_hash, exit_code, duration_ms, raw_json,
              seq, agent_id, turn_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_id"],
                event["schema_version"],
                event["ts"],
                event["event_type"],
                event.get("agent"),
                event.get("session_id"),
                event.get("cwd"),
                event.get("repo"),
                event.get("branch"),
                event.get("commit"),
                event.get("exit_code"),
                event.get("duration_ms"),
                json.dumps(event, ensure_ascii=False, sort_keys=True),
                seq,
                event.get("agent_id"),
                event.get("turn_id"),
            ),
        )
    return True, seq


def _insert_event(root: str | Path | None, event: dict[str, Any]) -> tuple[bool, int]:
    init_db(root)
    with closing(connect(root)) as conn:
        return _insert_row(conn, event)


def insert_event(root: str | Path | None, event: dict[str, Any]) -> bool:
    return _insert_event(root, event)[0]


def delete_event(root: str | Path | None, event_id: str) -> None:
    with closing(connect(root)) as conn:
        conn.execute("DELETE FROM events WHERE event_id = ?", (event_id,))


@contextmanager
def _write_lock(root_path: Path) -> Iterator[None]:
    """Serialize "SQLite insert + JSONL append" between processes on one root.

    SQLite alone keeps seq unique, but without this lock two writers could
    append their JSONL lines in the opposite order of their seq values.
    """
    if fcntl is None:
        yield
        return
    secure_dir(root_path)
    fd = os.open(root_path / WRITE_LOCK_FILENAME, os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _persist(root_path: Path, event: dict[str, Any]) -> StoredEvent:
    path = root_path / "events" / f"{_date_from_ts(event['ts'])}.jsonl"
    # init_db may take the write lock itself; flock does not nest across file
    # descriptors of one process, so it has to run before the lock below.
    init_db(root_path)
    with _write_lock(root_path):
        with closing(connect(root_path)) as conn:
            inserted, seq = _insert_row(conn, event)
        if not inserted:
            return StoredEvent(path, False, seq)
        try:
            path = append_jsonl_event(root_path, event)
        except OSError:
            # Keep SQLite (read path) and the JSONL mirror consistent: if the
            # mirror append fails, roll back the SQLite row so a retry re-attempts
            # both writes instead of permanently skipping the mirror line.
            try:
                delete_event(root_path, event["event_id"])
            except Exception:
                # SQLite is the primary read path. If rollback also fails, keep
                # surfacing the original append error; masking it would make the
                # actionable filesystem failure harder to diagnose.
                pass
            raise
    return StoredEvent(path, True, seq)


def persist_event(root: str | Path | None, event: dict[str, Any]) -> tuple[Path, bool]:
    stored = _persist(_root_path(root), event)
    return stored.path, stored.inserted


def _mirror_event_to_project_roots(event: dict[str, Any]) -> None:
    for config in discover_project_mirror_configs(event):
        if not event_matches_project(config, event):
            continue
        try:
            persist_event(config.mirror_root, event)
        except Exception as exc:
            print(
                f"failed to mirror Agentic Journal event {event.get('event_id')} "
                f"to {config.mirror_root}: {exc}",
                file=sys.stderr,
            )


def record_event(root: str | Path | None, event: dict[str, Any]) -> StoredEvent:
    """Write an event to the journal root and its project mirrors.

    Returns the JSONL path, whether the event was new, and its seq; a duplicate
    ``event_id`` reports the seq it was stored under the first time.
    """
    stored = _persist(_root_path(root), event)
    if stored.inserted:
        _mirror_event_to_project_roots(event)
    return stored


def write_event(root: str | Path | None, event: dict[str, Any]) -> Path:
    return record_event(root, event).path


def _rows_to_events(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    events = []
    for row in rows:
        event = json.loads(row["raw_json"])
        if event.get("schema_version", 0) > SCHEMA_VERSION:
            continue
        events.append(event)
    return events


def read_events_for_date(root: str | Path | None, date: str | None) -> list[dict[str, Any]]:
    root_path = _root_path(root)
    init_db(root_path)
    query = "SELECT raw_json FROM events"
    params: tuple[str, ...] = ()
    if date:
        query += " WHERE ts LIKE ?"
        params = (f"{date}%",)
    query += " ORDER BY seq"
    with closing(connect(root_path)) as conn:
        rows = conn.execute(query, params).fetchall()
    return _rows_to_events(rows)


def read_events_for_session(root: str | Path | None, session_id: str) -> list[dict[str, Any]]:
    """Read every event for one session across all dates, using the index.

    The session guard needs to see a session that may span local midnight, so it
    cannot scope to a single date; querying by the indexed ``session_id`` avoids
    a full-table scan of the entire journal history on every session exit.
    """
    root_path = _root_path(root)
    init_db(root_path)
    with closing(connect(root_path)) as conn:
        rows = conn.execute(
            "SELECT raw_json FROM events WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    return _rows_to_events(rows)
