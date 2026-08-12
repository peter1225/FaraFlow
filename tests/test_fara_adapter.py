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


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.payloads: list[dict] = []

    async def post(self, path: str, *, json: dict) -> FakeResponse:
        del path
        self.payloads.append(json)
        return FakeResponse(self.responses.pop(0))


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
