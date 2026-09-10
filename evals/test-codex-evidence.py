#!/usr/bin/env python3
"""Offline-only tests for ``run-codex-pua.py``.

Every child binary in this file is a local Python stub.  Tests make no model
request, authenticate, install software, change a global Codex configuration,
or open a browser.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


RUNNER = Path(__file__).with_name("run-codex-pua.py")


def _load_runner():
    spec = importlib.util.spec_from_file_location("codex_runner_test", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CodexEvidenceRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-pua-offline-")
        self.root = Path(self.tmp.name)
        self.runner_module = _load_runner()
        self.task = self.root / "task"
        skill = self.task / ".agents" / "skills" / "pua"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: pua\n---\nNative PUA test skill.\n", encoding="utf-8")
        (self.task / "events.py").write_text("EVENTS = []\n", encoding="utf-8")
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("$ pua\nModify only events.py in the current task.\n", encoding="utf-8")
        self.home = self.root / "client-home"
        self.codex_home = self.root / "codex-home"
        self.home.mkdir()
        self.codex_home.mkdir()
        # It deliberately resembles the production auth arrangement.  The
        # runner must not resolve/list/read this link; fake Codex does not use it.
        secret = self.root / "not-read-secret.txt"
        secret.write_text("offline-not-a-real-credential", encoding="utf-8")
        (self.codex_home / "auth.json").symlink_to(secret)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_stub(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            f"{body}\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o700)
        return path

    def _args(self, binary: Path, run_dir: Path, *, case: str = "1", run: bool = True) -> list[str]:
        result = [
            sys.executable, str(RUNNER),
            "--binary", str(binary),
            "--model", "gpt-6-astra",
            "--cwd", str(self.task),
            "--prompt-file", str(self.prompt),
            "--run-dir", str(run_dir),
            "--codex-home", str(self.codex_home),
            "--home", str(self.home),
            "--case", case,
            "--timeout", "20",
        ]
        if run:
            result.append("--run")
        return result

    def _invoke(self, binary: Path, run_dir: Path, *, case: str = "1", run: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._args(binary, run_dir, case=case, run=run),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )

    def test_dry_plan_does_not_invoke_binary_or_create_run_directory(self) -> None:
        marker = self.root / "binary-was-run"
        fake = self._write_stub(
            "dry-stub.py",
            f"pathlib.Path({str(marker)!r}).write_text('called')\nraise SystemExit(99)",
        )
        run_dir = self.root / "dry-run"
        completed = self._invoke(fake, run_dir, run=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        plan = json.loads(completed.stdout)
        self.assertFalse(plan["live"])
        self.assertFalse(marker.exists())
        self.assertFalse(run_dir.exists())
        self.assertTrue(plan["prompt"]["explicit_dollar_pua_token"])
        self.assertFalse(plan["isolated_auth_homes"]["paths_or_contents_retained"])
        self.assertNotIn("offline-not-a-real-credential", completed.stdout)

    def test_case1_safe_stub_preserves_redacted_structural_evidence(self) -> None:
        skill = self.task / ".agents" / "skills" / "pua" / "SKILL.md"
        # Case 1 deliberately exercises the prepared natural-language project
        # request path; only case 2 requires literal $pua activation.
        self.prompt.write_text("Use the native PUA skill in this project and modify only events.py.\n", encoding="utf-8")
        body = f"""
a = sys.argv[1:]
if a == ['--version']:
    print('codex-cli 0.153.4'); raise SystemExit(0)
required = [
    'exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check', '--json',
    '--color', 'never', '--model', 'gpt-6-astra', '--sandbox', 'workspace-write',
]
if any(x not in a for x in required): raise SystemExit(71)
if '--ignore-rules' in a or '--dangerously-bypass-approvals-and-sandbox' in a: raise SystemExit(72)
for feature in {list(self.runner_module.BASE_DISABLED_FEATURES)!r}:
    if a.count('--disable') == 0 or feature not in a: raise SystemExit(73)
