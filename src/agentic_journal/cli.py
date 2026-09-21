from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

# Only the modules every short command needs load here. Reports, diagnostics,
# installers, and the web server are imported by their handlers, so hook-driven
# commands such as `ingest` and `events` start quickly.
from agentic_journal.config import journal_root, secure_dir, secure_file
from agentic_journal.events import (
    GIT_COMMIT_EVENT_TYPE,
    JOURNAL_MISSING_STATUS,
    SESSION_OUTCOME_EVENT_TYPES,
    TASK_BLOCKED_EVENT_TYPE,
    TASK_COMPLETED_CLAIM_EVENT_TYPE,
    VERIFICATION_EVENT_TYPE,
    normalize_event,
)
from agentic_journal.git_context import event_context, get_git_context, get_head_commit_files
from agentic_journal.storage import (
    persist_event,
    read_events_for_date,
    read_events_for_session,
    read_track_events,
    record_event,
    write_event,
)


def run_web_server(*args, **kwargs):
    from agentic_journal.web import run_web_server as _run_web_server

    return _run_web_server(*args, **kwargs)


def _add_event_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("event", help="Write an event")
    parser.add_argument("--type", required=True, dest="event_type")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--task")
    parser.add_argument("--note")
    parser.add_argument("--summary")
    parser.add_argument(
        "--outcome",
        choices=["completed", "in_progress", "blocked", "no_work", "unknown"],
    )
    parser.add_argument("--reason")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--operation")
    parser.add_argument("--source")
    parser.add_argument("--status")
    parser.add_argument("--session-id")
    parser.add_argument("--command", dest="agent_command")
    parser.add_argument("--exit-code", type=int)
    parser.add_argument("--duration-ms", type=int)
    parser.add_argument("--input-tokens", type=int)
    parser.add_argument("--output-tokens", type=int)
    parser.add_argument("--cached-input-tokens", type=int)
    parser.add_argument("--cache-creation-input-tokens", type=int)
    parser.add_argument("--reasoning-tokens", type=int)
    parser.add_argument("--error-code")
    parser.add_argument("--verification")
    parser.add_argument("--verification-status", choices=["passed", "failed"])


def _add_report_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("report", help="Generate a daily report")
    parser.add_argument("--root", help="Read and write reports from this journal root")
    parser.add_argument("--today", action="store_true")
    parser.add_argument("--date")
    parser.add_argument("--print", action="store_true", dest="print_report")
    parser.add_argument("--write", action="store_true", help="Write report to disk (default behavior)")
    parser.add_argument("--output")


def _add_status_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("status", help="Print a compact daily status")
    parser.add_argument("--root", help="Read events from this journal root")
    parser.add_argument("--today", action="store_true")
    parser.add_argument("--date")


def _add_doctor_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("doctor", help="Audit Agentic Journal setup and daily coverage")
    parser.add_argument("--root", help="Read events from this journal root")
    parser.add_argument("--today", action="store_true")
    parser.add_argument("--date")


def _add_web_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("web", help="Run the live web dashboard")
    parser.add_argument("--root", help="Serve events from this journal root")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--today", action="store_true")
    parser.add_argument("--date")
    parser.add_argument("--refresh-ms", type=int, default=2000)
    parser.add_argument("--token", help="Require this token for /api/events requests")


def _add_install_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("install", help="Install wrappers or print setup instructions")
    install_sub = parser.add_subparsers(dest="install_target")
    install_sub.add_parser("wrappers", help="Install codex/claude/gemini wrappers")
    install_sub.add_parser("shell-profile", help="Add wrapper PATH setup to .zprofile and .zshrc")
    install_sub.add_parser("agent-instructions", help="Add Agentic Journal reporting rules to global agent instruction files")
    git_hook = install_sub.add_parser("git-hook", help="Install post-commit hook into a repo")
    git_hook.add_argument("--repo", default=".")
    install_sub.add_parser("mcp-snippets", help="Print MCP config snippets")


def _add_guard_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("guard", help="Enforce journal lifecycle expectations")
    guard_sub = parser.add_subparsers(dest="guard_target")
    session_end = guard_sub.add_parser("session-end", help="Write a fallback event if a session lacks semantic journal output")
    session_end.add_argument("--agent", required=True)
    session_end.add_argument("--session-id", required=True)
    session_end.add_argument(
        "--note",
        default="Session ended without semantic journal entry",
        help="Fallback note written when no semantic event exists for the session",
    )


