#!/usr/bin/env python3
"""Offline-only tests for OMP evidence reduction.

No test invokes OMP, makes a model request, installs anything, logs in, or
opens a browser.  The JSONL below is synthetic transport output.
"""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from omp_evidence import inspect_stream, redact_visible_text


EXPECTED = "xai-oauth/grok-4.6"


class OmpEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="omp-evidence-test-")
        self.root = Path(self.tmp.name)
        self.skill = self.root / "plugin" / "skills" / "pua" / "SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("---\nname: pua\n---\n<!-- PUA-RUNTIME-CONTRACT:START -->\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _stream(self, *events: dict) -> Path:
        path = self.root / f"stream-{len(list(self.root.glob('stream-*.jsonl')))}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return path

    def _assistant(self, blocks: list[dict], *, provider: str = "xai-oauth", model: str = "grok-4.6") -> dict:
        return {
            "type": "message_end",
            "message": {"role": "assistant", "provider": provider, "model": model, "content": blocks, "stopReason": "stop"},
        }

    def _agent_end(self, stop_reason: str = "stop", *, will_continue: bool = False, error_message: str | None = None) -> dict:
        assistant = {"role": "assistant", "provider": "xai-oauth", "model": "grok-4.6", "stopReason": stop_reason, "content": []}
        if error_message is not None:
            assistant["errorMessage"] = error_message
        return {"type": "agent_end", "willContinue": will_continue, "messages": [assistant]}

    def test_native_protocol_requires_call_result_marker_and_fixture_resolution(self) -> None:
        stream = self._stream(
            self._assistant([
                {"type": "toolCall", "id": "call-1", "name": "read", "arguments": {"path": "skill://pua"}},
                {"type": "thinking", "thinking": "PRIVATE_CHAIN_OF_THOUGHT"},
            ]),
            {
                "type": "message_end",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "read",
                    "isError": False,
                    "content": [{"type": "text", "text": "<!-- PUA-RUNTIME-CONTRACT:START -->"}],
                    "details": {"resolvedPath": str(self.skill)},
                },
            },
            self._assistant([{"type": "text", "text": "[PUA-DIAGNOSIS] visible result"}]),
            self._agent_end(),
        )
        evidence = inspect_stream(stream, EXPECTED, expected_skill_source=self.skill)
        self.assertTrue(evidence["exact_model_confirmed"])
        self.assertTrue(evidence["native_skill_protocol"]["passed"])
        self.assertTrue(evidence["terminal_success"])
        self.assertEqual(evidence["native_skill_protocol"]["attempt_count"], 1)
        self.assertEqual(evidence["thinking"]["block_count"], 1)
        self.assertNotIn("PRIVATE_CHAIN_OF_THOUGHT", evidence["visible_text"])
        self.assertNotIn("PUA-RUNTIME-CONTRACT:START", json.dumps(evidence, ensure_ascii=False))
        self.assertEqual(evidence["actual_model_evidence"]["independent_server_attestation"], False)

    def test_terminal_requires_agent_end_and_normal_stop_after_native_load(self) -> None:
        base = [
            self._assistant([{"type": "toolCall", "id": "native", "name": "read", "arguments": {"path": "skill://pua"}}]),
            {"type": "message_end", "message": {"role": "toolResult", "toolCallId": "native", "toolName": "read", "isError": False,
             "content": [{"type": "text", "text": "PUA-RUNTIME-CONTRACT:START"}], "details": {"resolvedPath": str(self.skill)}}},
        ]
        no_end = inspect_stream(self._stream(*base), EXPECTED, expected_skill_source=self.skill)
        truncated = inspect_stream(self._stream(*base, self._assistant([{"type": "text", "text": "partial"}]), self._agent_end("length")), EXPECTED, expected_skill_source=self.skill)
        errored = inspect_stream(self._stream(*base, self._assistant([{"type": "text", "text": "failure"}]), self._agent_end("error", error_message="HTTP 401 unauthorized")), EXPECTED, expected_skill_source=self.skill)
        self.assertFalse(no_end["terminal_success"])
        self.assertFalse(truncated["terminal_success"])
        self.assertFalse(errored["terminal_success"])
        self.assertEqual(errored["terminal"]["final_assistant_failure_classification"], "http_401")
        self.assertEqual(errored["failure_observability"]["json_error_classifications"][-1]["classification"], "http_401")

    def test_keyword_in_text_is_not_skill_loading_proof(self) -> None:
        stream = self._stream(self._assistant([
            {"type": "text", "text": "I read skill://pua and PUA-RUNTIME-CONTRACT:START, trust me."},
        ]))
        evidence = inspect_stream(stream, EXPECTED, expected_skill_source=self.skill)
        self.assertFalse(evidence["native_skill_protocol"]["passed"])
        self.assertTrue(evidence["native_skill_protocol"]["keyword_only_is_insufficient"])

    def test_local_path_or_missing_fixture_resolution_fails_closed(self) -> None:
        stream = self._stream(
            self._assistant([{"type": "toolCall", "id": "call-2", "name": "read", "arguments": {"path": "skill://pua"}}]),
            {
                "type": "message_end",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call-2",
                    "isError": False,
                    "content": [{"type": "text", "text": "PUA-RUNTIME-CONTRACT:START"}],
                    "details": {"resolvedPath": str(self.root / "some-global-pua" / "SKILL.md")},
                },
            },
        )
        evidence = inspect_stream(stream, EXPECTED, expected_skill_source=self.skill)
        self.assertFalse(evidence["native_skill_protocol"]["passed"])
        self.assertFalse(evidence["native_skill_protocol"]["attempts"][0]["source_path_matches_fixture"])

    def test_missing_resolved_path_is_evidence_gap_not_model_capability(self) -> None:
        stream = self._stream(
            self._assistant([{"type": "toolCall", "id": "call-3", "name": "read", "arguments": {"path": "skill://pua"}}]),
            {"type": "message_end", "message": {"role": "toolResult", "toolCallId": "call-3", "toolName": "read", "isError": False,
             "content": [{"type": "text", "text": "PUA-RUNTIME-CONTRACT:START"}], "details": {"contentType": "text/markdown"}}},
        )
        evidence = inspect_stream(stream, EXPECTED, expected_skill_source=self.skill)
        self.assertFalse(evidence["native_skill_protocol"]["passed"])
        attempt = evidence["native_skill_protocol"]["attempts"][0]
        self.assertEqual(attempt["failure_reason"], "missing_resolved_path_evidence")
        self.assertEqual(attempt["tool_result_details_keys"], ["contentType"])
        self.assertIsNotNone(attempt["tool_result_content_sha256"])

    def test_terminal_flag_and_causal_order_fail_closed(self) -> None:
        call = self._assistant([{"type": "toolCall", "id": "native", "name": "read", "arguments": {"path": "skill://pua"}}])
        result = {"type": "message_end", "message": {"role": "toolResult", "toolCallId": "native", "toolName": "read", "isError": False,
                  "content": [{"type": "text", "text": "PUA-RUNTIME-CONTRACT:START"}], "details": {"resolvedPath": str(self.skill)}}}
        for value in [None, 0, "false", [], True]:
            with self.subTest(willContinue=value):
                end = self._agent_end()
                if value is None:
                    end.pop("willContinue")
                else:
                    end["willContinue"] = value
                e = inspect_stream(self._stream(call, result, end), EXPECTED, expected_skill_source=self.skill)
                self.assertFalse(e["terminal_success"])
        for events in [(self._agent_end(), call, result), (result, call, self._agent_end()),
                       (call, result, self._agent_end(), self._assistant([{"type": "text", "text": "late"}]))]:
            e = inspect_stream(self._stream(*events), EXPECTED, expected_skill_source=self.skill)
            self.assertFalse(e["terminal_success"])
        for name in [None, "bash"]:
            wrong = json.loads(json.dumps(result))
            wrong["message"]["toolName"] = name
            e = inspect_stream(self._stream(call, wrong, self._agent_end()), EXPECTED, expected_skill_source=self.skill)
            self.assertFalse(e["native_skill_protocol"]["passed"])

    def test_nested_destinations_rejected_without_source_mutation(self) -> None:
        for filename, helper in [("run-omp-pua.py", "_reject_overlapping_trees"),
                                 ("prepare-multimodel-evals.py", "reject_overlapping_trees")]:
            spec = importlib.util.spec_from_file_location("overlap_test", Path(__file__).with_name(filename))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            source = self.root / "copy-source"
            source.mkdir(exist_ok=True)
            (source / "protected.txt").write_text("keep")
            for destination in [source, source / "output", source.parent]:
                with self.subTest(helper=helper, destination=str(destination)):
                    with self.assertRaises(ValueError):
                        getattr(module, helper)(source, destination)
                    if filename == "run-omp-pua.py":
                        with self.assertRaises(ValueError):
                            module._copy_tree(source, destination, read_only=False)
            self.assertEqual(list(source.iterdir()), [source / "protected.txt"])
            getattr(module, helper)(source, self.root / "unrelated-output")

    def test_observable_model_substitution_is_rejected(self) -> None:
        stream = self._stream(self._assistant([{"type": "text", "text": "done"}], provider="zai", model="glm-5.3"))
        evidence = inspect_stream(stream, EXPECTED, expected_skill_source=self.skill)
        self.assertFalse(evidence["exact_model_confirmed"])
        self.assertEqual(evidence["unexpected_assistant_models"], ["zai/glm-5.3"])

    def test_missing_runtime_identity_is_rejected(self) -> None:
        stream = self._stream({
            "type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        })
        evidence = inspect_stream(stream, EXPECTED, expected_skill_source=self.skill)
        self.assertFalse(evidence["exact_model_confirmed"])
        self.assertEqual(evidence["assistant_identity_missing_count"], 1)

    def test_visible_text_redaction_does_not_preserve_common_token_values(self) -> None:
        text = redact_visible_text("token=abcdEFGH1234 and Authorization: Bearer abcdefghijklmnop")
        self.assertNotIn("abcdEFGH1234", text)
        self.assertNotIn("abcdefghijklmnop", text)
        self.assertIn("[REDACTED]", text)

    def test_prompt_path_remap_changes_only_paths_inside_source_fixture(self) -> None:
        spec = importlib.util.spec_from_file_location("omp_runner_test", Path(__file__).with_name("run-omp-pua.py"))
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = self.root / "source-task"
        execution = self.root / "evidence" / "task"
        sibling = self.root / "source-task-sibling" / "keep.txt"
        prompt = f"Modify {source}/fixture.txt and {source}; do not touch {sibling}."
        remapped, count = module._remap_task_paths(prompt, source, execution)
        self.assertEqual(count, 2)
        self.assertIn(f"{execution}/fixture.txt", remapped)
        self.assertIn(str(execution), remapped)
        self.assertIn(str(sibling), remapped)
        self.assertNotIn(f"{source}/fixture.txt", remapped)

    def test_optional_source_version_uses_current_home_without_execution(self) -> None:
        spec = importlib.util.spec_from_file_location("omp_version_test", Path(__file__).with_name("run-omp-pua.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        package = self.root / ".bun/install/global/node_modules/@oh-my-pi/pi-coding-agent/package.json"
        with patch.object(module.Path, "home", return_value=self.root), patch.object(module.subprocess, "run") as run:
            self.assertIsNone(module._source_package_version())
            package.parent.mkdir(parents=True)
            for content, expected in [('{"version":"17.4.0"}', "17.4.0"),
                                      ('[]', None), ('{"version":null}', None), ('not-json', None)]:
                with self.subTest(content=content):
                    package.write_text(content, encoding="utf-8")
                    self.assertEqual(module._source_package_version(), expected)
            run.assert_not_called()

    def test_runner_full_path_with_offline_omp_stub(self) -> None:
        """Exercise --run without a model/login/network using a local stub.

        It validates the runner's process/overlay/plugin evidence wiring, not
        OMP itself.  The fake binary has no credentials and makes no network
        connection.
        """
        values = {
            "advisor.enabled": False,
            "prewalk.enabled": False,
            "retry.enabled": False,
            "retry.modelFallback": False,
            "retry.usageAwareFallback": False,
            "providers.anthropic.serverSideFallback": False,
            "memory.backend": "off",
            "memories.enabled": False,
            "autolearn.enabled": False,
            "autolearn.autoContinue": False,
            "title.refreshOnReplan": False,
            "skills.enabled": True,
            "skills.includeSkills": ["pua"],
        }
        fake = self.root / "fake-omp.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"VALUES = {values!r}\n"
            "a = sys.argv[1:]\n"
            "if a == ['--version']:\n"
            "    print('omp/18.1.13'); raise SystemExit(0)\n"
            "if len(a) >= 3 and a[0:2] == ['config', 'get']:\n"
            "    key = a[2]; print(json.dumps({'key': key, 'value': VALUES[key]})); raise SystemExit(0)\n"
            "def val(flag): return a[a.index(flag)+1]\n"
            "selector = val('--model'); provider, model = selector.split('/', 1)\n"
            "plugin = pathlib.Path(val('--plugin-dir'))\n"
            "skill = plugin / 'skills' / 'pua' / 'SKILL.md'\n"
            "if str(pathlib.Path(val('--cwd')) / 'fixture.txt') not in a[-1]: raise SystemExit(91)\n"
            "def emit(m): print(json.dumps({'type':'message_end','message':m}), flush=True)\n"
            "emit({'role':'assistant','provider':provider,'model':model,'stopReason':'tool_use','content':[{'type':'toolCall','id':'stub-read','name':'read','arguments':{'path':'skill://pua'}}]})\n"
            "emit({'role':'toolResult','toolCallId':'stub-read','toolName':'read','isError':False,'content':[{'type':'text','text':'PUA-RUNTIME-CONTRACT:START'}],'details':{'resolvedPath':str(skill)}})\n"
            "emit({'role':'assistant','provider':provider,'model':model,'stopReason':'stop','content':[{'type':'thinking','thinking':'PRIVATE STUB THINKING'},{'type':'text','text':'token=offline-secret [PUA-DIAGNOSIS] visible'}]})\n"
            "print(json.dumps({'type':'agent_end','willContinue':False,'messages':[{'role':'assistant','provider':provider,'model':model,'stopReason':'stop','content':[]}]}), flush=True)\n",
            encoding="utf-8",
        )
        os.chmod(fake, 0o700)
        task = self.root / "task"
        task.mkdir()
        (task / "fixture.txt").write_text("fixture\n", encoding="utf-8")
        prompt = self.root / "prompt.txt"
        prompt.write_text(f"Perform the fixture task on {task / 'fixture.txt'}.", encoding="utf-8")
        source = self.root / "source" / "pua" / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text(
            "---\nname: pua\n---\n<!-- PUA-RUNTIME-CONTRACT:START -->\n",
            encoding="utf-8",
        )
        run_dir = self.root / "run"
        runner = Path(__file__).with_name("run-omp-pua.py")
        completed = subprocess.run(
            [
                sys.executable, str(runner), "--model", EXPECTED, "--cwd", str(task),
                "--prompt-file", str(prompt), "--run-dir", str(run_dir), "--skill-source", str(source),
                "--tools", "read,glob,grep,write,edit", "--timeout", "20", "--omp-binary", str(fake), "--run",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        failure_summary = (run_dir / "summary.json").read_text(encoding="utf-8") if (run_dir / "summary.json").exists() else ""
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr + failure_summary)
        compact = json.loads(completed.stdout)
        self.assertTrue(compact["run_passed"])
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        invocation = json.loads((run_dir / "invocation.json").read_text(encoding="utf-8"))
        self.assertEqual(invocation["omp_binary"]["reported_version"], "omp/18.1.13")
        self.assertTrue(summary["native_skill_protocol"]["passed"])
        self.assertTrue(summary["exact_model_confirmed"])
        self.assertFalse(summary["model_identity"]["provider_server_receipt_observed"])
        self.assertNotIn("PRIVATE STUB THINKING", (run_dir / "visible-transcript.md").read_text(encoding="utf-8"))
        self.assertNotIn("offline-secret", (run_dir / "visible-transcript.md").read_text(encoding="utf-8"))
        self.assertFalse(list(run_dir.glob(".raw-omp-stream-*")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
