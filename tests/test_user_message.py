import json
from contextlib import closing

import pytest

from agentic_journal import storage
from agentic_journal.cli import main
from agentic_journal.events import (
    SESSION_EVENT_TYPES,
    SESSION_VIEW_EVENT_TYPES,
    USER_MESSAGE_EVENT_TYPE,
    PromptLoggingDisabledError,
    normalize_event,
)
from agentic_journal.project_config import load_project_config
from agentic_journal.storage import read_events_for_date, read_jsonl_events, record_event

VERBATIM_TEXTS = {
    "edge_whitespace": "  \t leading and trailing \n\n",
    "crlf": "first line\r\nsecond line\r\n",
    "tabs": "col1\tcol2\t\tcol3",
    "long": "0123456789" * 3000,
    "zwj_emoji": "family \U0001f468\u200d\U0001f469\u200d\U0001f467 flag \U0001f3f3\ufe0f\u200d\U0001f308",
    "fenced_code": "Run this:\n```bash\nmake test\n```\n",
    "secret_assignment": "API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345\npassword: hunter2",
    "pem": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----\n",
    "line_separators": "a\u2028b\u2029c\x85d\x1ce",
}


def _enable_prompts(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text("[privacy]\nlog_prompts = true\n", encoding="utf-8")


def _user_message(text, **updates):
    raw = {
        "event_type": USER_MESSAGE_EVENT_TYPE,
        "agent": "claude",
        "session_id": "session-1",
        "ts": "2026-09-14T10:12:03.482915+03:00",
        "semantic": {"text": text, "origin": "claude:cli"},
    }
    raw.update(updates)
    return normalize_event(raw)


def _write_project_config(project, *extra_mirror_lines):
    config_path = project / ".agentic-journal.toml"
    config_path.write_text(
        "\n".join(["[project]", 'path = "."', "", "[mirror]", 'path = ".agentic-journal"', *extra_mirror_lines, ""]),
        encoding="utf-8",
    )
    return config_path


@pytest.mark.parametrize("name", sorted(VERBATIM_TEXTS))
def test_user_message_text_is_stored_byte_for_byte(tmp_path, name):
    root = tmp_path / "journal"
    _enable_prompts(root)
    text = VERBATIM_TEXTS[name]

    stored = record_event(root, _user_message(text))

    with closing(storage.connect(root)) as conn:
        raw_json = conn.execute("SELECT raw_json FROM events").fetchone()["raw_json"]
    [jsonl_event] = read_jsonl_events(stored.path)
    for event in (json.loads(raw_json), jsonl_event, read_events_for_date(root, "2026-09-14")[0]):
        assert event["semantic"]["text"].encode("utf-8") == text.encode("utf-8")


def test_user_message_other_fields_are_still_redacted():
    event = _user_message("keep API_KEY=abc123", semantic={"text": "keep API_KEY=abc123", "origin": "password=hunter2"})

    assert event["semantic"]["text"] == "keep API_KEY=abc123"
    assert "hunter2" not in event["semantic"]["origin"]


def test_user_message_requires_string_text():
    with pytest.raises(ValueError, match="semantic.text"):
        normalize_event({"event_type": USER_MESSAGE_EVENT_TYPE, "agent": "claude", "semantic": {"origin": "claude:cli"}})
    with pytest.raises(ValueError, match="semantic.text"):
        normalize_event({"event_type": USER_MESSAGE_EVENT_TYPE, "agent": "claude", "semantic": {"text": 42}})


def test_user_message_is_rejected_when_prompt_logging_is_disabled(tmp_path):
    root = tmp_path / "journal"

    with pytest.raises(PromptLoggingDisabledError, match="log_prompts"):
        record_event(root, _user_message("hello"))

    assert read_events_for_date(root, None) == []
    assert not (root / "events" / "2026-09-14-unscoped.jsonl").exists()


def test_user_message_is_not_a_session_event():
    assert USER_MESSAGE_EVENT_TYPE not in SESSION_EVENT_TYPES
    assert USER_MESSAGE_EVENT_TYPE not in SESSION_VIEW_EVENT_TYPES


def test_project_mirror_skips_user_message_without_include_prompts(tmp_path):
    global_root = tmp_path / "global"
    _enable_prompts(global_root)
    project = tmp_path / "project"
    project.mkdir()
    _write_project_config(project)
    note = normalize_event({"event_type": "semantic_note", "agent": "claude", "cwd": str(project), "semantic": {"note": "n"}})

    record_event(global_root, _user_message("hello", cwd=str(project)))
    record_event(global_root, note)

    mirror_types = [event["event_type"] for event in read_events_for_date(project / ".agentic-journal", None)]
    assert mirror_types == ["semantic_note"]
    assert len(read_events_for_date(global_root, None)) == 2


def test_project_mirror_receives_user_message_with_include_prompts(tmp_path):
    global_root = tmp_path / "global"
    _enable_prompts(global_root)
    project = tmp_path / "project"
    project.mkdir()
    _write_project_config(project, "include_prompts = true")

    record_event(global_root, _user_message("hello\r\n", cwd=str(project)))

    [mirrored] = read_events_for_date(project / ".agentic-journal", None)
    assert mirrored["semantic"]["text"] == "hello\r\n"


def test_mirror_sync_skips_user_message_without_include_prompts(tmp_path, capsys):
    global_root = tmp_path / "global"
    _enable_prompts(global_root)
    project = tmp_path / "project"
    project.mkdir()
    config_path = _write_project_config(project)
    record_event(global_root, _user_message("hello", cwd=str(project)))
    config_path.unlink()
    record_event(
        global_root,
        normalize_event({"event_type": "semantic_note", "agent": "claude", "cwd": str(project), "semantic": {"note": "n"}}),
    )
    _write_project_config(project)

    assert main(["mirror", "sync", "--root", str(global_root), "--config", str(config_path)]) == 0

    assert "matched=1" in capsys.readouterr().out
    assert [event["event_type"] for event in read_events_for_date(project / ".agentic-journal", None)] == ["semantic_note"]


def test_project_config_rejects_non_boolean_include_prompts(tmp_path):
    config_path = _write_project_config(tmp_path, 'include_prompts = "yes"')

    with pytest.raises(ValueError, match="include_prompts"):
        load_project_config(config_path)
