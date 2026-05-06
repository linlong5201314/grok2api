import importlib.util
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.dataplane.reverse.protocol.tool_parser import ParsedToolCall, parse_tool_calls
from app.dataplane.reverse.protocol.tool_prompt import extract_tool_names, tool_calls_to_xml
from app.dataplane.reverse.protocol.tool_request import (
    allowed_tool_names,
    normalize_openai_tool_choice,
    normalize_openai_tools,
    normalize_parsed_tool_calls,
    tool_choice_allows_calls,
    validate_strict_tools,
    validate_tool_choice_tools,
)
from app.platform.errors import ValidationError


def load_tool_sieve():
    module_path = Path(__file__).resolve().parents[1] / "app" / "products" / "openai" / "_tool_sieve.py"
    spec = importlib.util.spec_from_file_location("grok2api_tool_sieve_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.ToolSieve


def load_openai_schemas():
    module_path = Path(__file__).resolve().parents[1] / "app" / "products" / "openai" / "schemas.py"
    spec = importlib.util.spec_from_file_location("grok2api_openai_schemas_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_config_env_override_supports_nested_sections():
    from app.platform.config.loader import _apply_env

    data = {
        "proxy": {
            "egress": {"mode": "direct", "proxy_url": ""},
            "clearance": {"mode": "none", "flaresolverr_url": ""},
        },
        "account": {"refresh": {"basic_interval_sec": 1}},
    }

    old_env = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update({
            "GROK_PROXY_EGRESS_MODE": "single_proxy",
            "GROK_PROXY_EGRESS_PROXY_URL": "socks5://127.0.0.1:1080",
            "GROK_PROXY_CLEARANCE_FLARESOLVERR_URL": "http://flaresolverr:8191",
            "GROK_ACCOUNT_REFRESH_BASIC_INTERVAL_SEC": "86400",
        })
        result = _apply_env(data)
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    assert result["proxy"]["egress"]["mode"] == "single_proxy"
    assert result["proxy"]["egress"]["proxy_url"] == "socks5://127.0.0.1:1080"
    assert result["proxy"]["clearance"]["flaresolverr_url"] == "http://flaresolverr:8191"
    assert result["account"]["refresh"]["basic_interval_sec"] == "86400"


def test_sampling_model_config_preserves_zero_values():
    pytest.importorskip("loguru")
    from app.products.openai.chat import _sampling_model_config

    assert _sampling_model_config(0, 0) == {
        "temperature": 0,
        "topP": 0,
        "top_p": 0,
    }


def test_chat_schema_accepts_malformed_tool_items_for_normalization():
    schemas = load_openai_schemas()
    req = schemas.ChatCompletionRequest.model_validate({
        "model": "grok-test",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [None, {"type": "function", "function": {"name": "search"}}],
        "functions": [None, {"name": "legacy"}],
    })

    assert req.tools[0] is None
    assert req.functions[1]["name"] == "legacy"


def test_anthropic_convert_tools_skips_entries_without_name():
    pytest.importorskip("loguru")
    from app.products.anthropic.messages import _convert_tools

    result = _convert_tools([
        None,
        {"description": "no name"},
        {"name": "  "},
        {"name": "search", "input_schema": {"type": "object"}},
    ])

    assert len(result) == 1
    assert result[0]["function"]["name"] == "search"


def test_anthropic_tool_result_coerces_dict_content_to_json():
    pytest.importorskip("loguru")
    from app.products.anthropic.messages import _anthropic_content_to_internal

    messages = _anthropic_content_to_internal(
        [{
            "type":         "tool_result",
            "tool_use_id":  "call_1",
            "content":      {"ok": True, "rows": 3},
        }],
        role="user",
    )

    assert len(messages) == 1
    assert messages[0]["role"] == "tool"
    assert json.loads(messages[0]["content"]) == {"ok": True, "rows": 3}


def test_responses_function_call_output_coerces_non_string():
    pytest.importorskip("loguru")
    from app.products.openai.responses import _parse_input

    messages = _parse_input([
        {
            "type":    "function_call_output",
            "call_id": "call_1",
            "output":  {"status": "ok", "rows": [1, 2]},
        },
    ])

    assert len(messages) == 1
    assert messages[0]["role"] == "tool"
    assert json.loads(messages[0]["content"]) == {"status": "ok", "rows": [1, 2]}


def test_responses_stream_reasoning_only_closes_reasoning_without_empty_message(monkeypatch):
    pytest.importorskip("loguru")
    from app.control.model.enums import ModeId
    from app.dataplane import account as account_module
    from app.products.openai import responses

    class FakeConfig:
        def get_int(self, *_args, **_kwargs):
            return 0

        def get_float(self, *_args, **_kwargs):
            return 1.0

        def get(self, *_args, **_kwargs):
            return None

    class FakeDirectory:
        async def release(self, _acct):
            return None

        async def feedback(self, *_args, **_kwargs):
            return None

    class FakeStreamAdapter:
        image_urls = []

        def references_suffix(self):
            return ""

        def feed(self, data):
            if data == "thinking":
                return [SimpleNamespace(kind="thinking", content="hidden reasoning")]
            return []

    async def fake_reserve(*_args, **_kwargs):
        return SimpleNamespace(token="tok"), int(ModeId.FAST)

    async def fake_stream_chat(*_args, **_kwargs):
        yield "data: thinking"
        yield "data: [DONE]"

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(account_module, "_directory", FakeDirectory())
    monkeypatch.setattr(responses, "get_config", lambda: FakeConfig())
    monkeypatch.setattr(responses, "StreamAdapter", FakeStreamAdapter)
    monkeypatch.setattr(responses, "_reserve_chat_account", fake_reserve)
    monkeypatch.setattr(responses, "_stream_chat", fake_stream_chat)
    monkeypatch.setattr(responses, "_quota_sync", noop_async)
    monkeypatch.setattr(responses, "_fail_sync", noop_async)

    async def collect():
        stream = await responses.create(
            model="grok-test",
            input_val="hello",
            instructions=None,
            stream=True,
            emit_think=True,
            temperature=0.8,
            top_p=0.95,
        )
        return [chunk async for chunk in stream]

    chunks = asyncio.run(collect())
    event_names = [
        line[len("event: "):]
        for chunk in chunks
        for line in chunk.splitlines()
        if line.startswith("event: ")
    ]
    data_payloads = [
        json.loads(line[len("data: "):])
        for chunk in chunks
        for line in chunk.splitlines()
        if line.startswith("data: {")
    ]
    completed = next(payload for payload in data_payloads if payload.get("type") == "response.completed")
    output = completed["response"]["output"]

    assert "response.reasoning_summary_text.done" in event_names
    assert "response.reasoning_summary_part.done" in event_names
    assert any(
        payload.get("type") == "response.output_item.done"
        and payload.get("item", {}).get("type") == "reasoning"
        for payload in data_payloads
    )
    assert [item["type"] for item in output] == ["reasoning"]
    assert output[0]["summary"][0]["text"] == "hidden reasoning"


def test_chat_schema_accepts_malformed_message_content_and_tool_calls():
    schemas = load_openai_schemas()
    req = schemas.ChatCompletionRequest.model_validate({
        "model": "grok-test",
        "messages": [
            {
                "role": "user",
                "content": [None, {"type": "text", "text": "hi"}, "stray"],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    None,
                    {"function": "bad"},
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    },
                ],
            },
        ],
    })

    assert req.messages[0].content[0] is None
    assert req.messages[1].tool_calls[0] is None
    assert req.messages[1].tool_calls[2]["function"]["name"] == "search"


