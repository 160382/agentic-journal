from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms have no flock
    fcntl = None

from agentic_journal.config import FILE_MODE, ensure_config, journal_root, load_config, secure_dir, secure_file
from agentic_journal.events import SCHEMA_VERSION, USER_MESSAGE_EVENT_TYPE, PromptLoggingDisabledError
from agentic_journal.project_config import discover_project_mirror_configs, event_matches_project

# Layout version of the SQLite database, tracked in PRAGMA user_version. It is
# independent of the event SCHEMA_VERSION: index columns can change without
# changing the event payload.
DB_SCHEMA_VERSION = 3
WRITE_LOCK_FILENAME = ".write.lock"
BUSY_TIMEOUT_SECONDS = 30.0
MIRROR_LAYOUT_LEGACY = 1
MIRROR_LAYOUT_SESSION = 2
SESSION_SLUG_MAX_BYTES = 160
RESERVED_SESSION_SLUGS = {"unscoped"}

SESSION_SOURCE_RANK = {
    "id": 0,
    "prompt": 10,
    "claude-slug": 20,
    "codex-thread": 30,
    "claude-ai-title": 30,
}


@dataclass(frozen=True)
class StoredEvent:
    path: Path
    inserted: bool
    seq: int


@dataclass(frozen=True)
class InsertResult:
    inserted: bool
    seq: int
    slug: str
    mirror_layout: int = MIRROR_LAYOUT_SESSION
    stored_ts: str | None = None
    previous_slug: str | None = None
    previous_session: tuple[str, str, int, str, str] | None = None
    session_created: bool = False


def _date_from_ts(ts: str) -> str:
    date = ts[:10]
    if "/" in date or "\\" in date or ".." in date:
        raise ValueError(f"Unsafe ts for date routing: {ts!r}")
    return date


def _root_path(root: str | Path | None) -> Path:
    return Path(root).expanduser() if root else journal_root()


def _session_hash(agent: str, session_id: str) -> str:
    return hashlib.sha256(f"{agent}\0{session_id}".encode("utf-8")).hexdigest()[:8]


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", "ignore")


def session_slug(name: str) -> str:
    normalized = unicodedata.normalize("NFC", name).casefold()
    slug = re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE).strip("-_.")
    return _truncate_utf8(slug, SESSION_SLUG_MAX_BYTES).rstrip("-_.")


def _fallback_name(agent: str, session_id: str) -> str:
    return f"session-{_session_hash(agent, session_id)}"


def _event_path(root: str | Path, event: dict[str, Any], slug: str | None = None) -> Path:
    date = _date_from_ts(event["ts"])
    resolved_slug = slug or session_slug(str(event.get("session_name") or ""))
    if not resolved_slug:
        session_id = str(event.get("session_id") or "")
        agent = str(event.get("agent") or "unknown")
        resolved_slug = _fallback_name(agent, session_id) if session_id else "unscoped"
    return Path(root).expanduser() / "events" / f"{date}-{resolved_slug}.jsonl"


def append_jsonl_event(root: str | Path, event: dict[str, Any], *, slug: str | None = None) -> Path:
    root_path = Path(root).expanduser()
    event_dir = secure_dir(root_path / "events")
    path = _event_path(root_path, event, slug)
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
    # Split on "\n" only: str.splitlines() also breaks on U+2028, U+2029 and
    # U+0085, which json.dumps(ensure_ascii=False) leaves unescaped in strings.
    for line in jsonl_path.read_text(encoding="utf-8").split("\n"):
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


