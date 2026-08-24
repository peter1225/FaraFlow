import pytest
from faraflow.config import Settings
from faraflow.model.fara_adapter import FaraAdapter


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self.content}}]}


class HealthResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.payloads: list[dict] = []

    async def post(self, path: str, *, json: dict) -> FakeResponse:
        del path
        self.payloads.append(json)
        return FakeResponse(self.responses.pop(0))


class HealthClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def get(self, path: str, *, timeout: float) -> HealthResponse:
        del path, timeout
        return HealthResponse(self.payload)


@pytest.mark.asyncio
async def test_health_rejects_endpoint_with_wrong_model() -> None:
    adapter = FaraAdapter(Settings(fara_model="microsoft/Fara1.5-27B"))
    await adapter._client.aclose()
    adapter._client = HealthClient(
        {"data": [{"id": "hf.co/bartowski/Fara1.5-9B-GGUF:Q4_K_M"}]}
    )  # type: ignore[assignment]

    health = await adapter.health()

    assert health["status"] == "model_mismatch"
    assert health["available_models"] == ["hf.co/bartowski/Fara1.5-9B-GGUF:Q4_K_M"]


@pytest.mark.asyncio
async def test_pixel_mode_adapts_model_coordinates_to_css_pixels() -> None:
    adapter = FaraAdapter(Settings(fara_coordinate_mode="pixel"))
    await adapter._client.aclose()
    adapter._client = FakeClient(
        [
            '<tool_call>{"name":"computer_use","arguments":'
            '{"action":"left_click","coordinate":[1074,437]}}</tool_call>'
        ]
    )  # type: ignore[assignment]

    decision = await adapter.next_action([])

    assert decision.action.coordinate == (1074.0, 437.0)


@pytest.mark.asyncio
async def test_normalized_mode_adapts_to_configured_viewport_css_pixels() -> None:
    adapter = FaraAdapter(Settings(fara_coordinate_mode="normalized_1000"))
    await adapter._client.aclose()
    adapter._client = FakeClient(
        [
            '<tool_call>{"name":"computer_use","arguments":'
            '{"action":"left_click","coordinate":[500,500]}}</tool_call>'
        ]
    )  # type: ignore[assignment]

    decision = await adapter.next_action([])

    assert decision.action.coordinate == (720.0, 450.0)


@pytest.mark.asyncio
async def test_reprompts_after_invalid_tool_call() -> None:
    adapter = FaraAdapter(Settings())
    await adapter._client.aclose()
    fake = FakeClient(
        [
            '<tool_call>{"name":"computer_use","arguments":{...}}</tool_call>',
            (
                '<tool_call>{"name":"computer_use","arguments":'
                '{"action":"type","text":"竹知了"}}</tool_call>'
            ),
        ]
    )
    adapter._client = fake  # type: ignore[assignment]

    decision = await adapter.next_action([])

    assert decision.action.action == "type"
    assert decision.action.text == "竹知了"
    assert len(fake.payloads) == 2
    assert "previous <tool_call> was invalid JSON" in fake.payloads[1]["messages"][-1][
        "content"
    ]


def test_messages_follow_official_image_first_order() -> None:
    adapter = FaraAdapter(Settings())
    initial = adapter.initial_user_message("search for bamboo", b"image")
    observation = adapter.observation_message("clicked", b"image")

    assert initial["content"][0]["type"] == "image_url"
    assert initial["content"][1] == {"type": "text", "text": "search for bamboo"}
    assert observation["content"][0]["type"] == "image_url"
    assert "Here is the next screenshot" in observation["content"][1]["text"]
