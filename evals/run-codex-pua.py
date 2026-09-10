#!/usr/bin/env python3
"""Bounded, evidence-preserving runner for an Astra PUA-skill evaluation.

The default invocation is an offline dry plan: it reads and hashes the supplied
fixture but neither starts Codex nor creates a run directory.  ``--run`` is the
only path that starts the caller-supplied Codex CLI.  It intentionally uses a
new evidence directory, closed stdin, an owned process group, and a bounded
in-memory JSONL stream.

This runner is *not* an operating-system sandbox and is not a model identity
attestation service.  In particular, a Codex JSON event that echoes ``--model``
is client/runtime metadata, not proof of a provider-side routing decision; and
the literal prompt token ``$pua`` is not proof that the host injected a skill
body.  Those evidence gaps are recorded rather than guessed.

No raw stdout JSONL, stderr, prompts, reasoning items, tool output, credentials,
or environment values are persisted.  The only retained model-visible content
is redacted ``agent_message`` text and narrowly allowed command text whose
paths are inside the case fixture or its PUA skill tree.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Iterable


RUNNER_SCHEMA_VERSION = 1
EXPECTED_CLI_VERSION = "0.153.4"
EXPECTED_MODEL = "gpt-6-astra"
DEFAULT_BINARY = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
DEFAULT_TIMEOUT_SECONDS = 600
MAX_STDOUT_BYTES = 16 * 1024 * 1024
MAX_STDERR_BYTES = 1 * 1024 * 1024
MAX_VISIBLE_TEXT_BYTES = 512 * 1024

# Verified from ``codex features list`` on the target 0.153.4 binary.  Keep the
# list concrete rather than adding guessed configuration keys.
BASE_DISABLED_FEATURES = (
    "hooks",
    "memories",
    "multi_agent",
    "apps",
    "in_app_browser",
    "shell_snapshot",
    "skill_mcp_dependency_install",
)

_EXPLICIT_PUA = re.compile(r"(?<![A-Za-z0-9_.-])\$\s*pua\b", re.IGNORECASE)
_SECRET_TEXT = re.compile(
    r"(?ix)"
    r"(?:authorization\s*:\s*bearer\s+|bearer\s+)[A-Za-z0-9._~+\-/=]{8,}"
    r"|(?:api[_ -]?key|access[_ -]?key|secret|token|password|credential)\s*[=:]\s*[^\s,;]{4,}"
    r"|\b(?:sk|rk|pk)_[A-Za-z0-9_-]{8,}"
)
_SENSITIVE_COMMAND = re.compile(
    r"(?ix)"
    r"(?:authorization|bearer\s+[A-Za-z0-9._~+\-/=]{8,}|api[_ -]?key|access[_ -]?key|"
    r"secret|token|password|credential|auth\.json|keychain|security\s+(?:find|dump)|"
    r"printenv|\benv\b|/etc/|/dev/|\$\{?(?:HOME|CODEX_HOME)\}?|(?<!\w)~[/\\])"
)
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^\s'\"`|;&<>()\[\]{}]+)")
_TRAILING_PATH_PUNCTUATION = ".,:!?)]}"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_write(path: Path, text: str) -> None:
    """Create an evidence artifact once, mode 0600, inside a fresh run dir."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        raise


