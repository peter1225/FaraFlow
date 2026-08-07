from faraflow.runtime.chat_service import ChatService


def test_chat_route_falls_back_to_browser_for_explicit_web_request() -> None:
    route = ChatService.parse_route("not-json", "请帮我搜索最新的 vLLM 文档")

    assert route.mode == "automation"
    assert route.start_url == "https://www.bing.com/"
    assert route.allowed_domains == ["bing.com"]


def test_chat_route_respects_explicit_no_browser_request() -> None:
    route = ChatService.parse_route("", "请直接回答，不需要查询网页")

    assert route.mode == "chat"


def test_chat_route_extracts_json_after_reasoning_text() -> None:
    route = ChatService.parse_route(
        'I should answer directly. {"mode":"chat","reply":"你好，有什么可以帮你？"}',
        "你好",
    )

    assert route.mode == "chat"
    assert route.reply == "你好，有什么可以帮你？"
