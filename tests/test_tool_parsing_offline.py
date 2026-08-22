"""Offline unit tests for tool-call parsing, the streaming ToolSieve,
multimodal message extraction, and asset data-URI parsing.

No network or live server required.
"""

import base64
import json

import pytest

from app.dataplane.reverse.protocol.tool_parser import parse_tool_calls
from app.products.openai._tool_sieve import ToolSieve


TOOLS = ["get_weather", "query_order_status"]


def _xml(*calls: str) -> str:
    items = "".join(
        f"<tool_call><tool_name>{n}</tool_name><parameters>{p}</parameters></tool_call>"
        for n, p in calls
    )
    return f"<tool_calls>{items}</tool_calls>"


# ---------------------------------------------------------------------------
# tool_parser — canonical XML
# ---------------------------------------------------------------------------

class TestXmlParsing:
    def test_canonical_single_call(self):
        text = _xml(("get_weather", '{"city": "Paris"}'))
        r = parse_tool_calls(text, TOOLS)
        assert [c.name for c in r.calls] == ["get_weather"]
        assert json.loads(r.calls[0].arguments) == {"city": "Paris"}
        assert r.unmatched_names == []

    def test_multiple_calls_in_one_block(self):
        text = _xml(
            ("get_weather", '{"city": "Paris"}'),
            ("query_order_status", '{"order_id": "A1"}'),
        )
        r = parse_tool_calls(text, TOOLS)
        assert [c.name for c in r.calls] == ["get_weather", "query_order_status"]

    def test_leading_prose_is_ignored(self):
        text = "I'll check the weather for you.\n" + _xml(("get_weather", '{"city": "Rome"}'))
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1

    def test_markdown_fence_around_xml(self):
        text = "```xml\n" + _xml(("get_weather", "{}")) + "\n```"
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1

    def test_tags_with_attributes(self):
        text = ('<tool_calls id="1"><tool_call seq="a">'
                '<tool_name>get_weather</tool_name>'
                '<parameters>{"city": "Oslo"}</parameters>'
                '</tool_call></tool_calls>')
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1
        assert json.loads(r.calls[0].arguments) == {"city": "Oslo"}

    def test_truncated_block_without_root_close(self):
        text = ('<tool_calls><tool_call><tool_name>get_weather</tool_name>'
                '<parameters>{"city": "Tokio"}</parameters>')
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1
        assert json.loads(r.calls[0].arguments) == {"city": "Tokio"}

    def test_truncated_parameters_json(self):
        text = ('<tool_calls><tool_call><tool_name>get_weather</tool_name>'
                '<parameters>{"city": "Par')
        r = parse_tool_calls(text, TOOLS)
        # Call survives with wrapped raw text — never a silent drop.
        assert len(r.calls) == 1
        args = json.loads(r.calls[0].arguments)
        assert isinstance(args, dict)

    def test_bare_item_without_root(self):
        text = '<tool_call><tool_name>get_weather</tool_name><parameters>{"a": 1}</parameters></tool_call>'
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1


# ---------------------------------------------------------------------------
# tool_parser — JSON envelope / array
# ---------------------------------------------------------------------------

class TestJsonParsing:
    def test_envelope_with_leading_prose(self):
        text = ('Sure, let me look that up.\n'
                '{"tool_calls": [{"name": "get_weather", "input": {"city": "Berlin"}}]}')
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1
        assert json.loads(r.calls[0].arguments) == {"city": "Berlin"}

    def test_envelope_openai_style_nested_function(self):
        text = ('{"tool_calls": [{"id": "call_1", "type": "function", '
                '"function": {"name": "get_weather", "arguments": "{\\"city\\": \\"Nice\\"}"}}]}')
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1
        assert r.calls[0].name == "get_weather"
        assert json.loads(r.calls[0].arguments) == {"city": "Nice"}

    def test_envelope_pretty_printed_newlines_as_whitespace(self):
        text = ('{"tool_calls": [\n'
                '  {"name": "get_weather", "input": {"city": "Lyon"}}\n'
                ']}')
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1

    def test_envelope_raw_newline_inside_string_value(self):
        text = ('{"tool_calls": [{"name": "query_order_status", '
                '"input": {"note": "line1\nline2"}}]}')
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1
        assert json.loads(r.calls[0].arguments)["note"] == "line1\nline2"

    def test_envelope_trailing_comma_repaired(self):
        text = '{"tool_calls": [{"name": "get_weather", "input": {"city": "Kyoto"}},]}'
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1

    def test_bare_json_array(self):
        text = '[{"name": "get_weather", "input": {"city": "Seoul"}}]'
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1


