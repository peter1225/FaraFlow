import json
import re
from typing import Any, List, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError

from faraflow.domain.schemas import (
    ChatCreate,
    ChatMessageCreate,
    ChatMessageView,
    ChatReply,
    ChatSummary,
    ChatView,
    TaskCreate,
)
from faraflow.infra.repository import Repository
from faraflow.model.fara_adapter import FaraAdapter, ModelEndpointError

from .task_service import TaskService

CHAT_ROUTER_PROMPT = """You are the conversational coordinator for FaraFlow.
Reply in the same language as the user and decide whether the request needs a live browser.

Use mode=chat for explanations, writing, brainstorming, coding knowledge, and questions that can
be answered without current web data. Put the complete helpful answer in reply.

Use mode=automation when the user asks to search, open, inspect, compare, or interact with a live
website, or when current/time-sensitive web information is required. For automation, produce a
concise acknowledgement plus a safe browser task. Default to https://www.bing.com/ and bing.com
for general searches. If a specific site is requested, include its HTTPS start URL and every
hostname the task must visit. Do not invent personal information. Irreversible actions remain
subject to FaraFlow's approval policy.

Return exactly one JSON object and no markdown, XML, tool call, or surrounding commentary:
{
  "mode": "chat" | "automation",
  "reply": "answer or acknowledgement",
  "task_name": "short title when mode is automation",
  "description": "complete browser goal when mode is automation",
  "start_url": "https://... when mode is automation",
  "allowed_domains": ["example.com"]
}
"""

_BROWSER_HINT = re.compile(
    r"(搜索|搜一下|查一下|查询网页|查网页|打开(?:网站|网页|网址)|进入(?:网站|网页)|"
    r"访问(?:网站|网页|网址)|浏览网页|官网|最新|实时|今日|今天|"
    r"search (?:the )?web|browse|look up|open (?:the )?(?:site|website|url)|latest|current)",
    re.IGNORECASE,
)
_BROWSER_NEGATION = re.compile(
    r"(?:不需要|无需|不用|不要|别).{0,8}(?:搜索|查询|查|打开|访问|浏览|联网|网页|网站)|"
    r"(?:without|do not|don't|no need to).{0,16}(?:search|browse|open|web|website)",
    re.IGNORECASE,
)


class ChatRouteDecision(BaseModel):
    mode: Literal["chat", "automation"]
    reply: str = ""
    task_name: str = ""
    description: str = ""
    start_url: str = ""
    allowed_domains: List[str] = Field(default_factory=list)


