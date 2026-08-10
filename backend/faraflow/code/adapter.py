from typing import Any, Dict, List

import httpx

from faraflow.config import Settings
from faraflow.model.fara_adapter import ModelEndpointError

from .protocol import CodeDecision, CodeProtocolError, parse_code_response


class CodeAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.code_base_url.rstrip("/") or "http://127.0.0.1",
            headers={"Authorization": f"Bearer {settings.code_api_key}"},
            timeout=settings.code_timeout_seconds,
        )

    @property
    def configured(self) -> bool:
        return bool(self.settings.code_base_url.strip() and self.settings.code_model.strip())

    @staticmethod
    def _content(data: Dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise KeyError("model response has no choices")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        return str(content)

    async def next_decision(
        self, messages: List[Dict[str, str]], *, system_prompt: str
    ) -> CodeDecision:
        if not self.configured:
            raise ModelEndpointError("coding model is not configured")
        payload: Dict[str, Any] = {
            "model": self.settings.code_model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "temperature": 0.0,
            "max_tokens": self.settings.code_max_tokens,
        }
        last_error: Exception = CodeProtocolError("no response")
        for attempt in range(3):
            try:
                response = await self._client.post("/chat/completions", json=payload)
                response.raise_for_status()
                content = self._content(response.json())
                try:
                    return parse_code_response(content)
                except CodeProtocolError as exc:
                    last_error = exc
                    if attempt >= 2:
                        raise
                    payload["messages"] = [
                        *payload["messages"],
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                "The previous response violated the protocol. Return exactly one "
                                "valid <tool_call> JSON object or one <final> block."
                            ),
                        },
                    ]
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                last_error = exc
                if isinstance(exc, CodeProtocolError):
                    continue
                if attempt >= 2:
                    break
        raise ModelEndpointError(f"coding model call failed: {last_error}") from last_error

    async def health(self) -> Dict[str, Any]:
        if not self.configured:
            return {"status": "not_configured", "model": self.settings.code_model or None}
        try:
            response = await self._client.get("/models")
            response.raise_for_status()
            return {"status": "ok", "model": self.settings.code_model}
        except httpx.HTTPError as exc:
            return {"status": "error", "detail": str(exc), "model": self.settings.code_model}

    async def close(self) -> None:
        await self._client.aclose()
