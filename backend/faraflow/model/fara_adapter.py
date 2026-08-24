import asyncio
import base64
from typing import Any, Dict, List, Optional

import httpx

from faraflow.config import Settings

from .coordinate_adapter import CoordinateMode, ObservationGeometry, adapt_action
from .fara_protocol import (
    ModelDecision,
    ModelProtocolError,
    build_system_prompt,
    parse_raw_tool_call,
)


class ModelEndpointError(RuntimeError):
    pass


class FaraAdapter:
    """OpenAI-compatible adapter for a self-hosted Fara1.5 vLLM endpoint."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.fara_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.fara_timeout_seconds),
            headers={
                "Authorization": f"Bearer {settings.fara_api_key}",
                "Content-Type": "application/json",
            },
        )
        self.coordinate_mode = CoordinateMode(settings.fara_coordinate_mode)
        self.geometry = ObservationGeometry.full_viewport(
            settings.browser_viewport_width,
            settings.browser_viewport_height,
        )
        if self.coordinate_mode is CoordinateMode.PIXEL:
            prompt_width = settings.browser_viewport_width
            prompt_height = settings.browser_viewport_height
        else:
            prompt_width = settings.fara_coordinate_space
            prompt_height = settings.fara_coordinate_space
        self.system_prompt = build_system_prompt(
            prompt_width,
            prompt_height,
            coordinate_mode=self.coordinate_mode.value,
            screenshot_width=settings.browser_viewport_width,
            screenshot_height=settings.browser_viewport_height,
        )

    @staticmethod
    def image_part(screenshot: bytes) -> Dict[str, Any]:
        encoded = base64.b64encode(screenshot).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{encoded}"},
        }

    def initial_user_message(self, goal: str, screenshot: bytes) -> Dict[str, Any]:
        return {
            "role": "user",
            "content": [
                self.image_part(screenshot),
                {"type": "text", "text": goal},
            ],
        }

    def observation_message(
        self,
        observation: str,
        screenshot: bytes,
        *,
        user_response: str = "",
        facts: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        text = (
            f"Observation: {observation}\n"
            "Here is the next screenshot. Think about what to do next."
        )
        if user_response:
            text = f"User response: {user_response}\n{text}"
        if facts:
            text += "\nRemembered facts:\n- " + "\n- ".join(facts[-10:])
        return {
            "role": "user",
            "content": [self.image_part(screenshot), {"type": "text", "text": text}],
        }

    def _trim_screenshots(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        keep = self.settings.fara_max_screenshots
        seen = 0
        result: List[Dict[str, Any]] = []
        for message in reversed(messages):
            copy = dict(message)
            content = copy.get("content")
            if isinstance(content, list):
                kept_parts = []
                for part in reversed(content):
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        seen += 1
                        if seen > keep:
                            continue
                    kept_parts.append(part)
                copy["content"] = list(reversed(kept_parts))
            result.append(copy)
        return list(reversed(result))

    @staticmethod
    def _response_content(data: Dict[str, Any]) -> str:
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str):
            raise ModelProtocolError("model response content was not text")
        return content

    async def next_action(self, conversation: List[Dict[str, Any]]) -> ModelDecision:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt}
        ]
        messages.extend(self._trim_screenshots(conversation))
        payload: Dict[str, Any] = {
            "model": self.settings.fara_model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": self.settings.fara_max_tokens,
        }
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = await self._client.post("/chat/completions", json=payload)
                response.raise_for_status()
                content = self._response_content(response.json())
                try:
                    raw_decision = parse_raw_tool_call(content)
                    try:
                        action = adapt_action(
                            raw_decision.action,
                            self.coordinate_mode,
                            self.geometry,
                        )
                    except ValueError as exc:
                        raise ModelProtocolError(str(exc)) from exc
                    return ModelDecision(
                        action=action,
                        raw_response=raw_decision.raw_response,
                        reasoning_present=raw_decision.reasoning_present,
                    )
                except ModelProtocolError as exc:
                    last_error = exc
                    if attempt < 2:
                        messages = [
                            *messages,
                            {"role": "assistant", "content": content},
                            {
                                "role": "user",
                                "content": (
                                    "Your previous <tool_call> was invalid JSON and could not "
                                    "be executed. Return one corrected <tool_call> now. "
                                    "Use a real action object, double-quoted JSON properties, "
                                    "and no placeholders. Copy any non-English text exactly "
                                    "as written in the original user task instead of using "
                                    "escape codes."
                                ),
                            },
                        ]
                        payload["messages"] = messages
                        await asyncio.sleep(2**attempt)
                        continue
                    raise
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        raise ModelEndpointError(f"Fara endpoint call failed after 3 attempts: {last_error}")

    async def complete_chat(
        self,
        messages: List[Dict[str, str]],
        *,
        system_prompt: str,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> str:
        payload = {
            "model": self.settings.fara_model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "temperature": temperature,
            "max_tokens": max_tokens or self.settings.fara_max_tokens,
        }
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = await self._client.post("/chat/completions", json=payload)
                response.raise_for_status()
                return self._response_content(response.json()).strip()
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        raise ModelEndpointError(f"Fara chat call failed after 3 attempts: {last_error}")

    async def health(self) -> Dict[str, Any]:
        try:
            response = await self._client.get("/models", timeout=5.0)
            response.raise_for_status()
            payload = response.json()
            entries = payload.get("data") if isinstance(payload, dict) else None
            model_ids = [
                entry.get("id")
                for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            ] if isinstance(entries, list) else []
            if model_ids and self.settings.fara_model not in model_ids:
                return {
                    "status": "model_mismatch",
                    "model": self.settings.fara_model,
                    "available_models": model_ids,
                }
            if not model_ids:
                return {
                    "status": "unavailable",
                    "model": self.settings.fara_model,
                    "detail": "model endpoint returned no OpenAI-compatible model ids",
                }
            return {"status": "ok", "model": self.settings.fara_model}
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "unavailable", "detail": str(exc)}

    async def close(self) -> None:
        await self._client.aclose()
