from __future__ import annotations

import copy
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

from agentic_journal.events import SCHEMA_VERSION

DEFAULT_CONFIG = f"""# Agentic Journal local configuration
[journal]
schema_version = {SCHEMA_VERSION}
jsonl_mirror = true
sqlite_wal = true

[privacy]
log_prompts = false
log_file_contents = false
redact_secrets = true
"""

# Parsed form of DEFAULT_CONFIG; load_config layers config.toml over it.
DEFAULTS: dict[str, dict[str, Any]] = {
    "journal": {"schema_version": SCHEMA_VERSION, "jsonl_mirror": True, "sqlite_wal": True},
    "privacy": {"log_prompts": False, "log_file_contents": False, "redact_secrets": True},
}

# Journal data (events, summaries, possibly secret-bearing free text) is
# sensitive and must not be world-readable on shared hosts.
DIR_MODE = 0o700
FILE_MODE = 0o600


def secure_dir(path: str | Path) -> Path:
    """Create ``path`` (and parents) and restrict it to the owner."""
    dir_path = Path(path).expanduser()
    dir_path.mkdir(parents=True, exist_ok=True)
    try:
        dir_path.chmod(DIR_MODE)
    except OSError:
        pass
    return dir_path


def secure_file(path: str | Path) -> Path:
    """Restrict an already-created file to the owner (best-effort)."""
    file_path = Path(path).expanduser()
    try:
        file_path.chmod(FILE_MODE)
    except OSError:
        pass
    return file_path


def journal_root() -> Path:
    configured = os.environ.get("AGENTIC_JOURNAL_HOME") or os.environ.get("AGENT_JOURNAL_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".agentic-journal"


def ensure_config(root: str | Path | None = None) -> Path:
    root_path = secure_dir(root if root is not None else journal_root())
    config_path = root_path / "config.toml"
    if not config_path.exists():
        config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")
        secure_file(config_path)
    return config_path


def _warn(message: str) -> None:
    print(f"agentic-journal config: {message}", file=sys.stderr)


def load_config(root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Read ``<root>/config.toml`` layered over ``DEFAULTS``.

    A missing file yields the defaults. An unreadable or invalid file, or a
    value whose type differs from its default, falls back to the default with a
    warning on stderr: a broken config must not stop event writes. Keys without
    a default are kept for the features that read them.
    """
    config = copy.deepcopy(DEFAULTS)
    config_path = (Path(root).expanduser() if root else journal_root()) / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        _warn(f"ignoring {config_path}: {exc}")
        return config
    for section, values in data.items():
        if not isinstance(values, dict):
            _warn(f"ignoring {section!r} in {config_path}: expected a table")
            continue
        defaults = DEFAULTS.get(section, {})
        target = config.setdefault(section, {})
        for key, value in values.items():
            # bool is an int subclass, so compare exact types.
            if key in defaults and type(value) is not type(defaults[key]):
                _warn(f"ignoring {section}.{key} in {config_path}: expected {type(defaults[key]).__name__}")
                continue
            target[key] = value
    return config