def _private_json(path: Path, value: Any) -> None:
    _private_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _is_under(child: Path, parent: Path) -> bool:
    """Return whether a lexical/resolved child is at or below parent."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _reject_links_and_special_files(root: Path) -> None:
    """Reject traversal surprises before hashing an evaluation fixture."""
    if root.is_symlink():
        raise ValueError(f"symlink source is not permitted: {root}")
    if not root.is_dir():
        raise ValueError(f"expected a directory: {root}")
    for base, dirs, files in os.walk(root, followlinks=False):
        base_path = Path(base)
        retained_dirs: list[str] = []
        for name in dirs:
            item = base_path / name
            mode = item.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ValueError(f"only non-symlink directories are permitted: {item}")
            retained_dirs.append(name)
        dirs[:] = retained_dirs
        for name in files:
            item = base_path / name
            mode = item.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ValueError(f"only regular files are permitted in source tree: {item}")


def tree_manifest(root: Path) -> dict[str, Any]:
    """Return a deterministic regular-tree manifest without retaining contents.

    Directory and mode metadata are included so creating an empty directory or
    chmod-ing a protected entry cannot disappear from the integrity comparison.
    """
    root = root.resolve(strict=True)
    _reject_links_and_special_files(root)
    directories: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for base, dirs, names in os.walk(root, followlinks=False):
        base_path = Path(base)
        dirs.sort()
        for name in dirs:
            item = base_path / name
            directories.append({
                "path": item.relative_to(root).as_posix(),
                "mode": stat.S_IMODE(item.lstat().st_mode),
            })
        for name in sorted(names):
            item = base_path / name
            metadata = item.lstat()
            files.append({
                "path": item.relative_to(root).as_posix(),
                "bytes": metadata.st_size,
                "mode": stat.S_IMODE(metadata.st_mode),
                "sha256": _sha256_file(item),
            })
    body = {"schema_version": 1, "directories": directories, "files": files}
    return {**body, "tree_sha256": _sha256_bytes(_canonical_json(body))}


def _manifest_after(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return tree_manifest(root), None
    except (OSError, ValueError, UnicodeError):
        # Error type is adequate to demonstrate that the postcondition could not
        # be checked.  Do not preserve arbitrary exception text or host paths.
        return None, type(sys.exception()).__name__


def _manifest_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    """Describe structural changes only; never include source content."""
    changes: list[dict[str, str]] = []
    for key in ("directories", "files"):
        old = {entry["path"]: entry for entry in before[key]}
        new = {entry["path"]: entry for entry in after[key]}
        for path in sorted(old.keys() - new.keys()):
            changes.append({"kind": key[:-1], "path": path, "change": "deleted"})
        for path in sorted(new.keys() - old.keys()):
            changes.append({"kind": key[:-1], "path": path, "change": "added"})
        for path in sorted(old.keys() & new.keys()):
            if old[path] != new[path]:
                changes.append({"kind": key[:-1], "path": path, "change": "modified"})
    return changes


def _read_regular_utf8(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a non-symlink regular file")
    return path.read_text(encoding="utf-8")


def _validate_fixture(cwd_arg: Path) -> tuple[Path, Path, dict[str, Any]]:
    if cwd_arg.is_symlink():
        raise ValueError("--cwd must be a non-symlink directory")
    cwd = cwd_arg.resolve(strict=True)
    _reject_links_and_special_files(cwd)
    skill = cwd / ".agents" / "skills" / "pua" / "SKILL.md"
    text = _read_regular_utf8(skill, "native PUA skill")
    if not re.search(r"(?m)^name:\s*[\"']?pua[\"']?\s*$", text):
        raise ValueError("native PUA skill frontmatter must declare name: pua")
    return cwd, skill, tree_manifest(skill.parent)


def _validate_private_home(path: Path, option: str) -> Path:
    # The caller may deliberately place an auth.json symlink *inside* this
    # directory.  We only validate the directory itself and never enumerate,
    # resolve, copy, hash, or write its contents.
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{option} must be an existing non-symlink directory")
    return path.absolute()


def _validate_binary(path: Path) -> Path:
    binary = path.resolve(strict=True)
    if not binary.is_file():
        raise ValueError("--binary must resolve to a regular file")
    if not os.access(binary, os.X_OK):
        raise ValueError("--binary is not executable")
    return binary


def _read_version(binary: Path) -> str | None:
    """Call only the local version endpoint; never a model endpoint."""
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout.decode("utf-8", "replace").strip()
    match = re.fullmatch(r"codex-cli\s+([0-9][A-Za-z0-9._-]*)", output)
    return match.group(1) if completed.returncode == 0 and match else None


def _safe_prompt_evidence(prompt: str, case: str) -> dict[str, Any]:
    raw = prompt.encode("utf-8")
    explicit = bool(_EXPLICIT_PUA.search(prompt))
    return {
        "sha256": _sha256_bytes(raw),
        "bytes": len(raw),
        "explicit_dollar_pua_token": explicit,
        "activation_request": "explicit_user_text_$pua" if explicit else "natural_language_project_skill_request",
        "case_requires_explicit_dollar_pua": case == "2",
        "content_retained": False,
    }


def _command_for_case(binary: Path, model: str, cwd: Path, prompt: str, case: str) -> list[str]:
    command = [
        str(binary),
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
        "--color", "never",
        "--cd", str(cwd),
        "--model", model,
        "-c", 'approval_policy="never"',
        "-c", 'model_reasoning_effort="high"',
        "-c", 'web_search="disabled"',
    ]
    for feature in BASE_DISABLED_FEATURES:
        command.extend(("--disable", feature))
    if case == "1":
        command.extend(("--sandbox", "workspace-write"))
    else:
        command.extend(("--sandbox", "read-only", "--disable", "shell_tool"))
    # A single final argv item prevents the prompt from being read from stdin;
    # stdin itself is additionally connected to DEVNULL below.
    command.append(prompt)
    return command


def _redacted_command_template(model: str, case: str) -> list[str]:
    command = [
        "codex", "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
        "--json", "--color", "never", "--cd", "<case-root>", "--model", model,
        "-c", 'approval_policy="never"', "-c", 'model_reasoning_effort="high"', "-c", 'web_search="disabled"',
    ]
    for feature in BASE_DISABLED_FEATURES:
        command.extend(("--disable", feature))
    if case == "1":
        command.extend(("--sandbox", "workspace-write"))
    else:
        command.extend(("--sandbox", "read-only", "--disable", "shell_tool"))
    command.append("<prompt-not-retained>")
    return command


def _child_environment(home: Path, codex_home: Path) -> tuple[dict[str, str], list[str]]:
    """Use only the supplied isolated homes plus non-secret runtime basics."""
    environment = {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": os.environ.get("LANG", "C"),
        "LC_ALL": os.environ.get("LC_ALL", "C"),
        "NO_COLOR": "1",
    }
    # Values are deliberately not put in any artifact.  This is not proof the
    # child cannot reach credentials in its explicitly provided CODEX_HOME.
    return environment, sorted(environment)


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate only the process group created for this runner child."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


def _classify_error_text(value: str | None) -> str:
    if not value:
        return "unknown"
    text = value.lower()
    if "401" in text or "unauthorized" in text:
        return "http_401"
    if "403" in text or "forbidden" in text:
        return "http_403"
    if "429" in text or "rate limit" in text or "rate_limit" in text:
        return "rate_limit"
    if ("unsupported" in text and "model" in text) or "model_not_found" in text or "unknown model" in text:
        return "unsupported_model"
    if "auth" in text or "login" in text or "credential" in text or "api key" in text:
        return "missing_auth"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "unknown"


def _event_keys(event: dict[str, Any]) -> list[str]:
    return sorted(str(key) for key in event)


def _item_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    item = event.get("item")
    return item if isinstance(item, dict) else None


def _event_item_type(event: dict[str, Any]) -> str | None:
    item = _item_from_event(event)
    if item is not None and isinstance(item.get("type"), str):
        return item["type"]
    if event.get("type") in {"agent_message", "command_execution", "reasoning"}:
        return str(event["type"])
    return None


def _extract_visible_agent_text(event: dict[str, Any]) -> str | None:
    item = _item_from_event(event)
    source: dict[str, Any] = item if _event_item_type(event) == "agent_message" and item is not None else event
    for field in ("text", "message"):
        value = source.get(field)
        if isinstance(value, str):
            return value
    return None


def _extract_command(event: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    item = _item_from_event(event)
    source: dict[str, Any] = item if _event_item_type(event) == "command_execution" and item is not None else event
    command = source.get("command")
    details: dict[str, Any] = {}
    for field in ("status", "exit_code"):
        value = source.get(field)
        if isinstance(value, (str, int)) or value is None:
            details[field] = value
    return command if isinstance(command, str) else None, details


def _root_spellings(root: Path) -> set[str]:
    """Return only exact root spellings, including macOS /var aliasing."""
    spellings = {str(root.absolute())}
    with contextlib.suppress(OSError):
        spellings.add(str(root.resolve(strict=False)))
    # ``Path.resolve`` commonly changes /var/... to /private/var/... on macOS.
    # Add only the corresponding exact root alias, never a broad /var prefix.
    for spelling in list(spellings):
        if spelling.startswith("/private/var/"):
            spellings.add(spelling.removeprefix("/private"))
        elif spelling.startswith("/var/"):
            spellings.add("/private" + spelling)
    return spellings


def _replace_roots(command: str, roots: Iterable[tuple[Path, str]]) -> str:
    result = command
    # Replace the longest root first: the PUA skill is nested under the case.
    for root, replacement in sorted(roots, key=lambda pair: len(str(pair[0])), reverse=True):
        for spelling in sorted(_root_spellings(root), key=len, reverse=True):
            result = result.replace(spelling, replacement)
    return result


def _command_is_retained(command: str, *, case_root: Path, skill_root: Path) -> tuple[bool, str | None, str | None]:
    """Retain a command only when it has no sensitive marker or external path."""
    if not command or len(command.encode("utf-8", "replace")) > 16 * 1024 or "\x00" in command:
        return False, "malformed_or_oversized", None
    if _SENSITIVE_COMMAND.search(command):
        return False, "sensitive_marker", None
    allowed = (case_root.resolve(strict=False), skill_root.resolve(strict=False))
    for match in _ABSOLUTE_PATH.finditer(command):
        token = match.group(0).rstrip(_TRAILING_PATH_PUNCTUATION)
        # Only absolute source addresses inside the fixture are retainable.
        try:
            candidate = Path(token).resolve(strict=False)
        except OSError:
            return False, "unparseable_absolute_path", None
        if not any(_is_under(candidate, root) for root in allowed):
            return False, "outside_case_or_skill_path", None
    return True, None, _replace_roots(command, ((case_root, "<case>"), (skill_root, "<skill>")))


def _safe_file_change_path(value: Any, *, case_root: Path, skill_root: Path) -> tuple[bool, str | None]:
    """Keep only a case/skill-relative changed path; never retain a diff."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return False, None
    candidate: Path
    try:
        raw = Path(value)
        candidate = raw.resolve(strict=False) if raw.is_absolute() else (case_root / raw).resolve(strict=False)
    except OSError:
        return False, None
    if not (_is_under(candidate, case_root.resolve(strict=False)) or _is_under(candidate, skill_root.resolve(strict=False))):
        return False, None
    if raw.is_absolute():
        return True, _replace_roots(value, ((case_root, "<case>"), (skill_root, "<skill>")))
    return True, candidate.relative_to(case_root.resolve(strict=False)).as_posix()


