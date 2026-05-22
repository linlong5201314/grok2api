from contextlib import contextmanager
import sys
import types


class _LoggerStub:
    def add(self, *args, **kwargs):
        return 1

    def remove(self, *args, **kwargs):
        return None

    def bind(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return None
        return _noop


@contextmanager
def _loguru_stub():
    inserted = False
    try:
        __import__("loguru")
    except ModuleNotFoundError:
        module = types.ModuleType("loguru")
        module.logger = _LoggerStub()
        sys.modules["loguru"] = module
        inserted = True
    try:
        yield
    finally:
        if inserted:
            sys.modules.pop("loguru", None)


def test_video_prompt_extracts_multiple_references():
    with _loguru_stub():
        from app.products.openai.video import _extract_video_prompt_and_reference

        content = [{"type": "text", "text": "make video"}]
        content.extend(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{idx}"}}
            for idx in range(9)
        )

        prompt, references = _extract_video_prompt_and_reference([
            {"role": "user", "content": content},
        ])

        assert prompt == "make video"
        assert references == [
            {"image_url": f"data:image/png;base64,{idx}"}
            for idx in range(7)
        ]


def test_video_create_payload_uses_reference_to_video_config():
    with _loguru_stub():
        from app.products.openai.video import _video_create_payload

        payload = _video_create_payload(
            prompt="make video",
            parent_post_id="post_1",
            aspect_ratio="16:9",
            resolution_name="720p",
            video_length=6,
            preset="custom",
            image_references=["https://assets.grok.com/a/content"],
        )

        config = payload["responseMetadata"]["modelConfigOverride"]["modelMap"]["videoGenModelConfig"]
        assert config["isVideoEdit"] is False
        assert config["isReferenceToVideo"] is True
        assert config["imageReferences"] == ["https://assets.grok.com/a/content"]


def test_clearance_host_distinguishes_origins():
    with _loguru_stub():
        from app.control.proxy import _clearance_host

        assert _clearance_host("https://grok.com/rest/rate-limits") == "grok.com"
        assert _clearance_host("https://accounts.x.ai/accept-tos") == "accounts.x.ai"
        assert _clearance_host("") == "grok.com"


def test_select_any_uses_any_available_mode_bucket():
    from app.dataplane.account.selector import select, select_any
    from app.dataplane.account.table import make_empty_table
    from app.dataplane.shared.enums import ModeId, PoolId, StatusId

    table = make_empty_table()
    idx = table._append_slot(
        token="tok",
        pool_id=int(PoolId.BASIC),
        status_id=int(StatusId.ACTIVE),
        quota_auto=0,
        quota_fast=3,
        quota_expert=0,
        quota_heavy=0,
        reset_auto=0,
        reset_fast=0,
        reset_expert=0,
        reset_heavy=0,
        health=1.0,
        last_use_s=0,
        last_fail_s=0,
        fail_count=0,
        tags=[],
    )

    assert select(table, int(PoolId.BASIC), int(ModeId.AUTO), now_s=100) is None
    assert select_any(table, int(PoolId.BASIC), now_s=100) == idx
