"""One-use receipts for notes saved by a client hook before an MCP call.

Receipts contain only a hash of the visible arguments and an event id. The
runtime metadata stays in the journal, never in MCP tool arguments.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from agentic_journal.config import FILE_MODE, journal_root, secure_dir, secure_file

RECEIPT_TTL_SECONDS = 30
CLIENT_NAMES = {"codex", "claude"}


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


def _paths(instance: str) -> tuple[Path, Path]:
    directory = secure_dir(journal_root() / "note-bridge")
    return directory / f"{instance}.json", directory / f"{instance}.lock"


@contextmanager
def _locked(path: Path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), FILE_MODE)
    try:
        secure_file(path)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, list):
        raise ValueError("invalid journal receipt queue")
    now = time.time()
    return [item for item in data if isinstance(item, dict)
            and isinstance(item.get("created"), (int, float))
            and 0 <= now - item["created"] <= RECEIPT_TTL_SECONDS]


def _write(path: Path, entries: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(entries, out, ensure_ascii=False)
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def add_receipt(instance: str, note: str, category: str, event_id: str) -> None:
    path, lock = _paths(instance)
    with _locked(lock):
        entries = _read(path)
        entries.append({"key": _argument_key(note, category), "event_id": event_id,
                        "created": time.time()})
        _write(path, entries)


def consume_receipt(note: str, category: str) -> bool:
    path, lock = _paths(client_instance())
    key = _argument_key(note, category)
    with _locked(lock):
        entries = _read(path)
        matched = next((i for i, item in enumerate(entries) if item.get("key") == key), None)
        if matched is None:
            _write(path, entries)
            return False
        entries.pop(matched)
        _write(path, entries)
        return True