# ---------------------------------------------------------------------------
# tool_parser — normalisation guarantees (anti-phantom contract)
# ---------------------------------------------------------------------------

class TestNormalisation:
    def test_arguments_always_valid_json_object(self):
        cases = [
            _xml(("get_weather", '{"city": "X"}')),
            '{"tool_calls": [{"name": "get_weather", "input": "just a plain string"}]}',
            '{"tool_calls": [{"name": "get_weather", "input": [1, 2]}]}',
        ]
        for text in cases:
            r = parse_tool_calls(text, TOOLS)
            assert r.calls, text
            for call in r.calls:
                parsed = json.loads(call.arguments)  # must never raise
                assert isinstance(parsed, dict)

    def test_plain_string_args_wrapped(self):
        text = '{"tool_calls": [{"name": "get_weather", "input": "not json at all"}]}'
        r = parse_tool_calls(text, TOOLS)
        assert json.loads(r.calls[0].arguments) == {"input": "not json at all"}

    def test_name_matched_case_insensitively(self):
        text = _xml(("GET_WEATHER", "{}"))
        r = parse_tool_calls(text, TOOLS)
        assert r.calls[0].name == "get_weather"
        assert r.unmatched_names == []

    def test_unknown_name_is_kept_not_dropped(self):
        # The phantom-tool-call bug: filtering must never silently empty the
        # call list and leave the client with finish_reason=tool_calls + 0 calls.
        text = _xml(("hallucinated_tool", '{"q": 1}'))
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1
        assert r.calls[0].name == "hallucinated_tool"
        assert r.unmatched_names == ["hallucinated_tool"]

    def test_alt_xml_function_call_format(self):
        text = ('<function_call><name>get_weather</name>'
                '<arguments>{"city": "Vienna"}</arguments></function_call>')
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1

    def test_alt_invoke_format(self):
        text = '<invoke name="get_weather">{"city": "Bern"}</invoke>'
        r = parse_tool_calls(text, TOOLS)
        assert len(r.calls) == 1

    def test_no_false_positive_on_prose(self):
        r = parse_tool_calls("The tool_calls field of the OpenAI API is neat.", TOOLS)
        assert r.calls == []
        assert r.saw_tool_syntax is True  # detected, but nothing parseable


# ---------------------------------------------------------------------------
# ToolSieve — streaming capture
# ---------------------------------------------------------------------------

def _drain(sieve: ToolSieve, chunks: list[str]):
    text_parts: list[str] = []
    calls = None
    for chunk in chunks:
        safe, c = sieve.feed(chunk)
        if safe:
            text_parts.append(safe)
        if c:
            calls = c
            break
    if calls is None:
        flushed_text, flushed_calls = sieve.flush()
        if flushed_text:
            text_parts.append(flushed_text)
        calls = flushed_calls or None
    return "".join(text_parts), calls