def _add_mirror_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("mirror", help="Manage project-local journal mirrors")
    mirror_sub = parser.add_subparsers(dest="mirror_target")
    sync = mirror_sub.add_parser("sync", help="Backfill matching events into a project mirror")
    sync.add_argument("--root", help="Source journal root; defaults to the global journal")
    sync.add_argument("--config", required=True, help="Path to the project's .agentic-journal.toml")
    sync.add_argument("--date", help="Only sync events for this YYYY-MM-DD date")
    sync.add_argument("--from", dest="date_from", help="Only sync events on or after this YYYY-MM-DD date")
    sync.add_argument("--to", dest="date_to", help="Only sync events on or before this YYYY-MM-DD date")


def _add_ingest_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "ingest",
        help="Write one JSON event read from stdin",
        description=(
            "Normalize and store one JSON event from stdin, then print "
            '{"event_id", "inserted", "seq"}. Exit 0 when stored or already present, '
            "2 when the event is invalid or refused by config, 1 on a storage error."
        ),
    )
    parser.add_argument("--root", help="Write to this journal root")


def _add_note_bridge_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    subparsers.add_parser("note-bridge", help="Record a hook note from JSON on stdin")


def _add_events_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("events", help="Print one agent track as JSONL in write order")
    parser.add_argument("--root", help="Read events from this journal root")
    parser.add_argument("--agent", required=True, help="Client that wrote the events, e.g. claude or codex")
    parser.add_argument("--session-id", required=True)
    track = parser.add_mutually_exclusive_group(required=True)
    track.add_argument("--agent-id", help="Select a sub-agent track")
    track.add_argument("--main", action="store_true", help="Select the main agent track (events without agent_id)")
    parser.add_argument("--type", action="append", dest="event_types", help="Only this event type; repeatable")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-journal")
    subparsers = parser.add_subparsers(dest="command")
    _add_event_parser(subparsers)
    _add_report_parser(subparsers)
    _add_status_parser(subparsers)
    _add_doctor_parser(subparsers)
    _add_web_parser(subparsers)
    _add_install_parser(subparsers)
    _add_guard_parser(subparsers)
    _add_mirror_parser(subparsers)
    _add_ingest_parser(subparsers)
    _add_note_bridge_parser(subparsers)
    _add_events_parser(subparsers)
    return parser


def _event_from_args(args: argparse.Namespace) -> dict:
    cwd = Path.cwd()
    git_context = get_git_context(cwd)
    semantic = {}
    if args.task:
        semantic["task_id"] = args.task
    if args.note:
        semantic["note"] = args.note
    if args.summary:
        semantic["summary"] = args.summary
    if args.outcome:
        semantic["outcome"] = args.outcome
    if args.reason:
        semantic["reason"] = args.reason
    if args.provider:
        semantic["provider"] = args.provider
    if args.model:
        semantic["model"] = args.model
    if args.operation:
        semantic["operation"] = args.operation
    if args.source:
        semantic["source"] = args.source
    if args.status:
        semantic["status"] = args.status
    if args.event_type == TASK_COMPLETED_CLAIM_EVENT_TYPE:
        semantic["status"] = "completed_claimed"
    if args.event_type == TASK_BLOCKED_EVENT_TYPE:
        semantic["status"] = "blocked"

    evidence = {}
    if args.verification:
        evidence["verification"] = args.verification
    if args.verification_status:
        evidence["verification_status"] = args.verification_status
    token_usage = {
        key: value
        for key, value in {
            "input_tokens": args.input_tokens,
            "output_tokens": args.output_tokens,
            "cached_input_tokens": args.cached_input_tokens,
            "cache_creation_input_tokens": args.cache_creation_input_tokens,
            "reasoning_tokens": args.reasoning_tokens,
        }.items()
        if value is not None
    }
    if token_usage:
        evidence["token_usage"] = token_usage
    if args.error_code:
        evidence["error_code"] = args.error_code

    files_changed = git_context.get("changed_files") or []
    if args.event_type == GIT_COMMIT_EVENT_TYPE:
        files_changed = get_head_commit_files(cwd)

    return normalize_event(
        {
            "event_type": args.event_type,
            "agent": args.agent,
            "session_id": args.session_id,
            **event_context(cwd, git=git_context),
            "command": args.agent_command,
            "exit_code": args.exit_code,
            "duration_ms": args.duration_ms,
            "files_changed": files_changed,
            "semantic": semantic,
            "evidence": evidence,
        }
    )


def _handle_event(args: argparse.Namespace) -> int:
    event = _event_from_args(args)
    write_event(journal_root(), event)
    print("logged")
    return 0


def _report_date(args: argparse.Namespace) -> str:
    if args.date:
        return args.date
    if args.today:
        return datetime.now().astimezone().date().isoformat()
    return datetime.now().astimezone().date().isoformat()


def _root_from_args(args: argparse.Namespace) -> Path:
    root = getattr(args, "root", None)
    return Path(root).expanduser() if root else journal_root()


