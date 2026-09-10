#!/usr/bin/env python3
"""Privacy-preserving evidence reduction for an OMP JSON-mode run.

This module deliberately treats OMP's JSON stream as *client-observed* evidence.
It does not claim that a provider independently attested the model identity.  It
also never place thinking text, tool arguments (other than the fixed
``skill://<name>`` predicate), or tool-result text into the returned summary.

The functions are intentionally stdlib-only so that ``test-omp-evidence.py`` can
exercise them entirely offline with synthetic JSONL streams.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable


SCHEMA_VERSION = 2

# This is intentionally conservative.  The runner also never stores raw stderr
# or raw JSONL.  Redaction cannot turn an arbitrary model response into a secret
# safe channel, so a caller must not put credentials in its evaluation prompt.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key|secret|token|password|authorization)"
    r"\s*([:=])\s*([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]{8,}")


def sha256_bytes(data: bytes) -> str:
    """Return a hexadecimal SHA-256 digest."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Hash a file without interpreting its contents."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_visible_text(text: str) -> str:
    """Redact common inline credential forms from visible assistant text."""
    # Bearer first: otherwise ``Authorization: Bearer TOKEN`` would redact only
    # the word "Bearer" and leave TOKEN behind for the next expression.
    text = _BEARER.sub("Bearer [REDACTED]", text)
    return _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)


