import json

import httpx
import pytest
from faraflow.config import Settings
from faraflow.model.chat_adapter import ChatAdapter


@pytest.mark.asyncio
async def test_chat_adapter_reuses_fara_when_chat_model_is_not_configured() -> None:
    settings = Settings(
        _env_file=None,
        fara_base_url="http://fara.test:8003/v1",
        fara_api_key="fara-key",
        fara_model="microsoft/Fara1.5-27B",
        chat_base_url="",
        chat_model="",
    )
    adapter = ChatAdapter(settings)
    try:
        assert adapter.uses_fara_fallback is True
        assert adapter.base_url == "http://fara.test:8003/v1"
        assert adapter.api_key == "fara-key"
        assert adapter.model == "microsoft/Fara1.5-27B"
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_chat_adapter_uses_dedicated_model_when_fully_configured() -> None:
    settings = Settings(
        _env_file=None,
        fara_base_url="http://fara.test:8003/v1",
        fara_model="microsoft/Fara1.5-27B",
        chat_base_url="http://chat.test:8004/v1",
        chat_api_key="chat-key",
        chat_model="qwen-chat",
    )
    adapter = ChatAdapter(settings)
    try:
        assert adapter.uses_fara_fallback is False
        assert adapter.base_url == "http://chat.test:8004/v1"
        assert adapter.api_key == "chat-key"
        assert adapter.model == "qwen-chat"
    finally:
        await adapter.close()


def test_chat_adapter_removes_reasoning_and_duplicate_answer() -> None:
    content = (
        "Internal reasoning that should not be shown.\n"
        "</think>\n\nFARA27B_CHAT_OK\n<think>\n\nFARA27B_CHAT_OK"
    )

    assert ChatAdapter._clean_reasoning(content) == "FARA27B_CHAT_OK"
    assert ChatAdapter._clean_reasoning("A normal answer") == "A normal answer"


@pytest.mark.asyncio
async def test_chat_adapter_streams_openai_deltas_and_disables_thinking() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        body = (
            'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, content=body.encode(), headers={"Content-Type": "text/event-stream"}
        )

    settings = Settings(
        _env_file=None,
        chat_base_url="http://chat.test/v1",
        chat_model="qwen-chat",
        chat_disable_thinking=True,
    )
    adapter = ChatAdapter(settings)
    await adapter._client.aclose()
    adapter._client = httpx.AsyncClient(
        base_url=adapter.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        chunks = [
            chunk
            async for chunk in adapter.stream_chat(
                [{"role": "user", "content": "Hello"}],
                system_prompt="Answer directly.",
            )
        ]
    finally:
        await adapter.close()

    assert chunks == ["Hello", " world"]
    assert captured["stream"] is True
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_chat_adapter_hides_thinking_and_streams_only_the_final_answer() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        body = (
            'data: {"choices":[{"delta":{"content":"Internal reasoning"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"\\n</think>\\n\\n"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"Final"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":" answer"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body.encode())

    settings = Settings(
        _env_file=None,
        chat_base_url="http://chat.test/v1",
        chat_model="qwen-chat",
        chat_disable_thinking=True,
    )
    adapter = ChatAdapter(settings)
    await adapter._client.aclose()
    adapter._client = httpx.AsyncClient(
        base_url=adapter.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        events = [
            event
            async for event in adapter.stream_chat_events(
                [{"role": "user", "content": "Think first"}],
                system_prompt="Answer carefully.",
                enable_thinking=True,
            )
        ]
    finally:
        await adapter.close()

    reasoning = "".join(
        event["content"] for event in events if event["type"] == "reasoning_delta"
    )
    answer = "".join(
        event["content"] for event in events if event["type"] == "content_delta"
    )
    assert reasoning == "Internal reasoning\n"
    assert answer == "Final answer"
    assert [event["type"] for event in events].count("reasoning_done") == 1
    assert "</think>" not in reasoning
    assert captured["chat_template_kwargs"] == {"enable_thinking": True}