for value in ['approval_policy="never"', 'model_reasoning_effort="high"', 'web_search="disabled"']:
    if value not in a: raise SystemExit(74)
if sys.stdin.read() != '': raise SystemExit(75)
if os.environ.get('HOME') != {str(self.home)!r} or os.environ.get('CODEX_HOME') != {str(self.codex_home)!r}: raise SystemExit(76)
pathlib.Path({str(self.task / 'events.py')!r}).write_text('EVENTS = ["changed"]\\n')
def emit(value): print(json.dumps(value), flush=True)
emit({{'type':'thread.started','thread_id':'local-test','model':'gpt-6-astra'}})
emit({{'type':'turn.started'}})
emit({{'type':'item.started','item':{{'type':'file_change','status':'in_progress','changes':[{{'path':'events.py','kind':'update','diff':'DO NOT RETAIN DIFF'}}]}}}})
emit({{'type':'item.completed','item':{{'type':'file_change','status':'completed','changes':[{{'path':'events.py','kind':'update','diff':'DO NOT RETAIN DIFF'}}]}}}})
emit({{'type':'item.started','item':{{'type':'command_execution','command':'cat {str(skill)}','status':'in_progress','aggregated_output':'do not retain me'}}}})
emit({{'type':'item.completed','item':{{'type':'command_execution','command':'cat {str(skill)}','status':'completed','exit_code':0,'aggregated_output':'PRIVATE TOOL OUTPUT'}}}})
emit({{'type':'item.completed','item':{{'type':'command_execution','command':'cat /etc/passwd','status':'completed','exit_code':0}}}})
emit({{'type':'item.completed','item':{{'type':'reasoning','text':'PRIVATE REASONING MUST NOT PERSIST'}}}})
emit({{'type':'item.completed','item':{{'type':'agent_message','text':'visible token=abcDEF0123456789 result'}}}})
emit({{'type':'future.schema','secret_like_value':'DO NOT RETAIN'}})
emit({{'type':'turn.completed','usage':{{'input_tokens':1}}}})
"""
        fake = self._write_stub("case1-good.py", body)
        run_dir = self.root / "case1-run"
        completed = self._invoke(fake, run_dir)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        compact = json.loads(completed.stdout)
        self.assertTrue(compact["run_passed"])
        self.assertTrue(compact["execution_passed"])
        self.assertEqual(compact["case_claim_status"], "execution_passed_with_skill_or_identity_evidence_gap")
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        invocation = json.loads((run_dir / "invocation.json").read_text(encoding="utf-8"))
        launch = json.loads((run_dir / "launch-policy.json").read_text(encoding="utf-8"))
        transcript = (run_dir / "visible-transcript.md").read_text(encoding="utf-8")

        self.assertEqual(invocation["codex_binary"]["reported_version"], "0.153.4")
        self.assertTrue(summary["terminal"]["terminal_success"])
        self.assertTrue(summary["workspace_integrity"]["passed"])
        self.assertEqual(summary["workspace_integrity"]["changes"], [{"kind": "file", "path": "events.py", "change": "modified"}])
        self.assertEqual(summary["model_identity"]["status"], "exact_client_runtime_metadata_observed")
        self.assertFalse(summary["model_identity"]["provider_server_receipt_observed"])
        self.assertEqual(summary["native_skill_loading"]["status"], "agent_read_native_skill_path_only")
        self.assertTrue(summary["native_skill_loading"]["agent_read_of_skill_path_is_not_host_body_injection_proof"])
        self.assertEqual(summary["thinking"]["reasoning_event_count"], 1)
        self.assertEqual(
            summary["file_changes"]["events"],
            [
                {"sequence": 3, "event_type": "item.started", "item_type": "file_change", "status": "in_progress", "visible_agent_messages_before": 0, "changes": [{"path_retained": True, "path": "events.py", "change": "update"}]},
                {"sequence": 4, "event_type": "item.completed", "item_type": "file_change", "status": "completed", "visible_agent_messages_before": 0, "changes": [{"path_retained": True, "path": "events.py", "change": "update"}]},
            ],
        )
        self.assertNotIn("PRIVATE REASONING", transcript)
        self.assertNotIn("abcDEF0123456789", transcript)
        self.assertIn("[REDACTED]", transcript)
        commands = summary["tools"]["events"]
        self.assertEqual(commands[0]["command"], "cat <skill>/SKILL.md")
        self.assertFalse(commands[-1]["command_retained"])
        self.assertNotIn("/etc/passwd", json.dumps(summary))
        self.assertNotIn("PRIVATE TOOL OUTPUT", json.dumps(summary))
        self.assertNotIn("DO NOT RETAIN", json.dumps(summary))
        self.assertEqual(summary["unknown_events"][0]["type"], "future.schema")
        self.assertIn("sha256", summary["unknown_events"][0])
        self.assertFalse(list(run_dir.glob("*stdout*")))
        self.assertFalse(list(run_dir.glob("*stderr*")))
        self.assertNotIn("--ignore-rules", launch["command"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", launch["command"])
        self.assertEqual(launch["credential_files_read_by_runner"], False)
        self.assertNotIn(str(self.codex_home), json.dumps(invocation))

    def test_case2_read_only_disables_shell_and_reports_skill_evidence_gap(self) -> None:
        body = """