def test_recognizes_openai_tool_calls_envelope():
    result = parse_tool_calls(
        'I will read it.\n'
        '{"tool_calls":[{"id":"call_001","type":"function",'
        '"function":{"name":"Read","arguments":"{\\"file_path\\":\\"README.md\\"}"}}]}',
        ["Read"],
    )

    assert len(result.calls) == 1
    assert result.calls[0].call_id == "call_001"
    assert result.calls[0].name == "Read"
    assert json.loads(result.calls[0].arguments) == {"file_path": "README.md"}


def test_picks_outermost_envelope_with_multiple_tools():
    result = parse_tool_calls(
        '{"tool_calls":[{"id":"a","type":"function","function":{"name":"Glob",'
        '"arguments":"{\\"pattern\\":\\"**/*.go\\"}"}},'
        '{"id":"b","type":"function","function":{"name":"Read",'
        '"arguments":"{\\"file_path\\":\\"main.go\\"}"}}]}',
        ["Glob", "Read"],
    )

    assert [call.name for call in result.calls] == ["Glob", "Read"]
    assert json.loads(result.calls[0].arguments) == {"pattern": "**/*.go"}
    assert json.loads(result.calls[1].arguments) == {"file_path": "main.go"}


def test_skips_invalid_items_inside_tool_calls_list():
    result = parse_tool_calls(
        '{"tool_calls":[{"type":"text","text":"ignore me"},'
        '{"function":{"name":"Read","arguments":{"file_path":"README.md"}}}]}',
        ["Read"],
    )

    assert len(result.calls) == 1
    assert result.calls[0].name == "Read"
    assert json.loads(result.calls[0].arguments) == {"file_path": "README.md"}


