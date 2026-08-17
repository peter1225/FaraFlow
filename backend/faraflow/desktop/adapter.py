import asyncio
import base64
from typing import Any, Dict, List, Optional

import httpx

from faraflow.config import Settings
from faraflow.model.fara_adapter import ModelEndpointError

from .protocol import (
    DesktopDecision,
    DesktopProtocolError,
    build_desktop_response_format,
    build_desktop_system_prompt,
    parse_desktop_response,
)


class DesktopAdapter:
    """OpenAI-compatible adapter for screenshot-driven desktop decisions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.desktop_base_url.rstrip("/") or "http://127.0.0.1",
            timeout=httpx.Timeout(settings.desktop_timeout_seconds),
            headers={
                "Authorization": f"Bearer {settings.desktop_api_key}",
                "Content-Type": "application/json",
            },
        )
        self.system_prompt = build_desktop_system_prompt()

    @property
    def configured(self) -> bool:
        return bool(self.settings.desktop_base_url.strip() and self.settings.desktop_model.strip())

    @staticmethod
    def image_part(screenshot: bytes) -> Dict[str, Any]:
        encoded = base64.b64encode(screenshot).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{encoded}"},
        }

    def initial_message(self, instruction: str, screenshot: bytes) -> Dict[str, Any]:
        return {
            "role": "user",
            "content": [self.image_part(screenshot), {"type": "text", "text": instruction}],
        }

    def observation_message(self, observation: str, screenshot: bytes) -> Dict[str, Any]:
        return {
            "role": "user",
            "content": [
                self.image_part(screenshot),
                {
                    "type": "text",
                    "text": (
                        f"Desktop observation: {observation}\n"
                        "Here is the updated screenshot. Continue only if the next action is safe."
                    ),
                },
            ],
        }

    def _trim_screenshots(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep recent screenshots without discarding the textual action history."""
        keep = self.settings.desktop_max_screenshots
        seen = 0
        result: List[Dict[str, Any]] = []
        for message in reversed(messages):
            copied = dict(message)
            content = copied.get("content")
            if isinstance(content, list):
                kept_parts = []
                for part in reversed(content):
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        seen += 1
                        if seen > keep:
                            continue
                    kept_parts.append(part)
                copied["content"] = list(reversed(kept_parts))
            result.append(copied)
        return list(reversed(result))

    @staticmethod
    def _response_content(data: Dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise KeyError("desktop response has no choices")
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str):
            raise ValueError("desktop response content was not text")
        return content

    async def next_decision(
        self,
        conversation: List[Dict[str, Any]],
        *,
        allow_terminal: bool = True,
    ) -> DesktopDecision:
        if not self.configured:
            raise ModelEndpointError("desktop model is not configured")
        payload: Dict[str, Any] = {
            "model": self.settings.desktop_model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                *self._trim_screenshots(conversation),
            ],
            "temperature": 0.0,
            "max_tokens": self.settings.desktop_max_tokens,
            "response_format": build_desktop_response_format(
                allow_terminal=allow_terminal
            ),
        }
        last_error: Optional[Exception] = DesktopProtocolError("no response")
        for attempt in range(3):
            try:
                response = await self._client.post("/chat/completions", json=payload)
                response.raise_for_status()
                content = self._response_content(response.json())
                try:
                    return parse_desktop_response(content)
                except DesktopProtocolError as exc:
                    last_error = exc
                    if attempt >= 2:
                        raise
                    payload["messages"] = [
                        *payload["messages"],
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                "The previous desktop response violated the protocol. "
                                "Return exactly one valid JSON object matching the supplied "
                                "desktop_action schema. Do not include XML, Markdown, or prose."
                            ),
                        },
                    ]
                    await asyncio.sleep(2**attempt)
            except httpx.HTTPStatusError as exc:
                body = exc.response.text.strip()
                status = exc.response.status_code
                detail = body[:2000] if body else str(exc)
                last_error = RuntimeError(f"HTTP {status} from desktop model: {detail}")
                # Retrying an unchanged client request cannot repair a 4xx response.
                if 400 <= status < 500:
                    break
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        raise ModelEndpointError(f"desktop model call failed after 3 attempts: {last_error}")

    async def health(self) -> Dict[str, Any]:
        if not self.configured:
            return {"status": "not_configured", "model": self.settings.desktop_model or None}
        try:
            response = await self._client.get("/models", timeout=5.0)
            response.raise_for_status()
            return {"status": "ok", "model": self.settings.desktop_model}
        except httpx.HTTPError as exc:
            return {"status": "error", "detail": str(exc), "model": self.settings.desktop_model}

    async def close(self) -> None:
        await self._client.aclose()
