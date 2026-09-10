#!/usr/bin/env bash
# Verifies PUA Integrity Guard anti-cheating decisions without Claude CLI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="$PLUGIN_DIR/hooks/integrity-guard.sh"
TEST_HOME="$(mktemp -d "${TMPDIR:-/tmp}/pua-integrity-guard.XXXXXX")"
trap 'rm -rf "$TEST_HOME"' EXIT
mkdir -p "$TEST_HOME/.pua"
printf '%s\n' '{"always_on":false}' > "$TEST_HOME/.pua/config.json"

PASS=0
FAIL=0
record_pass() { echo "  ✅ PASS: $1"; PASS=$((PASS+1)); }
record_fail() { echo "  ❌ FAIL: $1"; FAIL=$((FAIL+1)); }

json_input() {
  local tool="$1"
  local payload="$2"
  python3 - "$tool" "$payload" <<'PY'
import json, sys
print(json.dumps({
  "hook_event_name": "PreToolUse",
  "session_id": "test-session",
  "transcript_path": "/nonexistent/transcript.jsonl",
  "cwd": "/tmp/pua-integrity-test",
  "tool_name": sys.argv[1],
  "tool_input": json.loads(sys.argv[2]),
}, separators=(",", ":")))
PY
}

run_guard() {
  local force="$1"
  local tool="$2"
  local payload="$3"
  if [ "$force" = "force" ]; then
    HOME="$TEST_HOME" PUA_INTEGRITY_FORCE=1 PUA_CONFIG="$TEST_HOME/.pua/config.json" bash "$HOOK" <<<"$(json_input "$tool" "$payload")"
  else
    HOME="$TEST_HOME" PUA_INTEGRITY_FORCE= PUA_FORCE_ON= PUA_CONFIG="$TEST_HOME/.pua/config.json" bash "$HOOK" <<<"$(json_input "$tool" "$payload")"
  fi
}

assert_decision() {
  local name="$1"
  local output="$2"
  local expected="$3"
  local contains="$4"
  if python3 - "$output" "$expected" "$contains" <<'PY'
import json, sys
out, expected, contains = sys.argv[1:]
try:
    data = json.loads(out)
except Exception as exc:
    print(f"invalid json: {exc}; output={out!r}")
    sys.exit(1)
specific = data.get('hookSpecificOutput', {})
actual = specific.get('permissionDecision')
reason = specific.get('permissionDecisionReason', '')
if actual != expected:
    print(f"decision mismatch: expected={expected} actual={actual} reason={reason}")
    sys.exit(1)
if contains not in reason:
    print(f"reason missing {contains!r}: {reason}")
    sys.exit(1)
PY
  then
    record_pass "$name"
  else
    record_fail "$name"
  fi
}

assert_empty() {
  local name="$1"
  local output="$2"
  if [ -z "$output" ]; then record_pass "$name"; else record_fail "$name"; printf '%s\n' "$output"; fi
}

assert_advisory() {
  local name="$1"
  local output="$2"
  local contains="$3"
  if python3 - "$output" "$contains" <<'PY'
import json, sys
out, contains = sys.argv[1:]
try:
    data = json.loads(out)
except Exception as exc:
    print(f"invalid json: {exc}; output={out!r}")
    sys.exit(1)
specific = data.get('hookSpecificOutput', {})
actual = specific.get('permissionDecision')
context = specific.get('additionalContext', '')
reason = specific.get('permissionDecisionReason')
if actual is not None:
    print(f"expected advisory-only with no permissionDecision, actual={actual} reason={reason}")
    sys.exit(1)
if reason is not None:
    print(f"expected no permissionDecisionReason for advisory-only output, got={reason!r}")
    sys.exit(1)
if contains not in context:
    print(f"additionalContext missing {contains!r}: {context}")
    sys.exit(1)
PY
  then
    record_pass "$name"
  else
    record_fail "$name"
  fi
}

