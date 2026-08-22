"""Tool call parser — extract structured tool calls from model text output.

Tries multiple formats in priority order:
  1. <tool_calls> XML  (canonical format we inject)
  2. JSON envelope {"tool_calls": [...]}
  3. JSON array  [{"name": ..., "input": ...}]
  4. Alternative XML tags (<function_call>, <invoke>)

Robustness contract (never silently drop an explicit tool call):
  - Tags may carry attributes: <tool_calls foo="1"> parses.
  - Truncated blocks (missing closing tags, cut-off parameters JSON) are
    repaired best-effort so a stream cut still yields the call.
  - Arguments are ALWAYS normalized to a valid JSON object string; raw
    non-JSON strings are wrapped as {"input": "..."} instead of leaking
    invalid JSON to clients.
  - When *available_tools* is given, names match case-insensitively and
    unknown names are passed through (kept) rather than dropped, so the
    client agent loop can react instead of receiving a phantom
    finish_reason="tool_calls" with zero calls.

Returns a list of ParsedToolCall dataclasses.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ParsedToolCall:
    call_id: str
    name: str
    arguments: str          # always a valid JSON object string

    @staticmethod
    def make(name: str, arguments: Any) -> "ParsedToolCall":
        call_id = f"call_{int(time.time() * 1000)}{os.urandom(3).hex()}"
        return ParsedToolCall(
            call_id=call_id,
            name=_clean_name(name),
            arguments=_normalize_arguments(arguments),
        )


@dataclass
class ParseResult:
    calls: list[ParsedToolCall] = field(default_factory=list)
    saw_tool_syntax: bool = False   # detected XML/JSON envelope even if parsing failed
    unmatched_names: list[str] = field(default_factory=list)  # calls kept despite name mismatch


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_tool_calls(
    text: str,
    available_tools: list[str] | None = None,
) -> ParseResult:
    """Parse tool calls from model-generated text.

    Args:
        text: Full or partial model output text.
        available_tools: If provided, calls whose name matches (case-insensitive)
                         are canonicalised to the declared spelling. Unknown
                         names are kept as-is instead of dropped.
    """
    result = ParseResult()
    if not text or not text.strip():
        return result

    # Fast path: check whether tool-call syntax is present at all
    if not _has_tool_syntax(text):
        # Strict fallback: with a declared tool list, a bare JSON array whose
        # items ALL reference declared tools is accepted as a tool block.
        if available_tools:
            calls = _parse_json_array_strict(text, available_tools)
            if calls:
                result.saw_tool_syntax = True
                result.calls = calls
        return result
    result.saw_tool_syntax = True

    # Try parsers in priority order
    calls = (
        _parse_xml_tool_calls(text)
        or _parse_json_envelope(text)
        or _parse_json_array(text)
        or _parse_alt_xml(text)
    )

    if calls and available_tools:
        calls, unmatched = _match_tool_names(calls, available_tools)
        result.unmatched_names = unmatched

    result.calls = calls or []
    return result


# ---------------------------------------------------------------------------
# Syntax detection
# ---------------------------------------------------------------------------

_TOOL_SYNTAX_PATTERNS = re.compile(
    r"<tool_calls|<tool_call(?=[\s>])|<function_call|<invoke\s|"
    r'"tool_calls"\s*:|\btool_calls\b',
    re.IGNORECASE,
)

def _has_tool_syntax(text: str) -> bool:
    return bool(_TOOL_SYNTAX_PATTERNS.search(text))


# ---------------------------------------------------------------------------
# Name / argument normalisation
# ---------------------------------------------------------------------------

def _clean_name(name: Any) -> str:
    """Collapse whitespace inside tool names (regex captures may span lines)."""
    return re.sub(r"\s+", " ", str(name or "")).strip()


def _normalize_arguments(args: Any) -> str:
    """Guarantee a valid JSON *object* string.

    - dict → compact dump
    - str  → parse as JSON if possible (object kept, other types wrapped);
             non-JSON strings wrapped as {"input": "..."}
    - list / other → wrapped as {"input": <value>}
    """
    if args is None:
        return "{}"
    if isinstance(args, dict):
        return _compact_json(args)
    if isinstance(args, str):
        stripped = args.strip()
        if not stripped:
            return "{}"
        if stripped.startswith("{") or stripped.startswith("["):
            parsed = _parse_json_tolerant(stripped)
            if isinstance(parsed, dict):
                return _compact_json(parsed)
            if parsed is not None:
                return _compact_json({"input": parsed})
        # Plain string (or broken JSON) — keep the raw text visible to the client.
        return _compact_json({"input": stripped})
    try:
        return _compact_json({"input": args})
    except (TypeError, ValueError):
        return "{}"


def _compact_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


def _match_tool_names(
    calls: list[ParsedToolCall],
    available: list[str],
) -> tuple[list[ParsedToolCall], list[str]]:
    """Canonicalise names case-insensitively; keep unknown names as-is."""
    lower_map = {n.lower(): n for n in available if n}
    matched: list[ParsedToolCall] = []
    unmatched: list[str] = []
    for call in calls:
        canonical = lower_map.get(call.name.lower())
        if canonical:
            call.name = canonical
        else:
            unmatched.append(call.name)
        matched.append(call)
    return matched, unmatched


# ---------------------------------------------------------------------------
# Parser 1: <tool_calls> XML (canonical)
# ---------------------------------------------------------------------------

# [^>]* tolerates attributes on any tag; the call/item regexes only run inside
# root content so <tool_call...> can never accidentally swallow <tool_calls>.
_XML_ROOT_RE   = re.compile(r"<tool_calls[^>]*>(.*?)</tool_calls\s*>", re.DOTALL | re.IGNORECASE)
_XML_CALL_RE   = re.compile(r"<tool_call[^>]*>(.*?)(?:</tool_call\s*>|$)", re.DOTALL | re.IGNORECASE)
_XML_NAME_RE   = re.compile(r"<tool_name[^>]*>(.*?)(?:</tool_name\s*>|$)", re.DOTALL | re.IGNORECASE)
_XML_PARAMS_RE = re.compile(r"<parameters[^>]*>(.*?)(?:</parameters\s*>|$)", re.DOTALL | re.IGNORECASE)


def _parse_xml_tool_calls(text: str) -> list[ParsedToolCall]:
    calls: list[ParsedToolCall] = []
    # Every root block (models occasionally emit two separate blocks).
    for root_m in _XML_ROOT_RE.finditer(text):
        calls.extend(_parse_xml_call_items(root_m.group(1)))
    if calls:
        return calls
    # Truncated output: opening tag present but the closing </tool_calls>
    # never arrived, or the model omitted the root wrapper entirely.
    # Recover every <tool_call> item from the raw text.
    if re.search(r"<tool_calls?[^>]*>", text, re.IGNORECASE):
        for call_m in _XML_CALL_RE.finditer(text):
            calls.extend(_parse_xml_call_items(call_m.group(0)))
        if calls:
            return calls
    return []


def _parse_xml_call_items(inner_text: str) -> list[ParsedToolCall]:
    calls: list[ParsedToolCall] = []
    for call_m in _XML_CALL_RE.finditer(inner_text):
        inner = call_m.group(1)
        name_m = _XML_NAME_RE.search(inner)
        if not name_m or not _clean_name(name_m.group(1)):
            continue
        name   = name_m.group(1)
        params_m = _XML_PARAMS_RE.search(inner)
        params = params_m.group(1).strip() if params_m else ""
        parsed_args = _parse_json_tolerant(params) if params else {}
        if parsed_args is None:
            # Parameters JSON is broken/truncated — keep the call with the
            # raw text wrapped so the client tool executor can report it.
            parsed_args = {"input": params}
        calls.append(ParsedToolCall.make(name, parsed_args))
    return calls


# ---------------------------------------------------------------------------
# Parser 2: {"tool_calls": [...]} JSON envelope
# ---------------------------------------------------------------------------

# Envelope openings, tolerant of whitespace: { "tool_calls" :
_ENVELOPE_START_RE = re.compile(r'\{\s*"tool_calls"\s*:')


def _parse_json_envelope(text: str) -> list[ParsedToolCall]:
    if '"tool_calls"' not in text:
        return []
    # Prefer decoding from each envelope-looking "{" so leading prose with
    # stray braces cannot shadow the real envelope.
    starts = [m.start() for m in _ENVELOPE_START_RE.finditer(text)]
    for start in starts:
        obj = _raw_decode_at(text, start)
        if isinstance(obj, dict):
            raw_calls = obj.get("tool_calls")
            if isinstance(raw_calls, list):
                calls = _extract_from_call_list(raw_calls)
                if calls:
                    return calls
    # Fallback: first top-level object anywhere in the text.
    obj = _extract_outermost_json_obj(text)
    if isinstance(obj, dict):
        raw_calls = obj.get("tool_calls")
        if isinstance(raw_calls, list):
            return _extract_from_call_list(raw_calls)
    return []


_JSON_DECODER = json.JSONDecoder()


def _raw_decode_at(text: str, start: int) -> Any:
    try:
        obj, _ = _JSON_DECODER.raw_decode(text, start)
        return obj
    except (json.JSONDecodeError, ValueError):
        end = text.find("}", start)
        end = text.rfind("}") + 1 if end == -1 else end + 1
        return _try_repair_json(text[start:end]) if end > start else None


def _extract_outermost_json_obj(text: str) -> Any:
    """Find and parse the first top-level JSON object in *text*."""
    start = text.find("{")
    if start == -1:
        return None
    try:
        obj, _ = _JSON_DECODER.raw_decode(text, start)
        return obj
    except (json.JSONDecodeError, ValueError):
        end = text.rfind("}") + 1
        return _try_repair_json(text[start:end]) if end > start else None


# ---------------------------------------------------------------------------
# Parser 3: bare JSON array [{"name":..., "input":...}]
# ---------------------------------------------------------------------------

_JSON_ARR_RE = re.compile(r"\[[\s\S]+\]", re.DOTALL)

def _parse_json_array(text: str) -> list[ParsedToolCall]:
    m = _JSON_ARR_RE.search(text)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        arr = _try_repair_json(m.group(0))
        if arr is None:
            return []
    if not isinstance(arr, list):
        return []
    return _extract_from_call_list(arr)


def _parse_json_array_strict(
    text: str,
    available_tools: list[str],
) -> list[ParsedToolCall]:
    """Bare-array fallback used only when *available_tools* is declared.

    Accepts the text only if it parses to a non-empty JSON array whose items
    are ALL objects naming declared tools — keeps false positives at zero
    while recovering models that skip the envelope."""
    stripped = text.strip()
    if not stripped.startswith("["):
        return []
    try:
        arr = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(arr, list) or not arr:
        return []
    lower_map = {n.lower(): n for n in available_tools if n}
    calls: list[ParsedToolCall] = []
    for item in arr:
        if not isinstance(item, dict):
            return []
        name = (item.get("name") or item.get("tool_name") or "").strip()
        canonical = lower_map.get(name.lower())
        if canonical is None:
            return []
        item["name"] = canonical
        calls.extend(_extract_from_call_list([item]))
    return calls


def _extract_from_call_list(items: list[Any]) -> list[ParsedToolCall]:
    calls: list[ParsedToolCall] = []
    for item in items:
        if not isinstance(item, dict):
            # OpenAI-style envelope items: {"id":..., "type":"function", "function":{...}}
            continue
        if "function" in item and isinstance(item["function"], dict):
            fn = item["function"]
            name = fn.get("name") or item.get("name") or item.get("tool_name") or ""
            args = fn.get("arguments", item.get("input") or item.get("arguments") or item.get("parameters") or {})
            if name:
                calls.append(ParsedToolCall.make(name, args))
            continue
        name = (item.get("name") or item.get("tool_name") or "").strip()
        args = item.get("input") or item.get("arguments") or item.get("parameters") or {}
        if not name:
            continue
        calls.append(ParsedToolCall.make(name, args))
    return calls


# ---------------------------------------------------------------------------
# Parser 4: alternative XML tags (<function_call>, <invoke name="...">)
# ---------------------------------------------------------------------------

_FC_RE      = re.compile(r"<function_call[^>]*>(.*?)</function_call\s*>", re.DOTALL | re.IGNORECASE)
_INVOKE_RE  = re.compile(r'<invoke\s+name=["\']?([\w.-]+)["\']?[^>]*>(.*?)(?:</invoke\s*>|$)', re.DOTALL | re.IGNORECASE)
_FC_NAME_RE = re.compile(r"<name[^>]*>(.*?)</name\s*>",           re.DOTALL | re.IGNORECASE)
_FC_ARGS_RE = re.compile(r"<arguments[^>]*>(.*?)(?:</arguments\s*>|$)", re.DOTALL | re.IGNORECASE)


def _parse_alt_xml(text: str) -> list[ParsedToolCall]:
    calls: list[ParsedToolCall] = []

    # <function_call><name>...</name><arguments>...</arguments></function_call>
    for m in _FC_RE.finditer(text):
        inner  = m.group(1)
        name_m = _FC_NAME_RE.search(inner)
        args_m = _FC_ARGS_RE.search(inner)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        args = _parse_json_tolerant(args_m.group(1).strip() if args_m else "{}")
        if args is None:
            args = {"input": args_m.group(1).strip()} if args_m else {}
        calls.append(ParsedToolCall.make(name, args))

    # <invoke name="tool_name">...</invoke>
    for m in _INVOKE_RE.finditer(text):
        name  = m.group(1).strip()
        inner = m.group(2)
        args  = _parse_json_tolerant(inner.strip())
        if args is None:
            args = {}
        calls.append(ParsedToolCall.make(name, args))

    return calls


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _parse_json_tolerant(s: str) -> Any:
    """Try to parse JSON; attempt light repair on failure. None if hopeless."""
    if not s:
        return {}
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return _try_repair_json(s)


def _try_repair_json(s: str) -> Any:
    """Lightweight JSON repair for common model-output defects:
      - raw control characters (newline/tab) inside string literals
      - trailing commas before } / ]
    """
    if not s:
        return None
    fixed = _escape_in_string_control_chars(s)
    for candidate in (fixed, _strip_trailing_commas(fixed)):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _escape_in_string_control_chars(s: str) -> str:
    """Escape raw control chars, but only inside string literals so pretty
    printed JSON (newlines as inter-token whitespace) stays untouched."""
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in s:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            elif ch < " ":
                out.append(_CONTROL_ESCAPES.get(ch) or f"\\u{ord(ch):04x}")
                continue
        elif ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


_CONTROL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


def _strip_trailing_commas(s: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", s)


__all__ = ["ParsedToolCall", "ParseResult", "parse_tool_calls"]
