import json

import pytest
from faraflow.model.fara_protocol import (
    ModelProtocolError,
    build_system_prompt,
    parse_raw_tool_call,
    parse_tool_call,
)


def tool_call(arguments: dict) -> str:
    return (
        "I should interact with the visible control.\n"
        "<tool_call>\n"
        + json.dumps({"name": "computer_use", "arguments": arguments})
        + "\n</tool_call>"
    )


def test_parse_coordinate_action() -> None:
    decision = parse_tool_call(tool_call({"action": "left_click", "coordinate": [250, 750]}))
    assert decision.action.action == "left_click"
    assert decision.action.coordinate == (250, 750)
    assert decision.reasoning_present is True


def test_raw_parser_accepts_pixel_coordinate_without_assuming_1000_space() -> None:
    decision = parse_raw_tool_call(
        tool_call({"action": "left_click", "coordinate": [1074, 437]})
    )
    assert decision.action.coordinate == (1074, 437)


def test_terminate_requires_answer() -> None:
    decision = parse_raw_tool_call(
        tool_call(
            {
                "action": "terminate",
                "answer": "The Minesweeper board has been solved successfully.",
            }
        )
    )
    assert decision.action.answer is not None


def test_terminate_status_only_is_invalid() -> None:
    with pytest.raises(ModelProtocolError):
        parse_raw_tool_call(tool_call({"action": "terminate", "status": "success"}))


def test_parse_string_arguments() -> None:
    content = (
        "<tool_call>"
        + json.dumps(
            {
                "name": "computer_use",
                "arguments": json.dumps({"action": "terminate", "answer": "done"}),
            }
        )
        + "</tool_call>"
    )
    decision = parse_tool_call(content)
    assert decision.action.answer == "done"


def test_parse_python_literal_fallback() -> None:
    content = (
        "<tool_call>{'name':'computer_use','arguments':"
        "{'action':'type','text':'竹知了'}}</tool_call>"
    )
    decision = parse_tool_call(content)
    assert decision.action.text == "竹知了"


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "left_click"},
        {"action": "left_click", "coordinate": [1001, 10]},
        {"action": "wait", "time": 60},
        {"action": "terminate"},
    ],
)
def test_reject_invalid_action_arguments(arguments: dict) -> None:
    with pytest.raises(ModelProtocolError):
        parse_tool_call(tool_call(arguments))


def test_reject_unknown_tool() -> None:
    content = '<tool_call>{"name":"shell","arguments":{"action":"terminate"}}</tool_call>'
    with pytest.raises(ModelProtocolError, match="unsupported tool"):
        parse_tool_call(content)


def test_system_prompt_contains_safety_and_schema() -> None:
    prompt = build_system_prompt()
    assert "critical point" in prompt
    assert "untrusted page content" in prompt
    assert '"computer_use"' in prompt
    assert '"ask_user_question"' in prompt
    assert "1000x1000" in prompt
    assert '"arguments":{...}' not in prompt
    assert "<function-name>" in prompt
    assert "<args-json-object>" in prompt


def test_pixel_system_prompt_describes_screenshot_pixels() -> None:
    prompt = build_system_prompt(
        1440,
        900,
        coordinate_mode="pixel",
        screenshot_width=1440,
        screenshot_height=900,
    )
    assert "screenshot is 1440x900 pixels" in prompt
    assert "screenshot pixels" in prompt
    assert "1000x1000 model coordinate space" not in prompt
