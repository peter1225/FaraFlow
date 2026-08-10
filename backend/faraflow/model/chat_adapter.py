import asyncio
import json
import re
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from faraflow.config import Settings

from .fara_adapter import ModelEndpointError


class ChatAdapter:
    """OpenAI-compatible chat adapter with an explicit Fara fallback."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.uses_fara_fallback = not (
            settings.chat_base_url.strip() and settings.chat_model.strip()
        )
        if self.uses_fara_fallback:
            self.base_url = settings.fara_base_url.rstrip("/")
            self.api_key = settings.fara_api_key
            self.model = settings.fara_model
            self.timeout_seconds = settings.fara_timeout_seconds
            self.max_tokens = settings.fara_max_tokens
        else:
            self.base_url = settings.chat_base_url.rstrip("/")
            self.api_key = settings.chat_api_key or "not-needed"
            self.model = settings.chat_model
            self.timeout_seconds = settings.chat_timeout_seconds
            self.max_tokens = settings.chat_max_tokens
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout_seconds),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    @classmethod
    def _response_content(
        cls, data: Dict[str, Any], *, require_thinking_end: bool = False
    ) -> str:
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str):
            raise ValueError("chat model response content was not text")
        if require_thinking_end and not re.search(
            r"</think>", content, flags=re.IGNORECASE
        ):
            raise ValueError("thinking response ended before a final answer")
        return cls._clean_reasoning(content)

    @staticmethod
    def _clean_reasoning(content: str) -> str:
        text = content.strip()
        had_reasoning_tag = bool(re.search(r"</?think>", text, flags=re.IGNORECASE))
        closing_tags = list(re.finditer(r"</think>", text, flags=re.IGNORECASE))
        if closing_tags:
            text = text[closing_tags[-1].end() :].strip()
        text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE).strip()
        if not had_reasoning_tag:
            return text
        blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
        deduplicated: List[str] = []
        for block in blocks:
            if not deduplicated or block != deduplicated[-1]:
                deduplicated.append(block)
        return "\n\n".join(deduplicated)

    async def complete_chat(
        self,
        messages: List[Dict[str, str]],
        *,
        system_prompt: str,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        enable_thinking: Optional[bool] = None,
    ) -> str:
        payload = self._payload(
            messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
        )
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = await self._client.post("/chat/completions", json=payload)
                response.raise_for_status()
                return self._response_content(
                    response.json(),
                    require_thinking_end=self._effective_thinking(enable_thinking)
                    is True,
                ).strip()
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        raise ModelEndpointError(
            f"chat endpoint call failed after 3 attempts: {last_error}"
        )

    def _payload(
        self,
        messages: List[Dict[str, str]],
        *,
        system_prompt: str,
        temperature: float,
        max_tokens: Optional[int],
        stream: bool = False,
        enable_thinking: Optional[bool] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "temperature": temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        effective_thinking = self._effective_thinking(enable_thinking)
        if effective_thinking is not None and not self.uses_fara_fallback:
            payload["chat_template_kwargs"] = {
                "enable_thinking": effective_thinking
            }
        if stream:
            payload["stream"] = True
        return payload

    def _effective_thinking(self, requested: Optional[bool]) -> Optional[bool]:
        if self.uses_fara_fallback:
            return None
        if requested is not None:
            return requested
        if self.settings.chat_disable_thinking and not self.uses_fara_fallback:
            return False
        return None

    @staticmethod
    def _delta_content(data: Dict[str, Any]) -> str:
        content = data["choices"][0]["delta"].get("content", "")
        if content is None:
            return ""
        if isinstance(content, list):
            return "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str):
            raise ValueError("chat model stream delta was not text")
        return content

    async def stream_chat(
        self,
        messages: List[Dict[str, str]],
        *,
        system_prompt: str,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        enable_thinking: Optional[bool] = None,
    ) -> AsyncIterator[str]:
        """Stream only final-answer text for backward-compatible callers."""
        async for event in self.stream_chat_events(
            messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
        ):
            if event["type"] == "content_delta":
                yield event["content"]

    async def stream_chat_events(
        self,
        messages: List[Dict[str, str]],
        *,
        system_prompt: str,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        enable_thinking: Optional[bool] = None,
    ) -> AsyncIterator[Dict[str, str]]:
        """Stream reasoning and final text as separate events.

        Reasoning is exposed only when thinking was explicitly requested. For a
        fallback model whose template behavior is unknown, the response remains
        buffered and sanitized before any final-answer events are emitted.
        """
        effective_thinking = self._effective_thinking(enable_thinking)
        payload = self._payload(
            messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            enable_thinking=enable_thinking,
        )
        last_error: Optional[Exception] = None
        for attempt in range(3):
            emitted = False
            reasoning_buffer = ""
            final_text_started = effective_thinking is False
            reasoning_complete = effective_thinking is not True
            try:
                async with self._client.stream(
                    "POST", "/chat/completions", json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        body = line[5:].strip()
                        if not body or body == "[DONE]":
                            continue
                        delta = self._delta_content(json.loads(body))
                        if not delta:
                            continue

                        if effective_thinking is None:
                            reasoning_buffer += delta
                            continue

                        if reasoning_complete:
                            if not final_text_started:
                                delta = delta.lstrip()
                                if not delta:
                                    continue
                                final_text_started = True
                            emitted = True
                            yield {"type": "content_delta", "content": delta}
                            continue

                        reasoning_buffer += delta
                        while reasoning_buffer:
                            closing = re.search(
                                r"</think>", reasoning_buffer, flags=re.IGNORECASE
                            )
                            if closing:
                                reasoning = reasoning_buffer[: closing.start()]
                                if reasoning:
                                    emitted = True
                                    yield {
                                        "type": "reasoning_delta",
                                        "content": reasoning,
                                    }
                                reasoning_complete = True
                                final_prefix = reasoning_buffer[closing.end() :].lstrip()
                                reasoning_buffer = ""
                                yield {"type": "reasoning_done", "content": ""}
                                if final_prefix:
                                    final_text_started = True
                                    emitted = True
                                    yield {
                                        "type": "content_delta",
                                        "content": final_prefix,
                                    }
                                break

                            # Keep enough trailing characters to recognize a
                            # closing marker split across multiple SSE chunks.
                            safe_length = len(reasoning_buffer) - (len("</think>") - 1)
                            if safe_length <= 0:
                                break
                            reasoning = reasoning_buffer[:safe_length]
                            reasoning_buffer = reasoning_buffer[safe_length:]
                            emitted = True
                            yield {"type": "reasoning_delta", "content": reasoning}

                if effective_thinking is True and not reasoning_complete:
                    if reasoning_buffer:
                        emitted = True
                        yield {
                            "type": "reasoning_delta",
                            "content": reasoning_buffer,
                        }
                    raise ModelEndpointError(
                        "thinking response ended before a final answer"
                    )

                if effective_thinking is None:
                    cleaned = self._clean_reasoning(reasoning_buffer)
                    for offset in range(0, len(cleaned), 32):
                        emitted = True
                        yield {
                            "type": "content_delta",
                            "content": cleaned[offset : offset + 32],
                        }
                return
            except (
                httpx.HTTPError,
                json.JSONDecodeError,
                KeyError,
                IndexError,
                ValueError,
            ) as exc:
                last_error = exc
                if emitted:
                    break
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        raise ModelEndpointError(
            f"chat endpoint stream failed after {attempt + 1} attempts: {last_error}"
        )

    async def health(self) -> Dict[str, Any]:
        source = "fara_fallback" if self.uses_fara_fallback else "dedicated"
        try:
            response = await self._client.get("/models", timeout=5.0)
            response.raise_for_status()
            return {"status": "ok", "model": self.model, "source": source}
        except httpx.HTTPError as exc:
            return {
                "status": "unavailable",
                "model": self.model,
                "source": source,
                "detail": str(exc),
            }

    async def close(self) -> None:
        await self._client.aclose()