def _extract_file_changes(event: dict[str, Any], *, case_root: Path, skill_root: Path) -> list[dict[str, Any]]:
    """Reduce Codex native ``file_change`` metadata to path/change-kind only."""
    item = _item_from_event(event) or event
    raw_changes = item.get("changes")
    source_changes = raw_changes if isinstance(raw_changes, list) else [item]
    reduced: list[dict[str, Any]] = []
    for change in source_changes:
        mapping = change if isinstance(change, dict) else {}
        raw_path = mapping.get("path", mapping.get("file_path"))
        retained, path = _safe_file_change_path(raw_path, case_root=case_root, skill_root=skill_root)
        change_kind = mapping.get("kind", mapping.get("change", mapping.get("type")))
        record: dict[str, Any] = {
            "path_retained": retained,
            "path": path,
            "change": change_kind if isinstance(change_kind, str) else None,
        }
        reduced.append(record)
    return reduced


def _explicit_model_values(event: dict[str, Any]) -> list[dict[str, str]]:
    """Read only named structural identity fields, not arbitrary content."""
    findings: list[dict[str, str]] = []
    candidates: list[tuple[str, dict[str, Any]]] = [("event", event)]
    item = _item_from_event(event)
    if item is not None:
        candidates.append(("item", item))
    thread = event.get("thread")
    if isinstance(thread, dict):
        candidates.append(("thread", thread))
    turn = event.get("turn")
    if isinstance(turn, dict):
        candidates.append(("turn", turn))
    for location, mapping in candidates:
        for field in ("model", "model_name", "model_id"):
            value = mapping.get(field)
            if isinstance(value, str) and value:
                findings.append({"location": location, "field": field, "value": value})
    return findings