def _handle_report(args: argparse.Namespace) -> int:
    from agentic_journal.report import render_daily_report

    date = _report_date(args)
    root = _root_from_args(args)
    resolved_date, markdown, _ = render_daily_report(root, date)
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
    else:
        output = secure_dir(root / "reports") / f"{resolved_date}.md"
        output.write_text(markdown, encoding="utf-8")
        secure_file(output)
    if args.print_report:
        print(markdown)
    else:
        print(output)
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    from agentic_journal.report import build_provider_coverage, classify_daily_work

    date = _report_date(args)
    events = read_events_for_date(_root_from_args(args), date)
    classified = classify_daily_work(events)
    print(f"Date: {date}")
    print(f"Raw events: {len(events)}")
    print(f"Completed verified: {len(classified['completed_verified'])}")
    print(f"Completed claimed: {len(classified['completed_claimed'])}")
    print(f"Session summaries: {len(classified['session_summaries'])}")
    print(f"Model activity: {len(classified['model_activity'])}")
    print(f"In progress: {len(classified['in_progress'])}")
    print(f"Blocked: {len(classified['blocked'])}")
    print(f"Notes: {len(classified['notes'])}")
    print(f"Risky: {len(classified['risky'])}")
    for agent, stats in build_provider_coverage(events).items():
        print(
            f"{agent}: {stats['sessions']} sessions, {stats['summarized']} summarized, "
            f"{stats['missing']} missing, {stats['in_progress']} in progress, "
            f"{stats['coverage_percent']}% coverage"
        )
    return 0


def _handle_doctor(args: argparse.Namespace) -> int:
    from agentic_journal.diagnostics import build_doctor_report, build_doctor_result

    date = _report_date(args)
    result = build_doctor_result(_root_from_args(args), date)
    print(build_doctor_report(result), end="")
    return 0


def _handle_web(args: argparse.Namespace) -> int:
    try:
        run_web_server(
            _root_from_args(args),
            args.host,
            args.port,
            args.date,
            refresh_ms=args.refresh_ms,
            api_token=args.token,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def _event_date(event: dict) -> str:
    return str(event.get("ts", ""))[:10]


def _event_within_sync_range(event: dict, args: argparse.Namespace) -> bool:
    event_date = _event_date(event)
    if args.date and event_date != args.date:
        return False
    if args.date_from and event_date < args.date_from:
        return False
    if args.date_to and event_date > args.date_to:
        return False
    return True


def _handle_mirror(args: argparse.Namespace) -> int:
    from agentic_journal.project_config import event_matches_project, load_project_config

    if args.mirror_target == "sync":
        source_root = _root_from_args(args)
        config = load_project_config(args.config)
        events = read_events_for_date(source_root, args.date if args.date else None)
        scanned = len(events)
        matched = 0
        inserted = 0
        duplicates = 0
        for event in events:
            if not _event_within_sync_range(event, args):
                continue
            if not event_matches_project(config, event):
                continue
            matched += 1
            _, was_inserted = persist_event(config.mirror_root, event)
            if was_inserted:
                inserted += 1
            else:
                duplicates += 1
        print(f"scanned={scanned} matched={matched} inserted={inserted} duplicates={duplicates}")
        return 0
    print("Choose a mirror target: sync", file=sys.stderr)
    return 2


def _handle_install(args: argparse.Namespace) -> int:
    from agentic_journal.install import (
        claude_mcp_snippet,
        codex_mcp_snippet,
        gemini_mcp_snippet,
        install_agent_instructions,
        install_git_hook,
        install_shell_profile,
        install_wrappers,
    )

    if args.install_target == "wrappers":
        installed = install_wrappers(journal_root())
        for agent, path in installed.items():
            print(f"{agent}: {path}")
        print(f'Add this to PATH before real agent binaries: export PATH="{journal_root() / "bin"}:$PATH"')
        return 0
    if args.install_target == "shell-profile":
        for path in install_shell_profile(journal_root()):
            print(path)
        return 0
    if args.install_target == "agent-instructions":
        for agent, path in install_agent_instructions().items():
            print(f"{agent}: {path}")
        return 0
    if args.install_target == "git-hook":
        print(install_git_hook(args.repo))
        return 0
    if args.install_target == "mcp-snippets":
        root = Path.cwd()
        print("# Codex")
        print(codex_mcp_snippet(root))
        print("# Claude Code")
        print(claude_mcp_snippet(root))
        print("# Gemini CLI")
        print(gemini_mcp_snippet(root))
        return 0
    print("Choose an install target: wrappers", file=sys.stderr)
    return 2


def _event_matches_session(event: dict, session_id: str, agent: str) -> bool:
    if event.get("session_id") != session_id:
        return False
    return not event.get("agent") or event.get("agent") == agent


def _has_semantic_session_entry(events: list[dict], session_id: str, agent: str) -> bool:
    return any(
        _event_matches_session(event, session_id, agent)
        and (
            event.get("event_type") in SESSION_OUTCOME_EVENT_TYPES
            or (event.get("semantic") or {}).get("status") == "blocked"
        )
        for event in events
    )


def _has_guard_fallback(events: list[dict], session_id: str, agent: str) -> bool:
    return any(
        _event_matches_session(event, session_id, agent)
        and event.get("event_type") == VERIFICATION_EVENT_TYPE
        and (event.get("semantic") or {}).get("status") == JOURNAL_MISSING_STATUS
        for event in events
    )


def _guard_fallback_event(args: argparse.Namespace) -> dict:
    cwd = Path.cwd()
    git_context = get_git_context(cwd)
    return normalize_event(
        {
            "event_type": VERIFICATION_EVENT_TYPE,
            "agent": args.agent,
            "session_id": args.session_id,
            **event_context(cwd, git=git_context),
            "files_changed": git_context.get("changed_files") or [],
            "semantic": {"status": JOURNAL_MISSING_STATUS, "note": args.note},
            "evidence": {
                "verification": "agentic-journal guard session-end",
                "verification_status": "failed",
            },
        }
    )


def _handle_guard(args: argparse.Namespace) -> int:
    if args.guard_target == "session-end":
        root = journal_root()
        events = read_events_for_session(root, args.session_id)
        if _has_semantic_session_entry(events, args.session_id, args.agent):
            print("semantic journal entry exists")
            return 0
        if _has_guard_fallback(events, args.session_id, args.agent):
            print("guard already recorded missing semantic journal entry")
            return 0
        write_event(root, _guard_fallback_event(args))
        print("guarded: missing semantic journal entry")
        return 0
    print("Choose a guard target: session-end", file=sys.stderr)
    return 2


def _handle_ingest(args: argparse.Namespace) -> int:
    try:
        raw = json.loads(sys.stdin.buffer.read())
        if not isinstance(raw, dict):
            raise ValueError("expected one JSON object")
        event = normalize_event(raw)
        stored = record_event(_root_from_args(args), event)
    except (ValueError, TypeError) as exc:
        # Covers malformed JSON, schema violations, text that cannot be stored
        # as UTF-8, and dialogue text refused by [privacy] log_prompts.
        print(f"agentic-journal ingest: rejected: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(f"agentic-journal ingest: storage error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"event_id": event["event_id"], "inserted": stored.inserted, "seq": stored.seq}))
    return 0


