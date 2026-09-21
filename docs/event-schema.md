# Agentic Journal Event Schema

Agentic Journal stores append-only events. Events are mirrored to per-session,
per-day JSONL files and inserted into SQLite.

Required fields:

- `schema_version`
- `event_id`
- `ts`
- `event_type`

Common optional fields:

- `agent`
- `session_id`
- `session_name` — human-readable name captured when the event is written
- `session_name_source` — `codex-thread`, `claude-ai-title`, `claude-slug`,
  `prompt`, or `id`
- `agent_id` — the sub-agent inside a session; absent for the main agent
- `agent_type`
- `turn_id` — the client turn or prompt the event belongs to
- `cwd`
- `repo`
- `branch`
- `commit`
- `command`
- `exit_code`
- `duration_ms`
- `files_changed`
- `semantic`
- `evidence`

Outcome events:

- `session_summary` is the preferred end-of-session semantic event for daily
  reporting.
- `semantic.summary` should be a concise human-readable description of what the
  agent did.
- `semantic.outcome` should be one of `completed`, `in_progress`, `blocked`,
  `no_work`, or `unknown`.
- `semantic.task_id` should be set when the session maps to a Backlog task or
  other stable task identifier.

Semantic note events:

- `semantic_note` holds `semantic.note` and an optional free-form
  `semantic.category` slug of at most 64 characters.
- `journal_note` accepts a `runtime` object from client hooks. Author and turn
  identity (`agent_id`, `agent_type`, `turn_id`) and session identity
  (`session_name`, `session_name_source`) land on the top level, `cwd`
  sets the event directory and git context, and execution conditions and
  measurements (model, modes, effort, `token_usage`, usage scope and status,
  elapsed time, native ids) land in `evidence`.

Model operation events:

- `model_operation` records one model call or model-backed step from a project
  runtime such as Cortex.
- Store labels in `semantic`: provider, model, operation, source, and status.
- Store measured facts in `evidence`: `token_usage` with numeric
  `input_tokens`, `output_tokens`, `cached_input_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens`,
  `cache_write_input_tokens`, `reasoning_tokens`, `reasoning_output_tokens`, or
  `total_tokens`, plus `error_code` when available.
- Use top-level `duration_ms` for elapsed runtime and `session_id` for the
  caller's correlation id when available.
- `model_operation` events are reported under Model Activity. They are not
  session outcome events and do not satisfy the session-end guard.
- Never store prompts, completions, transcripts, or file contents in
  `model_operation`.

User message events:

- `user_message` stores one message a person sent to an agent, exactly as the
  client delivered it. It is opt-in: a journal root accepts it only when its
  `config.toml` sets `[privacy] log_prompts = true`; otherwise the write is
  rejected and nothing is stored.
- `semantic.text` is required and holds the message verbatim. It is exempt from
  redaction, trimming, and the `MAX_SEMANTIC_TEXT` cap, so whitespace, line
  endings, code, Unicode, and anything secret-looking are kept byte for byte.
  Other fields, such as `semantic.origin` for the input source, are normalized
  like any other event.
- `user_message` is not a session outcome or lifecycle event and does not
  appear in daily report buckets. The web API returns it with the other raw
  events of the day.

Assistant message events:

- `assistant_message` stores one message an agent showed to the person, exactly as the client recorded it in its transcript. It is part of the verbatim dialogue history and follows the same opt-in: a journal root accepts it only with `[privacy] log_prompts = true`.
- `semantic.text` is required and stored verbatim under the same exemptions as `user_message` text. `semantic.phase` is required and is `commentary` for progress messages between tool calls or `final` for the answer that ends a turn; any other value is rejected.
- Writers set `ts` to the time the client recorded the message, not the time a hook stored it; an event without `ts` gets the write time, as any other event does. Writers that capture a message more than once must derive a stable `event_id` so repeats keep the original `seq`.
- `assistant_message` is not a session outcome or lifecycle event and does not appear in daily report buckets.

Correlation rules:

- `commit` is the strongest verification key. A `git_commit` item is
  `completed_verified` only when a passed `verification` event has the same
  commit hash.
- `session_id` links events emitted by the same agent process or MCP session.
  A `task_completed_claim` can become `completed_verified` when a passed
  `verification` event has the same `session_id` and a compatible `repo`;
  matching `session_id` is accepted only when task ids do not conflict.
- MCP tools inherit `AGENTIC_JOURNAL_SESSION_ID` and git context from the MCP
  server process. This lets `journal_task_completed`, `journal_task_blocked`,
  and `journal_session_summary` correlate with wrapper session lifecycle events.
- `semantic.task_id` links explicit task claims to explicit verification
  evidence. A `task_completed_claim` can become `completed_verified` when a
  passed `verification` event has the same `semantic.task_id` and a compatible
  `repo`.
- New writers should put task ids in `semantic.task_id`. Readers also accept a
  top-level `task_id` as a legacy fallback for older or external event writers.
