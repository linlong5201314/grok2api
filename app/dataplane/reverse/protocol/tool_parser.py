"""Tool call parser — extract structured tool calls from model text output.

Tries multiple formats in priority order:
  1. <tool_calls> XML  (canonical format we inject)
  2. JSON envelope {"tool_calls": [...]}
  3. JSON array  [{"name": ..., "input": ...}]
  4. Alternative XML tags (<function_call>, <invoke>)

Returns a list of ParsedToolCall dataclasses.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import unescape


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ParsedToolCall:
    call_id: str
    name: str
    arguments: str          # always a JSON string

    @staticmethod
    def make(name: str, arguments: Any, call_id: str | None = None) -> "ParsedToolCall":
        resolved_call_id = call_id or f"call_{int(time.time() * 1000)}{os.urandom(3).hex()}"
        if isinstance(arguments, str):
            args_str = arguments
        else:
            try:
                args_str = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                args_str = "{}"
        return ParsedToolCall(call_id=resolved_call_id, name=name, arguments=args_str)


@dataclass
class ParseResult:
    calls: list[ParsedToolCall] = field(default_factory=list)
    saw_tool_syntax: bool = False   # detected XML/JSON envelope even if parsing failed


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
        available_tools: If provided, only calls whose name appears in this
                         list are accepted (case-sensitive).
    """
    result = ParseResult()
    if not text or not text.strip():
        return result

    # Fast path: check whether tool-call syntax is present at all
    if not _has_tool_syntax(text):
        return result
    result.saw_tool_syntax = True

    # Try parsers in priority order
    calls = (
        _parse_xml_tool_calls(text)
        or _parse_json_envelope(text)
        or _parse_json_array(text)
        or _parse_json_single_object(text)
        or _parse_alt_xml(text)
    )

    if calls and available_tools:
        calls = [c for c in calls if c.name in available_tools]

    result.calls = calls or []
    return result


# ---------------------------------------------------------------------------
# Syntax detection
# ---------------------------------------------------------------------------

_TOOL_SYNTAX_PATTERNS = re.compile(
    r"<tool_calls|<tool_call|<function_call|<invoke\s|"
    r'"tool_calls"\s*:|\btool_calls\b|'
    r'"tool"\s*:|"name"\s*:|"function"\s*:|DSML|dsml|\uff5c',
    re.IGNORECASE,
)

def _has_tool_syntax(text: str) -> bool:
    return bool(_TOOL_SYNTAX_PATTERNS.search(text))


# ---------------------------------------------------------------------------
# Parser 1: <tool_calls> XML (canonical)
# ---------------------------------------------------------------------------

_XML_ROOT_RE    = re.compile(r"<tool_calls\s*>(.*?)</tool_calls\s*>", re.DOTALL | re.IGNORECASE)
_XML_CALL_RE    = re.compile(r"<tool_call\s*>(.*?)</tool_call\s*>",   re.DOTALL | re.IGNORECASE)
_XML_NAME_RE    = re.compile(r"<tool_name\s*>(.*?)</tool_name\s*>",   re.DOTALL | re.IGNORECASE)
_XML_PARAMS_RE  = re.compile(r"<parameters\s*>(.*?)</parameters\s*>", re.DOTALL | re.IGNORECASE)


def _parse_xml_tool_calls(text: str) -> list[ParsedToolCall]:
    text = _normalize_dsml_to_xml(text)
    root_m = _XML_ROOT_RE.search(text)
    if not root_m:
        return []
    calls: list[ParsedToolCall] = []
    for call_m in _XML_CALL_RE.finditer(root_m.group(1)):
        inner = call_m.group(1)
        name_m   = _XML_NAME_RE.search(inner)
        params_m = _XML_PARAMS_RE.search(inner)
        if not name_m:
            continue
        name   = _xml_unescape(name_m.group(1).strip())
        params = _xml_unescape(params_m.group(1).strip()) if params_m else "{}"
        parsed_args = _parse_json_tolerant(params)
        if parsed_args is None:
            continue
        calls.append(ParsedToolCall.make(name, parsed_args))
    calls.extend(_parse_invoke_calls(root_m.group(1)))
    return calls


# ---------------------------------------------------------------------------
# Parser 2: {"tool_calls": [...]} JSON envelope
# ---------------------------------------------------------------------------