def _handle_note_bridge() -> int:
    from agentic_journal.mcp_server import journal_note
    from agentic_journal.note_bridge import CLIENT_NAMES, add_receipt, client_instance

    try:
        payload = json.loads(sys.stdin.buffer.read())
        if not isinstance(payload, dict):
            raise ValueError("expected one JSON object")
        note, category = payload.get("note"), payload.get("category", "")
        session_id, runtime = payload.get("session_id"), payload.get("runtime")
        if not isinstance(note, str) or not note.strip() or not isinstance(category, str):
            raise ValueError("note must be nonempty and category must be a string")
        if not isinstance(session_id, str) or not isinstance(runtime, dict) \
                or runtime.get("client") not in CLIENT_NAMES:
            raise ValueError("session_id and client runtime are required")
        instance = client_instance()  # Fail before writing when the clients cannot share receipts.
        result = journal_note(note=note, category=category, session_id=session_id, runtime=runtime)
        if not result.startswith("logged "):
            raise ValueError(result)
        add_receipt(instance, note, category, result.removeprefix("logged "))
    except (ValueError, TypeError) as exc:
        print(f"agentic-journal note-bridge: rejected: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"agentic-journal note-bridge: storage error: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


def _handle_events(args: argparse.Namespace) -> int:
    events = read_track_events(
        _root_from_args(args),
        agent=args.agent,
        session_id=args.session_id,
        agent_id=None if args.main else args.agent_id,
        event_types=args.event_types,
    )
    # Bytes, not text: stored user text must come out unchanged whatever the
    # locale encoding of stdout is.
    for event in events:
        sys.stdout.buffer.write(json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if args.command == "event":
        return _handle_event(args)
    if args.command == "report":
        return _handle_report(args)
    if args.command == "status":
        return _handle_status(args)
    if args.command == "doctor":
        return _handle_doctor(args)
    if args.command == "web":
        return _handle_web(args)
    if args.command == "install":
        return _handle_install(args)
    if args.command == "guard":
        return _handle_guard(args)
    if args.command == "mirror":
        return _handle_mirror(args)
    if args.command == "ingest":
        return _handle_ingest(args)
    if args.command == "note-bridge":
        return _handle_note_bridge()
    if args.command == "events":
        return _handle_events(args)
    parser.print_help()
    return 0


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