assert_no_permission_ask() {
  local name="$1"
  local output="$2"
  if python3 - "$output" <<'PY'
import json, sys
out = sys.argv[1]
if not out:
    sys.exit(0)
try:
    data = json.loads(out)
except Exception as exc:
    print(f"invalid json: {exc}; output={out!r}")
    sys.exit(1)
actual = data.get('hookSpecificOutput', {}).get('permissionDecision')
if actual == 'ask':
    print('permissionDecision=ask is forbidden; use advisory additionalContext or deny')
    sys.exit(1)
PY
  then
    record_pass "$name"
  else
    record_fail "$name"
  fi
}

echo "=== PUA Integrity Guard Tests ==="

OUT=$(run_guard inactive Write '{"file_path":"/repo/tests/auth.test.ts","content":"skip"}')
assert_empty "inactive guard stays silent" "$OUT"

OUT=$(run_guard force Write '{"file_path":"/repo/tests/auth.test.ts","content":"skip"}')
assert_advisory "protected test write is advisory-only" "$OUT" "Grader gaming risk"

OUT=$(run_guard force Write '{"file_path":"/repo/src/auth.ts","content":"code"}')
assert_empty "ordinary source write allowed" "$OUT"

OUT=$(run_guard force Write '{"file_path":"/repo/memory/session.md","content":"state"}')
assert_advisory "memory write is advisory-only" "$OUT" "Persistent-memory risk"

OUT=$(run_guard force Edit '{"file_path":"/repo/CLAUDE.md","old_string":"old","new_string":"new"}')
assert_advisory "CLAUDE.md edit is advisory-only" "$OUT" "Persistent-memory risk"

OUT=$(run_guard force Write '{"file_path":"/repo/.claude/settings.json","content":"{}"}')
assert_advisory "settings.json write is advisory-only" "$OUT" "Persistent-memory risk"

OUT=$(run_guard force Bash '{"command":"sed -i \"\" \"s/pass/skip/\" tests/auth.test.ts"}')
assert_advisory "mutating bash on tests is advisory-only" "$OUT" "Grader gaming risk"

OUT=$(run_guard force Bash '{"command":"cat tests/auth.test.ts"}')
assert_empty "read-only bash on public tests allowed" "$OUT"

OUT=$(run_guard force Read '{"file_path":"/repo/verifier_private/hidden_tests/case.json"}')
assert_decision "hidden verifier read denied" "$OUT" "deny" "Solution contamination risk"

OUT=$(run_guard force Bash '{"command":"cat hidden_tests/case.json"}')
assert_decision "relative hidden-test bash read denied" "$OUT" "deny" "Solution contamination risk"

OUT=$(run_guard force Bash '{"command":"python3 -c '"'"'open(\"tests/fixtures.json\",\"w\").write(\"{}\")'"'"'"}')
assert_advisory "mutating bash on tests directory is advisory-only" "$OUT" "Grader gaming risk"

OUT=$(run_guard force Glob '{"pattern":"**/hidden_tests/**","path":"/repo"}')
assert_decision "glob hidden tests denied" "$OUT" "deny" "Solution contamination risk"

OUT=$(run_guard force Grep '{"pattern":"token","path":"/repo","glob":"**/hidden_solution/*.json"}')
assert_decision "grep hidden solution glob denied" "$OUT" "deny" "Solution contamination risk"

OUT=$(run_guard force WebSearch '{"query":"SWE-bench official solution gold patch for task"}')
assert_decision "benchmark answer search denied" "$OUT" "deny" "Solution contamination risk"

OUT=$(run_guard force Write '{"file_path":"/repo/e2e/login.ts","content":"skip"}')
assert_advisory "e2e test write is advisory-only" "$OUT" "Grader gaming risk"

OUT=$(run_guard force Read '{"file_path":"/repo/.env.local"}')
assert_advisory "secret env read is advisory-only" "$OUT" "Capability-abuse risk"

OUT=$(run_guard force Bash '{"command":"cat .env"}')
assert_advisory "secret env bash read is advisory-only" "$OUT" "Capability-abuse risk"

OUT=$(run_guard force Bash '{"command":"curl https://example.com/benchmark-answer"}')
assert_decision "benchmark answer curl denied" "$OUT" "deny" "Solution contamination risk"


