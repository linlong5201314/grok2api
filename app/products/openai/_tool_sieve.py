"""Tool Sieve — streaming tool-call detector and buffer.

Sits between the raw SSE text stream and the response formatter.
Accumulates chunks, detects when the model starts emitting a <tool_calls>
XML block, buffers the entire block, then parses it once complete.

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
    safe_text, tool_calls = sieve.flush()
    if safe_text:
        yield make_stream_chunk(safe_text)
    if tool_calls:
        yield make_tool_call_chunk(tool_calls)
"""

from __future__ import annotations

import re

from app.dataplane.reverse.protocol.tool_parser import ParsedToolCall, parse_tool_calls


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

# We start buffering as soon as we see the opening of a tool-call tag.
# Using a prefix match so we catch it even before the `>` arrives.
_OPEN_TAG_RE = re.compile(
    r"<\s*(?:"
    r"tool_calls\b|function_call\b|invoke\b|"
    r"\|\s*DSML\s*\|\s*(?:tool_calls|invoke)\b|"
    r"\uff5c\s*(?:DSML\s*\uff5c\s*)?(?:tool_calls|invoke)\s*\uff5c|"
    r"DSML(?:tool_calls|invoke)\b|"
    r"dsml\s*\|\s*(?:tool_calls|invoke)\b"
    r")",
    re.IGNORECASE,
)
_TOOL_CALLS_OPEN_RE = re.compile(
    r"^<\s*(?:"
    r"tool_calls\b|"
    r"\|\s*DSML\s*\|\s*tool_calls\b|"
    r"\uff5c\s*(?:DSML\s*\uff5c\s*)?tool_calls\s*\uff5c|"
    r"DSMLtool_calls\b|"
    r"dsml\s*\|\s*tool_calls\b"
    r")",
    re.IGNORECASE,
)
_TOOL_CALLS_CLOSE_RE = re.compile(
    r"</\s*tool_calls\s*>|"
    r"<\s*/\s*\|\s*DSML\s*\|\s*tool_calls\b[^>]*>|"
    r"<\s*\|/\s*DSML\s*\|\s*tool_calls\b[^>]*>|"
    r"<\s*\|\s*DSML\s*\|\s*/\s*tool_calls\b[^>]*>|"
    r"<\s*\uff5c\s*/\s*tool_calls\s*\uff5c\s*>|"
    r"<\s*/\s*DSMLtool_calls\b[^>]*>",
    re.IGNORECASE,
)
_FUNCTION_CALL_CLOSE_RE = re.compile(r"</\s*function_call\s*>", re.IGNORECASE)
_INVOKE_CLOSE_RE = re.compile(
    r"</\s*invoke\s*>|"
    r"<\s*/\s*\|\s*DSML\s*\|\s*invoke\b[^>]*>|"
    r"<\s*\|/\s*DSML\s*\|\s*invoke\b[^>]*>|"
    r"<\s*\|\s*DSML\s*\|\s*/\s*invoke\b[^>]*>|"
    r"<\s*\uff5c\s*/\s*invoke\s*\uff5c\s*>|"
    r"<\s*/\s*DSMLinvoke\b[^>]*>",
    re.IGNORECASE,
)
_BOUNDARY_PREFIXES = (
    "<tool_calls",
    "<function_call",
    "<invoke",
    "<|DSML",
    "<｜",
    "<DSML",
    "<dsml|",
)


# ---------------------------------------------------------------------------
# ToolSieve
# ---------------------------------------------------------------------------

class ToolSieve:
    """Stateful per-request sieve.

    Call :meth:`feed` for every text chunk from the model stream.
    Call :meth:`flush` once the stream ends to handle any buffered remainder.
    """

    __slots__ = ("_tool_names", "_buf", "_capturing", "_done")

    def __init__(self, tool_names: list[str]) -> None:
        self._tool_names = tool_names
        self._buf: str = ""
        self._capturing: bool = False
        self._done: bool = False          # already emitted tool calls once

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def feed(self, chunk: str) -> tuple[str, list[ParsedToolCall] | None]:
        """Process one text chunk.

        Returns:
            (safe_text, tool_calls)
            - safe_text: text safe to forward immediately to the client
            - tool_calls: non-None (possibly empty list) once a complete
              XML block has been parsed; None while still accumulating
        """
        if self._done or not chunk:
            return chunk if not self._capturing else "", None

        if self._capturing:
            return self._feed_capturing(chunk)
        else:
            return self._feed_scanning(chunk)

    def flush(self) -> tuple[str, list[ParsedToolCall] | None]:
        """Call after the stream ends.  Attempts to parse anything remaining
        in the buffer.  Returns any unparsed buffered text plus parsed calls."""
        if self._done or not self._buf:
            return "", None
        buffered = self._buf
        result = parse_tool_calls(self._buf, self._tool_names)
        self._buf = ""
        self._capturing = False
        if result.calls:
            self._done = True
            return "", result.calls
        return buffered, None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _feed_scanning(self, chunk: str) -> tuple[str, list[ParsedToolCall] | None]:
        """Not yet in capture mode — look for the opening tag."""
        combined = self._buf + chunk
        self._buf = ""

        m = _OPEN_TAG_RE.search(combined)
        if m is None:
            # No opening tag; safe to forward.  Keep the last few chars in
            # the buffer in case the tag straddles a chunk boundary.
            safe, leftover = _split_at_any_boundary(combined, _BOUNDARY_PREFIXES)
            self._buf = leftover
            return safe, None

        # Opening tag found → emit everything before it, start capturing.
        # Then immediately attempt to consume the rest of this chunk as the
        # capture phase (the closing tag may already be present).
        safe_part = combined[: m.start()]
        self._buf = combined[m.start():]
        self._capturing = True
        cap_safe, calls = self._feed_capturing("")
        return safe_part + cap_safe, calls

    def _feed_capturing(self, chunk: str) -> tuple[str, list[ParsedToolCall] | None]:
        """In capture mode — accumulate until closing tag."""
        self._buf += chunk

        close_m = _close_match(self._buf)
        if close_m is None:
            # Not complete yet — keep buffering, emit nothing
            return "", None

        # Complete block found
        xml_block = self._buf[: close_m.end()]
        trailing_text = self._buf[close_m.end():]
        self._buf = ""
        self._capturing = False

        result = parse_tool_calls(xml_block, self._tool_names)
        if result.calls:
            self._done = True
            return "", result.calls
        trailing_safe, trailing_calls = self._feed_scanning(trailing_text)
        return xml_block + trailing_safe, trailing_calls


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _split_at_any_boundary(text: str, prefixes: tuple[str, ...]) -> tuple[str, str]:
    """Split text so that any partial match of *prefix* at the end stays in
    the leftover buffer (to be checked again on the next chunk)."""
    lower_text = text.lower()
    lowered_prefixes = tuple(prefix.lower() for prefix in prefixes)
    max_len = min(max(len(prefix) for prefix in lowered_prefixes) - 1, len(text))
    for i in range(max_len, 0, -1):
        suffix = lower_text[-i:]
        if any(prefix.startswith(suffix) for prefix in lowered_prefixes):
            return text[: -i], text[-i:]
    return text, ""


def _close_match(buffer: str) -> re.Match[str] | None:
    if _TOOL_CALLS_OPEN_RE.match(buffer):
        return _TOOL_CALLS_CLOSE_RE.search(buffer)
    if re.match(r"<\s*function_call\b", buffer, re.IGNORECASE):
        return _FUNCTION_CALL_CLOSE_RE.search(buffer)
    return _INVOKE_CLOSE_RE.search(buffer)
