"""One-use receipts for notes saved by a client hook before an MCP call.

Receipts contain only a hash of the visible arguments and an event id. The
runtime metadata stays in the journal, never in MCP tool arguments.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import closing
from pathlib import Path

from agentic_journal.config import journal_root
from agentic_journal.storage import _immediate_transaction, connect, init_db

RECEIPT_TTL_SECONDS = 30
CLIENT_NAMES = {"codex", "claude"}
LEGACY_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:json|lock)$")


def _proc_client(pid: int) -> str:
    for _ in range(64):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            close = stat.rfind(")")
            name = stat[stat.index("(") + 1:close]
            fields = stat[close + 2:].split()
            parent = int(fields[1])
            start_time = fields[19]
        except (OSError, ValueError, IndexError) as exc:
            raise RuntimeError("cannot identify journal client process") from exc
        if name in CLIENT_NAMES:
            value = f"{os.getuid()}:{pid}:{start_time}"
            return hashlib.sha256(value.encode()).hexdigest()[:32]
        if parent <= 0 or parent == pid:
            break
        pid = parent
    raise RuntimeError("cannot identify journal client process")


def _ps_client(pid: int) -> str:
    """Use the POSIX process table where Linux /proc is unavailable (macOS)."""
    try:
        proc = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=", "-o", "ppid=", "-o", "comm=", "-o", "lstart="],
            capture_output=True, text=True, check=True, timeout=2,
            env={"LC_ALL": "C", "TZ": "UTC"},
        )
        rows = {}
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) < 8:
                continue
            try:
                child, parent = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            name = Path(" ".join(parts[2:-5])).name
            start_time = " ".join(parts[-5:])
            rows[child] = (parent, name, start_time)
        for _ in range(64):
            parent, name, start_time = rows[pid]
            if name in CLIENT_NAMES:
                value = f"{os.getuid()}:{pid}:{start_time}"
                return hashlib.sha256(value.encode()).hexdigest()[:32]
            if parent <= 0 or parent == pid:
                break
            pid = parent
    except (OSError, KeyError, subprocess.SubprocessError) as exc:
        raise RuntimeError("cannot identify journal client process") from exc
    raise RuntimeError("cannot identify journal client process")


def client_instance() -> str:
    """Identify the client process shared by its hooks and MCP server."""
    override = os.environ.get("AGENTIC_JOURNAL_BRIDGE_INSTANCE")
    if override:
        return hashlib.sha256(override.encode()).hexdigest()[:32]
    pid = os.getpid()
    if Path("/proc/self/stat").exists():
        return _proc_client(pid)
    return _ps_client(pid)


def _argument_key(note: str, category: str) -> str:
    raw = json.dumps([note, category], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cleanup_legacy_receipts() -> None:
    """Best-effort removal of expired files from the pre-SQLite bridge."""
    directory = journal_root() / "note-bridge"
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    cutoff = time.time() - RECEIPT_TTL_SECONDS
    instances = {
        path.stem
        for path in entries
        if path.is_file() and LEGACY_NAME_RE.fullmatch(path.name)
    }
    for instance in instances:
        lock = directory / f"{instance}.lock"
        queue = directory / f"{instance}.json"
        try:
            candidates = [path for path in (lock, queue) if path.exists()]
            recent = any(path.stat().st_mtime > cutoff for path in candidates)
        except OSError:
            continue
        if not candidates or recent:
            continue
        try:
            fd = os.open(lock, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            fd = None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
        try:
            for path in (queue, lock):
                try:
                    path.unlink()
                except OSError:
                    pass
        finally:
            if fd is not None:
                os.close(fd)
    try:
        directory.rmdir()
    except OSError:
        pass


def _purge_expired(conn, now: float) -> None:
    conn.execute("DELETE FROM note_receipts WHERE created < ?", (now - RECEIPT_TTL_SECONDS,))


def add_receipt(instance: str, note: str, category: str, event_id: str) -> None:
    root = journal_root()
    init_db(root)
    with closing(connect(root)) as conn, _immediate_transaction(conn):
        now = time.time()
        _purge_expired(conn, now)
        conn.execute(
            "INSERT INTO note_receipts(instance, argument_key, event_id, created) VALUES (?, ?, ?, ?)",
            (instance, _argument_key(note, category), event_id, now),
        )
    _cleanup_legacy_receipts()


def consume_receipt(note: str, category: str) -> bool:
    root = journal_root()
    init_db(root)
    instance = client_instance()
    key = _argument_key(note, category)
    with closing(connect(root)) as conn, _immediate_transaction(conn):
        # A competing writer may hold BEGIN IMMEDIATE for most of the receipt
        # lifetime, so evaluate expiry only after this transaction has the lock.
        now = time.time()
        _purge_expired(conn, now)
        matched = conn.execute(
            "SELECT id FROM note_receipts WHERE instance = ? AND argument_key = ? "
            "ORDER BY created, id LIMIT 1",
            (instance, key),
        ).fetchone()
        if matched is not None:
            conn.execute("DELETE FROM note_receipts WHERE id = ?", (matched["id"],))
    _cleanup_legacy_receipts()
    return matched is not None
