import asyncio
import hashlib
import time
from typing import Any, Dict, List, Optional, Set

from faraflow.browser.playwright_runner import BrowserPool
from faraflow.domain.enums import ApprovalStatus, SessionState
from faraflow.domain.schemas import ApprovalPolicy, SessionEvent
from faraflow.infra.events import EventBus
from faraflow.infra.repository import ConflictError, Repository
from faraflow.model.fara_adapter import FaraAdapter
from faraflow.model.fara_protocol import ComputerAction
from faraflow.security.policy import (
    CriticalActionPolicy,
    PolicyViolation,
    PromptInjectionGuard,
)

TERMINAL_STATES = {
    SessionState.COMPLETED,
    SessionState.FAILED,
    SessionState.TERMINATED,
    SessionState.EXPIRED,
}

DUPLICATE_GUARDED_ACTIONS = {
    "left_click",
    "double_click",
    "triple_click",
    "right_click",
}


class AgentRuntime:
    def __init__(
        self,
        repository: Repository,
        browser_pool: BrowserPool,
        fara: FaraAdapter,
        event_bus: EventBus,
    ) -> None:
        self.repository = repository
        self.browser_pool = browser_pool
        self.fara = fara
        self.event_bus = event_bus
        self.critical_policy = CriticalActionPolicy()
        self.injection_guard = PromptInjectionGuard()
        self._jobs: Dict[str, asyncio.Task] = {}
        self._conversations: Dict[str, List[Dict[str, Any]]] = {}
        self._pause_requests: Set[str] = set()
        self._job_lock = asyncio.Lock()

    async def emit(
        self,
        session_id: str,
        event_type: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> SessionEvent:
        record = await self.repository.append_event(
            session_id=session_id,
            event_type=event_type,
            message=message,
            payload=payload,
        )
        event = SessionEvent(
            event_id=record.event_id,
            session_id=record.session_id,
            event_type=record.event_type,
            message=record.message,
            payload=record.payload,
            created_at=record.created_at,
        )
        await self.event_bus.publish(event)
        return event

    async def start(self, task_id: str) -> None:
        task = await self.repository.get_task(task_id)
        session = await self.repository.get_session(task.session_id)
        state = SessionState(session.state)
        if state in TERMINAL_STATES:
            raise ConflictError(f"cannot start a terminal task in state {state.value}")
        async with self._job_lock:
            existing = self._jobs.get(session.session_id)
            if existing and not existing.done():
                raise ConflictError("task is already running")
            self._pause_requests.discard(session.session_id)
            job = asyncio.create_task(
                self._run(task_id, session.session_id),
                name=f"faraflow:{session.session_id}",
            )
            self._jobs[session.session_id] = job

    async def prepare_rerun(self, task_id: str) -> Any:
        """Release runtime state and reset a terminal task for a fresh run."""
        task = await self.repository.get_task(task_id)
        session = await self.repository.get_session(task.session_id)
        if SessionState(session.state) not in TERMINAL_STATES:
            raise ConflictError(
                "only a completed, failed, terminated, or expired task can be rerun"
            )
        async with self._job_lock:
            existing = self._jobs.get(session.session_id)
            if existing and not existing.done():
                raise ConflictError("task is still running")
            self._pause_requests.discard(session.session_id)
            self._conversations.pop(session.session_id, None)
        await self.browser_pool.close_session(session.session_id)
        return await self.repository.reset_task_for_rerun(task_id)

    async def pause(self, task_id: str) -> None:
        task = await self.repository.get_task(task_id)
        session = await self.repository.get_session(task.session_id)
        if SessionState(session.state) != SessionState.RUNNING:
            raise ConflictError("only a running task can be paused")
        self._pause_requests.add(session.session_id)
        await self.repository.update_state(
            task.task_id,
            session.session_id,
            SessionState.PAUSED,
            runtime_state=session.runtime_state,
        )
        await self.browser_pool.checkpoint(session.session_id)
        await self.emit(session.session_id, "session.paused", "任务已安全暂停")

    async def terminate(self, task_id: str, reason: str = "user_request") -> None:
        task = await self.repository.get_task(task_id)
        session = await self.repository.get_session(task.session_id)
        job = self._jobs.get(session.session_id)
        if job and not job.done():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self._pause_requests.discard(session.session_id)
        await self.repository.update_state(
            task.task_id,
            session.session_id,
            SessionState.TERMINATED,
            final_result={"reason": reason},
        )
        trace_ref = await self.browser_pool.close_session(session.session_id)
        self._conversations.pop(session.session_id, None)
        await self.emit(
            session.session_id,
            "session.terminated",
            "任务已终止",
            {"reason": reason, "trace_ref": trace_ref},
        )

    async def _run(self, task_id: str, session_id: str) -> None:
        task = await self.repository.get_task(task_id)
        session = await self.repository.get_session(session_id)
        runtime_state = dict(session.runtime_state or {})
        action_count = int(runtime_state.get("action_count", 0))
        facts = list(runtime_state.get("facts", []))
        runtime_policy = task.runtime_policy
        approval_policy = ApprovalPolicy.model_validate(task.approval_policy)
        max_actions = int(runtime_policy.get("max_model_actions", 100))
        deadline = time.monotonic() + int(runtime_policy.get("max_runtime_minutes", 30)) * 60
        max_retries = int(runtime_policy.get("max_step_retries", 3))
        consecutive_failures = 0
        last_action_signature = str(runtime_state.get("last_action_signature", ""))
        repeated_action_count = int(runtime_state.get("repeated_action_count", 0))

        try:
            await self.browser_pool.ensure_session(
                session_id,
                task.start_url,
                task.allowed_domains,
            )
            conversation = self._conversations.get(session_id)
            if conversation is None:
                screenshot, screenshot_ref = await self.browser_pool.screenshot(
                    session_id, "step_000_initial.png"
                )
                conversation = [self.fara.initial_user_message(task.description, screenshot)]
                self._conversations[session_id] = conversation
                await self.repository.update_state(
                    task_id,
                    session_id,
                    SessionState.RUNNING,
                    runtime_state=runtime_state,
                    last_screenshot_ref=screenshot_ref,
                )
            else:
                screenshot, screenshot_ref = await self.browser_pool.screenshot(
                    session_id, f"step_{action_count:03d}_resume.png"
                )
                response = str(runtime_state.pop("user_response", ""))
                conversation.append(
                    self.fara.observation_message(
                        runtime_state.get("last_observation", "Execution resumed."),
                        screenshot,
                        user_response=response,
                        facts=facts,
                    )
                )
                await self.repository.update_state(
                    task_id,
                    session_id,
                    SessionState.RUNNING,
                    runtime_state=runtime_state,
                    last_screenshot_ref=screenshot_ref,
                )

            await self.emit(
                session_id,
                "session.running",
                "Fara 浏览器会话开始执行",
                {
                    "model": self.fara.settings.fara_model,
                    "viewport": [
                        self.fara.settings.browser_viewport_width,
                        self.fara.settings.browser_viewport_height,
                    ],
                },
            )

            approved_action: Optional[ComputerAction] = None
            if SessionState(session.state) == SessionState.WAITING_APPROVAL:
                approvals = await self.repository.list_approvals(session_id)
                if not approvals or approvals[0].status != ApprovalStatus.APPROVED.value:
                    raise ConflictError("approval has not been granted")
                approved_action = ComputerAction.model_validate(
                    approvals[0].pending_payload["action"]
                )

            while action_count < max_actions and time.monotonic() < deadline:
                fresh_session = await self.repository.get_session(session_id)
                fresh_state = SessionState(fresh_session.state)
                if (
                    session_id in self._pause_requests
                    or fresh_state in {SessionState.PAUSED, SessionState.HANDOFF}
                ):
                    return
                if fresh_state in TERMINAL_STATES:
                    return

                visible_text = await self.browser_pool.visible_text(session_id)
                injection = self.injection_guard.scan(visible_text)
                if injection:
                    runtime_state["security_alert"] = injection
                    await self.browser_pool.checkpoint(session_id)
                    await self.repository.update_state(
                        task_id,
                        session_id,
                        SessionState.HANDOFF,
                        runtime_state=runtime_state,
                    )
                    release_payload = await self._release_handoff_session(session_id)
                    await self.emit(
                        session_id,
                        "security.prompt_injection_detected",
                        "页面包含疑似提示注入内容，已停止自动执行并请求人工接管",
                        {"matched_text": injection, **release_payload},
                    )
                    return

                if approved_action is not None:
                    action = approved_action
                    approved_action = None
                    raw_response = "<approved_pending_action>"
                    action_was_approved = True
                else:
                    try:
                        decision = await self.fara.next_action(conversation)
                    except Exception:
                        if session_id in self._pause_requests:
                            return
                        raise
                    action = decision.action
                    raw_response = decision.raw_response
                    conversation.append({"role": "assistant", "content": raw_response})
                    action_was_approved = False

                if session_id in self._pause_requests:
                    return
                current_session = await self.repository.get_session(session_id)
                if SessionState(current_session.state) != SessionState.RUNNING:
                    return

                if action.action == "ask_user_question":
                    runtime_state.update(
                        {
                            "pending_question": action.question,
                            "last_observation": f"Fara asks: {action.question}",
                            "action_count": action_count,
                            "facts": facts,
                        }
                    )
                    await self.browser_pool.checkpoint(session_id)
                    await self.repository.rotate_resume_token(session_id)
                    await self.repository.update_state(
                        task_id,
                        session_id,
                        SessionState.WAITING_USER_INPUT,
                        runtime_state=runtime_state,
                    )
                    await self.emit(
                        session_id,
                        "session.user_input_required",
                        action.question or "Fara 需要补充信息",
                    )
                    return

                if action.action == "pause_and_memorize_fact":
                    facts.append(action.fact or "")
                    observation = f"Memorized fact: {action.fact}"
                    screenshot, screenshot_ref = await self.browser_pool.screenshot(
                        session_id, f"step_{action_count:03d}_memory.png"
                    )
                    conversation.append(
                        self.fara.observation_message(observation, screenshot, facts=facts)
                    )
                    runtime_state.update(
                        {
                            "facts": facts[-50:],
                            "last_observation": observation,
                            "action_count": action_count,
                        }
                    )
                    await self.repository.update_runtime_state(session_id, runtime_state)
                    continue

                if action.action == "terminate":
                    final = {
                        "answer": action.answer,
                        "action_count": action_count,
                        "trace_ref": None,
                    }
                    trace_ref = await self.browser_pool.close_session(session_id)
                    final["trace_ref"] = trace_ref
                    await self.repository.update_state(
                        task_id,
                        session_id,
                        SessionState.COMPLETED,
                        runtime_state=runtime_state,
                        final_result=final,
                    )
                    self._conversations.pop(session_id, None)
                    await self.emit(
                        session_id,
                        "session.completed",
                        action.answer or "任务完成",
                        final,
                    )
                    return

                action_signature = action.model_dump_json(exclude_none=True)
                next_repeated_action_count = (
                    repeated_action_count + 1
                    if action_signature == last_action_signature
                    else 1
                )
                if (
                    not action_was_approved
                    and action.action in DUPLICATE_GUARDED_ACTIONS
                    and next_repeated_action_count == 2
                ):
                    last_action_signature = action_signature
                    repeated_action_count = next_repeated_action_count
                    observation = (
                        "The exact same click action was already executed at the same "
                        "coordinate and did not advance the task. Do not repeat it. "
                        "Inspect the latest screenshot and choose a different safe action. "
                        "If the task is complete, terminate with an answer."
                    )
                    screenshot, screenshot_ref = await self.browser_pool.screenshot(
                        session_id, f"step_{action_count:03d}_duplicate.png"
                    )
                    conversation.append(
                        self.fara.observation_message(observation, screenshot, facts=facts)
                    )
                    runtime_state.update(
                        {
                            "action_count": action_count,
                            "facts": facts[-50:],
                            "last_observation": observation,
                            "last_action_signature": last_action_signature,
                            "repeated_action_count": repeated_action_count,
                        }
                    )
                    await self.repository.update_state(
                        task_id,
                        session_id,
                        SessionState.RUNNING,
                        runtime_state=runtime_state,
                        last_screenshot_ref=screenshot_ref,
                    )
                    await self.emit(
                        session_id,
                        "action.duplicate_blocked",
                        "已阻止相同坐标的重复点击，并要求模型改用其他操作",
                        {
                            "action": action.action,
                            "parameters": self._redact_action(action),
                            "repeat_count": repeated_action_count,
                            "screenshot_ref": screenshot_ref,
                        },
                    )
                    continue
                if (
                    not action_was_approved
                    and action.action in DUPLICATE_GUARDED_ACTIONS
                    and next_repeated_action_count >= 3
                ):
                    raise RuntimeError(
                        f"browser model repeated the same {action.action} action 3 times"
                    )

                if (
                    not action_was_approved
                    and action.coordinate is not None
                    and action.action
                    in {
                        "left_click",
                        "double_click",
                        "triple_click",
                        "right_click",
                    }
                ):
                    target = await self.browser_pool.describe_target(session_id, action.coordinate)
                    label = " ".join(
                        filter(
                            None,
                            [
                                target.get("text"),
                                target.get("aria"),
                                target.get("title"),
                                target.get("type"),
                            ],
                        )
                    )
                    target_risk = self.critical_policy.classify(label)
                    if target_risk and self.critical_policy.requires_approval(
                        target_risk, approval_policy
                    ):
                        approval = await self.repository.create_approval(
                            task_id=task_id,
                            session_id=session_id,
                            action_summary=f"{action.action}: {target_risk.label}",
                            risk_description=target_risk.reason,
                            pending_payload={
                                "action": action.model_dump(mode="json"),
                                "target": target,
                            },
                        )
                        runtime_state.update(
                            {
                                "pending_approval_id": approval.approval_id,
                                "last_observation": target_risk.reason,
                                "action_count": action_count,
                                "facts": facts,
                            }
                        )
                        await self.browser_pool.checkpoint(session_id)
                        await self.repository.rotate_resume_token(session_id)
                        await self.repository.update_state(
                            task_id,
                            session_id,
                            SessionState.WAITING_APPROVAL,
                            runtime_state=runtime_state,
                        )
                        await self.emit(
                            session_id,
                            "session.approval_required",
                            target_risk.reason,
                            {
                                "approval_id": approval.approval_id,
                                "action_summary": approval.action_summary,
                                "target": target,
                            },
                        )
                        return

                before_bytes, before_ref = await self.browser_pool.screenshot(
                    session_id, f"step_{action_count + 1:03d}_before.png"
                )
                del before_bytes
                try:
                    result = await self.browser_pool.execute(session_id, action)
                    consecutive_failures = 0
                except Exception as exc:
                    consecutive_failures += 1
                    if consecutive_failures > max_retries:
                        raise
                    observation = (
                        f"Action {action.action} failed with {type(exc).__name__}: {exc}. "
                        "Inspect the new screenshot and choose a safe recovery action."
                    )
                    screenshot, screenshot_ref = await self.browser_pool.screenshot(
                        session_id, f"step_{action_count + 1:03d}_error.png"
                    )
                    conversation.append(
                        self.fara.observation_message(observation, screenshot, facts=facts)
                    )
                    await self.emit(
                        session_id,
                        "action.retry",
                        observation,
                        {"retry": consecutive_failures, "action": action.action},
                    )
                    continue

                action_count += 1
                last_action_signature = action_signature
                repeated_action_count = next_repeated_action_count
                screenshot, screenshot_ref = await self.browser_pool.screenshot(
                    session_id, f"step_{action_count:03d}_after.png"
                )
                safe_parameters = self._redact_action(action)
                await self.repository.append_action(
                    task_id=task_id,
                    session_id=session_id,
                    step_no=action_count,
                    action_type=action.action,
                    parameters=safe_parameters,
                    page_url=result.url,
                    screenshot_before=before_ref,
                    screenshot_after=screenshot_ref,
                    result={
                        "success": result.success,
                        "observation": result.observation,
                        **result.data,
                    },
                    verification={"ok": result.success, "strategy": "executor_result"},
                )
                runtime_state.update(
                    {
                        "action_count": action_count,
                        "facts": facts[-50:],
                        "last_observation": result.observation,
                        "current_url": result.url,
                        "last_action_signature": last_action_signature,
                        "repeated_action_count": repeated_action_count,
                    }
                )
                next_state = (
                    SessionState.PAUSED
                    if session_id in self._pause_requests
                    else SessionState.RUNNING
                )
                await self.repository.update_state(
                    task_id,
                    session_id,
                    next_state,
                    runtime_state=runtime_state,
                    last_screenshot_ref=screenshot_ref,
                )
                await self.emit(
                    session_id,
                    "action.completed",
                    result.observation,
                    {
                        "step_no": action_count,
                        "action": action.action,
                        "parameters": safe_parameters,
                        "url": result.url,
                        "screenshot_ref": screenshot_ref,
                    },
                )
                conversation.append(
                    self.fara.observation_message(
                        result.observation,
                        screenshot,
                        facts=facts,
                    )
                )
                if next_state == SessionState.PAUSED:
                    return

            reason = (
                "max_model_actions_exceeded"
                if action_count >= max_actions
                else "max_runtime_exceeded"
            )
            raise RuntimeError(reason)
        except asyncio.CancelledError:
            return
        except PolicyViolation as exc:
            runtime_state["policy_violation"] = str(exc)
            await self.repository.update_state(
                task_id,
                session_id,
                SessionState.HANDOFF,
                runtime_state=runtime_state,
            )
            release_payload = await self._release_handoff_session(session_id)
            await self.emit(
                session_id,
                "security.policy_blocked",
                "安全策略阻止了浏览器动作，任务转人工处理",
                {"detail": str(exc), **release_payload},
            )
        except Exception as exc:
            trace_ref = await self.browser_pool.close_session(session_id)
            await self.repository.update_state(
                task_id,
                session_id,
                SessionState.FAILED,
                runtime_state=runtime_state,
                final_result={
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "trace_ref": trace_ref,
                },
            )
            self._conversations.pop(session_id, None)
            await self.emit(
                session_id,
                "session.failed",
                "任务执行失败",
                {
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "trace_ref": trace_ref,
                },
            )

    async def _release_handoff_session(self, session_id: str) -> Dict[str, Any]:
        try:
            trace_ref = await self.browser_pool.close_session(session_id)
        except Exception as exc:
            return {
                "trace_ref": None,
                "browser_close_error": f"{type(exc).__name__}: {exc}",
            }
        return {"trace_ref": trace_ref}

    @staticmethod
    def _redact_action(action: ComputerAction) -> Dict[str, Any]:
        data = action.model_dump(mode="json", exclude_none=True)
        if action.action == "type" and action.text is not None:
            data["text"] = {
                "redacted": True,
                "length": len(action.text),
                "sha256": hashlib.sha256(action.text.encode("utf-8")).hexdigest(),
            }
        return data

    async def close(self) -> None:
        for job in self._jobs.values():
            if not job.done():
                job.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs.values(), return_exceptions=True)