def _event_error_values(event: dict[str, Any]) -> list[str]:
    """Read error strings transiently for category assignment; never save them."""
    values: list[str] = []
    event_type = str(event.get("type", ""))
    if event_type not in {"error", "turn.failed", "turn_failed"}:
        item = _item_from_event(event)
        if not (item and item.get("status") in {"failed", "error"}):
            return values
    for mapping in (event, _item_from_event(event) or {}):
        for field in ("error", "error_message", "errorMessage", "message"):
            value = mapping.get(field)
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, dict):
                nested = value.get("message")
                if isinstance(nested, str):
                    values.append(nested)
    return values


def _redact_visible_text(value: str) -> str:
    return _SECRET_TEXT.sub("[REDACTED]", value)


def _reduce_jsonl(raw_stdout: bytes, *, case_root: Path, skill_root: Path, expected_model: str) -> tuple[dict[str, Any], str, bool]:
    """Turn raw JSONL into safe evidence; release caller's raw bytes afterward.

    Returns ``(summary, visible_transcript, observed_model_mismatch)``.  JSON
    formats not explicitly recognized remain structural-only evidence: event
    type, key names, sequence, and SHA-256 of the raw line, never event text.
    """
    visible: list[str] = []
    visible_bytes = 0
    event_count = 0
    malformed_count = 0
    unknown_events: list[dict[str, Any]] = []
    tool_events: list[dict[str, Any]] = []
    file_change_events: list[dict[str, Any]] = []
    valid_event_order: list[dict[str, Any]] = []
    reasoning_count = 0
    agent_message_count = 0
    command_count = 0
    retained_command_count = 0
    turn_completed: list[int] = []
    turn_failed: list[int] = []
    error_events: list[dict[str, Any]] = []
    observed_models: list[dict[str, Any]] = []
    model_mismatch = False
    skill_events: list[dict[str, Any]] = []
    agent_read_skill_path = False

    for raw_line in raw_stdout.splitlines():
        if not raw_line:
            continue
        event_count += 1
        sequence = event_count
        try:
            event = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed_count += 1
            unknown_events.append({
                "sequence": sequence,
                "type": "<malformed-jsonl>",
                "keys": [],
                "sha256": _sha256_bytes(raw_line),
            })
            continue
        if not isinstance(event, dict):
            malformed_count += 1
            unknown_events.append({
                "sequence": sequence,
                "type": f"<{type(event).__name__}>",
                "keys": [],
                "sha256": _sha256_bytes(raw_line),
            })
            continue

        event_type = event.get("type") if isinstance(event.get("type"), str) else "<missing-type>"
        item_type = _event_item_type(event)
        valid_event_order.append({"sequence": sequence, "event_type": event_type, "item_type": item_type})
        for finding in _explicit_model_values(event):
            observation = {"sequence": sequence, "event_type": event_type, **finding}
            if observation not in observed_models:
                observed_models.append(observation)
            if finding["value"] != expected_model:
                model_mismatch = True

        errors = _event_error_values(event)
        if errors or event_type in {"error", "turn.failed", "turn_failed"}:
            categories = sorted(set(_classify_error_text(value) for value in errors)) or ["unknown"]
            error_events.append({"sequence": sequence, "event_type": event_type, "classifications": categories})

        if event_type == "turn.completed":
            turn_completed.append(sequence)
        elif event_type in {"turn.failed", "turn_failed"}:
            turn_failed.append(sequence)

        if item_type == "file_change":
            # Codex emits file-change items independently of shell commands.
            # Preserve sequence, state, and safe changed paths, but never a
            # patch/diff/content payload.  The visible-message count lets a
            # reviewer see whether the change preceded the first visible reply.
            item = _item_from_event(event) or {}
            status = item.get("status")
            file_change_events.append({
                "sequence": sequence,
                "event_type": event_type,
                "item_type": "file_change",
                "status": status if isinstance(status, str) else None,
                "visible_agent_messages_before": agent_message_count,
                "changes": _extract_file_changes(event, case_root=case_root, skill_root=skill_root),
            })
            continue

        if item_type == "reasoning":
            reasoning_count += 1
            # Reasoning has no per-item structural record: retaining a content
            # hash can still become an unnecessary private-thinking identifier.
            continue

        if item_type == "agent_message":
            agent_message_count += 1
            text = _extract_visible_agent_text(event)
            if text is not None:
                redacted = _redact_visible_text(text)
                rendered = f"### agent_message #{sequence}\n\n{redacted}\n"
                encoded = rendered.encode("utf-8", "replace")
                if visible_bytes + len(encoded) <= MAX_VISIBLE_TEXT_BYTES:
                    visible.append(rendered)
                    visible_bytes += len(encoded)
                else:
                    visible.append(f"### agent_message #{sequence}\n\n[VISIBLE_TEXT_LIMIT_REACHED]\n")
            continue

        if item_type == "command_execution":
            command_count += 1
            command, details = _extract_command(event)
            retained, reason, safe_command = _command_is_retained(command or "", case_root=case_root, skill_root=skill_root)
            record: dict[str, Any] = {
                "sequence": sequence,
                "event_type": event_type,
                "item_type": "command_execution",
                "command_retained": retained,
                "retention_reason": reason,
                **details,
            }
            if retained and safe_command is not None:
                record["command"] = safe_command
                retained_command_count += 1
                # The normalized placeholder handles macOS /var vs /private/var
                # spellings without retaining either host path.
                if "<skill>" in safe_command:
                    agent_read_skill_path = True
            tool_events.append(record)
            continue

        # Normal Codex CLI JSON currently includes thread/turn/item lifecycle
        # records.  A future unknown record is useful only structurally.
        known_lifecycle = {
            "thread.started", "turn.started", "item.started", "item.completed", "turn.completed",
            "turn.failed", "turn_failed", "error",
        }
        if event_type not in known_lifecycle:
            unknown_events.append({
                "sequence": sequence,
                "type": event_type,
                "keys": _event_keys(event),
                "sha256": _sha256_bytes(raw_line),
            })

        # There is no normal ``skill.loaded`` schema in 0.153.4's exec JSON
        # protocol.  If a future explicit skill event appears, retain only its
        # structural path/name result—not a skill body—and make the distinction
        # explicit.  Do not infer it from $pua or an agent message.
        if event_type in {"skill.loaded", "skill_load", "skill.invoked"}:
            name = event.get("name") if isinstance(event.get("name"), str) else None
            path = event.get("path") if isinstance(event.get("path"), str) else None
            path_matches = False
            if path:
                with contextlib.suppress(OSError):
                    path_matches = Path(path).resolve(strict=False) == skill_root / "SKILL.md"
            skill_events.append({
                "sequence": sequence,
                "event_type": event_type,
                "name": name,
                "path_matches_expected_skill_file": path_matches,
            })

    explicit_load = any(item["name"] == "pua" and item["path_matches_expected_skill_file"] for item in skill_events)
    if explicit_load:
        skill_status = "explicit_json_skill_load_observed"
    elif agent_read_skill_path:
        skill_status = "agent_read_native_skill_path_only"
    else:
        skill_status = "evidence_gap_no_native_load_event"

    if model_mismatch:
        identity_status = "observable_model_mismatch"
    elif observed_models:
        identity_status = "exact_client_runtime_metadata_observed"
    else:
        identity_status = "evidence_gap_no_json_model_field"

    last_turn_completed = turn_completed[-1] if turn_completed else None
    events_after_last_completed = [
        record for record in valid_event_order if last_turn_completed is not None and record["sequence"] > last_turn_completed
    ]
    terminal_success = (
        bool(turn_completed)
        and not turn_failed
        and not error_events
        and malformed_count == 0
        and not events_after_last_completed
    )
    summary = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "event_count": event_count,
        "malformed_jsonl_count": malformed_count,
        "unknown_events": unknown_events,
        "thinking": {"reasoning_event_count": reasoning_count, "content_retained": False},
        "visible": {"agent_message_count": agent_message_count, "content_retained": True, "max_bytes": MAX_VISIBLE_TEXT_BYTES},
        "tools": {
            "command_execution_count": command_count,
            "retained_command_count": retained_command_count,
            "sequence_is_event_order_only": True,
            "events": tool_events,
            "tool_output_retained": False,
        },
        "file_changes": {
            "native_file_change_event_count": len(file_change_events),
            "events": file_change_events,
            "diff_or_content_retained": False,
        },
        "terminal": {
            "turn_completed_sequences": turn_completed,
            "turn_failed_sequences": turn_failed,
            "error_events": error_events,
            "events_after_last_turn_completed": events_after_last_completed,
            "malformed_jsonl_prevents_success": malformed_count > 0,
            "terminal_success": terminal_success,
            "policy": "requires a final turn.completed after every valid event, no malformed JSONL, and no turn.failed/error; CLI exit code alone is insufficient",
        },
        "model_identity": {
            "requested_model": expected_model,
            "status": identity_status,
            "json_model_fields": observed_models,
            "client_runtime_metadata_is_not_provider_server_receipt": True,
            "provider_server_receipt_observed": False,
        },
        "native_skill_loading": {
            "status": skill_status,
            "explicit_json_events": skill_events,
            "agent_read_of_skill_path_is_not_host_body_injection_proof": agent_read_skill_path,
            "literal_prompt_token_is_not_loading_proof": True,
        },
    }
    return summary, "\n".join(visible).rstrip() + ("\n" if visible else ""), model_mismatch