class TestToolSieveXml:
    def test_plain_text_passthrough(self):
        sieve = ToolSieve(TOOLS)
        text, calls = _drain(sieve, ["Hello ", "world, no tools here."])
        assert text == "Hello world, no tools here."
        assert not calls

    def test_xml_capture_with_prefix_text(self):
        sieve = ToolSieve(TOOLS)
        block = _xml(("get_weather", '{"city": "Paris"}'))
        text, calls = _drain(sieve, ["Checking now.\n", block])
        assert text == "Checking now.\n"
        assert calls and calls[0].name == "get_weather"

    def test_xml_tag_straddles_chunk_boundary(self):
        sieve = ToolSieve(TOOLS)
        block = _xml(("get_weather", '{"city": "Paris"}'))
        half = len("<tool_ca")
        head, tail = block[:half], block[half:]
        text, calls = _drain(sieve, [head, tail])
        assert not text
        assert calls and calls[0].name == "get_weather"

    def test_char_by_char_streaming(self):
        sieve = ToolSieve(TOOLS)
        block = "Sure! " + _xml(("get_weather", '{"city": "Perth"}'))
        text, calls = _drain(sieve, list(block))
        assert text == "Sure! "
        assert calls and json.loads(calls[0].arguments) == {"city": "Perth"}

    def test_markdown_fence_trimmed_before_xml(self):
        sieve = ToolSieve(TOOLS)
        block = "```xml\n" + _xml(("get_weather", "{}"))
        text, calls = _drain(sieve, [block])
        assert text == ""  # fence swallowed, not leaked
        assert calls

    def test_incomplete_xml_recovered_at_flush(self):
        sieve = ToolSieve(TOOLS)
        text, calls = _drain(sieve, ["<tool_calls><tool_call><tool_name>get_weather"
                                     "</tool_name><parameters>{\"city\": \"Dubai\"}"])
        assert calls and calls[0].name == "get_weather"

    def test_broken_block_does_not_kill_sieve(self):
        sieve = ToolSieve(TOOLS)
        # First block is garbage XML, a valid one follows.
        chunks = [
            "<tool_calls>garbage with no items</tool_calls>",
            "intermediate text ",
            _xml(("get_weather", '{"city": "Nice"}')),
        ]
        text, calls = _drain(sieve, chunks)
        assert "intermediate text" in text
        assert calls and calls[0].name == "get_weather"


class TestToolSieveJson:
    def test_envelope_capture_streamed(self):
        sieve = ToolSieve(TOOLS)
        payload = ('Here you go: {"tool_calls": [{"name": "get_weather", '
                   '"input": {"city": "Madrid"}}]}')
        text, calls = _drain(sieve, [payload])
        assert text == "Here you go: "
        assert calls and calls[0].name == "get_weather"

    def test_envelope_char_by_char_never_leaks(self):
        sieve = ToolSieve(TOOLS)
        payload = ('{"tool_calls": [{"name": "get_weather", "input": '
                   '{"q": "a{b}c"}}]}')
        text, calls = _drain(sieve, list(payload))
        assert text == ""
        assert calls
        assert json.loads(calls[0].arguments) == {"q": "a{b}c"}

    def test_envelope_with_fence_trimmed(self):
        sieve = ToolSieve(TOOLS)
        payload = '```json\n{"tool_calls": [{"name": "get_weather", "input": {}}]}'
        text, calls = _drain(sieve, [payload])
        assert text == ""
        assert calls

    def test_balanced_json_without_calls_released_as_text(self):
        sieve = ToolSieve(TOOLS)
        payload = '{"tool_calls": "mentioned but not a list"}'
        text, calls = _drain(sieve, [payload])
        assert text == payload
        assert not calls

    def test_prose_mentioning_tool_calls_not_captured(self):
        sieve = ToolSieve(TOOLS)
        payload = 'The "tool_calls" array in the API has a special shape.'
        text, calls = _drain(sieve, [payload])
        assert text == payload
        assert not calls

    def test_unterminated_json_envelope_released_as_text(self):
        # Stream cut mid-envelope: no calls, but the buffered text must
        # still reach the client — content never silently vanishes.
        sieve = ToolSieve(TOOLS)
        payload = '{"tool_calls": [{"name": "get_weather", "input": {"city": "Rome"'
        text, calls = _drain(sieve, [payload])
        assert text == payload
        assert not calls

    def test_partial_trigger_tail_released_at_flush(self):
        sieve = ToolSieve(TOOLS)
        text, calls = _drain(sieve, ["answer is 42 <tool_ca"])
        assert text.endswith("<tool_ca")
        assert not calls


# ---------------------------------------------------------------------------
# _extract_message — multimodal input compatibility
# ---------------------------------------------------------------------------