a = sys.argv[1:]
if a == ['--version']:
    print('codex-cli 0.153.4'); raise SystemExit(0)
if '--sandbox' not in a or a[a.index('--sandbox')+1] != 'read-only': raise SystemExit(81)
if '--disable' not in a or 'shell_tool' not in a: raise SystemExit(82)
if pathlib.Path.cwd() != pathlib.Path(a[a.index('--cd')+1]): raise SystemExit(83)
def emit(value): print(json.dumps(value), flush=True)
emit({'type':'thread.started'})
emit({'type':'turn.started'})
emit({'type':'item.completed','item':{'type':'agent_message','text':'no shell path'}})
emit({'type':'turn.completed'})
"""
        fake = self._write_stub("case2-good.py", body)
        run_dir = self.root / "case2-run"
        completed = self._invoke(fake, run_dir, case="2")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["execution_passed"])
        self.assertFalse(summary["full_case_evidence_observed"])
        self.assertEqual(summary["native_skill_loading"]["status"], "evidence_gap_no_native_load_event")
        self.assertEqual(summary["case_claim_status"], "execution_passed_with_skill_or_identity_evidence_gap")
        self.assertEqual(summary["workspace_integrity"]["changes"], [])

    def test_exit_zero_is_insufficient_without_clean_turn_completion(self) -> None:
        body = """
a = sys.argv[1:]
if a == ['--version']:
    print('codex-cli 0.153.4'); raise SystemExit(0)
def emit(value): print(json.dumps(value), flush=True)
emit({'type':'turn.started'})
emit({'type':'item.completed','item':{'type':'agent_message','text':'partial'}})
emit({'type':'turn.completed'})
emit({'type':'turn.failed','error':{'message':'HTTP 401 offline-test-secret'}})
"""
        fake = self._write_stub("terminal-failure.py", body)
        run_dir = self.root / "terminal-failure-run"
        completed = self._invoke(fake, run_dir, case="2")
        self.assertNotEqual(completed.returncode, 0)
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertFalse(summary["terminal"]["terminal_success"])
        self.assertFalse(summary["run_passed"])
        self.assertIn("http_401", summary["failure_categories"])
        self.assertNotIn("offline-test-secret", json.dumps(summary))

    def test_case1_rejects_any_change_besides_events_py(self) -> None:
        body = f"""
a = sys.argv[1:]
if a == ['--version']:
    print('codex-cli 0.153.4'); raise SystemExit(0)