def _watch_process(process: subprocess.Popen[bytes], *, timeout_seconds: int) -> dict[str, Any]:
    """Drain child streams in memory; on timeout/limit kill only its PGID."""
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("child pipes were not created")
    selector = selectors.DefaultSelector()
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    for stream in streams.values():
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    stdout = bytearray()
    stderr = bytearray()
    stdout_limited = False
    stderr_limited = False
    timed_out = False
    stopped = False
    deadline = time.monotonic() + timeout_seconds
    try:
        while selector.get_map():
            now = time.monotonic()
            if not stopped and now >= deadline:
                timed_out = True
                stopped = True
                _stop_process_group(process)
            events = selector.select(timeout=0.05)
            for key, _ in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    with contextlib.suppress(Exception):
                        selector.unregister(stream)
                    continue
                target = stdout if stream is streams["stdout"] else stderr
                cap = MAX_STDOUT_BYTES if stream is streams["stdout"] else MAX_STDERR_BYTES
                if len(target) + len(chunk) > cap:
                    allowed = max(0, cap - len(target))
                    target.extend(chunk[:allowed])
                    if stream is streams["stdout"]:
                        stdout_limited = True
                    else:
                        stderr_limited = True
                    if not stopped:
                        stopped = True
                        _stop_process_group(process)
                else:
                    target.extend(chunk)
            # Once exited, pipe EOF should arrive shortly.  The selector loop
            # continues to drain it, avoiding a subprocess pipe deadlock.
            if stopped and process.poll() is not None and not events:
                # A dead child with still-open inherited FDs is pathological;
                # close only our read ends after its owned process group ended.
                for stream in list(selector.get_map().values()):
                    with contextlib.suppress(Exception):
                        selector.unregister(stream.fileobj)
    finally:
        selector.close()
        for stream in streams.values():
            with contextlib.suppress(Exception):
                stream.close()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)
    return {
        "exit_code": process.poll(),
        "timed_out": timed_out,
        "stdout_limited": stdout_limited,
        "stderr_limited": stderr_limited,
        "stdout": bytes(stdout),
        "stderr": bytes(stderr),
    }