def _parse_json_envelope(text: str) -> list[ParsedToolCall]:
    # Only attempt if the text literally contains "tool_calls" key
    if '"tool_calls"' not in text:
        return []
    for start, end in reversed(_collect_json_pairs(text)):
        if text[start] != "{":
            continue
        try:
            obj = json.loads(text[start:end])
        except (json.JSONDecodeError, ValueError):
            obj = _try_repair_json(text[start:end])
        calls = _extract_from_parsed_value(obj)
        if calls:
            return calls
    return []


# ---------------------------------------------------------------------------
# Parser 3: bare JSON array [{"name":..., "input":...}]
# ---------------------------------------------------------------------------

def _parse_json_array(text: str) -> list[ParsedToolCall]:
    for start, end in reversed(_collect_json_pairs(text)):
        if text[start] != "[":
            continue
        try:
            arr = json.loads(text[start:end])
        except (json.JSONDecodeError, ValueError):
            continue
        calls = _extract_from_parsed_value(arr)
        if calls:
            return calls
    return []


def _parse_json_single_object(text: str) -> list[ParsedToolCall]:
    for start, end in reversed(_collect_json_pairs(text)):
        if text[start] != "{":
            continue
        try:
            obj = json.loads(text[start:end])
        except (json.JSONDecodeError, ValueError):
            continue
        calls = _extract_from_parsed_value(obj)
        if calls:
            return calls
    return []


def _extract_from_call_list(items: list[Any]) -> list[ParsedToolCall]:
    calls: list[ParsedToolCall] = []
    for index, item in enumerate(items):
        call = _extract_single_call(item, index)
        if call is None:
            continue
        calls.append(call)
    return calls


def _extract_from_parsed_value(raw: Any) -> list[ParsedToolCall]:
    if isinstance(raw, dict):
        raw_calls = raw.get("tool_calls")
        if isinstance(raw_calls, list):
            return _extract_from_call_list(raw_calls)
        call = _extract_single_call(raw, 0)
        return [call] if call is not None else []
    if isinstance(raw, list):
        return _extract_from_call_list(raw)
    return []


def _extract_single_call(item: Any, index: int) -> ParsedToolCall | None:
    if not isinstance(item, dict):
        return None

    func = item.get("function")
    if isinstance(func, dict):
        name = str(func.get("name") or "").strip()
        args = _first_present(func, ("arguments", "input", "args", "parameters"))
        if args is None:
            args = {
                key: value
                for key, value in func.items()
                if key != "name"
            }
    else:
        name = str(item.get("name") or item.get("tool_name") or item.get("tool") or "").strip()
        args = _first_present(item, ("input", "arguments", "args", "parameters"))

    if not name:
        return None
    if args is None:
        args = {
            key: value
            for key, value in item.items()
            if key not in {"id", "type", "name", "tool_name", "tool", "function"}
        }

    call_id = item.get("id")
    if not isinstance(call_id, str) or not call_id.strip():
        call_id = f"call_{index + 1:03d}"
    return ParsedToolCall.make(name, args if args is not None else {}, call_id=call_id)


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


def _collect_json_pairs(text: str) -> list[tuple[int, int]]:
    brace_stack: list[int] = []
    bracket_stack: list[int] = []
    pairs: list[tuple[int, int]] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            brace_stack.append(index)
        elif char == "[":
            bracket_stack.append(index)
        elif char == "}":
            if brace_stack:
                pairs.append((brace_stack.pop(), index + 1))
        elif char == "]":
            if bracket_stack:
                pairs.append((bracket_stack.pop(), index + 1))
    return pairs


# ---------------------------------------------------------------------------
# Parser 4: alternative XML tags (<function_call>, <invoke name="...">)
# ---------------------------------------------------------------------------