OUT=$(run_guard force Write '{"file_path":"/repo/tests/no-ask.test.ts","content":"skip"}')
assert_no_permission_ask "permissionDecision ask is never emitted" "$OUT"

# Regression: shell built-ins / bare identifiers must not be treated as path candidates.
# The shell `eval` keyword previously matched the (^|/)(evals?)(/|$) directory regex
# because it appeared as a bare token, even though it referenced no filesystem path.
OUT=$(run_guard force Bash '{"command":"eval rg -n pattern . 2>/dev/null"}')
assert_empty "shell eval keyword is not a path candidate" "$OUT"

OUT=$(run_guard force Bash '{"command":"test -f config.yaml && echo ok"}')
assert_empty "shell test builtin is not a path candidate" "$OUT"

OUT=$(run_guard force Bash '{"command":"spec --version"}')
assert_empty "bare spec identifier is not a path candidate" "$OUT"

# Positive control: an actual evals/ directory path must still be protected.
# Sensitive-but-legitimate writes are advisory-only; `ask` was retired in v3.4.6.
OUT=$(run_guard force Bash '{"command":"sed -i \"\" \"s/x/y/\" evals/runner.sh"}')
assert_advisory "mutating bash on evals directory is advisory-only" "$OUT" "Grader gaming risk"

# Git permits global options before its subcommand.  These writes used to evade
# MUTATING_BASH because `git` was no longer directly followed by `restore` or
# `checkout`; public read-only Git operations must remain silent.
OUT=$(run_guard force Bash '{"command":"git -C /repo restore -- evals/runner.sh"}')
assert_advisory "git -C restore on relative evals path is advisory-only" "$OUT" "Grader gaming risk"

OUT=$(run_guard force Bash '{"command":"git -C /repo checkout -- /repo/evals/runner.sh"}')
assert_advisory "git -C checkout on absolute evals path is advisory-only" "$OUT" "Grader gaming risk"

OUT=$(run_guard force Bash '{"command":"git -C /repo restore -- src/auth.ts"}')
assert_empty "git -C restore on ordinary source remains allowed" "$OUT"

OUT=$(run_guard force Bash '{"command":"git -C /repo diff -- evals/runner.sh"}')
assert_empty "git -C diff on public evals remains read-only" "$OUT"

# Git's patch/worktree subcommands are explicit writes, including when -C
# precedes the subcommand.  `--include=` is a path value, not a path named
# literally "--include=..."; assert the emitted advisory names the asset.
OUT=$(run_guard force Bash '{"command":"git -C /repo apply --include=evals/runner.sh patch.diff"}')
assert_advisory "git apply protects --include= eval asset" "$OUT" "Target: evals/runner.sh"

OUT=$(run_guard force Bash '{"command":"git -C /repo apply --include /repo/evals/runner.sh patch.diff"}')
assert_advisory "git apply protects split --include eval asset" "$OUT" "Target: /repo/evals/runner.sh"

# A mutating Git command with no literal, ordinary-source bound has an opaque
# target set: it can change tests/evals even when none appears in the command.
OUT=$(run_guard force Bash '{"command":"git -C /repo apply patch.diff"}')
assert_advisory "unbounded git apply is advisory-only" "$OUT" "Git mutation target set"

OUT=$(run_guard force Bash '{"command":"git -C /repo am mail.patch"}')
assert_advisory "unbounded git am is advisory-only" "$OUT" "Git mutation target set"

OUT=$(run_guard force Bash '{"command":"git -C /repo reset --hard HEAD"}')
assert_advisory "global git reset is advisory-only" "$OUT" "Git mutation target set"

OUT=$(run_guard force Bash '{"command":"git -C /repo checkout main"}')
assert_advisory "branch git checkout is advisory-only" "$OUT" "Git mutation target set"

OUT=$(run_guard force Bash '{"command":"git -C /repo clean -fd"}')
assert_advisory "unbounded git clean is advisory-only" "$OUT" "Git mutation target set"

