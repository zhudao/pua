#!/usr/bin/env bash
# PUA Skill Triggering Test
# Tests whether the skill triggers on correct prompts and doesn't trigger on incorrect ones
#
# Usage: ./run-trigger-test.sh [--plugin-dir <path>]
# Requires: claude CLI

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/test-helpers.sh"
case "${1:-}" in
    --plugin-dir)
        [ "$#" -eq 2 ] || { echo "Usage: $0 [--plugin-dir PATH]" >&2; exit 2; }
        PLUGIN_DIR="$2"
        ;;
    -h|--help)
        echo "Usage: $0 [--plugin-dir PATH] (a single positional PATH is also accepted)"
        exit 0
        ;;
    "") ;;
    *)
        [ "$#" -eq 1 ] || { echo "Usage: $0 [--plugin-dir PATH]" >&2; exit 2; }
        PLUGIN_DIR="$1"
        ;;
esac
PLUGIN_DIR="$(cd "$PLUGIN_DIR" && pwd)"
RESULTS_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pua-evals.XXXXXX")"
EVAL_PUA_CONFIG="$RESULTS_DIR/pua-config.json"
printf '%s\n' '{"always_on":false,"feedback_frequency":0}' > "$EVAL_PUA_CONFIG"
EVAL_WORKSPACE="$RESULTS_DIR/workspace"
mkdir -p "$EVAL_WORKSPACE"

echo "=== PUA Trigger Tests ==="
echo "Plugin dir: $PLUGIN_DIR"
echo "Results: $RESULTS_DIR"
echo ""

PASS=0
FAIL=0

test_prompt() {
    local prompt="$1"
    local should_trigger="$2"
    local label="$3"
    local outfile="$RESULTS_DIR/$(echo "$label" | tr ' ' '_').json"

    local run_status=0
    (
        cd "$EVAL_WORKSPACE"
        PUA_CONFIG="$EVAL_PUA_CONFIG" PUA_FORCE_ON=1 run_with_timeout 120 claude -p "$prompt" \
            --plugin-dir "$PLUGIN_DIR" \
            --dangerously-skip-permissions \
            --max-turns 2 \
            --output-format stream-json \
            --verbose \
            </dev/null > "$outfile" 2>"${outfile}.stderr"
    ) || run_status=$?

    if [ "$run_status" -ne 0 ] || ! python3 "$EVIDENCE_INSPECTOR" "$outfile" --terminal-success; then
        echo "  ❌ FAIL: $label (execution failed or terminal result missing)"
        FAIL=$((FAIL + 1))
        return
    fi
    local triggered=false
    if python3 "$EVIDENCE_INSPECTOR" "$outfile" --skill pua; then
        triggered=true
    fi
    # Flavor words and available-skills metadata are not proof of a native load.

    if [ "$should_trigger" = "yes" ] && [ "$triggered" = "true" ]; then
        echo "  ✅ PASS: $label (correctly triggered)"
        PASS=$((PASS + 1))
    elif [ "$should_trigger" = "no" ] && [ "$triggered" = "false" ]; then
        echo "  ✅ PASS: $label (correctly NOT triggered)"
        PASS=$((PASS + 1))
    elif [ "$should_trigger" = "yes" ] && [ "$triggered" = "false" ]; then
        echo "  ❌ FAIL: $label (should trigger but didn't)"
        FAIL=$((FAIL + 1))
    else
        echo "  ❌ FAIL: $label (should NOT trigger but did)"
        FAIL=$((FAIL + 1))
    fi
}

echo "--- Should Trigger ---"
test_prompt "你怎么又失败了？再试试" "yes" "frustration-zh"
test_prompt "Try harder, stop giving up" "yes" "frustration-en"
test_prompt "PUA模式 帮我修这个bug" "yes" "explicit-pua"
test_prompt "你这个交付质量太差了" "yes" "quality-complaint"
test_prompt "没跑测试别说完成" "yes" "done-without-test-zh"
test_prompt "证据呢？数据在哪？" "yes" "evidence-request-zh"
test_prompt "老板体感不是 oracle，给我一条置身钉外提醒" "yes" "ding-workplace-zh"
test_prompt "Change approach, don't keep tweaking the same thing" "yes" "change-approach-en"

echo ""
echo "--- Should NOT Trigger ---"
test_prompt "Help me write a sort function" "no" "simple-coding"
test_prompt "What is async await in JavaScript?" "no" "info-query"
test_prompt "帮我翻译这段话" "no" "translation"

echo ""
echo "=== Results ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"
echo "Total:  $((PASS + FAIL))"
echo "Results dir: $RESULTS_DIR"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