def _integrity_result(case: str, before: dict[str, Any], after: dict[str, Any] | None, after_error: str | None) -> dict[str, Any]:
    if after is None:
        return {
            "passed": False,
            "post_manifest_error_type": after_error,
            "changes": None,
            "policy": "post-run task manifest could not be produced",
        }
    changes = _manifest_changes(before, after)
    if case == "1":
        allowed = len(changes) == 1 and changes[0]["kind"] == "file" and changes[0]["path"] == "events.py" and changes[0]["change"] in {"added", "modified"}
        policy = "case 1 requires exactly one added/modified file: events.py"
    else:
        allowed = not changes
        policy = "case 2 requires no task-tree change"
    return {
        "passed": allowed,
        "post_manifest_error_type": None,
        "changes": changes,
        "policy": policy,
        "before_tree_sha256": before["tree_sha256"],
        "after_tree_sha256": after["tree_sha256"],
    }


def _plan(
    *, args: argparse.Namespace, binary: Path, cwd: Path, skill_file: Path, skill_manifest: dict[str, Any],
    task_manifest: dict[str, Any], prompt: str, prompt_path: Path, home: Path, codex_home: Path,
) -> dict[str, Any]:
    prompt_evidence = _safe_prompt_evidence(prompt, args.case)
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "live": False,
        "requested_model": args.model,
        "model_selection_policy": "literal --model value only; runner accepts only gpt-6-astra and never aliases or fallback selectors",
        "codex_binary": {
            "sha256": _sha256_file(binary),
            "reported_version": None,
            "required_cli_version": EXPECTED_CLI_VERSION,
            "version_checked_only_with_run": True,
        },
        "prompt": prompt_evidence,
        "task_fixture": {
            "cwd": str(cwd),
            "before_manifest": task_manifest,
            "protected_files_policy": "case 1: exactly events.py added/modified; case 2: no task-tree change",
        },
        "native_pua_source": {
            "skill_file": str(skill_file),
            "before_manifest": skill_manifest,
            "source_is_inside_task_fixture": _is_under(skill_file, cwd),
        },
        "isolated_auth_homes": {
            "home_provided": True,
            "codex_home_provided": True,
            "paths_or_contents_retained": False,
            "runner_does_not_enumerate_copy_hash_or_write_them": True,
            "auth_symlink_target_not_inspected": True,
        },
        "command_policy": {
            "command": _redacted_command_template(args.model, args.case),
            "stdin": "closed",
            "process_group": "new session; timeout/limit only terminates owned group",
            "features_disabled": list(BASE_DISABLED_FEATURES) + (["shell_tool"] if args.case == "2" else []),
            "sandbox": "workspace-write" if args.case == "1" else "read-only",
            "approval_policy": "never",
            "web_search": "disabled",
            "ignore_rules_used": False,
            "bypass_used": False,
        },
        "evidence_boundary": {
            "raw_stdout_persisted": False,
            "raw_stderr_persisted": False,
            "reasoning_persisted": False,
            "tool_output_persisted": False,
            "prompt_content_persisted": False,
            "unknown_json_event_policy": "type/key names/raw-line SHA-256 only",
            "model_identity_note": "JSON model fields are client/runtime metadata, not an independent provider receipt",
            "skill_loading_note": "a literal $pua token or catalog listing is not native skill-body loading proof",
        },
        "timeout_seconds": args.timeout,
        "run_directory": "must be new and outside --cwd; created only with --run",
        "global_configuration_changed_by_runner": False,
        "run_only_note": "--run is required before version probe, child launch, or run-directory creation",
    }


