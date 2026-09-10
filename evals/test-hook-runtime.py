#!/usr/bin/env python3
"""Regression tests for the real Claude Code PUA hook lifecycle.

Run with:
    python3 evals/test-hook-runtime.py

No package installation or network access is required. Every case uses an
isolated HOME plus the trusted process-only PUA_STATE_DIR override.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"


def scoped_state_path(state_dir: Path, session_id: str, cwd: Path) -> Path:
    canonical_cwd = os.path.realpath(os.path.abspath(str(cwd)))
    material = f"pua-runtime-v1\0{session_id}\0{canonical_cwd}".encode("utf-8", "surrogatepass")
    return state_dir / f"{hashlib.sha256(material).hexdigest()}.json"


class HookRuntimeTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pua-hook-runtime-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.project_a = self.root / "project-a"
        self.project_b = self.root / "project-b"
        self.state_dir = self.root / "process-state"
        for directory in (self.home / ".pua", self.project_a, self.project_b):
            directory.mkdir(parents=True, exist_ok=True)
        self.config_path = self.home / ".pua" / "config.json"
        self.write_config(enabled=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_config(
        self, *, enabled: bool, flavor: str | None = "alibaba", language: str | None = ""
    ) -> None:
        config: dict[str, Any] = {"always_on": enabled}
        if flavor is not None:
            config["flavor"] = flavor
        if language is not None:
            config["language"] = language
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

    def env(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        environment["PUA_CONFIG"] = str(self.config_path)
        # This is a trusted process environment input, never a hook-payload field.
        environment["PUA_STATE_DIR"] = str(self.state_dir)
        return environment

    def event(
        self,
        event_name: str,
        *,
        session_id: str = "session-a",
        cwd: Path | None = None,
        tool_use_id: str | None = "tool-1",
        tool_name: str = "Bash",
        **fields: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "hook_event_name": event_name,
            "session_id": session_id,
            "cwd": str(cwd or self.project_a),
            "tool_name": tool_name,
        }
        if tool_use_id is not None:
            payload["tool_use_id"] = tool_use_id
        payload.update(fields)
        return payload

    def run_hook(
        self, script: str, payload: dict[str, Any], environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.run(
            ["bash", str(HOOKS / script)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=self.project_a,
            env=environment or self.env(),
            check=False,
        )
        self.assertEqual(
            process.returncode,
            0,
            msg=f"{script} returned {process.returncode}\nstdout:\n{process.stdout}\nstderr:\n{process.stderr}",
        )
        return process

    def flavor_state(self, environment: dict[str, str] | None = None) -> tuple[str, str]:
        """Read the sourced helper's effective flavor plus explicit-lock flag."""

        process = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; get_flavor; printf "%s\\t%s" "$PUA_FLAVOR" "$PUA_FLAVOR_LOCKED"',
                "bash",
                str(HOOKS / "flavor-helper.sh"),
            ],
            text=True,
            capture_output=True,
            cwd=self.project_a,
            env=environment or self.env(),
            check=False,
        )
        self.assertEqual(
            process.returncode,
            0,
            msg=f"get_flavor returned {process.returncode}\nstdout:\n{process.stdout}\nstderr:\n{process.stderr}",
        )
        flavor, separator, locked = process.stdout.partition("\t")
        self.assertEqual(separator, "\t", f"unexpected get_flavor output: {process.stdout!r}")
        return flavor, locked

    def state(self, session_id: str = "session-a", cwd: Path | None = None) -> dict[str, Any]:
        path = scoped_state_path(self.state_dir, session_id, cwd or self.project_a)
        self.assertTrue(path.is_file(), f"missing scoped state: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def additional_context(self, process: subprocess.CompletedProcess[str], event_name: str) -> str:
        """Assert the host-visible command-hook response contract."""

        self.assertNotEqual(process.stdout, "", "an escalation must emit additionalContext JSON")
        payload = json.loads(process.stdout)
        hook_output = payload["hookSpecificOutput"]
        self.assertEqual(hook_output["hookEventName"], event_name)
        context = hook_output["additionalContext"]
        self.assertIsInstance(context, str)
        return context

    def test_hooks_json_uses_real_command_lifecycle_events(self) -> None:
        config = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
        hooks = config["hooks"]

        post_failure = hooks["PostToolUseFailure"][0]["hooks"][0]
        self.assertEqual(post_failure["type"], "command")
        self.assertIn("failure-detector.sh", post_failure["command"])

        precompact = hooks["PreCompact"][0]["hooks"][0]
        self.assertEqual(precompact["type"], "command")
        self.assertIn("checkpoint-save.sh", precompact["command"])
        self.assertNotIn("prompt", precompact)

        session_matchers = {entry["matcher"] for entry in hooks["SessionStart"]}
        self.assertIn("startup|resume|clear", session_matchers)

    def test_nonzero_tool_response_and_post_tool_use_failure_are_confirmed_without_text(self) -> None:
        # Official tool_response, not legacy tool_result: nonzero without any
        # error keyword must count because the exit status is host evidence.
        first = self.event(
            "PostToolUse",
            tool_use_id="nonzero-no-text",
            tool_response={"exit_code": 17, "content": ""},
            state_dir=str(self.root / "payload-controlled-state"),
        )
        first_result = self.run_hook("failure-detector.sh", first)
        self.assertEqual(first_result.stdout, "")  # L0/first observation stays quiet.
        self.assertEqual(self.state()["failure_count"], 1)
        self.assertFalse((self.root / "payload-controlled-state").exists())

        # A legacy-looking field alone is intentionally ignored; this guards the
        # migration to Claude Code's official tool_response payload.
        legacy = self.event(
            "PostToolUse",
            tool_use_id="legacy-only",
            tool_result={"exit_code": 99, "content": "Error: should not count"},
        )
        self.assertEqual(self.run_hook("failure-detector.sh", legacy).stdout, "")
        self.assertEqual(self.state()["failure_count"], 1)

        # PostToolUseFailure is an explicit host failure event even when no
        # tool_response exists. Its error text must not be persisted.
        second = self.event(
            "PostToolUseFailure",
            tool_use_id="official-post-failure",
            error="SUPER_SECRET_FAILURE_TEXT_DO_NOT_PERSIST",
            is_interrupt=False,
        )
        second_result = self.run_hook("failure-detector.sh", second)
        second_context = self.additional_context(second_result, "PostToolUseFailure")
        self.assertIn("[PUA Candidate L1 Template", second_context)
        self.assertIn("其实，我对你是有一些失望的", second_context)  # Original Alibaba voice.
        self.assertIn(str(ROOT / "skills" / "pua" / "SKILL.md"), second_context)
        self.assertNotIn("invoke Skill", second_context)
        self.assertNotIn("Skill tool with 'pua'", second_context)
        self.assertNotIn("memory/evolution.md", second_context)
        self.assertNotIn("write to memory", second_context.lower())
        state = self.state()
        self.assertEqual(state["failure_count"], 2)
        self.assertNotIn("SUPER_SECRET_FAILURE_TEXT_DO_NOT_PERSIST", json.dumps(state))
        self.assertNotIn("tool_response", json.dumps(state))
        self.assertNotIn("error_history", json.dumps(state))

        # User interruption is not a confirmed task/tool failure observation.
        interrupted = self.event(
            "PostToolUseFailure",
            tool_use_id="cancelled-tool",
            error="cancelled",
            is_interrupt=True,
        )
        self.assertEqual(self.run_hook("failure-detector.sh", interrupted).stdout, "")
        self.assertEqual(self.state()["failure_count"], 2)

    def test_successful_ls_does_not_reset_and_duplicate_events_do_not_increment(self) -> None:
        failure_one = self.event(
            "PostToolUse", tool_use_id="failure-one", tool_response={"exit_code": 1, "content": ""}
        )
        failure_two = self.event(
            "PostToolUse", tool_use_id="failure-two", tool_response={"exit_code": 2, "content": ""}
        )
        self.run_hook("failure-detector.sh", failure_one)
        second_result = self.run_hook("failure-detector.sh", failure_two)
        second_context = self.additional_context(second_result, "PostToolUse")
        self.assertIn("[PUA Candidate L1 Template", second_context)
        self.assertIn(str(ROOT / "skills" / "pua" / "SKILL.md"), second_context)
        self.assertNotIn("invoke Skill", second_context)
        self.assertEqual(self.state()["failure_count"], 2)

        successful_ls = self.event(
            "PostToolUse",
            tool_use_id="successful-ls",
            tool_input={"command": "ls"},
            tool_response={"exit_code": 0, "content": "hooks\n"},
        )
        success_result = self.run_hook("failure-detector.sh", successful_ls)
        self.assertEqual(success_result.stdout, "")
        self.assertNotIn("突破", success_result.stdout)
        self.assertEqual(self.state()["failure_count"], 2)

        duplicate_result = self.run_hook("failure-detector.sh", failure_two)
        self.assertEqual(duplicate_result.stdout, "")
        self.assertEqual(self.state()["failure_count"], 2)

        # Same workspace but a second session has a separate state file/count.
        other_session = self.event(
            "PostToolUse",
            session_id="session-b",
            tool_use_id="other-session-failure",
            tool_response={"exit_code": 3, "content": ""},
        )
        self.run_hook("failure-detector.sh", other_session)
        self.assertEqual(self.state("session-a")["failure_count"], 2)
        self.assertEqual(self.state("session-b")["failure_count"], 1)

        # Same session but a different workspace is also isolated.
        other_workspace = self.event(
            "PostToolUse",
            cwd=self.project_b,
            tool_use_id="other-workspace-failure",
            tool_response={"exit_code": 4, "content": ""},
        )
        self.run_hook("failure-detector.sh", other_workspace)
        self.assertEqual(self.state("session-a", self.project_b)["failure_count"], 1)
        self.assertEqual(self.state("session-a", self.project_a)["failure_count"], 2)

    def test_explicit_huawei_flavor_locks_voice_and_switches_method_only(self) -> None:
        self.write_config(enabled=True, flavor="huawei")
        self.assertEqual(self.flavor_state(), ("huawei", "true"))

        session = self.run_hook("session-restore.sh", self.event("SessionStart"))
        session_context = self.additional_context(session, "SessionStart")
        self.assertIn("Locked Current Flavor: huawei 🔴", session_context)
        self.assertIn("Use Huawei military-order rhetoric", session_context)
        self.assertNotIn("Default Flavor Starting Point", session_context)

        contexts: dict[int, str] = {}
        for number in range(1, 6):
            result = self.run_hook(
                "failure-detector.sh",
                self.event(
                    "PostToolUse",
                    session_id="locked-huawei",
                    tool_use_id=f"locked-huawei-{number}",
                    tool_response={"exit_code": number, "content": ""},
                ),
            )
            if result.stdout:
                contexts[number] = self.additional_context(result, "PostToolUse")

        l2 = contexts[3]
        self.assertIn("烧不死的鸟是凤凰", l2)  # Original Huawei L2 voice.
        self.assertIn("Keep the locked 🔴 huawei voice", l2)
        self.assertIn("保持 🔴 huawei 语气", l2)
        self.assertNotIn("切换到 [new flavor]", l2)
        for foreign_voice in ("switch to ⬛ Musk", "Netflix", "Baidu", "Jobs"):
            self.assertNotIn(foreign_voice, l2)

        l4 = contexts[5]
        self.assertIn("胜则举杯相庆", l4)  # Original Huawei L4 voice.
        self.assertIn("Keep the locked 🔴 huawei voice", l4)
        self.assertIn("switch analytical methodology", l4)
        for foreign_voice in ("⬛ Musk", "🔴 Huawei", "🔶 Amazon", "🟣 Pinduoduo"):
            self.assertNotIn(foreign_voice, l4)

    def test_missing_flavor_uses_unlocked_default_router_and_recovery_does_not_lock(self) -> None:
        # This is deliberately exactly {"always_on": true}; the effective
        # Alibaba fallback must not be mistaken for a user-selected lock.
        self.write_config(enabled=True, flavor=None, language=None)
        self.assertEqual(self.flavor_state(), ("alibaba", "false"))

        initial = self.run_hook(
            "session-restore.sh", self.event("SessionStart", session_id="unlocked-missing")
        )
        initial_context = self.additional_context(initial, "SessionStart")
        self.assertIn("Default Flavor Starting Point: alibaba 🟠", initial_context)
        self.assertIn("No valid user flavor is locked", initial_context)
        self.assertIn("Debug/Fix (error, bug, crash, 报错) → Huawei", initial_context)
        self.assertIn("Workplace Process (无招, ONE, 老板体感", initial_context)
        self.assertNotIn("Use Alibaba corporate rhetoric", initial_context)
        self.assertNotIn("Locked Current Flavor:", initial_context)

        contexts: dict[int, str] = {}
        for number in range(1, 4):
            result = self.run_hook(
                "failure-detector.sh",
                self.event(
                    "PostToolUse",
                    session_id="unlocked-missing",
                    tool_use_id=f"unlocked-missing-{number}",
                    tool_response={"exit_code": number, "content": ""},
                ),
            )
            if result.stdout:
                contexts[number] = self.additional_context(result, "PostToolUse")

        l2 = contexts[3]
        self.assertIn("alibaba is only a default starting point, not a user lock", l2)
        self.assertIn("switch to ⬛ Musk", l2)
        self.assertIn("switch to 🟤 Netflix", l2)
        self.assertIn("switch to ⚫ Baidu", l2)
        self.assertIn("switch to ⬜ Jobs", l2)
        self.assertIn("从默认 🟠 alibaba 切换到 [new flavor]", l2)
        self.assertNotIn("Keep the locked 🟠 alibaba voice", l2)
        self.assertLess(
            l2.index("[PUA Conditional Application Gate — Candidate Only]"),
            l2.index("[方法论/风味切换建议 🔄]"),
        )

        compact = self.run_hook(
            "checkpoint-save.sh", self.event("PreCompact", session_id="unlocked-missing", tool_use_id=None)
        )
        self.assertEqual(compact.stdout, "")
        restored = self.run_hook(
            "session-restore.sh", self.event("SessionStart", session_id="unlocked-missing", tool_use_id=None)
        )
        restored_context = self.additional_context(restored, "SessionStart")
        self.assertIn("[PUA Scoped Checkpoint Recovery]", restored_context)
        self.assertIn("Default Flavor Starting Point: alibaba 🟠", restored_context)
        self.assertNotIn("Locked Current Flavor:", restored_context)
        serialized_state = json.dumps(self.state("unlocked-missing"), ensure_ascii=False)
        self.assertNotIn("flavor", serialized_state.lower())
        self.assertNotIn("locked", serialized_state.lower())

    def test_auto_flavor_uses_unlocked_default_starting_point(self) -> None:
        self.write_config(enabled=True, flavor="auto")
        self.assertEqual(self.flavor_state(), ("alibaba", "false"))

        session = self.run_hook("session-restore.sh", self.event("SessionStart", session_id="unlocked-auto"))
        context = self.additional_context(session, "SessionStart")
        self.assertIn("Default Flavor Starting Point: alibaba 🟠", context)
        self.assertIn("No valid user flavor is locked", context)
        self.assertIn("lightweight router", context)
        self.assertNotIn("Locked Current Flavor:", context)

    def test_candidate_gate_precedes_every_pressure_template_and_only_observes(self) -> None:
        session = self.run_hook("session-restore.sh", self.event("SessionStart", session_id="candidate-gate"))
        session_context = self.additional_context(session, "SessionStart")
        # The always-on protocol exposes its flavor examples before any tool
        # event, so it must not carry invented peer/other-model claims either.
        self.assertIn("数据拿不出来，这个绩效你拿什么解释", session_context)
        self.assertNotIn("你的 peer 都觉得你最近状态不好", session_context)
        self.assertIn("赛马场上，解决不了就让能解决的来", session_context)
        self.assertNotIn("别的模型都能解决这种问题", session_context)

        contexts: dict[int, str] = {}
        for number in range(1, 6):
            result = self.run_hook(
                "failure-detector.sh",
                self.event(
                    "PostToolUse",
                    session_id="candidate-gate",
                    tool_use_id=f"candidate-{number}",
                    tool_response={"exit_code": number, "content": ""},
                ),
            )
            if result.stdout:
                contexts[number] = self.additional_context(result, "PostToolUse")

        gate = "[PUA Conditional Application Gate — Candidate Only]"
        expected = {2: ("L1", "2"), 3: ("L2", "3"), 4: ("L3", "4"), 5: ("L4", "5+")}
        for number, (level, threshold) in expected.items():
            context = contexts[number]
            self.assertIn(f"[PUA Candidate {level} Template", context)
            self.assertIn(gate, context)
            self.assertLess(context.index(gate), context.index("> "))
            self.assertIn("报告状态：只观察，不控制", context)
            self.assertIn("工具失败观察不是本任务/子目标失败计数", context)
            self.assertIn("NOT a task/sub-goal failure count", context)
            self.assertIn("先核对当前子目标与可见实验验收", context)
            self.assertIn("CURRENT same sub-goal", context)
            self.assertIn("预期复现、无匹配、未验证关联或其他子目标均不升级", context)
            self.assertIn("Expected reproduction, no match", context)
            self.assertIn("只有当前同一子目标已确认失败数达到原门限才应用候选模板", context)
            self.assertIn("L1=2, L2=3, L3=4, L4=5+", context)
            self.assertIn(
                f"candidate {level} at the {threshold} observation threshold only", context
            )
            self.assertIn("Otherwise keep the task's current level unchanged", context)

        self.assertIn("If and only if the Conditional Application Gate passes", contexts[2])
        self.assertIn("Only if the Conditional Application Gate passes", contexts[3])
        self.assertIn("Only if the Conditional Application Gate passes", contexts[4])
        self.assertIn("IF (and only if) the Conditional Application Gate passes", contexts[5])

        # Preserve the original Alibaba pressure level while avoiding invented
        # peer or other-model judgments that have no observed evidence.
        self.assertIn("数据拿不出来，这个绩效你拿什么解释", contexts[4])
        self.assertNotIn("你的 peer 都觉得你最近状态不好", contexts[4])
        self.assertIn("赛马场上，解决不了就让能解决的来", contexts[5])
        self.assertNotIn("别的模型都能解决这种问题", contexts[5])

    def test_missing_tool_use_id_fails_closed(self) -> None:
        unidentifiable = self.event(
            "PostToolUse",
            tool_use_id=None,
            tool_response={"exit_code": 9, "content": ""},
        )
        result = self.run_hook("failure-detector.sh", unidentifiable)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.state_dir.exists(), "no idempotency key must not create state")

    def test_active_runtime_uses_python_fallback_and_cygpath_conversion_boundary(self) -> None:
        """Exercise the active helper path when python3 is unavailable.

        This is a portable simulation of the Git-Bash/native-Python boundary:
        the fake cygpath records every conversion but returns the same POSIX
        path so the local stdlib Python can execute it. The existing shell
        regression covers the Windows-shaped-path case with PUA disabled.
        """

        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        cygpath_log = self.root / "cygpath.log"
        (fake_bin / "python3").write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
        (fake_bin / "python").write_text(
            "#!/bin/sh\nexec \"$REAL_PY\" \"$@\"\n", encoding="utf-8"
        )
        (fake_bin / "cygpath").write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = \"-w\" ]; then\n"
            "  shift\n"
            "  printf '%s\\n' \"$1\" >> \"$CYGPATH_LOG\"\n"
            "fi\n"
            "printf '%s\\n' \"${1:-}\"\n",
            encoding="utf-8",
        )
        for executable in fake_bin.iterdir():
            executable.chmod(0o755)

        environment = self.env()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment.get('PATH', '')}"
        environment["REAL_PY"] = sys.executable
        environment["CYGPATH_LOG"] = str(cygpath_log)
        fallback_event = self.event(
            "PostToolUse",
            tool_use_id="python-fallback",
            tool_response={"exit_code": 8, "content": ""},
        )
        self.assertEqual(self.run_hook("failure-detector.sh", fallback_event, environment).stdout, "")
        self.assertEqual(self.state()["failure_count"], 1)
        converted = cygpath_log.read_text(encoding="utf-8")
        self.assertIn(str(self.project_a), converted)
        self.assertIn("runtime-state.py", converted)

    def test_disabled_mode_is_silent_and_writes_no_runtime_state(self) -> None:
        self.write_config(enabled=False)
        failure = self.event(
            "PostToolUse",
            tool_use_id="off-failure",
            tool_response={"exit_code": 7, "content": ""},
            # An untrusted payload path must never redirect where state is saved.
            state_dir=str(self.root / "payload-controlled-state"),
        )
        self.assertEqual(self.run_hook("failure-detector.sh", failure).stdout, "")
        self.assertEqual(self.run_hook("checkpoint-save.sh", self.event("PreCompact")).stdout, "")
        self.assertEqual(self.run_hook("session-restore.sh", self.event("SessionStart")).stdout, "")
        self.assertFalse(self.state_dir.exists())
        self.assertFalse((self.root / "payload-controlled-state").exists())
        self.assertFalse((self.home / ".pua" / "builder-journal.md").exists())

    def test_precompact_command_saves_only_scoped_observations_and_restores_same_task(self) -> None:
        # Establish a confirmed observation before compaction.
        failure = self.event(
            "PostToolUse",
            session_id="compact-session",
            tool_use_id="compact-failure",
            tool_response={"exit_code": 1, "content": ""},
        )
        self.run_hook("failure-detector.sh", failure)

        compact = self.event("PreCompact", session_id="compact-session", tool_use_id=None)
        compact_result = self.run_hook("checkpoint-save.sh", compact)
        self.assertEqual(compact_result.stdout, "")
        state = self.state("compact-session")
        self.assertEqual(
            set(state["checkpoint"]),
            {"saved_at", "kind", "failure_count", "peak_pressure_level"},
        )
        self.assertEqual(state["checkpoint"]["kind"], "tool_observation_only")
        self.assertEqual(state["checkpoint"]["failure_count"], 1)
        self.assertFalse((self.home / ".pua" / "runtime-state").exists())
        self.assertFalse((self.home / ".pua" / "builder-journal.md").exists())

        same_task = self.run_hook(
            "session-restore.sh", self.event("SessionStart", session_id="compact-session", tool_use_id=None)
        )
        restored = json.loads(same_task.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[PUA Scoped Checkpoint Recovery]", restored)
        self.assertIn("工具观察，不是任务失败/验收结论", restored)
        self.assertIn("Locked Current Flavor: alibaba", restored)
        self.assertIn("其实，我对你是有一些失望的", restored)  # Flavor lines stay raw.
        self.assertNotIn("C6 楼", restored)  # Ding is no longer globally injected.
        self.assertNotIn("置身钉内", restored)
        self.assertNotIn("无招", restored)

        cross_workspace = self.run_hook(
            "session-restore.sh",
            self.event("SessionStart", session_id="compact-session", cwd=self.project_b, tool_use_id=None),
        )
        cross_workspace_context = json.loads(cross_workspace.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("[PUA Scoped Checkpoint Recovery]", cross_workspace_context)

        cross_session = self.run_hook(
            "session-restore.sh", self.event("SessionStart", session_id="different-session", tool_use_id=None)
        )
        cross_session_context = json.loads(cross_session.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("[PUA Scoped Checkpoint Recovery]", cross_session_context)

    def test_clear_session_start_discards_same_scope_checkpoint_and_failure_count(self) -> None:
        session_id = "clear-reused-session"
        for number in range(1, 6):
            self.run_hook(
                "failure-detector.sh",
                self.event(
                    "PostToolUse",
                    session_id=session_id,
                    tool_use_id=f"clear-before-{number}",
                    tool_response={"exit_code": number, "content": ""},
                ),
            )
        self.assertEqual(self.state(session_id)["failure_count"], 5)
        self.run_hook(
            "checkpoint-save.sh", self.event("PreCompact", session_id=session_id, tool_use_id=None)
        )
        self.assertIn("checkpoint", self.state(session_id))
        self.run_hook(
            "failure-detector.sh",
            self.event(
                "PostToolUse",
                session_id="clear-neighbor-session",
                tool_use_id="clear-neighbor-1",
                tool_response={"exit_code": 1, "content": ""},
            ),
        )
        self.assertEqual(self.state("clear-neighbor-session")["failure_count"], 1)

        # A host may reuse the exact session id + cwd after /clear. It must
        # still inject current configuration without restoring old observations.
        self.write_config(enabled=True, flavor="huawei")
        cleared = self.run_hook(
            "session-restore.sh",
            self.event("SessionStart", session_id=session_id, tool_use_id=None, source="clear"),
        )
        clear_context = self.additional_context(cleared, "SessionStart")
        self.assertIn("Locked Current Flavor: huawei 🔴", clear_context)
        self.assertNotIn("Locked Current Flavor: alibaba", clear_context)
        self.assertNotIn("[PUA Scoped Checkpoint Recovery]", clear_context)
        self.assertNotIn("confirmed tool-failure observations: 5", clear_context)
        self.assertFalse(scoped_state_path(self.state_dir, session_id, self.project_a).exists())
        self.assertEqual(self.state("clear-neighbor-session")["failure_count"], 1)

        first_after_clear = self.run_hook(
            "failure-detector.sh",
            self.event(
                "PostToolUse",
                session_id=session_id,
                tool_use_id="clear-after-1",
                tool_response={"exit_code": 1, "content": ""},
            ),
        )
        self.assertEqual(first_after_clear.stdout, "")
        self.assertEqual(self.state(session_id)["failure_count"], 1)

        second_after_clear = self.run_hook(
            "failure-detector.sh",
            self.event(
                "PostToolUse",
                session_id=session_id,
                tool_use_id="clear-after-2",
                tool_response={"exit_code": 2, "content": ""},
            ),
        )
        second_context = self.additional_context(second_after_clear, "PostToolUse")
        self.assertIn("[PUA Candidate L1 Template", second_context)
        self.assertIn("我先立军令状", second_context)
        self.assertNotIn("[PUA Candidate L4 Template", second_context)
        self.assertEqual(self.state(session_id)["failure_count"], 2)

    def test_clear_while_disabled_resets_existing_scope_before_enablement_gate(self) -> None:
        session_id = "clear-while-off"
        for number in range(1, 3):
            self.run_hook(
                "failure-detector.sh",
                self.event(
                    "PostToolUse",
                    session_id=session_id,
                    tool_use_id=f"clear-off-before-{number}",
                    tool_response={"exit_code": number, "content": ""},
                ),
            )
        self.run_hook(
            "checkpoint-save.sh", self.event("PreCompact", session_id=session_id, tool_use_id=None)
        )
        self.assertEqual(self.state(session_id)["failure_count"], 2)

        self.run_hook(
            "failure-detector.sh",
            self.event(
                "PostToolUse",
                session_id="clear-off-neighbor",
                tool_use_id="clear-off-neighbor-1",
                tool_response={"exit_code": 1, "content": ""},
            ),
        )
        self.assertEqual(self.state("clear-off-neighbor")["failure_count"], 1)

        self.write_config(enabled=False)
        clear_result = self.run_hook(
            "session-restore.sh",
            self.event("SessionStart", session_id=session_id, tool_use_id=None, source="clear"),
        )
        self.assertEqual(clear_result.stdout, "")
        self.assertFalse(scoped_state_path(self.state_dir, session_id, self.project_a).exists())
        self.assertEqual(self.state("clear-off-neighbor")["failure_count"], 1)

        self.write_config(enabled=True)
        first_after_clear = self.run_hook(
            "failure-detector.sh",
            self.event(
                "PostToolUse",
                session_id=session_id,
                tool_use_id="clear-off-after-1",
                tool_response={"exit_code": 1, "content": ""},
            ),
        )
        # Without pre-gate cleanup, this old count of 2 would immediately
        # become candidate L2 at 3. A new task must begin at observation 1.
        self.assertEqual(first_after_clear.stdout, "")
        self.assertEqual(self.state(session_id)["failure_count"], 1)

    def test_clear_with_missing_config_resets_existing_scope_before_config_gate(self) -> None:
        session_id = "clear-without-config"
        for number in range(1, 3):
            self.run_hook(
                "failure-detector.sh",
                self.event(
                    "PostToolUse",
                    session_id=session_id,
                    tool_use_id=f"clear-missing-before-{number}",
                    tool_response={"exit_code": number, "content": ""},
                ),
            )
        self.run_hook(
            "checkpoint-save.sh", self.event("PreCompact", session_id=session_id, tool_use_id=None)
        )
        self.assertEqual(self.state(session_id)["failure_count"], 2)

        self.config_path.unlink()
        clear_result = self.run_hook(
            "session-restore.sh",
            self.event("SessionStart", session_id=session_id, tool_use_id=None, source="clear"),
        )
        self.assertEqual(clear_result.stdout, "")
        self.assertFalse(scoped_state_path(self.state_dir, session_id, self.project_a).exists())

        self.write_config(enabled=True)
        first_after_clear = self.run_hook(
            "failure-detector.sh",
            self.event(
                "PostToolUse",
                session_id=session_id,
                tool_use_id="clear-missing-after-1",
                tool_response={"exit_code": 1, "content": ""},
            ),
        )
        self.assertEqual(first_after_clear.stdout, "")
        self.assertEqual(self.state(session_id)["failure_count"], 1)

    def test_clear_without_existing_scope_state_creates_no_state_root(self) -> None:
        self.write_config(enabled=False)
        clear_result = self.run_hook(
            "session-restore.sh",
            self.event("SessionStart", session_id="clear-empty", tool_use_id=None, source="clear"),
        )
        self.assertEqual(clear_result.stdout, "")
        self.assertFalse(self.state_dir.exists(), "clear with no state must not create a state directory")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(HookRuntimeTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
