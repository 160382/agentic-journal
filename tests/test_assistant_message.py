import json
from contextlib import closing

import pytest

from agentic_journal import storage
from agentic_journal.events import (
    ASSISTANT_MESSAGE_EVENT_TYPE,
    SESSION_EVENT_TYPES,
    SESSION_VIEW_EVENT_TYPES,
    PromptLoggingDisabledError,
    normalize_event,
)
from agentic_journal.storage import read_events_for_date, read_jsonl_events, read_track_events, record_event
from test_cli import _run_ingest
from test_user_message import VERBATIM_TEXTS, _enable_prompts, _user_message, _write_project_config


def _assistant_message(text, phase="commentary", **updates):
    raw = {
        "event_type": ASSISTANT_MESSAGE_EVENT_TYPE,
        "agent": "codex",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "ts": "2026-09-14T10:12:05.000123+03:00",
        "semantic": {"text": text, "phase": phase},
    }
    raw.update(updates)
    return normalize_event(raw)


@pytest.mark.parametrize("name", sorted(VERBATIM_TEXTS))
def test_assistant_message_text_is_stored_byte_for_byte(tmp_path, name):
    root = tmp_path / "journal"
    _enable_prompts(root)
    text = VERBATIM_TEXTS[name]

    stored = record_event(root, _assistant_message(text, phase="final"))

    with closing(storage.connect(root)) as conn:
        raw_json = conn.execute("SELECT raw_json FROM events").fetchone()["raw_json"]
    [jsonl_event] = read_jsonl_events(stored.path)
    for event in (json.loads(raw_json), jsonl_event, read_events_for_date(root, "2026-09-14")[0]):
        assert event["semantic"]["text"].encode("utf-8") == text.encode("utf-8")
        assert event["semantic"]["phase"] == "final"


def test_assistant_message_keeps_message_time_and_redacts_other_fields():
    event = _assistant_message("API_KEY=abc123", evidence={"message_id": "msg_1", "password": "hunter2"})

    assert event["ts"] == "2026-09-14T10:12:05.000123+03:00"
    assert event["semantic"]["text"] == "API_KEY=abc123"
    assert event["evidence"]["message_id"] == "msg_1"
    assert "hunter2" not in json.dumps(event)


@pytest.mark.parametrize("semantic", [{"phase": "final"}, {"text": 42, "phase": "final"}])
def test_assistant_message_requires_string_text(semantic):
    with pytest.raises(ValueError, match="assistant_message requires semantic.text"):
        normalize_event({"event_type": ASSISTANT_MESSAGE_EVENT_TYPE, "agent": "codex", "semantic": semantic})


@pytest.mark.parametrize("phase", [None, "", "final_answer", "Final", 1])
def test_assistant_message_requires_known_phase(phase):
    semantic = {"text": "done"} if phase is None else {"text": "done", "phase": phase}
    with pytest.raises(ValueError, match="semantic.phase"):
        normalize_event({"event_type": ASSISTANT_MESSAGE_EVENT_TYPE, "agent": "codex", "semantic": semantic})


def test_assistant_message_follows_prompt_logging(tmp_path):
    root = tmp_path / "journal"

    with pytest.raises(PromptLoggingDisabledError, match="assistant_message rejected: .*log_prompts"):
        record_event(root, _assistant_message("hello"))

    assert read_events_for_date(root, None) == []


def test_ingest_rejects_assistant_message_without_prompt_logging(tmp_path, monkeypatch, capsys):
    root = tmp_path / "journal"
    payload = {"event_type": ASSISTANT_MESSAGE_EVENT_TYPE, "agent": "codex", "session_id": "s1",
               "semantic": {"text": "hi", "phase": "final"}}

    assert _run_ingest(monkeypatch, payload, "--root", str(root)) == 2
    assert "log_prompts" in capsys.readouterr().err
    assert read_events_for_date(root, None) == []


def test_assistant_message_is_not_a_session_event():
    assert ASSISTANT_MESSAGE_EVENT_TYPE not in SESSION_EVENT_TYPES
    assert ASSISTANT_MESSAGE_EVENT_TYPE not in SESSION_VIEW_EVENT_TYPES


def test_repeated_event_id_keeps_the_first_seq(tmp_path):
    root = tmp_path / "journal"
    _enable_prompts(root)
    first = record_event(root, _assistant_message("step", event_id="fixed-id"))
    record_event(root, _assistant_message("note in between", event_id="other-id"))

    again = record_event(root, _assistant_message("step", event_id="fixed-id"))

    assert again.inserted is False
    assert again.seq == first.seq
    assert [event["event_id"] for event in read_events_for_date(root, None)] == ["fixed-id", "other-id"]


def test_dialogue_track_is_read_in_write_order(tmp_path):
    root = tmp_path / "journal"
    _enable_prompts(root)
    record_event(root, _user_message("start", agent="codex"))
    record_event(root, _assistant_message("working", ts="2026-09-14T10:12:04+03:00"))
    record_event(root, normalize_event({"event_type": "semantic_note", "agent": "codex", "session_id": "session-1",
                                        "semantic": {"note": "checked"}}))
    record_event(root, _assistant_message("sub step", agent_id="sub-1"))
    record_event(root, _assistant_message("done", phase="final"))

    main_track = read_track_events(root, agent="codex", session_id="session-1", agent_id=None,
                                   event_types=["user_message", "assistant_message", "semantic_note"])
    sub_track = read_track_events(root, agent="codex", session_id="session-1", agent_id="sub-1")

    assert [event.get("semantic", {}).get("text") or event["semantic"].get("note") for event in main_track] == [
        "start", "working", "checked", "done"]
    assert [event["semantic"]["text"] for event in sub_track] == ["sub step"]


def test_project_mirror_follows_include_prompts(tmp_path):
    global_root = tmp_path / "global"
    _enable_prompts(global_root)
    skipped, included = tmp_path / "skipped", tmp_path / "included"
    for project in (skipped, included):
        project.mkdir()
    _write_project_config(skipped)
    _write_project_config(included, "include_prompts = true")

    record_event(global_root, _assistant_message("hidden", cwd=str(skipped)))
    record_event(global_root, _assistant_message("shown\r\n", cwd=str(included)))

    assert read_events_for_date(skipped / ".agentic-journal", None) == []
    [mirrored] = read_events_for_date(included / ".agentic-journal", None)
    assert mirrored["semantic"]["text"] == "shown\r\n"
