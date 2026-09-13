# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses `vMAJOR.MINOR.PATCH` Git tags for GitHub releases.

## [Unreleased]

### Added

- `config.toml` in the journal root is now read: values are layered over the
  built-in defaults, and an invalid file or mistyped value falls back to the
  default with a warning.
- Opt-in `user_message` event: with `[privacy] log_prompts = true` the journal
  stores a person's message verbatim in `semantic.text`, bypassing redaction and
  the free-text cap. Project mirrors receive it only with
  `[mirror] include_prompts = true`.
- `journal_note` takes an optional `category` and a hook-supplied `runtime`
  object that places author, turn, directory, and usage metadata on the event;
  it returns `logged <event_id>` and declares non-destructive tool annotations.
  Events keep top-level `agent_id`, `agent_type`, and `turn_id`, and
  `token_usage` accepts cache-read, cache-write, reasoning-output, and total
  counters.

### Security

- Redaction now detects common secret formats by value (AWS keys, GitHub/GitLab
  PATs, Slack/Google keys, JWTs, Stripe keys, PEM private keys, and URL
  credentials), not just secret-named keys, so secrets in free-text fields are
  caught. The secret-named assignment matcher is now quote-aware and stops at
  delimiters instead of swallowing trailing content.
- The web dashboard refuses to bind a non-loopback host unless a token is set,
  since `/api/events` is unauthenticated without one. The dashboard strips the
  token from the URL after load and responses set `X-Content-Type-Options` and
  `Referrer-Policy: no-referrer`.
- Journal data files and directories are created with owner-only permissions
  (`0600`/`0700`).

### Fixed

- Web dashboard now computes the default date per request unless an explicit
  `--date` is provided, so long-running `agentic-journal web --today` servers roll
  over to the current day after midnight.
- Verification correlation no longer marks an unrelated task claim as verified
  just because it shares a session with a passed verification for a different
  task.
- `journal_daily_report` (MCP) now resolves a real date instead of writing
  `today.md`, and includes provider coverage, matching `agentic-journal report`.
- Event writes keep the SQLite store and JSONL mirror consistent: a failed
  mirror append rolls back the SQLite row instead of permanently desyncing.
- The session-end guard queries by indexed `session_id` instead of scanning the
  entire journal history on every session exit.
- A session that exits non-zero is no longer double-counted as both Risky and
  In Progress. Corrupt JSONL lines are skipped on read. Malformed timestamps and
  oversized free-text semantic fields are rejected/capped at normalization.
- The web token comparison no longer raises on a non-ASCII token.
- Concurrent writers to one journal root no longer interleave long JSONL lines
  or write them out of order: the SQLite insert and JSONL append run under an
  advisory `flock`, and each line is appended as one `O_APPEND` buffer.
- JSONL reading splits lines on `\n` only, so strings containing U+2028,
  U+2029, or U+0085 no longer corrupt the line they are in.
- Concurrent first use of a fresh journal no longer fails with
  `database is locked` while switching to WAL mode.

### Changed

- CI now runs the test and smoke suite on Python 3.11, 3.12, and 3.13.
- Events get a journal-wide `seq` and readers order by it instead of `ts`;
  default timestamps carry microseconds. The database layout moves to version 2
  (`seq`, `agent_id`, `turn_id` columns) with an in-place migration.

## [0.1.0] - 2026-06-02

### Added

- Local `agentic-journal` CLI for append-only AI agent activity events.
- `agentic-journal-mcp` server with semantic note, session summary, completed, blocked, and daily report tools.
- Codex, Claude, and Gemini wrapper installer with session start/end capture and missing-summary guard.
- Daily Markdown reports with verified, claimed, observed, blocked, notes, and risky evidence buckets.
- Live local web dashboard with provider coverage and missing-summary diagnosis.
- `agentic-journal doctor` setup audit for wrappers, MCP config hints, instructions, hooks, token mode, and provider coverage.
- Git post-commit hook installer that records commit metadata and changed files.
- Native hook guidance for Claude SessionEnd, Gemini hooks, and Codex automation.
- GitHub CI, package smoke checks, release metadata checks, and tag-driven GitHub release automation.