_FC_RE      = re.compile(r"<function_call\s*>(.*?)</function_call\s*>", re.DOTALL | re.IGNORECASE)
_INVOKE_RE  = re.compile(
    r'<invoke\s+name=["\']?([^"\'>\s]+)["\']?\s*>(.*?)</invoke\s*>',
    re.DOTALL | re.IGNORECASE,
)
_FC_NAME_RE = re.compile(r"<name\s*>(.*?)</name\s*>",                  re.DOTALL | re.IGNORECASE)
_FC_ARGS_RE = re.compile(r"<arguments\s*>(.*?)</arguments\s*>",        re.DOTALL | re.IGNORECASE)
_PARAM_RE   = re.compile(
    r'<parameter\s+name=["\']?([^"\'>\s]+)["\']?\s*>(.*?)</parameter\s*>',
    re.DOTALL | re.IGNORECASE,
)
_DSML_NORMALIZER_PATTERNS = [
    (
        re.compile(r"<\s*/\s*\|\s*DSML\s*\|\s*(tool_calls|invoke|parameter)\b", re.IGNORECASE),
        r"</\1",
    ),
    (
        re.compile(r"<\s*\|/\s*DSML\s*\|\s*(tool_calls|invoke|parameter)\b", re.IGNORECASE),
        r"</\1",
    ),
    (
        re.compile(r"<\s*\|\s*DSML\s*\|\s*(/?)\s*(tool_calls|invoke|parameter)\b", re.IGNORECASE),
        r"<\1\2",
    ),
    (
        re.compile(r"<\s*\uff5c\s*DSML\s*\uff5c\s*(/?)\s*(tool_calls|invoke|parameter)\b", re.IGNORECASE),
        r"<\1\2",
    ),
    (
        re.compile(r"<\s*\uff5c\s*(/?)\s*(tool_calls|invoke|parameter)\s*\uff5c\s*>", re.IGNORECASE),
        r"<\1\2>",
    ),
    (re.compile(r"<\s*\|\s*DSML\s+(/?)(tool_calls|invoke|parameter)\b", re.IGNORECASE), r"<\1\2"),
    (re.compile(r"<\s*(/?)DSML(tool_calls|invoke|parameter)\b", re.IGNORECASE), r"<\1\2"),
    (re.compile(r"<\s*(/?)dsml\s*\|\s*(tool_calls|invoke|parameter)\b", re.IGNORECASE), r"<\1\2"),
]


def _parse_alt_xml(text: str) -> list[ParsedToolCall]:
    text = _normalize_dsml_to_xml(text)
    calls: list[ParsedToolCall] = []

    # <function_call><name>...</name><arguments>...</arguments></function_call>
    for m in _FC_RE.finditer(text):
        inner  = m.group(1)
        name_m = _FC_NAME_RE.search(inner)
        args_m = _FC_ARGS_RE.search(inner)
        if not name_m:
            continue
        name = _xml_unescape(name_m.group(1).strip())
        args = _parse_json_tolerant(_xml_unescape(args_m.group(1).strip()) if args_m else "{}")
        if args is None:
            continue
        calls.append(ParsedToolCall.make(name, args))

    calls.extend(_parse_invoke_calls(text))

    return calls


def _parse_invoke_calls(text: str) -> list[ParsedToolCall]:
    calls: list[ParsedToolCall] = []
    for match in _INVOKE_RE.finditer(text):
        name = _xml_unescape(match.group(1).strip())
        inner = match.group(2)
        params = _parse_xml_parameters(inner)
        if params:
            args = params
        else:
            args = _parse_json_tolerant(inner.strip())
            if args is None:
                args = {}
        calls.append(ParsedToolCall.make(name, args))
    return calls


def _parse_xml_parameters(text: str) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for name, raw_value in _PARAM_RE.findall(text):
        params[_xml_unescape(name)] = _coerce_parameter_value(_xml_unescape(raw_value))
    return params


def _coerce_parameter_value(raw_value: str) -> Any:
    stripped = (raw_value or "").strip()
    if not stripped:
        return ""
    if stripped[0] in '{["' or stripped in {"true", "false", "null"} or _looks_like_number(stripped):
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return stripped
    return stripped


def _looks_like_number(value: str) -> bool:
    return bool(re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", value or ""))


def _normalize_dsml_to_xml(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    if "DSML" not in text and "dsml" not in text and "\uff5c" not in text:
        return text
    normalized = text
    for pattern, replacement in _DSML_NORMALIZER_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    return normalized


def _xml_unescape(value: str) -> str:
    return unescape(value, {"&quot;": '"', "&apos;": "'"})


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _parse_json_tolerant(s: str) -> Any:
    """Try to parse JSON; attempt light repair on failure."""
    if not s:
        return {}
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        repaired = _try_repair_json(s)
        return repaired


def _try_repair_json(s: str) -> Any:
    """Very lightweight JSON repair: fix unescaped newlines inside strings."""
    try:
        # Replace literal newlines inside strings (common model output issue)
        fixed = re.sub(r'(?<!\\)\n', r'\\n', s)
        return json.loads(fixed)
    except (json.JSONDecodeError, ValueError):
        return None