- Repos are compatible when both events have the same `repo`, or when one side
  was produced by a legacy/MCP writer that did not include repo metadata.
- If no matching passed verification exists, task completion remains
  `completed_claimed` and commit work remains `in_progress`.
- Failed verification events are reported as risky and do not verify matching
  tasks or commits.
- `agentic-journal guard session-end` writes a failed `verification` event with
  `semantic.status = "journal_missing"` when a session ends without a
  `session_summary`, `task_completed_claim`, or `task_blocked` event. Generic
  `semantic_note` entries do not satisfy the session outcome requirement.
- Guard fallback events include `files_changed` from git status when available.
  This gives objective context for missing summaries without storing prompt
  transcripts or inventing completed work.
- Duplicate `event_id` writes are ignored so SQLite and the JSONL files
  remain aligned.
- SQLite stores the complete event payload in `raw_json`; `raw_json` is the source of truth.
  denormalized index columns such as `ts`, `repo`, `agent`, `event_type`, and
  `session_id` are query accelerators that can be rebuilt from `raw_json`.
- Events with a future `schema_version` are skipped by current readers instead
  of being silently misclassified.

Write ordering rules:

- Every inserted row gets `seq`, a journal-wide integer that increases by one
  per new event. A duplicate `event_id` keeps the `seq` of its first write.
  Readers order events by `seq`, not by `ts`, so events keep the order in which
  they were written even when a caller supplies an older timestamp.
- Default `ts` values carry microseconds.
- Writers to one journal root are serialized by an advisory `flock` on
  `<root>/.write.lock`: the SQLite insert and the JSONL append of one event
  happen under the lock, so JSONL lines follow `seq`. The lock also guards
  one-time database setup (WAL mode and migrations) for readers and writers.
  The lock relies on POSIX `fcntl.flock`; on platforms without it writes are
  not serialized and the JSONL order is not guaranteed to follow `seq`.
- Each JSONL line is encoded up front and appended with `O_APPEND`, so a long
  line is never interleaved with another writer's output.
- New JSONL files are named `YYYY-MM-DD-SESSION-SLUG.jsonl`; unscoped events
  use `YYYY-MM-DD-unscoped.jsonl`. The slug is Unicode-aware and collision-safe.
  A later native title updates the session registry and rebuilds only that
  session's new-layout files under the new name. Legacy `YYYY-MM-DD.jsonl`
  files are not migrated or removed.
- The JSONL files are a derived copy of SQLite. The lock orders
  concurrent writes but does not make the two stores atomic: a process killed
  between the insert and the append leaves the event in SQLite only, and
  nothing reconciles the JSONL copy afterwards.
- The database layout version lives in `PRAGMA user_version` and changes
  independently of the event `schema_version`. Layout 2 adds the `seq`,
  `agent_id`, and `turn_id` index columns; existing rows get `seq` in
  `ts, event_id` order during the migration. Layout 3 adds session identity,
  the per-session mirror marker and registries for session names and one-use
  note confirmations. Existing rows remain on the legacy mirror layout.

Project mirror rules:

- A `.agentic-journal.toml` file can opt a project into a local mirror. Matching
  is based on exact or child-path matches against the event `repo` or `cwd`.
- Mirror roots use the same event schema, SQLite table, per-session JSONL layout, and
  idempotent `event_id` behavior as the global journal.
- Mirror writes preserve the original event payload. They do not add
  project-specific fields or rewrite paths. A mirror root's session registry
  may route an out-of-order backfill to a differently named JSONL file, but the
  event stored in SQLite and JSONL is unchanged.
- Global journal writes remain primary. A mirror write failure is reported to
  stderr and does not fail the global write.
- Readers can point `status`, `report`, or `web` at a mirror root with `--root`
  and receive the same report or API payload shape as the global journal.
- `user_message` and `assistant_message` events reach a mirror, through live writes or `mirror sync`, only when the project config sets `[mirror] include_prompts = true`.

Privacy rules:

- Do not log prompt transcripts by default. The only exceptions are the opt-in `user_message` and `assistant_message` events described above, whose `semantic.text` is stored without redaction or truncation.
- Do not log full file contents.
- Redact known API keys, bearer tokens, passwords, URL credentials, PEM private
  keys, and secret-looking values in both structured fields and free text.
- Journal directories and files are owner-only by default: directories are
  written with `0700`, files and SQLite/WAL sidecars with `0600`.
- Project mirrors contain the same sensitive summaries, notes, paths, branch
  names, commit hashes, and evidence metadata as the global journal. Keep mirror
  directories out of git and mount them only into trusted containers.
- Free-text semantic fields are capped by `MAX_SEMANTIC_TEXT`; oversized
  `summary`, `note`, and `reason` values are truncated with `…[truncated]`.
- Numeric `evidence.token_usage` values are preserved for model operation
  reporting; secret-looking token keys such as API tokens are still redacted.