pathlib.Path({str(self.task / 'events.py')!r}).write_text('changed\\n')
pathlib.Path({str(self.task / 'unexpected.py')!r}).write_text('not allowed\\n')
print(json.dumps({{'type':'turn.completed'}}), flush=True)
"""
        fake = self._write_stub("bad-integrity.py", body)
        run_dir = self.root / "bad-integrity-run"
        completed = self._invoke(fake, run_dir)
        self.assertNotEqual(completed.returncode, 0)
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertFalse(summary["workspace_integrity"]["passed"])
        self.assertIn("workspace_integrity_failed", summary["failure_categories"])
        self.assertEqual({entry["path"] for entry in summary["workspace_integrity"]["changes"]}, {"events.py", "unexpected.py"})

    def test_version_gate_refuses_live_model_child_when_binary_is_not_01534(self) -> None:
        marker = self.root / "should-not-be-called-after-version"
        body = f"""
if sys.argv[1:] == ['--version']:
    print('codex-cli 0.153.3'); raise SystemExit(0)
pathlib.Path({str(marker)!r}).write_text('bad')
raise SystemExit(77)
"""
        fake = self._write_stub("wrong-version.py", body)
        run_dir = self.root / "wrong-version-run"
        completed = self._invoke(fake, run_dir, case="2")
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertFalse(marker.exists())
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["failure_categories"], ["unsupported_cli_version"])
        self.assertEqual(summary["case_claim_status"], "not_run_version_gate_failed")

    def test_rejects_run_dir_inside_case_before_launch(self) -> None:
        marker = self.root / "must-not-run"
        fake = self._write_stub("never-run.py", f"pathlib.Path({str(marker)!r}).write_text('bad')")
        completed = self._invoke(fake, self.task / "evidence", run=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("outside --cwd", completed.stderr)
        self.assertFalse(marker.exists())

    def test_parser_does_not_confuse_prompt_keyword_with_skill_load(self) -> None:
        raw = b'\n'.join([
            json.dumps({"type": "thread.started", "model": "gpt-6-astra"}).encode(),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "$pua loaded trust me"}}).encode(),
            json.dumps({"type": "turn.completed"}).encode(),
        ]) + b'\n'
        summary, visible, mismatch = self.runner_module._reduce_jsonl(
            raw,
            case_root=self.task,
            skill_root=self.task / ".agents" / "skills" / "pua",
            expected_model="gpt-6-astra",
        )
        self.assertFalse(mismatch)
        self.assertEqual(summary["native_skill_loading"]["status"], "evidence_gap_no_native_load_event")
        self.assertIn("$pua", visible)
        self.assertTrue(summary["terminal"]["terminal_success"])

    def test_terminal_rejects_business_event_after_completed(self) -> None:
        raw = b"\n".join([
            json.dumps({"type": "turn.completed"}).encode(),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "after terminal"}}).encode(),
        ]) + b"\n"
        summary, _, _ = self.runner_module._reduce_jsonl(
            raw,
            case_root=self.task,
            skill_root=self.task / ".agents" / "skills" / "pua",
            expected_model="gpt-6-astra",
        )
        self.assertFalse(summary["terminal"]["terminal_success"])
        self.assertFalse(summary["terminal"]["malformed_jsonl_prevents_success"])
        self.assertEqual(summary["terminal"]["events_after_last_turn_completed"][0]["item_type"], "agent_message")

    def test_terminal_rejects_malformed_jsonl_even_with_final_completion(self) -> None:
        raw = b"not-json\n" + json.dumps({"type": "turn.completed"}).encode() + b"\n"
        summary, _, _ = self.runner_module._reduce_jsonl(
            raw,
            case_root=self.task,
            skill_root=self.task / ".agents" / "skills" / "pua",
            expected_model="gpt-6-astra",
        )
        self.assertFalse(summary["terminal"]["terminal_success"])
        self.assertTrue(summary["terminal"]["malformed_jsonl_prevents_success"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