def _compact_output(summary: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    return {
        "run_passed": summary.get("run_passed"),
        "execution_passed": summary.get("execution_passed"),
        "case_claim_status": summary.get("case_claim_status"),
        "native_skill_loading_status": summary.get("native_skill_loading", {}).get("status"),
        "model_identity_status": summary.get("model_identity", {}).get("status"),
        "terminal_success": summary.get("terminal", {}).get("terminal_success"),
        "workspace_integrity_passed": summary.get("workspace_integrity", {}).get("passed"),
        "failure_categories": summary.get("failure_categories"),
        "run_dir": str(run_dir),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY, help="exact local Codex CLI binary")
    parser.add_argument("--model", required=True, help="must be the exact literal gpt-6-astra")
    parser.add_argument("--cwd", type=Path, required=True, help="existing native task fixture containing .agents/skills/pua")
    parser.add_argument("--prompt-file", type=Path, required=True,
                        help="UTF-8 prompt; case 2 requires an explicit $pua token")
    parser.add_argument("--run-dir", type=Path, required=True, help="new evidence directory; must lie outside --cwd")
    parser.add_argument("--codex-home", type=Path, required=True, help="caller-prepared isolated CODEX_HOME; never inspected")
    parser.add_argument("--home", type=Path, required=True, help="caller-prepared isolated HOME; never inspected")
    parser.add_argument("--case", choices=("1", "2"), required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--run", action="store_true", help="required before invoking Codex or creating --run-dir")
    args = parser.parse_args(argv)

    if args.model != EXPECTED_MODEL:
        parser.error(f"--model must be the exact supported selector {EXPECTED_MODEL!r}; aliases/fuzzy names are rejected")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if not args.prompt_file.is_absolute():
        parser.error("--prompt-file must be absolute")

    try:
        binary = _validate_binary(args.binary)
        cwd, skill_file, skill_manifest = _validate_fixture(args.cwd)
        task_manifest = tree_manifest(cwd)
        prompt_path = args.prompt_file.resolve(strict=True)
        prompt = _read_regular_utf8(prompt_path, "--prompt-file")
        if args.case == "2" and not _EXPLICIT_PUA.search(prompt):
            raise ValueError("case 2 --prompt-file must contain an explicit $pua token; the runner will not inject one")
        home = _validate_private_home(args.home, "--home")
        codex_home = _validate_private_home(args.codex_home, "--codex-home")
        run_dir = args.run_dir.absolute()
        if run_dir.exists():
            raise ValueError("--run-dir must not already exist; refusing to overwrite evidence")
        # Absolute lexical and resolved paths both matter on macOS /var vs
        # /private/var.  Neither spelling may be at/below the task root.
        run_resolved = run_dir.resolve(strict=False)
        cwd_spellings = {cwd, args.cwd.absolute()}
        if any(_is_under(run_dir, root) or _is_under(run_resolved, root.resolve(strict=False)) for root in cwd_spellings):
            raise ValueError("--run-dir must be outside --cwd")
    except (OSError, ValueError, UnicodeError) as error:
        parser.error(str(error))

    plan = _plan(
        args=args, binary=binary, cwd=cwd, skill_file=skill_file, skill_manifest=skill_manifest,
        task_manifest=task_manifest, prompt=prompt, prompt_path=prompt_path, home=home, codex_home=codex_home,
    )
    if not args.run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    old_umask = os.umask(0o077)
    try:
        run_dir.mkdir(parents=True, mode=0o700)
        os.chmod(run_dir, 0o700)
        reported_version = _read_version(binary)
        invocation = {
            **plan,
            "live": True,
            "run_directory": str(run_dir),
            "codex_binary": {**plan["codex_binary"], "reported_version": reported_version},
            "version_gate": {
                "expected": EXPECTED_CLI_VERSION,
                "observed": reported_version,
                "passed": reported_version == EXPECTED_CLI_VERSION,
                "method": "local --version only; no model/login request",
            },
            "task_fixture": {**plan["task_fixture"], "prompt_file_before_sha256": _sha256_file(prompt_path)},
        }
        _private_json(run_dir / "invocation.json", invocation)
        _private_json(run_dir / "task-before-manifest.json", task_manifest)
        _private_json(run_dir / "native-pua-before-manifest.json", skill_manifest)

        if reported_version != EXPECTED_CLI_VERSION:
            summary = {
                "schema_version": RUNNER_SCHEMA_VERSION,
                "run_passed": False,
                "execution_passed": False,
                "case_claim_status": "not_run_version_gate_failed",
                "failure_categories": ["unsupported_cli_version"],
                "terminal": {"terminal_success": False, "policy": "child not launched"},
                "workspace_integrity": {"passed": None, "policy": "child not launched"},
                "model_identity": {"requested_model": args.model, "status": "not_observed_child_not_launched", "provider_server_receipt_observed": False},
                "native_skill_loading": {"status": "not_observed_child_not_launched", "literal_prompt_token_is_not_loading_proof": True},
            }
            _private_json(run_dir / "summary.json", summary)
            print(json.dumps(_compact_output(summary, run_dir), ensure_ascii=False))
            return 2

        command = _command_for_case(binary, args.model, cwd, prompt, args.case)
        child_env, child_env_names = _child_environment(home, codex_home)
        _private_json(run_dir / "launch-policy.json", {
            "command": _redacted_command_template(args.model, args.case),
            "child_environment_names_only": child_env_names,
            "environment_values_retained": False,
            "stdin": "DEVNULL",
            "start_new_session": True,
            "timeout_kills_only_owned_process_group": True,
            "raw_stream_files_created": False,
            "credential_files_read_by_runner": False,
        })

        started = time.monotonic()
        launch_error_type: str | None = None
        watched: dict[str, Any] = {
            "exit_code": None, "timed_out": False, "stdout_limited": False, "stderr_limited": False,
            "stdout": b"", "stderr": b"",
        }
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=child_env,
                cwd=str(cwd),
            )
            watched = _watch_process(process, timeout_seconds=args.timeout)
        except OSError as error:
            launch_error_type = type(error).__name__

        # Reduce stdout before creating any artifact, then release raw buffers.
        stream_summary, visible_transcript, observable_mismatch = _reduce_jsonl(
            watched["stdout"], case_root=cwd, skill_root=skill_file.parent, expected_model=args.model,
        )
        stderr_classification = _classify_error_text(watched["stderr"].decode("utf-8", "replace"))
        stderr_nonempty = bool(watched["stderr"])
        watched["stdout"] = b""
        watched["stderr"] = b""

        task_after, task_after_error = _manifest_after(cwd)
        skill_after, skill_after_error = _manifest_after(skill_file.parent)
        integrity = _integrity_result(args.case, task_manifest, task_after, task_after_error)
        prompt_after_sha: str | None
        try:
            prompt_after_sha = _sha256_file(prompt_path)
        except OSError:
            prompt_after_sha = None
        prompt_unchanged = prompt_after_sha == invocation["task_fixture"]["prompt_file_before_sha256"]
        skill_unchanged = skill_after is not None and skill_after["tree_sha256"] == skill_manifest["tree_sha256"]

        failure_categories: set[str] = set()
        if launch_error_type:
            failure_categories.add("launch_error")
        if watched["timed_out"]:
            failure_categories.add("timeout")
        if watched["stdout_limited"]:
            failure_categories.add("stdout_size_limit")
        if watched["stderr_limited"]:
            failure_categories.add("stderr_size_limit")
        if stderr_nonempty and stderr_classification != "unknown":
            failure_categories.add(stderr_classification)
        for record in stream_summary["terminal"]["error_events"]:
            failure_categories.update(record["classifications"])
        if observable_mismatch:
            failure_categories.add("observable_model_mismatch")
        if not integrity["passed"]:
            failure_categories.add("workspace_integrity_failed")
        if not prompt_unchanged:
            failure_categories.add("prompt_input_changed")
        if not skill_unchanged:
            failure_categories.add("native_skill_source_changed")

        terminal_success = stream_summary["terminal"]["terminal_success"]
        execution_passed = (
            launch_error_type is None
            and watched["exit_code"] == 0
            and not watched["timed_out"]
            and not watched["stdout_limited"]
            and not watched["stderr_limited"]
            and terminal_success
            and integrity["passed"]
            and prompt_unchanged
            and skill_unchanged
            and not observable_mismatch
        )
        native_status = stream_summary["native_skill_loading"]["status"]
        identity_status = stream_summary["model_identity"]["status"]
        full_case_evidence = (
            execution_passed
            and native_status == "explicit_json_skill_load_observed"
            and identity_status == "exact_client_runtime_metadata_observed"
        )
        if full_case_evidence:
            case_claim_status = "bounded_evidence_complete_but_not_provider_server_attested"
        elif execution_passed:
            case_claim_status = "execution_passed_with_skill_or_identity_evidence_gap"
        else:
            case_claim_status = "execution_failed"

        summary = {
            **stream_summary,
            "process": {
                "exit_code": watched["exit_code"],
                "timed_out": watched["timed_out"],
                "stdout_size_limited": watched["stdout_limited"],
                "stderr_size_limited": watched["stderr_limited"],
                "launch_error_type": launch_error_type,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "stderr": {
                    "nonempty": stderr_nonempty,
                    "classification": stderr_classification if stderr_nonempty else None,
                    "content_retained": False,
                },
            },
            "workspace_integrity": integrity,
            "frozen_inputs": {
                "prompt_file_unchanged": prompt_unchanged,
                "native_pua_skill_unchanged": skill_unchanged,
                "native_pua_post_manifest_error_type": skill_after_error,
            },
            "failure_categories": sorted(failure_categories),
            "execution_passed": execution_passed,
            # ``run_passed`` is deliberately the runner transport/integrity
            # gate, not a claim that $pua was injected or that a provider signed
            # the model identity.  Consumers must inspect case_claim_status.
            "run_passed": execution_passed,
            "case_claim_status": case_claim_status,
            "full_case_evidence_observed": full_case_evidence,
            "full_case_evidence_limit": "provider_server_receipt_not_observed_even_when_JSON_runtime_model_matches",
        }
        _private_write(run_dir / "visible-transcript.md", visible_transcript)
        _private_json(run_dir / "summary.json", summary)
        print(json.dumps(_compact_output(summary, run_dir), ensure_ascii=False))
        return 0 if execution_passed else (watched["exit_code"] if isinstance(watched["exit_code"], int) and watched["exit_code"] != 0 else 1)
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    raise SystemExit(main())