class ChatService:
    def __init__(
        self,
        repository: Repository,
        fara: FaraAdapter,
        tasks: TaskService,
    ) -> None:
        self.repository = repository
        self.fara = fara
        self.tasks = tasks

    async def create(self, request: ChatCreate) -> ChatView:
        chat = await self.repository.create_chat(request)
        return ChatView(**self._summary(chat).model_dump(), messages=[])

    async def list(self, tenant_id: Optional[str] = None) -> List[ChatSummary]:
        rows = await self.repository.list_chats(tenant_id=tenant_id)
        return [self._summary(item) for item in rows]

    async def get(self, chat_id: str) -> ChatView:
        chat = await self.repository.get_chat(chat_id)
        messages = await self.repository.list_chat_messages(chat_id)
        return ChatView(
            **self._summary(chat).model_dump(),
            messages=[self._message(item) for item in messages],
        )

    async def respond(self, chat_id: str, request: ChatMessageCreate) -> ChatReply:
        chat = await self.repository.get_chat(chat_id)
        previous = await self.repository.list_chat_messages(chat_id, limit=40)
        await self.repository.append_chat_message(
            chat_id=chat_id,
            role="user",
            content=request.content,
        )
        if chat.title == "新对话":
            title = " ".join(request.content.split())[:40] or "新对话"
            await self.repository.update_chat_title(chat_id, title)

        conversation = [
            {"role": item.role, "content": item.content}
            for item in previous[-20:]
            if item.role in {"user", "assistant"}
        ]
        conversation.append({"role": "user", "content": request.content})
        try:
            raw = await self.fara.complete_chat(
                conversation,
                system_prompt=CHAT_ROUTER_PROMPT,
                temperature=0.1,
            )
        except ModelEndpointError:
            decision = self.parse_route("", request.content)
            if decision.mode == "chat":
                await self.repository.append_chat_message(
                    chat_id=chat_id,
                    role="assistant",
                    content=(
                        "当前模型服务暂时不可用。请确认 vLLM 已启动，并检查 .env 中的 "
                        "FARAFLOW_FARA_BASE_URL 与 FARAFLOW_FARA_MODEL 配置。"
                    ),
                    metadata={"error": "model_unavailable"},
                )
                return ChatReply(route="chat", chat=await self.get(chat_id), task=None)
        else:
            decision = self.parse_route(raw, request.content)

        task = None
        if decision.mode == "automation":
            task_request = self._task_request(
                decision, request.content, chat.tenant_id, chat.user_id
            )
            task = await self.tasks.create(task_request)
            if request.auto_start:
                task = await self.tasks.start(task.task_id)
            reply = decision.reply.strip() or "这个问题需要实时网页信息，已创建浏览器任务。"
            await self.repository.append_chat_message(
                chat_id=chat_id,
                role="assistant",
                content=reply,
                mode="automation",
                task_id=task.task_id,
                metadata={
                    "task_id": task.task_id,
                    "start_url": task.start_url,
                    "allowed_domains": task.allowed_domains,
                },
            )
        else:
            reply = decision.reply.strip() or raw.strip()
            await self.repository.append_chat_message(
                chat_id=chat_id,
                role="assistant",
                content=reply,
            )

        return ChatReply(route=decision.mode, chat=await self.get(chat_id), task=task)

    @classmethod
    def parse_route(cls, raw: str, user_message: str) -> ChatRouteDecision:
        decoder = json.JSONDecoder()
        for index, character in enumerate(raw):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(raw[index:])
                if isinstance(value, dict):
                    return ChatRouteDecision.model_validate(value)
            except (json.JSONDecodeError, ValidationError):
                continue
        if _BROWSER_HINT.search(user_message) and not _BROWSER_NEGATION.search(user_message):
            return ChatRouteDecision(
                mode="automation",
                reply="这个问题需要实时网页信息，已创建浏览器任务。",
                task_name=cls._fallback_title(user_message),
                description=user_message,
                start_url="https://www.bing.com/",
                allowed_domains=["bing.com"],
            )
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        return ChatRouteDecision(mode="chat", reply=cleaned or "暂时无法生成回答，请重试。")

    @classmethod
    def _task_request(
        cls,
        decision: ChatRouteDecision,
        user_message: str,
        tenant_id: str,
        user_id: str,
    ) -> TaskCreate:
        start_url = cls._safe_start_url(decision.start_url)
        host = (urlparse(start_url).hostname or "bing.com").lower().rstrip(".")
        policy_host = host[4:] if host.startswith("www.") else host
        domains: List[str] = []
        for item in decision.allowed_domains:
            normalized = cls._normalize_domain(item)
            if normalized and normalized not in domains:
                domains.append(normalized)
        if not any(host == item or host.endswith(f".{item}") for item in domains):
            domains.append(policy_host)
        return TaskCreate(
            task_name=(decision.task_name.strip() or cls._fallback_title(user_message))[:200],
            tenant_id=tenant_id,
            user_id=user_id,
            description=(decision.description.strip() or user_message)[:10_000],
            start_url=start_url,
            allowed_domains=domains,
        )

    @staticmethod
    def _safe_start_url(value: str) -> str:
        candidate = value.strip()
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return candidate
        return "https://www.bing.com/"

    @staticmethod
    def _normalize_domain(value: str) -> str:
        candidate = value.strip().lower().rstrip(".")
        if not candidate:
            return ""
        if "://" in candidate:
            candidate = (urlparse(candidate).hostname or "").lower().rstrip(".")
        return candidate.split(":", 1)[0]

    @staticmethod
    def _fallback_title(message: str) -> str:
        return (" ".join(message.split())[:36] or "网页任务")

    @staticmethod
    def _summary(record: Any) -> ChatSummary:
        return ChatSummary(
            chat_id=record.chat_id,
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            title=record.title,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _message(record: Any) -> ChatMessageView:
        return ChatMessageView(
            message_id=record.message_id,
            chat_id=record.chat_id,
            role=record.role,
            content=record.content,
            mode=record.mode,
            task_id=record.task_id,
            metadata=record.message_metadata,
            created_at=record.created_at,
        )
