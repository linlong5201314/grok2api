"""Tool Sieve — streaming tool-call detector and buffer.

Sits between the raw SSE text stream and the response formatter.
Accumulates chunks, detects when the model starts emitting a tool-call
block, buffers the entire block, then parses it once complete.

Two capture formats are supported in streaming:

  1. ``<tool_calls>`` XML        (canonical format injected by tool_prompt)
  2. ``{"tool_calls": [...]}``   (JSON envelope some models fall back to)

Both use prefix-aware boundary splitting so a trigger straddling two
chunks is never emitted to the client as half text. A markdown code
fence (```json / ```xml) directly preceding the block is trimmed instead
of leaking into the visible content.

Usage pattern (streaming path in chat.py):

    sieve = ToolSieve(tool_names)
    async for text_chunk in model_stream:
        safe_text, tool_calls = sieve.feed(text_chunk)
        if safe_text:
            yield make_stream_chunk(safe_text)
        if tool_calls:
            yield make_tool_call_chunk(tool_calls)
            break   # nothing more to send

    # After the stream ends, flush any remaining buffer
    tool_calls = sieve.flush()
    if tool_calls:
        yield make_tool_call_chunk(tool_calls)
"""

from __future__ import annotations

import re

from app.dataplane.reverse.protocol.tool_parser import ParsedToolCall, parse_tool_calls


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

# Prefix match so capture starts even before the tag's `>` arrives.
# Matches both <tool_calls> (root) and <tool_call> (bare item, no root).
_OPEN_TAG_RE = re.compile(r"<tool_calls?(?=[\s>])")
_CLOSE_TAG_RE = re.compile(r"</tool_calls?\s*>", re.IGNORECASE)

# JSON envelope opening, tolerant of whitespace: { "tool_calls" :
_ENVELOPE_OPEN_RE = re.compile(r'\{\s*"tool_calls"\s*:')

# Markdown fence opening directly before a captured block (```json etc.).
_FENCE_TAIL_RE = re.compile(r"(?:\r?\n)?```[^\r\n`]{0,24}[ \t]*(?:\r?\n)?$")

# Give up capturing a JSON envelope beyond this size and flush it as text.
_MAX_JSON_CAPTURE = 64 * 1024

# Triggers that partial-tail holding guards against (see _hold_partial_trigger).
_XML_TRIGGER = "<tool_calls"
_JSON_TRIGGER = '{"tool_calls"'


# ---------------------------------------------------------------------------
# ToolSieve
# ---------------------------------------------------------------------------