def _safe_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _message_content(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the OMP content shape without retaining unknown payloads."""
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _role(message: dict[str, Any]) -> str:
    raw = message.get("role", "")
    return raw if isinstance(raw, str) else ""


def classify_failure_text(text: str) -> str:
    """Classify an in-memory JSON failure without retaining the original text."""
    value = text.lower()
    # HTTP status has priority over a surrounding word such as "unauthorized",
    # so the report distinguishes concrete server feedback from a generic auth
    # hint.  This is a diagnostic classification, not proof of provider state.
    if "401" in value:
        return "http_401"
    if "403" in value:
        return "http_403"
    if "429" in value or "rate limit" in value or "rate_limit" in value or "too many requests" in value:
        return "rate_limit"
    if "unsupported model" in value or "model not found" in value or "unknown model" in value or "invalid model" in value:
        return "unsupported_model"
    if any(token in value for token in ("missing api", "api key", "authentication", "credential", "not authenticated", "unauthorized")):
        return "missing_auth"
    return "unknown"


def _event_messages(event: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    """Extract message-bearing events across known JSON-mode stream variants.

    ``message_end`` is preferred by :func:`inspect_stream`; direct-message forms
    are retained as a fallback for offline stubs and older OMP streams.
    """
    event_type = event.get("type")
    message = event.get("message")
    if isinstance(message, dict):
        yield str(event_type or ""), message
        return
    # Some integrations put a message object directly on the event.
    if isinstance(event.get("role"), str) and (
        "content" in event or event_type in {"assistant", "tool", "toolResult", "tool_result"}
    ):
        yield str(event_type or ""), event
    # ``agent_end`` sometimes embeds the final assistant message in a payload.
    if event_type == "agent_end":
        for key in ("finalMessage", "assistantMessage"):
            candidate = event.get(key)
            if isinstance(candidate, dict):
                yield "agent_end", candidate


def _agent_end_record(event: dict[str, Any], event_index: int) -> dict[str, Any]:
    """Reduce an agent_end frame without retaining its messages/content."""
    final_stop_reason: str | None = None
    final_failure_classification: str | None = None
    messages = event.get("messages")
    if isinstance(messages, list):
        for candidate in reversed(messages):
            if not isinstance(candidate, dict) or _role(candidate) != "assistant":
                continue
            reason = candidate.get("stopReason", candidate.get("stop_reason"))
            final_stop_reason = reason if isinstance(reason, str) else None
            error_message = candidate.get("errorMessage", candidate.get("error_message"))
            if isinstance(error_message, str) and error_message:
                final_failure_classification = classify_failure_text(error_message)
            break
    return {
        "event_index": event_index,
        "will_continue": event.get("willContinue") if isinstance(event.get("willContinue"), bool) else None,
        "final_assistant_stop_reason": final_stop_reason,
        "final_failure_classification": final_failure_classification,
    }


def _load_messages(
    stream_source: Path | bytes | bytearray,
) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, int], str, list[dict[str, Any]]]:
    """Read a JSONL stream, retaining messages only in process memory.

    ``bytes`` lets the bounded runner avoid ever creating a raw-output file.
    The returned raw-stream digest is for tamper correlation, not content
    publication.  Invalid/non-JSON lines are counted but never copied out.
    """
    counters: Counter[str] = Counter()
    all_messages: list[tuple[int, str, dict[str, Any]]] = []
    agent_end_events: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    if isinstance(stream_source, (bytes, bytearray)):
        lines = bytes(stream_source).splitlines(keepends=True)
    else:
        with stream_source.open("rb") as handle:
            lines = list(handle)
    for raw_line in lines:
        digest.update(raw_line)
        counters["stream_line_count"] += 1
        try:
            decoded = raw_line.decode("utf-8")
            event = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            counters["non_json_line_count"] += 1
            continue
        if not isinstance(event, dict):
            counters["non_object_event_count"] += 1
            continue
        counters["json_event_count"] += 1
        event_type = event.get("type")
        if isinstance(event_type, str):
            counters[f"event_type:{event_type}"] += 1
        event_index = counters["stream_line_count"]
        if event_type == "agent_end":
            agent_end_events.append(_agent_end_record(event, event_index))
        for kind, message in _event_messages(event):
            all_messages.append((event_index, kind, message))

    # A normal current OMP print stream has message_end events.  Prefer them so
    # agent_end's echoed final message does not inflate evidence.  If unavailable,
    # use the direct messages but de-duplicate identical message structures.
    primary = [(index, message) for index, kind, message in all_messages if kind == "message_end"]
    if primary:
        # message_end is authoritative and ordered; retaining each frame avoids
        # destroying the visible-text-before-tool-call ordering evidence.
        messages = primary
    else:
        candidates = [(index, message) for index, _, message in all_messages]
        seen: set[str] = set()
        messages = []
        for index, message in candidates:
            marker = sha256_bytes(_safe_json(message).encode("utf-8"))
            if marker in seen:
                continue
            seen.add(marker)
            messages.append((index, message))
    return messages, dict(counters), digest.hexdigest(), agent_end_events


def _canonical_selector(provider: Any, model: Any) -> str | None:
    if not isinstance(provider, str) or not isinstance(model, str):
        return None
    provider, model = provider.strip(), model.strip()
    if not provider or not model or "/" in provider or "/" in model:
        return None
    return f"{provider}/{model}"


def _block_type(block: dict[str, Any]) -> str:
    raw = block.get("type", "")
    return raw.lower().replace("_", "").replace("-", "") if isinstance(raw, str) else ""


def _tool_call_id(block: dict[str, Any]) -> str | None:
    for key in ("id", "toolCallId", "tool_call_id"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _tool_arguments(block: dict[str, Any]) -> dict[str, Any]:
    for key in ("arguments", "input", "args"):
        value = block.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _tool_result_id(message: dict[str, Any]) -> str | None:
    for key in ("toolCallId", "tool_call_id", "id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _tool_result_text(message: dict[str, Any]) -> str:
    """Collect only in-memory text needed for the marker predicate."""
    parts: list[str] = []
    for block in _message_content(message):
        value = block.get("text")
        if isinstance(value, str):
            parts.append(value)
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content)
    return "\n".join(parts)


def _result_details(message: dict[str, Any]) -> dict[str, Any]:
    details = message.get("details")
    return details if isinstance(details, dict) else {}


def _safe_resolved_path_matches(details: dict[str, Any], expected_source: Path | None) -> bool | None:
    """Compare an observed path locally, without putting it into evidence."""
    if expected_source is None:
        return None
    observed = details.get("resolvedPath")
    if not isinstance(observed, str):
        return False
    try:
        return Path(observed).resolve() == expected_source.resolve()
    except OSError:
        return False


def inspect_stream(
    stream_source: Path | bytes | bytearray,
    expected_selector: str,
    *,
    expected_skill_name: str = "pua",
    expected_marker: str = "PUA-RUNTIME-CONTRACT:START",
    expected_skill_source: Path | None = None,
) -> dict[str, Any]:
    """Reduce OMP JSON output to an evidence summary safe for persistence.

    A native skill proof requires all of the following:

    * an actual ``read`` tool call with the exact ``skill://<name>`` argument;
    * a matching non-error tool result;
    * the expected immutable marker in that result; and
    * when an expected source is supplied, OMP's structured ``resolvedPath``
      matching that copied fixture's ``SKILL.md``.

    Mere appearances of ``pua`` or ``skill://pua`` in assistant text never
    satisfy this predicate.
    """
    if "/" not in expected_selector or expected_selector.count("/") != 1:
        raise ValueError("expected_selector must be exact provider/model")
    messages, counters, raw_stream_sha, agent_end_events = _load_messages(stream_source)

    assistant_selectors: list[str] = []
    missing_assistant_identity = 0
    visible_text: list[str] = []
    thinking_block_count = 0
    thinking_char_count = 0
    visible_text_block_count = 0
    visible_diagnosis_seen = False
    tool_calls: dict[str, dict[str, Any]] = {}
    tool_call_ledger: list[dict[str, Any]] = []
    tool_results: dict[str, dict[str, Any]] = {}
    assistant_stop_reasons: list[tuple[int, str]] = []
    failure_classifications: list[dict[str, Any]] = []

    for event_index, message in messages:
        role = _role(message)
        if role == "assistant":
            selector = _canonical_selector(message.get("provider"), message.get("model"))
            if selector is None:
                missing_assistant_identity += 1
            else:
                assistant_selectors.append(selector)
            stop_reason = message.get("stopReason", message.get("stop_reason"))
            if isinstance(stop_reason, str):
                assistant_stop_reasons.append((event_index, stop_reason))
            error_message = message.get("errorMessage", message.get("error_message"))
            if isinstance(error_message, str) and error_message:
                failure_classifications.append({
                    "event_index": event_index,
                    "source": "assistant_error_message",
                    "classification": classify_failure_text(error_message),
                    "content_retained": False,
                })
            for block_index, block in enumerate(_message_content(message)):
                kind = _block_type(block)
                if kind == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        visible_text.append(redact_visible_text(text))
                        visible_text_block_count += 1
                        if "[PUA-DIAGNOSIS]" in text:
                            visible_diagnosis_seen = True
                elif kind in {"thinking", "reasoning", "redactedthinking"}:
                    thinking_block_count += 1
                    text = block.get("thinking", block.get("text", ""))
                    if isinstance(text, str):
                        thinking_char_count += len(text)
                elif kind == "toolcall":
                    call_id = _tool_call_id(block)
                    name = block.get("name") if isinstance(block.get("name"), str) else ""
                    args = _tool_arguments(block)
                    exact_native = name == "read" and args.get("path") == f"skill://{expected_skill_name}"
                    if call_id:
                        tool_calls[call_id] = {
                            "name": name,
                            "exact_native_skill_read": exact_native,
                            "event_index": event_index,
                            "block_index": block_index,
                            "visible_text_blocks_before_call": visible_text_block_count,
                            "visible_pua_diagnosis_before_call": visible_diagnosis_seen,
                        }
                    tool_call_ledger.append({
                        "call_id_sha256": sha256_bytes((call_id or "").encode("utf-8"))[:16],
                        "name": name or None,
                        "exact_native_skill_read": exact_native,
                        "potential_business_action": name in {"write", "edit", "bash"},
                        "event_index": event_index,
                        "block_index": block_index,
                        "visible_text_blocks_before_call": visible_text_block_count,
                        "visible_pua_diagnosis_before_call": visible_diagnosis_seen,
                        "arguments_retained": False,
                    })
        elif role.lower() in {"toolresult", "tool_result", "tool"}:
            result_id = _tool_result_id(message)
            if result_id:
                body = _tool_result_text(message)
                is_error = bool(message.get("isError", message.get("is_error", False)))
                tool_name = message.get("toolName", message.get("tool_name"))
                if is_error:
                    failure_classifications.append({
                        "event_index": event_index,
                        "source": "tool_result",
                        "tool_name": tool_name if isinstance(tool_name, str) else None,
                        "classification": classify_failure_text(body),
                        "content_retained": False,
                    })
                tool_results[result_id] = {
                    "event_index": event_index,
                    "tool_name": tool_name if isinstance(tool_name, str) else None,
                    "is_error": is_error,
                    "marker_found": expected_marker in body,
                    "content_bytes": len(body.encode("utf-8")),
                    "content_sha256": sha256_bytes(body.encode("utf-8")),
                    "details_keys": sorted(str(key) for key in _result_details(message).keys()),
                    "source_path_matches_fixture": _safe_resolved_path_matches(
                        _result_details(message), expected_skill_source
                    ),
                }

    observed = sorted(set(assistant_selectors))
    unexpected = sorted(selector for selector in set(assistant_selectors) if selector != expected_selector)
    exact_model_confirmed = bool(assistant_selectors) and not missing_assistant_identity and not unexpected

    native_attempts: list[dict[str, Any]] = []
    for call_id, call in tool_calls.items():
        if not call["exact_native_skill_read"]:
            continue
        result = tool_results.get(call_id)
        # No raw resolved path or tool output is persisted.  The Boolean is
        # sufficient to prove whether it pointed at the exact copied fixture.
        result_ok = bool(result) and not result["is_error"]
        marker_found = bool(result and result["marker_found"])
        source_match = result["source_path_matches_fixture"] if result else False
        if not result:
            failure_reason = "missing_matching_tool_result"
        elif result["is_error"]:
            failure_reason = "tool_result_error"
        elif result["event_index"] <= call["event_index"]:
            failure_reason = "tool_result_precedes_call"
        elif result["tool_name"] != "read":
            failure_reason = "tool_result_name_missing_or_mismatch"
        elif not result["marker_found"]:
            failure_reason = "runtime_marker_missing"
        elif expected_skill_source is not None and "resolvedPath" not in result["details_keys"]:
            # Do not mistake an OMP transport/schema omission for a model's
            # inability to use the skill.  It is an evidence-gap failure.
            failure_reason = "missing_resolved_path_evidence"
        elif source_match is False:
            failure_reason = "resolved_path_mismatch"
        else:
            failure_reason = None
        native_attempts.append({
            "call_id_sha256": sha256_bytes(call_id.encode("utf-8"))[:16],
            "event_index": call["event_index"],
            "block_index": call["block_index"],
            "visible_text_blocks_before_call": call["visible_text_blocks_before_call"],
            "visible_pua_diagnosis_before_call": call["visible_pua_diagnosis_before_call"],
            "matching_nonerror_result": result_ok,
            "tool_result_event_index": result["event_index"] if result else None,
            "tool_result_name": result["tool_name"] if result else None,
            "runtime_marker_found": marker_found,
            "source_path_matches_fixture": source_match,
            "tool_result_details_keys": result["details_keys"] if result else [],
            "tool_result_content_bytes": result["content_bytes"] if result else None,
            "tool_result_content_sha256": result["content_sha256"] if result else None,
            "failure_reason": failure_reason,
            "tool_result_content_retained": False,
        })
    native_passed = any(
        attempt["failure_reason"] is None
        and attempt["matching_nonerror_result"]
        and attempt["runtime_marker_found"]
        and (attempt["source_path_matches_fixture"] is not False)
        for attempt in native_attempts
    )
    native_failure_reasons = sorted({
        attempt["failure_reason"] for attempt in native_attempts if attempt["failure_reason"] is not None
    })

    first_native_event_index = min((attempt["event_index"] for attempt in native_attempts), default=None)
    post_native_failure_reasons = sorted({
        reason for index, reason in assistant_stop_reasons
        if first_native_event_index is not None
        and index > first_native_event_index
        and reason.lower() in {"error", "aborted", "cancelled", "canceled", "length"}
    })
    last_agent_end = agent_end_events[-1] if agent_end_events else None
    for event in agent_end_events:
        if event["final_failure_classification"] is not None:
            failure_classifications.append({
                "event_index": event["event_index"],
                "source": "agent_end_final_error_message",
                "classification": event["final_failure_classification"],
                "content_retained": False,
            })
    failure_classifications.sort(key=lambda item: (item["event_index"], item["source"]))
    normal_final_stop = bool(
        last_agent_end
        and last_agent_end["will_continue"] is False
        and last_agent_end["final_assistant_stop_reason"] == "stop"
    )
    terminal_after_native_result = bool(last_agent_end and any(
        attempt["failure_reason"] is None
        and last_agent_end["event_index"] > attempt["tool_result_event_index"]
        for attempt in native_attempts
    ))
    terminal_after_messages = bool(last_agent_end and all(
        index <= last_agent_end["event_index"] for index, _ in messages
    ))
    terminal_success = (normal_final_stop and not post_native_failure_reasons
                        and terminal_after_native_result and terminal_after_messages)

    return {
        "schema_version": SCHEMA_VERSION,
        "raw_stream_sha256": raw_stream_sha,
        "stream_counts": counters,
        "terminal_success": terminal_success,
        "terminal": {
            "agent_end_event_count": len(agent_end_events),
            "terminal_agent_end_event_count": sum(event["will_continue"] is False for event in agent_end_events),
            "last_agent_end_event_index": last_agent_end["event_index"] if last_agent_end else None,
            "last_agent_end_will_continue": last_agent_end["will_continue"] if last_agent_end else None,
            "final_assistant_stop_reason": last_agent_end["final_assistant_stop_reason"] if last_agent_end else None,
            "final_assistant_failure_classification": last_agent_end["final_failure_classification"] if last_agent_end else None,
            "normal_final_stop": normal_final_stop,
            "terminal_after_native_result": terminal_after_native_result,
            "terminal_after_all_messages": terminal_after_messages,
            "post_native_load_failure_stop_reasons": post_native_failure_reasons,
            "requires_agent_end": True,
        },
        "observed_assistant_models": observed,
        "assistant_message_count": len(assistant_selectors) + missing_assistant_identity,
        "assistant_identity_missing_count": missing_assistant_identity,
        "unexpected_assistant_models": unexpected,
        "exact_model_confirmed": exact_model_confirmed,
        "actual_model_evidence": {
            "level": "omp_client_runtime_metadata",
            "basis": "assistant provider/model fields in OMP JSON-mode events",
            "independent_server_attestation": False,
            "not_proven": [
                "provider-side routing identity",
                "account entitlement",
                "absence of server-side fallback before OMP emitted an event",
            ],
        },
        "failure_observability": {
            "json_error_classifications": failure_classifications,
            "stderr_classification": "unknown_not_captured",
            "taxonomy": ["missing_auth", "http_401", "http_403", "rate_limit", "unsupported_model", "unknown"],
            "classification_is_not_provider_attestation": True,
        },
        "tool_call_count": len(tool_call_ledger),
        "tool_calls": tool_call_ledger,
        "native_skill_protocol": {
            "required_uri": f"skill://{expected_skill_name}",
            "proof_standard": "native read call + matching non-error result + marker + fixture resolvedPath",
            "attempt_count": len(native_attempts),
            "attempts": native_attempts,
            "passed": native_passed,
            "failure_reasons": native_failure_reasons,
            "missing_resolved_path_is_evidence_gap_not_model_capability": True,
            "keyword_only_is_insufficient": True,
        },
        "thinking": {
            "block_count": thinking_block_count,
            "character_count": thinking_char_count,
            "content_retained": False,
        },
        "visible_text": "\n\n".join(visible_text),
    }