def test_recognizes_single_object_tool_aliases():
    result = parse_tool_calls('{"tool":"Glob","arguments":{"pattern":"**/nexu.txt"}}', ["Glob"])

    assert len(result.calls) == 1
    assert result.calls[0].name == "Glob"
    assert json.loads(result.calls[0].arguments) == {"pattern": "**/nexu.txt"}


def test_recognizes_nested_function_object():
    result = parse_tool_calls(
        '{"type":"function","function":{"name":"Read","arguments":{"file_path":"README.md"}}}',
        ["Read"],
    )

    assert len(result.calls) == 1
    assert result.calls[0].name == "Read"
    assert json.loads(result.calls[0].arguments) == {"file_path": "README.md"}


def test_recognizes_bare_array_and_brackets_inside_strings():
    result = parse_tool_calls(
        '[{"tool":"Glob","arguments":{"pattern":"**/*.go"}},'
        '{"tool":"Read","arguments":{"file_path":"src/foo[bar].txt"}}]',
        ["Glob", "Read"],
    )

    assert [call.name for call in result.calls] == ["Glob", "Read"]
    assert json.loads(result.calls[1].arguments) == {"file_path": "src/foo[bar].txt"}


def test_picks_last_tool_when_multiple_json_blocks_exist():
    result = parse_tool_calls(
        'recap: ```json\n'
        '{"tool":"Glob","arguments":{"pattern":"**/*.txt","path":"old"}}\n'
        '```\n```json\n'
        '{"tool":"Glob","arguments":{"pattern":"**/*.txt","path":"new"}}\n'
        '```',
        ["Glob"],
    )

    assert len(result.calls) == 1
    assert json.loads(result.calls[0].arguments) == {"pattern": "**/*.txt", "path": "new"}


def test_picks_last_tool_calls_envelope_when_multiple_exist():
    result = parse_tool_calls(
        '{"tool_calls":[{"function":{"name":"Glob","arguments":{"path":"old"}}}]}'
        '\n'
        '{"tool_calls":[{"function":{"name":"Glob","arguments":{"path":"new"}}}]}',
        ["Glob"],
    )

    assert len(result.calls) == 1
    assert json.loads(result.calls[0].arguments) == {"path": "new"}


def test_filters_unknown_tools():
    result = parse_tool_calls('{"tool":"Unknown","arguments":{"x":1}}', ["Known"])

    assert result.saw_tool_syntax is True
    assert result.calls == []


def test_legacy_functions_are_normalized_to_tools():
    functions = [
        {
            "name": "lookup",
            "description": "lookup data",
            "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
        }
    ]

    tools = normalize_openai_tools(None, functions)
    tool_choice = normalize_openai_tool_choice(None, {"name": "lookup"})

    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "lookup data",
                "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
        }
    ]
    assert tool_choice == {"type": "function", "function": {"name": "lookup"}}