class ToolSieve:
    """Stateful per-request sieve.

    Call :meth:`feed` for every text chunk from the model stream.
    Call :meth:`flush` once the stream ends to handle any buffered remainder.
    """

    __slots__ = ("_tool_names", "_buf", "_mode", "_done", "_depth", "_in_str", "_esc")

    _SCAN = 0
    _XML = 1
    _JSON = 2

    def __init__(self, tool_names: list[str]) -> None:
        self._tool_names = tool_names
        self._buf: str = ""
        self._mode: int = ToolSieve._SCAN
        self._done: bool = False          # already emitted tool calls once
        self._depth: int = 0              # JSON capture: brace depth
        self._in_str: bool = False        # JSON capture: inside string literal
        self._esc: bool = False           # JSON capture: previous char was backslash

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def feed(self, chunk: str) -> tuple[str, list[ParsedToolCall] | None]:
        """Process one text chunk.

        Returns:
            (safe_text, tool_calls)
            - safe_text: text safe to forward immediately to the client
            - tool_calls: non-empty list once a complete block parsed into
              calls; None while still accumulating or nothing parsed
        """
        if self._done or not chunk:
            return chunk, None
        if self._mode == ToolSieve._XML:
            return self._feed_xml(chunk)
        if self._mode == ToolSieve._JSON:
            return self._feed_json(chunk)
        return self._feed_scan(chunk)

    def flush(self) -> tuple[str, list[ParsedToolCall] | None]:
        """Call after the stream ends.

        Returns ``(leftover_text, tool_calls)``:
          - leftover_text: buffered text that never resolved into a tool
            block (e.g. an unterminated JSON envelope or a partial-trigger
            tail) — must still be emitted to the client so content never
            silently vanishes.
          - tool_calls: parsed calls (including truncated-but-repaired XML
            blocks), or None when nothing parseable was buffered.
        """
        if self._done:
            return "", None
        self._done = True
        if not self._buf:
            return "", None
        buf = self._buf
        self._buf = ""
        result = parse_tool_calls(buf, self._tool_names)
        if result.calls:
            return "", result.calls
        return buf, None

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _feed_scan(self, chunk: str) -> tuple[str, list[ParsedToolCall] | None]:
        """Not yet capturing — look for an XML or JSON trigger."""
        combined = self._buf + chunk
        self._buf = ""

        xml_m = _OPEN_TAG_RE.search(combined)
        json_m = _ENVELOPE_OPEN_RE.search(combined)

        if xml_m is None and json_m is None:
            # No trigger; forward everything except a partial-trigger tail.
            safe, leftover = _hold_partial_trigger(combined)
            self._buf = leftover
            return safe, None

        # Whichever trigger appears first wins.
        if xml_m is not None and (json_m is None or xml_m.start() <= json_m.start()):
            safe_part = combined[: xml_m.start()]
            self._buf = combined[xml_m.start():]
            self._mode = ToolSieve._XML
            cap_safe, calls = self._feed_xml("")
            return _trim_fence(safe_part) + cap_safe, calls

        safe_part = combined[: json_m.start()]
        pending   = combined[json_m.start():]
        self._buf = ""
        self._mode = ToolSieve._JSON
        self._depth = 0
        self._in_str = False
        self._esc = False
        cap_safe, calls = self._feed_json(pending)
        return _trim_fence(safe_part) + cap_safe, calls

    # ------------------------------------------------------------------
    # XML capture
    # ------------------------------------------------------------------

    def _feed_xml(self, chunk: str) -> tuple[str, list[ParsedToolCall] | None]:
        """In XML capture mode — accumulate until the closing tag."""
        self._buf += chunk

        close_m = _CLOSE_TAG_RE.search(self._buf)
        if close_m is None:
            # Not complete yet — keep buffering, emit nothing.
            return "", None

        xml_block = self._buf[: close_m.end()]
        remainder = self._buf[close_m.end():]

        result = parse_tool_calls(xml_block, self._tool_names)
        if result.calls:
            self._buf = ""
            self._mode = ToolSieve._SCAN
            self._done = True
            return "", result.calls

        # Looked like tool syntax but parsed to nothing — drop the block,
        # keep scanning the remainder for a subsequent valid block.
        self._buf = remainder
        self._mode = ToolSieve._SCAN
        safe, calls = self._feed_scan("")
        return safe, calls

    # ------------------------------------------------------------------
    # JSON envelope capture
    # ------------------------------------------------------------------

    def _feed_json(self, chunk: str) -> tuple[str, list[ParsedToolCall] | None]:
        """In JSON capture mode — accumulate with brace balancing."""
        self._buf += chunk

        end_idx = self._scan_balance(chunk)
        if end_idx == -2:
            # Oversized or structurally broken — give up, flush as text.
            text = self._buf
            self._buf = ""
            self._mode = ToolSieve._SCAN
            safe, calls = self._feed_scan("")
            return text + safe, calls
        if end_idx == -1:
            return "", None  # still accumulating

        obj = self._buf[: end_idx + 1]
        remainder = self._buf[end_idx + 1:]

        calls = parse_tool_calls(obj, self._tool_names).calls
        if calls:
            self._buf = ""
            self._mode = ToolSieve._SCAN
            self._done = True
            return "", calls

        # Balanced JSON but not a usable envelope — release as text and
        # keep scanning the remainder.
        self._buf = remainder
        self._mode = ToolSieve._SCAN
        safe, more_calls = self._feed_scan("")
        return obj + safe, more_calls

    def _scan_balance(self, chunk: str) -> int:
        """Consume *chunk* (already appended to buf) with the string/brace
        state machine. Returns:
          >=0  index in buf where depth returned to zero (object complete)
          -1   still accumulating
          -2   broken (unbalanced close) or over the size cap
        """
        start = len(self._buf) - len(chunk)
        for i in range(start, len(self._buf)):
            ch = self._buf[i]
            if self._in_str:
                if self._esc:
                    self._esc = False
                elif ch == "\\":
                    self._esc = True
                elif ch == '"':
                    self._in_str = False
                continue
            if ch == '"':
                self._in_str = True
            elif ch == "{" or ch == "[":
                self._depth += 1
            elif ch == "}" or ch == "]":
                self._depth -= 1
                if self._depth <= 0:
                    if self._depth == 0:
                        return i
                    return -2
        if len(self._buf) > _MAX_JSON_CAPTURE:
            return -2
        return -1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hold_partial_trigger(text: str) -> tuple[str, str]:
    """Split text so a trailing fragment that may still grow into a capture
    trigger stays in the leftover buffer for the next chunk.

    A fragment qualifies when it starts at the last ``<`` / ``{`` and is a
    prefix of the trigger (``<tool_calls`` / ``{"tool_calls"``), INCLUDING
    the full trigger itself — after ``<tool_calls`` arrives, one more char
    is still needed to decide whether it opens a tag (``>``/space) or is
    plain text like ``<tool_callsxyz``. Without this, a tag name straddling
    a chunk boundary leaks half-open syntax to the client.
    """
    best_pos = -1
    lt = text.rfind("<")
    if lt != -1 and _XML_TRIGGER.startswith(text[lt:]):
        best_pos = lt
    brace = text.rfind("{")
    if brace != -1 and _JSON_TRIGGER.startswith(text[brace:]) and brace > best_pos:
        best_pos = brace
    if best_pos != -1:
        return text[:best_pos], text[best_pos:]
    return text, ""


def _trim_fence(text: str) -> str:
    """Drop a trailing markdown fence opening (```json) so it never leaks
    into visible content right before a captured tool block."""
    if not text or "`" not in text:
        return text
    return _FENCE_TAIL_RE.sub("", text)
