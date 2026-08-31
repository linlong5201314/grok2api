"""Offline tests for the hardening / compatibility work:

  - GROK_ env overrides (two-level legacy + double-underscore deep keys)
  - proxy URL credential redaction
  - response_format prompt enforcement
  - previous_response_id response store
  - image asset file-id extraction (serve-route compatible)
  - Anthropic error type mapping
"""

import pytest

from app.control.proxy.models import redact_url
from app.platform.config.loader import apply_env_overrides
from app.platform.errors import NotFoundError, RateLimitError, ValidationError
from app.products.anthropic.router import _anthropic_error_payload
from app.products.openai.chat import _apply_response_format
from app.products.openai.images import _extract_image_file_id
from app.products.openai.responses import (
    _remember_response,
    _resolve_prior_messages,
    _response_store,
)


# ---------------------------------------------------------------------------
# Env overrides
# ---------------------------------------------------------------------------


def test_env_overrides_two_level(monkeypatch):
    monkeypatch.setenv("GROK_APP_API_KEY", "sk-test")
    data = {"app": {"api_key": "old"}}
    apply_env_overrides(data)
    assert data["app"]["api_key"] == "sk-test"


def test_env_overrides_three_level_double_underscore(monkeypatch):
    monkeypatch.setenv("GROK_PROXY__EGRESS__MODE", "subscription")
    data = {"proxy": {"egress": {"mode": "direct"}}}
    apply_env_overrides(data)
    assert data["proxy"]["egress"]["mode"] == "subscription"


def test_env_overrides_creates_missing_sections(monkeypatch):
    monkeypatch.setenv("GROK_ACCOUNT__SELECTION__COOLING_SEC", "300")
    data: dict = {}
    apply_env_overrides(data)
    assert data["account"]["selection"]["cooling_sec"] == "300"


# ---------------------------------------------------------------------------
# Proxy URL redaction
# ---------------------------------------------------------------------------


def test_redact_url_masks_credentials():
    assert redact_url("socks5h://user:pass@1.2.3.4:1080") == "socks5h://***@1.2.3.4:1080"
    assert redact_url("http://alice:secret@proxy.lan:8080") == "http://***@proxy.lan:8080"


def test_redact_url_passthrough_without_credentials():
    assert redact_url("http://1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert redact_url("") == ""
    assert redact_url(None) == ""


# ---------------------------------------------------------------------------
# response_format
# ---------------------------------------------------------------------------


def test_response_format_json_object_appends_instruction():
    msg = _apply_response_format("hello", {"type": "json_object"})
    assert msg.startswith("hello")
    assert "JSON" in msg


def test_response_format_json_schema_includes_schema():
    msg = _apply_response_format(
        "hello",
        {"type": "json_schema", "json_schema": {"schema": {"type": "object"}}},
    )
    assert msg.startswith("hello")
    assert "object" in msg


def test_response_format_passthrough():
    assert _apply_response_format("hello", None) == "hello"
    assert _apply_response_format("hello", {"type": "text"}) == "hello"
    assert _apply_response_format("hello", "not-a-dict") == "hello"


# ---------------------------------------------------------------------------
# previous_response_id store
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_response_store():
    _response_store.clear()
    yield
    _response_store.clear()


def test_previous_response_id_roundtrip():
    _remember_response(
        "resp_1", [{"role": "user", "content": "hi"}],
        full_text="hello", fc_items=[],
    )
    prior = _resolve_prior_messages("resp_1")
    assert prior[0]["role"] == "user"
    assert prior[-1] == {"role": "assistant", "content": "hello"}


def test_previous_response_id_with_tool_calls():
    fc = [{"call_id": "call_1", "name": "t", "arguments": "{}"}]
    _remember_response(
        "resp_2", [{"role": "user", "content": "q"}],
        full_text="", fc_items=fc,
    )
    prior = _resolve_prior_messages("resp_2")
    assert prior[-1]["tool_calls"][0]["id"] == "call_1"
    assert prior[-1]["content"] is None


def test_previous_response_id_unknown_raises_404():
    with pytest.raises(NotFoundError) as excinfo:
        _resolve_prior_messages("resp_missing")
    assert excinfo.value.status == 404


# ---------------------------------------------------------------------------
# Image file-id extraction
# ---------------------------------------------------------------------------


def test_extract_image_file_id_prefers_asset_segment():
    url = "https://assets.grok.com/users/abc123/9f8e7d6c5b4a3f2e1d0c/content"
    assert _extract_image_file_id(url) == "9f8e7d6c5b4a3f2e1d0c"


def test_extract_image_file_id_handles_extension():
    url = "https://assets.grok.com/generated/0f1e2d3c4b5a6978/image.jpg"
    assert _extract_image_file_id(url) == "0f1e2d3c4b5a6978"


def test_extract_image_file_id_falls_back_to_url_sha1():
    url = "https://assets.grok.com/users/x/content"
    fid = _extract_image_file_id(url)
    assert len(fid) == 32
    assert all(c in "0123456789abcdef" for c in fid)
    # Distinct URLs must produce distinct ids (no more content.jpg overwrite).
    assert fid != _extract_image_file_id(url + "2")


# ---------------------------------------------------------------------------
# Anthropic error mapping
# ---------------------------------------------------------------------------


def test_anthropic_error_payload_mapping():
    assert _anthropic_error_payload(RateLimitError())["type"] == "rate_limit_error"
    assert _anthropic_error_payload(NotFoundError("x"))["type"] == "not_found_error"
    assert _anthropic_error_payload(ValidationError("x"))["type"] == "invalid_request_error"