def _migrate_to_3(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE events ADD COLUMN session_name TEXT")
    conn.execute("ALTER TABLE events ADD COLUMN session_name_source TEXT")
    conn.execute(
        f"ALTER TABLE events ADD COLUMN mirror_layout INTEGER NOT NULL DEFAULT {MIRROR_LAYOUT_LEGACY}"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session_name ON events(session_name)")
    conn.execute(
        """
        CREATE TABLE sessions (
          agent TEXT NOT NULL,
          session_id TEXT NOT NULL,
          session_name TEXT NOT NULL,
          session_name_source TEXT NOT NULL,
          source_rank INTEGER NOT NULL,
          file_slug TEXT NOT NULL UNIQUE,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (agent, session_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE note_receipts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          instance TEXT NOT NULL,
          argument_key TEXT NOT NULL,
          event_id TEXT NOT NULL,
          created REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_note_receipts_lookup ON note_receipts(instance, argument_key, created, id)"
    )


MIGRATIONS = {
    1: _migrate_to_1,
    2: _migrate_to_2,
    3: _migrate_to_3,
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


def _slug_available(conn: sqlite3.Connection, slug: str, agent: str, session_id: str) -> bool:
    if slug in RESERVED_SESSION_SLUGS:
        return False
    owner = conn.execute(
        "SELECT agent, session_id FROM sessions WHERE file_slug = ?", (slug,)
    ).fetchone()
    return owner is None or (owner["agent"], owner["session_id"]) == (agent, session_id)


def _slug_with_suffix(base: str, suffix: str) -> str:
    budget = SESSION_SLUG_MAX_BYTES - len(suffix.encode("utf-8"))
    stem = _truncate_utf8(base, budget).rstrip("-_.") or "session"
    return f"{stem}{suffix}"


def _unique_slug(conn: sqlite3.Connection, base: str, agent: str, session_id: str) -> str:
    if _slug_available(conn, base, agent, session_id):
        return base
    digest = hashlib.sha256(f"{agent}\0{session_id}".encode("utf-8")).hexdigest()
    for width in range(8, len(digest) + 1, 8):
        candidate = _slug_with_suffix(base, f"-{digest[:width]}")
        if _slug_available(conn, candidate, agent, session_id):
            return candidate
    for ordinal in count(2):
        candidate = _slug_with_suffix(base, f"-{digest}-{ordinal}")
        if _slug_available(conn, candidate, agent, session_id):
            return candidate
    raise AssertionError("unreachable")


def _agent_key(value: Any) -> str:
    return str(value or "unknown")


def _timestamp_value(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _canonical_session(
    conn: sqlite3.Connection, event: dict[str, Any]
) -> tuple[str, str | None, tuple[str, str, int, str, str] | None, bool]:
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        event.pop("session_name", None)
        event.pop("session_name_source", None)
        return "unscoped", None, None, False

    agent = _agent_key(event.get("agent"))
    incoming_name = event.get("session_name")
    if not isinstance(incoming_name, str) or not incoming_name.strip():
        incoming_name = _fallback_name(agent, session_id)
        incoming_source = "id"
    else:
        incoming_name = " ".join(incoming_name.split())
        incoming_source = str(event.get("session_name_source") or "prompt")
        if incoming_source not in SESSION_SOURCE_RANK:
            incoming_source = "prompt"
    incoming_rank = SESSION_SOURCE_RANK[incoming_source]

    current = conn.execute(
        "SELECT session_name, session_name_source, source_rank, file_slug, updated_at FROM sessions "
        "WHERE agent = ? AND session_id = ?",
        (agent, session_id),
    ).fetchone()
    previous_slug = None
    previous_session = None
    same_rank_can_rename = incoming_source in {
        "codex-thread",
        "claude-ai-title",
        "claude-slug",
    }
    stale_same_rank = (
        current is not None
        and incoming_rank == current["source_rank"]
        and same_rank_can_rename
        and _timestamp_value(event["ts"]) < _timestamp_value(current["updated_at"])
    )
    if current is not None and (
        incoming_rank < current["source_rank"]
        or incoming_rank == current["source_rank"] and not same_rank_can_rename
        or stale_same_rank
    ):
        name = current["session_name"]
        source = current["session_name_source"]
        slug = current["file_slug"]
    else:
        name = incoming_name
        source = incoming_source
        if (
            current is not None
            and current["session_name"] == name
            and current["session_name_source"] == source
        ):
            # Once allocated, a collision suffix belongs to this session. Do not
            # opportunistically shorten it when another session vacates the base.
            slug = current["file_slug"]
        else:
            base = session_slug(name) or _fallback_name(agent, session_id)
            slug = _unique_slug(conn, base, agent, session_id)
        if current is not None:
            previous_session = (
                current["session_name"],
                current["session_name_source"],
                current["source_rank"],
                current["file_slug"],
                current["updated_at"],
            )
            if current["file_slug"] != slug:
                previous_slug = current["file_slug"]
        conn.execute(
            """
            INSERT INTO sessions (
              agent, session_id, session_name, session_name_source,
              source_rank, file_slug, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent, session_id) DO UPDATE SET
              session_name = excluded.session_name,
              session_name_source = excluded.session_name_source,
              source_rank = excluded.source_rank,
              file_slug = excluded.file_slug,
              updated_at = excluded.updated_at
            """,
            (agent, session_id, name, source, incoming_rank, slug, event["ts"]),
        )
    event["session_name"] = name
    event["session_name_source"] = source
    return slug, previous_slug, previous_session, current is None


def _insert_row(
    conn: sqlite3.Connection,
    event: dict[str, Any],
    *,
    preserve_payload: bool = False,
) -> InsertResult:
    with _immediate_transaction(conn):
        existing = conn.execute(
            "SELECT seq, ts, agent, session_id, session_name, session_name_source, mirror_layout "
            "FROM events WHERE event_id = ?",
            (event["event_id"],),
        ).fetchone()
        if existing is not None:
            layout = existing["mirror_layout"]
            slug = ""
            if layout == MIRROR_LAYOUT_SESSION:
                current = None
                if existing["session_id"]:
                    current = conn.execute(
                        "SELECT file_slug FROM sessions WHERE agent = ? AND session_id = ?",
                        (_agent_key(existing["agent"]), existing["session_id"]),
                    ).fetchone()
                if current is not None:
                    slug = current["file_slug"]
                else:
                    slug = session_slug(existing["session_name"] or "")
                    if not slug and existing["session_id"]:
                        slug = _fallback_name(_agent_key(existing["agent"]), existing["session_id"])
                    slug = slug or "unscoped"
            return InsertResult(
                False,
                existing["seq"],
                slug,
                mirror_layout=layout,
                stored_ts=existing["ts"],
            )
        missing = object()
        original_name = event.get("session_name", missing)
        original_source = event.get("session_name_source", missing)
        slug, previous_slug, previous_session, session_created = _canonical_session(conn, event)
        if preserve_payload:
            if original_name is missing:
                event.pop("session_name", None)
            else:
                event["session_name"] = original_name
            if original_source is missing:
                event.pop("session_name_source", None)
            else:
                event["session_name_source"] = original_source
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events").fetchone()[0]
        conn.execute(
            """
            INSERT INTO events (
              event_id, schema_version, ts, event_type, agent, session_id, cwd,
              repo, branch, commit_hash, exit_code, duration_ms, raw_json,
              seq, agent_id, turn_id, session_name, session_name_source, mirror_layout
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                event.get("session_name"),
                event.get("session_name_source"),
                MIRROR_LAYOUT_SESSION,
            ),
        )
    return InsertResult(
        True,
        seq,
        slug,
        mirror_layout=MIRROR_LAYOUT_SESSION,
        stored_ts=event["ts"],
        previous_slug=previous_slug,
        previous_session=previous_session,
        session_created=session_created,
    )


def _insert_event(root: str | Path | None, event: dict[str, Any]) -> tuple[bool, int]:
    init_db(root)
    with closing(connect(root)) as conn:
        result = _insert_row(conn, event)
    return result.inserted, result.seq


def insert_event(root: str | Path | None, event: dict[str, Any]) -> bool:
    return _insert_event(root, event)[0]


def delete_event(root: str | Path | None, event_id: str) -> None:
    with closing(connect(root)) as conn:
        conn.execute("DELETE FROM events WHERE event_id = ?", (event_id,))


def _restore_session(root: str | Path, event: dict[str, Any], result: InsertResult) -> None:
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    agent = _agent_key(event.get("agent"))
    with closing(connect(root)) as conn, _immediate_transaction(conn):
        if result.session_created:
            conn.execute(
                "DELETE FROM sessions WHERE agent = ? AND session_id = ?",
                (agent, session_id),
            )
        elif result.previous_session is not None:
            conn.execute(
                "UPDATE sessions SET session_name = ?, session_name_source = ?, source_rank = ?, "
                "file_slug = ?, updated_at = ? WHERE agent = ? AND session_id = ?",
                (*result.previous_session, agent, session_id),
            )


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


def _write_jsonl_snapshot(path: Path, events: list[dict[str, Any]]) -> None:
    secure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            for event in events:
                out.write((json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
        secure_file(path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _rewrite_session_jsonl(
    root_path: Path,
    agent: str,
    session_id: str,
    slug: str,
    previous_slug: str,
) -> None:
    with closing(connect(root_path)) as conn:
        rows = conn.execute(
            "SELECT raw_json FROM events WHERE COALESCE(NULLIF(agent, ''), 'unknown') = ? AND session_id = ? "
            "AND mirror_layout = ? ORDER BY seq",
            (agent, session_id, MIRROR_LAYOUT_SESSION),
        ).fetchall()
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        event = json.loads(row["raw_json"])
        by_date.setdefault(_date_from_ts(event["ts"]), []).append(event)
    for date, events in by_date.items():
        target = root_path / "events" / f"{date}-{slug}.jsonl"
        _write_jsonl_snapshot(target, events)
    for date in by_date:
        target = root_path / "events" / f"{date}-{slug}.jsonl"
        old = root_path / "events" / f"{date}-{previous_slug}.jsonl"
        if old != target:
            try:
                old.unlink()
            except OSError:
                pass


def _remove_rewritten_targets(root_path: Path, event: dict[str, Any], result: InsertResult) -> None:
    if result.previous_slug is None or result.previous_slug == result.slug:
        return
    dates = {_date_from_ts(event["ts"])}
    session_id = event.get("session_id")
    agent = _agent_key(event.get("agent"))
    if isinstance(session_id, str) and session_id:
        with closing(connect(root_path)) as conn:
            rows = conn.execute(
                "SELECT DISTINCT substr(ts, 1, 10) AS date FROM events "
                "WHERE COALESCE(NULLIF(agent, ''), 'unknown') = ? "
                "AND session_id = ? AND mirror_layout = ?",
                (agent, session_id, MIRROR_LAYOUT_SESSION),
            ).fetchall()
        dates.update(row["date"] for row in rows)
    for date in dates:
        try:
            (root_path / "events" / f"{date}-{result.slug}.jsonl").unlink()
        except OSError:
            pass


def _persist(
    root_path: Path,
    event: dict[str, Any],
    *,
    preserve_payload: bool = False,
) -> StoredEvent:
    path = _event_path(root_path, event)
    # init_db may take the write lock itself; flock does not nest across file
    # descriptors of one process, so it has to run before the lock below.
    init_db(root_path)
    with _write_lock(root_path):
        with closing(connect(root_path)) as conn:
            result = _insert_row(conn, event, preserve_payload=preserve_payload)
        if not result.inserted:
            date = _date_from_ts(result.stored_ts or event["ts"])
            if result.mirror_layout == MIRROR_LAYOUT_LEGACY:
                path = root_path / "events" / f"{date}.jsonl"
            else:
                path = root_path / "events" / f"{date}-{result.slug}.jsonl"
            return StoredEvent(path, False, result.seq)
        path = _event_path(root_path, event, result.slug)
        try:
            if result.previous_slug is not None:
                _rewrite_session_jsonl(
                    root_path,
                    _agent_key(event.get("agent")),
                    str(event.get("session_id") or ""),
                    result.slug,
                    result.previous_slug,
                )
            else:
                path = append_jsonl_event(root_path, event, slug=result.slug)
        except OSError:
            # Keep SQLite (read path) and the JSONL mirror consistent: if the
            # mirror append fails, roll back the SQLite row so a retry re-attempts
            # both writes instead of permanently skipping the mirror line.
            try:
                delete_event(root_path, event["event_id"])
                _restore_session(root_path, event, result)
                _remove_rewritten_targets(root_path, event, result)
            except Exception:
                # SQLite is the primary read path. If rollback also fails, keep
                # surfacing the original append error; masking it would make the
                # actionable filesystem failure harder to diagnose.
                pass
            raise
    return StoredEvent(path, True, result.seq)


def persist_event(root: str | Path | None, event: dict[str, Any]) -> tuple[Path, bool]:
    # This low-level entry point is used for live project mirrors and backfills.
    # Their row payload must remain byte-for-byte equivalent at the JSON object
    # level to the global event; only destination-local routing is canonicalized.
    stored = _persist(_root_path(root), dict(event), preserve_payload=True)
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
    root_path = _root_path(root)
    # Checked here rather than in persist_event: mirror roots carry their own
    # default config.toml and are gated by the project's include_prompts.
    if event.get("event_type") == USER_MESSAGE_EVENT_TYPE and not load_config(root_path)["privacy"]["log_prompts"]:
        raise PromptLoggingDisabledError(
            f"user_message rejected: [privacy] log_prompts is disabled in {root_path / 'config.toml'}"
        )
    stored = _persist(root_path, event)
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


def read_track_events(
    root: str | Path | None,
    agent: str,
    session_id: str,
    agent_id: str | None,
    event_types: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Read one agent track in write order.

    A track is the events of one client session written by one agent: the
    main agent when ``agent_id`` is None, otherwise the sub-agent with that id.
    """
    root_path = _root_path(root)
    init_db(root_path)
    query = "SELECT raw_json FROM events WHERE agent = ? AND session_id = ? AND agent_id IS ?"
    params: list[str | None] = [agent, session_id, agent_id]
    types = list(event_types or [])
    if types:
        query += f" AND event_type IN ({', '.join('?' for _ in types)})"
        params.extend(types)
    query += " ORDER BY seq"
    with closing(connect(root_path)) as conn:
        rows = conn.execute(query, params).fetchall()
    return _rows_to_events(rows)
