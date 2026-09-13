#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

VENV="$TMP_ROOT/venv"
PYTHON="$VENV/bin/python"
JOURNAL_HOME="$TMP_ROOT/journal"

cd "$ROOT"

scripts/release-check.sh
uv build --wheel --out-dir "$TMP_ROOT/dist" >/dev/null
python3 -m venv "$VENV"
"$PYTHON" -m pip install --no-cache-dir --upgrade pip >/dev/null
"$PYTHON" -m pip install --no-cache-dir "$TMP_ROOT"/dist/*.whl >/dev/null

AGENTIC_JOURNAL_HOME="$JOURNAL_HOME" "$VENV/bin/agentic-journal" --help >/dev/null
AGENTIC_JOURNAL_HOME="$JOURNAL_HOME" "$VENV/bin/agentic-journal" web --help >/dev/null
AGENTIC_JOURNAL_HOME="$JOURNAL_HOME" "$VENV/bin/agentic-journal" doctor --date "$(date +%F)" >/dev/null
AGENTIC_JOURNAL_HOME="$JOURNAL_HOME" "$VENV/bin/agentic-journal" event --type agent_start --agent codex --session-id PACKAGE-MISSING >/dev/null
AGENTIC_JOURNAL_HOME="$JOURNAL_HOME" "$VENV/bin/agentic-journal" guard session-end --agent codex --session-id PACKAGE-MISSING >/dev/null
AGENTIC_JOURNAL_HOME="$JOURNAL_HOME" "$VENV/bin/agentic-journal" event --type agent_start --agent codex --session-id PACKAGE-SUMMARY >/dev/null
AGENTIC_JOURNAL_HOME="$JOURNAL_HOME" "$VENV/bin/agentic-journal" event --type session_summary --agent codex --session-id PACKAGE-SUMMARY --task PACKAGE-SMOKE --summary "packaged session summary" --outcome completed >/dev/null
AGENTIC_JOURNAL_HOME="$JOURNAL_HOME" "$VENV/bin/agentic-journal" guard session-end --agent codex --session-id PACKAGE-SUMMARY >/dev/null
test -x "$VENV/bin/agentic-journal-mcp"

"$PYTHON" - <<PY
from agentic_journal.web import build_events_payload, render_dashboard_html

payload = build_events_payload("$JOURNAL_HOME", "$(date +%F)")
assert payload["summary"]["risky"] == 1
assert payload["summary"]["session_summaries"] == 1
assert any(session["summary"] == "packaged session summary" for session in payload["sessions"])
html = render_dashboard_html()
assert "Agentic Journal Live" in html
assert "X-Agent-Journal-Token" in html
PY

AGENTIC_JOURNAL_HOME="$JOURNAL_HOME" "$PYTHON" - <<'PY'
from agentic_journal.mcp_server import create_mcp_server

server = create_mcp_server()
expected = {
    "journal_note",
    "journal_session_summary",
    "journal_task_completed",
    "journal_task_blocked",
    "journal_model_operation",
    "journal_daily_report",
}
assert expected.issubset(set(server._tool_manager._tools))
PY

AGENTIC_JOURNAL_HOME="$JOURNAL_HOME" AGENTIC_JOURNAL_SESSION_ID="PACKAGE-MCP-SESSION" "$PYTHON" - <<'PY'
from agentic_journal.mcp_server import journal_model_operation, journal_task_completed
from agentic_journal.storage import read_events_for_date

journal_task_completed(agent="claude", task_id="PACKAGE-MCP", note="packaged MCP context")
journal_model_operation(
    agent="cortex",
    provider="claude",
    model="claude-opus-4-8-thinking-high",
    operation="package-smoke",
    status="completed",
    input_tokens=1,
    output_tokens=1,
)
events = read_events_for_date(None, None)
claim = [event for event in events if event["event_type"] == "task_completed_claim"][-1]
model_operation = [event for event in events if event["event_type"] == "model_operation"][-1]
assert claim["session_id"] == "PACKAGE-MCP-SESSION"
assert model_operation["session_id"] == "PACKAGE-MCP-SESSION"
assert model_operation["evidence"]["token_usage"] == {"input_tokens": 1, "output_tokens": 1}
assert claim["cwd"]
PY

"$PYTHON" - <<PY
from agentic_journal.events import normalize_event
from agentic_journal.storage import write_event
from agentic_journal.web import create_web_handler
from http.server import ThreadingHTTPServer
import http.client
import json
import threading

root = "$TMP_ROOT/token-journal"
write_event(root, normalize_event({
    "event_type": "semantic_note",
    "agent": "codex",
    "semantic": {"note": "token smoke"},
}))
server = ThreadingHTTPServer(("127.0.0.1", 0), create_web_handler(root, "$(date +%F)", api_token="secret"))
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
host, port = server.server_address
conn = http.client.HTTPConnection(host, port, timeout=5)
try:
    conn.request("GET", "/api/events")
    response = conn.getresponse()
    response.read()
    assert response.status == 401
    conn.request("GET", "/api/events", headers={"X-Agent-Journal-Token": "secret"})
    response = conn.getresponse()
    body = response.read()
    assert response.status == 200
    assert json.loads(body)["raw_event_count"] == 1
finally:
    conn.close()
    server.shutdown()
    server.server_close()
PY

mkdir -p "$TMP_ROOT/hook-repo"
git -C "$TMP_ROOT/hook-repo" init >/dev/null
AGENTIC_JOURNAL_HOME="$TMP_ROOT/hook-journal" \
  "$VENV/bin/agentic-journal" install git-hook --repo "$TMP_ROOT/hook-repo" >"$TMP_ROOT/install-git-hook.out"
test -x "$TMP_ROOT/hook-repo/.git/hooks/post-commit"
grep -q "agentic-journal event --type git_commit" "$TMP_ROOT/hook-repo/.git/hooks/post-commit"

HOME="$TMP_ROOT/home" AGENTIC_JOURNAL_HOME="$TMP_ROOT/profile-journal" \
  "$VENV/bin/agentic-journal" install shell-profile >"$TMP_ROOT/install-shell-profile.out"
grep -q "agentic-journal wrappers" "$TMP_ROOT/home/.zprofile"
grep -q "agentic-journal wrappers" "$TMP_ROOT/home/.zshrc"

HOME="$TMP_ROOT/home" AGENTIC_JOURNAL_HOME="$TMP_ROOT/profile-journal" \
  "$VENV/bin/agentic-journal" install agent-instructions >"$TMP_ROOT/install-agent-instructions.out"
grep -q "journal_session_summary" "$TMP_ROOT/home/.codex/AGENTS.md"
grep -q "journal_session_summary" "$TMP_ROOT/home/.claude/CLAUDE.md"
grep -q "journal_session_summary" "$TMP_ROOT/home/.gemini/GEMINI.md"

mkdir -p "$TMP_ROOT/real"
cat > "$TMP_ROOT/real/codex" <<'SH'
#!/usr/bin/env sh
exit 7
SH
chmod +x "$TMP_ROOT/real/codex"

PATH="$TMP_ROOT/real:$VENV/bin:$PATH" \
  AGENTIC_JOURNAL_HOME="$JOURNAL_HOME" \
  "$VENV/bin/agentic-journal" install wrappers >"$TMP_ROOT/install-wrappers.out"
test -x "$JOURNAL_HOME/bin/codex"

if grep -q "scripts/wrappers" "$JOURNAL_HOME/bin/codex"; then
  echo "generated wrapper unexpectedly depends on scripts/wrappers" >&2
  exit 1
fi

set +e
PATH="$VENV/bin:$PATH" AGENTIC_JOURNAL_HOME="$JOURNAL_HOME" "$JOURNAL_HOME/bin/codex"
WRAPPER_STATUS=$?
set -e
test "$WRAPPER_STATUS" -eq 7

"$PYTHON" - <<PY
from pathlib import Path
import glob
import json

paths = glob.glob("$JOURNAL_HOME/events/*.jsonl")
assert paths, "packaged wrapper did not write JSONL events"
events = [json.loads(line) for line in Path(paths[0]).read_text().splitlines()]
wrapper_events = [event for event in events if event.get("command") == "codex"]
assert [event["event_type"] for event in wrapper_events] == ["agent_start", "agent_end"]
assert wrapper_events[-1]["exit_code"] == 7
assert any(
    event["event_type"] == "verification"
    and event.get("agent") == "codex"
    and (event.get("semantic") or {}).get("status") == "journal_missing"
    for event in events
)
PY

set +e
PATH="/usr/bin:/bin" AGENTIC_JOURNAL_HOME="$TMP_ROOT/missing-path-journal" \
  "$JOURNAL_HOME/bin/codex" 2>"$TMP_ROOT/missing-path.err"
MISSING_PATH_STATUS=$?
set -e
test "$MISSING_PATH_STATUS" -eq 7
grep -q "agentic-journal command not found" "$TMP_ROOT/missing-path.err"

echo "agentic-journal package smoke passed"