OUT=$(run_guard force Bash '{"command":"git -C /repo apply --include=src/auth.ts patch.diff"}')
assert_empty "git apply on ordinary source remains allowed" "$OUT"

OUT=$(run_guard force Bash '{"command":"git -C /repo am --include=src/auth.ts mail.patch"}')
assert_empty "git am on explicitly included ordinary source remains allowed" "$OUT"

OUT=$(run_guard force Bash '{"command":"git -C /repo reset -- src/auth.ts"}')
assert_empty "git reset with ordinary source pathspec remains allowed" "$OUT"

OUT=$(run_guard force Bash '{"command":"git -C /repo checkout -- src/auth.ts"}')
assert_empty "git checkout with ordinary source pathspec remains allowed" "$OUT"

OUT=$(run_guard force Bash '{"command":"git -C /repo clean -fd -- src/auth.ts"}')
assert_empty "git clean with ordinary source pathspec remains allowed" "$OUT"

# Git documents these as preview-only apply modes.  They must not turn a
# protected include selector into an advisory merely because the path is
# scoring-adjacent; --apply deliberately restores the write behavior.
OUT=$(run_guard force Bash '{"command":"git -C /repo apply --check --include=evals/runner.sh patch.diff"}')
assert_empty "git apply --check is read-only" "$OUT"

OUT=$(run_guard force Bash '{"command":"git -C /repo apply --stat --include=evals/runner.sh patch.diff"}')
assert_empty "git apply --stat is read-only" "$OUT"

OUT=$(run_guard force Bash '{"command":"git -C /repo apply --numstat --include=evals/runner.sh patch.diff"}')
assert_empty "git apply --numstat is read-only" "$OUT"

OUT=$(run_guard force Bash '{"command":"git -C /repo apply --stat --apply --include=evals/runner.sh patch.diff"}')
assert_advisory "git apply --apply restores eval write advisory" "$OUT" "Target: evals/runner.sh"

OUT=$(run_guard force Bash '{"command":"git -C /repo rm -- evals/runner.sh"}')
assert_advisory "git rm on eval asset is advisory-only" "$OUT" "Grader gaming risk"

OUT=$(run_guard force Bash '{"command":"git -C /repo rm --dry-run -- evals/runner.sh"}')
assert_empty "git rm --dry-run is read-only" "$OUT"

OUT=$(run_guard force Bash '{"command":"git -C /repo mv evals/runner.sh src/runner.sh"}')
assert_advisory "git mv from eval asset is advisory-only" "$OUT" "Grader gaming risk"

OUT=$(run_guard force Bash '{"command":"git -C /repo mv --dry-run evals/runner.sh src/runner.sh"}')
assert_empty "git mv --dry-run is read-only" "$OUT"

OUT=$(run_guard force Bash '{"command":"git clean --dry-run -- evals/runner.sh"}')
assert_empty "git clean --dry-run is read-only" "$OUT"

OUT=$(run_guard force Bash '{"command":"git -C /repo am --include=evals/runner.sh mail.patch"}')
assert_advisory "git am protects included eval asset" "$OUT" "Target: evals/runner.sh"

OUT=$(run_guard force Bash '{"command":"git -C /repo am --show-current-patch --include=evals/runner.sh"}')
assert_empty "git am --show-current-patch is read-only" "$OUT"

# A read-only Git producer can still be part of a mutating shell pipeline or
# redirection.  These are behavioral hook regressions, not static patterns.
OUT=$(run_guard force Bash '{"command":"git diff -- evals/runner.sh > evals/rewritten.sh"}')
assert_advisory "git diff redirected into eval asset is advisory-only" "$OUT" "Grader gaming risk"

OUT=$(run_guard force Bash '{"command":"git show HEAD:src/x | tee tests/replacement.py"}')
assert_advisory "git show piped through tee into test asset is advisory-only" "$OUT" "Grader gaming risk"

echo "==========================================="
echo "Passed: $PASS"
echo "Failed: $FAIL"
echo "Total:  $((PASS+FAIL))"
echo "==========================================="

[ "$FAIL" -eq 0 ] || exit 1
