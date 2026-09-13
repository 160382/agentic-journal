from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_journal.events import USER_MESSAGE_EVENT_TYPE

CONFIG_FILENAME = ".agentic-journal.toml"


@dataclass(frozen=True)
class ProjectMirrorConfig:
    config_path: Path
    project_id: str | None
    project_path: Path
    mirror_enabled: bool
    mirror_root: Path
    include_prompts: bool = False


def _resolve_from(base: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Missing [{key}] section")
    return value


def _string_value(table: dict[str, Any], key: str, *, required: bool = True) -> str | None:
    value = table.get(key)
    if value is None:
        if required:
            raise ValueError(f"Missing {key!r}")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key!r} must be a non-empty string")
    return value.strip()


def load_project_config(path: str | Path) -> ProjectMirrorConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML: {exc}") from exc

    config_dir = config_path.parent
    project = _table(data, "project")
    mirror = _table(data, "mirror")

    project_path_raw = _string_value(project, "path")
    mirror_path_raw = _string_value(mirror, "path")
    project_id = _string_value(project, "id", required=False)
    enabled = mirror.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("'enabled' must be a boolean")
    include_prompts = mirror.get("include_prompts", False)
    if not isinstance(include_prompts, bool):
        raise ValueError("'include_prompts' must be a boolean")

    return ProjectMirrorConfig(
        config_path=config_path,
        project_id=project_id,
        project_path=_resolve_from(config_dir, project_path_raw or "."),
        mirror_enabled=enabled,
        mirror_root=_resolve_from(config_dir, mirror_path_raw or ".agentic-journal"),
        include_prompts=include_prompts,
    )


def _path_matches(candidate: str | Path | None, root: Path) -> bool:
    if not candidate:
        return False
    candidate_path = Path(candidate).expanduser().resolve()
    return candidate_path == root or candidate_path.is_relative_to(root)


def event_matches_project(config: ProjectMirrorConfig, event: dict[str, Any]) -> bool:
    if not config.mirror_enabled:
        return False
    # Verbatim user text stays in the global journal unless the project opts in.
    if event.get("event_type") == USER_MESSAGE_EVENT_TYPE and not config.include_prompts:
        return False
    return _path_matches(event.get("repo"), config.project_path) or _path_matches(event.get("cwd"), config.project_path)


def _candidate_ancestors(raw_path: str | Path | None) -> list[Path]:
    if not raw_path:
        return []
    path = Path(raw_path).expanduser().resolve()
    return [path, *path.parents]


def discover_project_mirror_configs(event: dict[str, Any]) -> list[ProjectMirrorConfig]:
    configs: list[ProjectMirrorConfig] = []
    seen: set[Path] = set()
    for raw_path in (event.get("cwd"), event.get("repo")):
        for ancestor in _candidate_ancestors(raw_path):
            config_path = ancestor / CONFIG_FILENAME
            if config_path in seen or not config_path.exists():
                continue
            seen.add(config_path)
            try:
                configs.append(load_project_config(config_path))
            except (OSError, ValueError) as exc:
                print(f"invalid Agentic Journal project config {config_path}: {exc}", file=sys.stderr)
    return configs