def test_tool_choice_name_shorthand_is_normalized():
    tool_choice = normalize_openai_tool_choice({"name": "lookup"}, None)

    assert tool_choice == {"type": "function", "function": {"name": "lookup"}}


def test_flat_function_tools_are_normalized_to_chat_tools():
    tools = normalize_openai_tools(
        [
            {
                "type": "function",
                "name": "lookup",
                "description": "lookup data",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
                "strict": True,
            }
        ],
        None,
    )

    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "lookup data",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }
    ]


def test_tool_choice_none_disables_tool_call_parsing():
    assert tool_choice_allows_calls("none") is False
    assert tool_choice_allows_calls({"type": "none"}) is False
    assert tool_choice_allows_calls("auto") is True
    assert tool_choice_allows_calls("required") is True
    assert tool_choice_allows_calls({"type": "function", "function": {"name": "lookup"}}) is True


def test_forced_tool_choice_limits_allowed_tool_names():
    tools = [
        {"type": "function", "function": {"name": "search"}},
        {"type": "function", "function": {"name": "read"}},
    ]

    assert allowed_tool_names(tools, {"type": "function", "function": {"name": "read"}}) == ["read"]
    assert allowed_tool_names(tools, "none") == []
    assert allowed_tool_names(tools, "auto") == ["search", "read"]


def test_forced_tool_choice_rejects_unknown_tool_name():
    tools = [{"type": "function", "function": {"name": "search"}}]

    with pytest.raises(ValidationError):
        allowed_tool_names(tools, {"type": "function", "function": {"name": "read"}})


def test_forced_tool_choice_requires_tools():
    with pytest.raises(ValidationError):
        validate_tool_choice_tools(None, {"type": "function", "function": {"name": "read"}})
    with pytest.raises(ValidationError):
        validate_tool_choice_tools(None, "required")


def test_required_tool_choice_rejects_empty_valid_tool_names():
    tools = [None, {"type": "function", "function": "bad"}]

    with pytest.raises(ValidationError):
        allowed_tool_names(tools, "required")


def test_forced_tool_choice_rejects_malformed_function_object():
    tools = [{"type": "function", "function": {"name": "read"}}]

    with pytest.raises(ValidationError):
        validate_tool_choice_tools(tools, {"type": "function", "function": "read"})


def test_tool_request_helpers_ignore_malformed_tool_items():
    tools = [
        None,
        {"type": "function", "function": "bad"},
        {"type": "function", "function": {"name": "search"}},
    ]

    assert allowed_tool_names(tools, "auto") == ["search"]
    assert validate_strict_tools(tools) == set()
    assert extract_tool_names(tools) == ["search"]


def test_strict_tool_schema_validation_rejects_non_strict_schema():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": [],
                    "additionalProperties": True,
                },
            },
        }
    ]

    with pytest.raises(ValidationError):
        validate_strict_tools(tools)


def test_strict_tool_schema_validation_rejects_unknown_required_property():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id", "missing"],
                    "additionalProperties": False,
                },
            },
        }
    ]

    with pytest.raises(ValidationError):
        validate_strict_tools(tools)