class TestExtractMessage:
    def _extract(self, messages):
        from app.products.openai.chat import _extract_message
        return _extract_message(messages)

    def test_tool_result_with_list_content(self):
        text, files = self._extract([{
            "role": "tool",
            "tool_call_id": "call_1",
            "content": [{"type": "text", "text": "result part 1"},
                        {"type": "text", "text": "result part 2"}],
        }])
        assert "result part 1" in text and "result part 2" in text

    def test_image_url_plain_string_form(self):
        text, files = self._extract([{
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": "https://example.com/a.png"},
            ],
        }])
        assert files == ["https://example.com/a.png"]

    def test_image_url_standard_form(self):
        text, files = self._extract([{
            "role": "user",
            "content": [{"type": "image_url",
                         "image_url": {"url": "https://example.com/b.jpg"}}],
        }])
        assert files == ["https://example.com/b.jpg"]

    def test_input_file_file_data(self):
        text, files = self._extract([{
            "role": "user",
            "content": [
                {"type": "input_file", "file_data": "data:application/pdf;base64,AAA"},
            ],
        }])
        assert files == ["data:application/pdf;base64,AAA"]

    def test_file_block_with_url(self):
        text, files = self._extract([{
            "role": "user",
            "content": [{"type": "file", "file": {"url": "https://x/y.pdf"}}],
        }])
        assert files == ["https://x/y.pdf"]

    def test_assistant_tool_calls_with_list_text_content(self):
        text, files = self._extract([
            {"role": "assistant",
             "content": [{"type": "text", "text": "Let me check."}],
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "get_weather", "arguments": "{}"}}]},
        ])
        assert "Let me check." in text
        assert "<tool_calls>" in text and "get_weather" in text

    def test_dedup_not_applied_at_extract(self):
        # dedupe happens in _prepare_file_attachments; extraction keeps order
        text, files = self._extract([{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://a/1.png"}},
                {"type": "image_url", "image_url": {"url": "https://a/1.png"}},
            ],
        }])
        assert files == ["https://a/1.png", "https://a/1.png"]


# ---------------------------------------------------------------------------
# asset upload — data URI compatibility
# ---------------------------------------------------------------------------

class TestParseDataUri:
    def _parse(self):
        from app.dataplane.reverse.transport.asset_upload import parse_data_uri
        return parse_data_uri

    def test_standard_base64(self):
        parse = self._parse()
        b64 = base64.b64encode(b"hello").decode()
        name, out_b64, mime = parse(f"data:text/plain;base64,{b64}")
        assert base64.b64decode(out_b64) == b"hello"
        assert mime == "text/plain"

    def test_url_encoded_payload(self):
        parse = self._parse()
        name, out_b64, mime = parse("data:text/plain,hello%20world")
        assert base64.b64decode(out_b64) == b"hello world"
        assert mime == "text/plain"

    def test_whitespace_and_missing_padding_repaired(self):
        parse = self._parse()
        b64 = base64.b64encode(b"pad-me!").decode().rstrip("=")
        name, out_b64, mime = parse(f"data:image/png;base64,{b64[:5]} {b64[5:]}")
        assert base64.b64decode(out_b64) == b"pad-me!"

    def test_invalid_base64_rejected(self):
        from app.platform.errors import ValidationError
        parse = self._parse()
        with pytest.raises(ValidationError):
            parse("data:image/png;base64,!!!not-base64!!!")

    def test_missing_comma_rejected(self):
        from app.platform.errors import ValidationError
        parse = self._parse()
        with pytest.raises(ValidationError):
            parse("data:image/png;base64")


class TestSniffMime:
    def test_magic_detection(self):
        from app.dataplane.reverse.transport.asset_upload import _sniff_mime
        assert _sniff_mime(b"\xff\xd8\xff\xe0....") == "image/jpeg"
        assert _sniff_mime(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == "image/png"
        assert _sniff_mime(b"RIFF\x00\x00\x00\x00WEBP") == "image/webp"
        assert _sniff_mime(b"%PDF-1.7") == "application/pdf"
        assert _sniff_mime(b"<html><body>hi</body></html>") == "text/html"
        assert _sniff_mime(b"random bytes") is None
