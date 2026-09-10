#!/usr/bin/env bash
# PUA v2 Test Helpers — reusable assertion functions
# Source this file in test scripts: source "$(dirname "$0")/test-helpers.sh"

PLUGIN_DIR="${PLUGIN_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
EVIDENCE_INSPECTOR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/inspect-claude-evidence.py"

# Portable timeout wrapper. macOS does not ship GNU `timeout`; Homebrew may
# provide `gtimeout`, and Perl is available by default on macOS/Linux.
run_with_timeout() {
    local seconds="$1"
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$seconds" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then
        gtimeout "$seconds" "$@"
    else
        perl -e '
            my $seconds = shift @ARGV;
            $SIG{ALRM} = sub { exit 124 };
            alarm($seconds);
            exec @ARGV;
        ' "$seconds" "$@"
    fi
}

run_pua() {
    local prompt="$1"
    local max_turns="${2:-2}"
    local outfile eval_config
    outfile=$(mktemp)
    eval_config=$(mktemp)
    printf '%s\n' '{"always_on":true,"feedback_frequency":0}' > "$eval_config"
    local run_status=0
    PUA_CONFIG="$eval_config" run_with_timeout 90 claude -p "$prompt" \
        --plugin-dir "$PLUGIN_DIR" \
        --dangerously-skip-permissions \
        --max-turns "$max_turns" \
        --output-format stream-json \
        --verbose </dev/null 2>"${outfile}.stderr" > "$outfile" || run_status=$?
    rm -f "$eval_config"
    if [ "$run_status" -ne 0 ] || ! python3 "$EVIDENCE_INSPECTOR" "$outfile" --terminal-success; then
        echo "Claude evaluation did not complete successfully; evidence: $outfile" >&2
        return 1
    fi
    echo "$outfile"
}

assert_skill_triggered() {
    local file="$1"
    local skill="$2"
    local label="${3:-$skill}"
    if python3 "$EVIDENCE_INSPECTOR" "$file" --skill "$skill"; then
        echo "  ✅ PASS: $label triggered"
        return 0
    else
        echo "  ❌ FAIL: $label NOT triggered"
        return 1
    fi
}

assert_skill_not_triggered() {
    local file="$1"
    local skill="$2"
    local label="${3:-$skill}"
    if ! python3 "$EVIDENCE_INSPECTOR" "$file" --terminal-success; then
        echo "  ❌ FAIL: $label has no successful terminal result"
        return 1
    fi
    if python3 "$EVIDENCE_INSPECTOR" "$file" --skill "$skill"; then
        echo "  ❌ FAIL: $label triggered (should not)"
        return 1
    else
        echo "  ✅ PASS: $label not triggered"
        return 0
    fi
}

assert_contains() {
    local file="$1"
    local pattern="$2"
    local label="${3:-pattern check}"
    if python3 "$EVIDENCE_INSPECTOR" "$file" --contains "$pattern"; then
        echo "  ✅ PASS: $label"
        return 0
    else
        echo "  ❌ FAIL: $label (pattern: $pattern)"
        return 1
    fi
}

assert_not_contains() {
    local file="$1"
    local pattern="$2"
    local label="${3:-pattern check}"
    if ! python3 "$EVIDENCE_INSPECTOR" "$file" --terminal-success; then
        echo "  ❌ FAIL: $label has no successful terminal result"
        return 1
    fi
    if python3 "$EVIDENCE_INSPECTOR" "$file" --contains "$pattern"; then
        echo "  ❌ FAIL: $label (found: $pattern)"
        return 1
    else
        echo "  ✅ PASS: $label"
        return 0
    fi
}

count_matches() {
    local file="$1"
    local pattern="$2"
    python3 "$EVIDENCE_INSPECTOR" "$file" --count "$pattern"
}

export -f run_with_timeout run_pua assert_skill_triggered assert_skill_not_triggered assert_contains assert_not_contains count_matches