def test_strict_tool_call_arguments_are_normalized():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "region": {"type": "string"},
                    },
                    "required": ["id", "region"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    calls = [
        ParsedToolCall.make(
            "lookup",
            {"id": "42", "extra": "drop-me"},
            call_id="call_001",
        )
    ]

    normalized = normalize_parsed_tool_calls(calls, tools)

    assert normalized[0].call_id == "call_001"
    assert json.loads(normalized[0].arguments) == {"id": "42", "region": None}


def test_recognizes_invoke_with_parameter_tags():
    text = (
        '<tool_calls>'
        '<invoke name="Read">'
        '<parameter name="file_path">README.md</parameter>'
        '<parameter name="limit">42</parameter>'
        '</invoke>'
        '</tool_calls>'
    )

    result = parse_tool_calls(text, ["Read"])

    assert len(result.calls) == 1
    assert result.calls[0].name == "Read"
    assert json.loads(result.calls[0].arguments) == {"file_path": "README.md", "limit": 42}


def test_tool_calls_to_xml_escapes_and_parser_unescapes_arguments():
    xml = tool_calls_to_xml([
        {
            "type": "function",
            "function": {
                "name": "Read",
                "arguments": json.dumps({"text": "</parameters><bad/>", "quote": '"'}),
            },
        }
    ])

    result = parse_tool_calls(xml, ["Read"])

    assert len(result.calls) == 1
    assert result.calls[0].name == "Read"
    assert json.loads(result.calls[0].arguments) == {"text": "</parameters><bad/>", "quote": '"'}


def test_recognizes_dsml_wrapped_invoke():
    text = (
        '<|DSML|tool_calls>'
        '<|DSML|invoke name="Glob">'
        '<|DSML|parameter name="pattern">**/*.py</|DSML|parameter>'
        '<|/DSML|invoke>'
        '<|/DSML|tool_calls>'
    )

    result = parse_tool_calls(text, ["Glob"])

    assert len(result.calls) == 1
    assert result.calls[0].name == "Glob"
    assert json.loads(result.calls[0].arguments) == {"pattern": "**/*.py"}


def test_tool_calls_to_xml_ignores_malformed_history_items():
    xml = tool_calls_to_xml([
        None,
        {"function": "bad"},
        {"function": {"name": "search", "arguments": "{\"q\":\"x\"}"}},
    ])

    assert "search" in xml
    assert "bad" not in xml


def test_tool_calls_to_xml_normalizes_dict_arguments():
    xml = tool_calls_to_xml([
        {"function": {"name": "search", "arguments": {"q": "x", "n": 1}}},
    ])
    result = parse_tool_calls(xml, ["search"])

    assert len(result.calls) == 1
    assert json.loads(result.calls[0].arguments) == {"q": "x", "n": 1}


def test_tool_sieve_detects_dsml_wrapped_tool_call():
    ToolSieve = load_tool_sieve()
    sieve = ToolSieve(["Glob"])

    safe, calls = sieve.feed("prefix <|DSML|tool_calls><|DSML|invoke name=\"Glob\">")
    assert safe == "prefix "
    assert calls is None

    safe, calls = sieve.feed(
        '<|DSML|parameter name="pattern">**/*.py</|DSML|parameter>'
        '<|/DSML|invoke><|/DSML|tool_calls> suffix'
    )

    assert safe == ""
    assert len(calls) == 1
    assert calls[0].name == "Glob"
    assert json.loads(calls[0].arguments) == {"pattern": "**/*.py"}


def test_tool_sieve_returns_text_for_unknown_tool_block():
    ToolSieve = load_tool_sieve()
    sieve = ToolSieve(["Known"])

    safe, calls = sieve.feed('<tool_calls><invoke name="Unknown">{"x":1}</invoke></tool_calls> trailing')

    assert calls is None
    assert safe == '<tool_calls><invoke name="Unknown">{"x":1}</invoke></tool_calls> trailing'

    safe, calls = sieve.feed('<tool_calls><invoke name="Known">{"ok":true}</invoke></tool_calls>')

    assert safe == ""
    assert len(calls) == 1
    assert calls[0].name == "Known"
    assert json.loads(calls[0].arguments) == {"ok": True}


def test_tool_sieve_can_continue_after_unknown_tool_in_same_chunk():
    ToolSieve = load_tool_sieve()
    sieve = ToolSieve(["Known"])

    safe, calls = sieve.feed(
        '<tool_calls><invoke name="Unknown">{"x":1}</invoke></tool_calls>'
        '<tool_calls><invoke name="Known">{"ok":true}</invoke></tool_calls>'
    )

    assert safe == '<tool_calls><invoke name="Unknown">{"x":1}</invoke></tool_calls>'
    assert len(calls) == 1
    assert calls[0].name == "Known"
    assert json.loads(calls[0].arguments) == {"ok": True}
