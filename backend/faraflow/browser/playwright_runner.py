import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from faraflow.config import Settings
from faraflow.infra.artifacts import ArtifactStore
from faraflow.model.fara_protocol import ComputerAction
from faraflow.security.policy import DomainPolicy, PolicyViolation


@dataclass
class BrowserResult:
    success: bool
    observation: str
    url: str
    data: Dict[str, Any]


@dataclass
class BrowserSession:
    context: BrowserContext
    page: Page
    domain_policy: DomainPolicy
    lock: asyncio.Lock
    step_no: int = 0


class BrowserPool:
    def __init__(self, settings: Settings, artifacts: ArtifactStore) -> None:
        self.settings = settings
        self.artifacts = artifacts
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._sessions: Dict[str, BrowserSession] = {}
        self._startup_lock = asyncio.Lock()
        self._capacity = asyncio.Semaphore(settings.max_concurrent_sessions)

    async def start(self) -> None:
        async with self._startup_lock:
            if self._browser is not None:
                return
            self._playwright = await async_playwright().start()
            launch_kwargs: Dict[str, Any] = {
                "headless": self.settings.browser_headless,
                "args": ["--disable-dev-shm-usage"],
            }
            if self.settings.browser_channel not in {"", "chromium"}:
                launch_kwargs["channel"] = self.settings.browser_channel
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)

    def _state_path(self, session_id: str) -> Path:
        if not session_id.replace("_", "").isalnum():
            raise ValueError("invalid session id")
        directory = (self.settings.browser_state_root / session_id).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "storage-state.json"

    async def ensure_session(
        self,
        session_id: str,
        start_url: str,
        allowed_domains: List[str],
    ) -> BrowserSession:
        if session_id in self._sessions:
            return self._sessions[session_id]
        await self.start()
        await self._capacity.acquire()
        assert self._browser is not None
        policy = DomainPolicy(
            allowed_domains,
            allow_private_networks=self.settings.allow_private_networks,
        )
        policy.assert_allowed(start_url)
        state_path = self._state_path(session_id)
        context_kwargs: Dict[str, Any] = {
            "viewport": {
                "width": self.settings.browser_viewport_width,
                "height": self.settings.browser_viewport_height,
            },
            "service_workers": "block",
            "accept_downloads": True,
        }
        if state_path.exists():
            context_kwargs["storage_state"] = str(state_path)
        try:
            context = await self._browser.new_context(**context_kwargs)

            async def route_handler(route: Any) -> None:
                try:
                    policy.assert_allowed(route.request.url)
                except PolicyViolation:
                    await route.abort("blockedbyclient")
                    return
                await route.continue_()

            await context.route("**/*", route_handler)
            await context.tracing.start(screenshots=True, snapshots=True, sources=False)
            page = await context.new_page()
            page.set_default_timeout(self.settings.browser_timeout_ms)
            await page.goto(start_url, wait_until="domcontentloaded")
            session = BrowserSession(
                context=context,
                page=page,
                domain_policy=policy,
                lock=asyncio.Lock(),
            )
            self._sessions[session_id] = session
            return session
        except Exception:
            self._capacity.release()
            raise

    def get_session(self, session_id: str) -> BrowserSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise LookupError(f"browser session {session_id} is not running") from exc

    def _coordinate_css(self, coordinate: Tuple[float, float]) -> Tuple[float, float]:
        """Runtime actions use Playwright CSS pixels after model adaptation."""
        x, y = coordinate
        if not (
            0 <= x <= self.settings.browser_viewport_width
            and 0 <= y <= self.settings.browser_viewport_height
        ):
            raise ValueError("runtime CSS coordinate is outside the viewport")
        return x, y

    async def screenshot(self, session_id: str, name: str) -> Tuple[bytes, str]:
        session = self.get_session(session_id)
        async with session.lock:
            screenshot = await session.page.screenshot(type="png")
        reference = self.artifacts.save_bytes(
            session_id, name, screenshot, subdirectory="screenshots"
        )
        return screenshot, reference

    async def visible_text(self, session_id: str, limit: int = 50_000) -> str:
        session = self.get_session(session_id)
        try:
            text = await session.page.locator("body").inner_text(timeout=3000)
        except Exception:
            return ""
        return text[:limit]

    async def describe_target(
        self, session_id: str, coordinate: Tuple[float, float]
    ) -> Dict[str, str]:
        session = self.get_session(session_id)
        x, y = self._coordinate_css(coordinate)
        result = await session.page.evaluate(
            """([x, y]) => {
              const element = document.elementFromPoint(x, y);
              if (!element) return {tag: "", text: "", aria: "", title: "", type: ""};
              return {
                tag: (element.tagName || "").toLowerCase(),
                text: (element.innerText || element.textContent || "").trim().slice(0, 300),
                aria: element.getAttribute("aria-label") || "",
                title: element.getAttribute("title") || "",
                type: element.getAttribute("type") || ""
              };
            }""",
            [x, y],
        )
        return {str(key): str(value) for key, value in result.items()}

    async def execute(self, session_id: str, action: ComputerAction) -> BrowserResult:
        session = self.get_session(session_id)
        async with session.lock:
            page = session.page
            name = action.action
            if name == "key":
                for key in action.keys:
                    await page.keyboard.down(key)
                for key in reversed(action.keys):
                    await page.keyboard.up(key)
                observation = f"Pressed keys: {action.keys}"
            elif name == "type":
                await page.keyboard.type(action.text or "")
                observation = f"Typed {len(action.text or '')} characters."
            elif name in {
                "mouse_move",
                "left_click",
                "double_click",
                "triple_click",
                "right_click",
                "left_click_drag",
            }:
                assert action.coordinate is not None
                x, y = self._coordinate_css(action.coordinate)
                if name == "mouse_move":
                    await page.mouse.move(x, y)
                elif name == "left_click":
                    await page.mouse.click(x, y)
                elif name == "double_click":
                    await page.mouse.dblclick(x, y)
                elif name == "triple_click":
                    await page.mouse.click(x, y, click_count=3)
                elif name == "right_click":
                    await page.mouse.click(x, y, button="right")
                else:
                    await page.mouse.down()
                    await page.mouse.move(x, y, steps=10)
                    await page.mouse.up()
                observation = f"Executed {name} at viewport coordinate ({x:.0f}, {y:.0f})."
            elif name in {"scroll", "hscroll"}:
                pixels = float(action.pixels or 0)
                if name == "scroll":
                    scaled = pixels * self.settings.browser_viewport_height / 1000
                    await page.mouse.wheel(0, -scaled)
                else:
                    scaled = pixels * self.settings.browser_viewport_width / 1000
                    await page.mouse.wheel(-scaled, 0)
                observation = f"Executed {name} by {pixels:g} model pixels."
            elif name == "visit_url":
                target = action.url or ""
                if not target.startswith(("http://", "https://", "about:")):
                    target = f"https://{target}"
                session.domain_policy.assert_allowed(target)
                await page.goto(target, wait_until="domcontentloaded")
                observation = f"Navigated to {target}."
            elif name == "history_back":
                await page.go_back(wait_until="domcontentloaded")
                observation = "Navigated back."
            elif name == "web_search":
                target = f"https://www.bing.com/search?q={quote_plus(action.query or '')}&FORM=QBLH"
                session.domain_policy.assert_allowed(target)
                await page.goto(target, wait_until="domcontentloaded")
                observation = f"Searched the web for: {action.query}"
            elif name == "read_page_answer_question":
                text = await self.visible_text(session_id, limit=20_000)
                observation = (
                    f"Question: {action.question}\nVisible page text for answering:\n{text}"
                )
            elif name == "wait":
                await asyncio.sleep(min(float(action.time or 0), 10.0))
                observation = f"Waited {min(float(action.time or 0), 10.0):g} seconds."
            else:
                raise ValueError(f"action {name} is handled by the coordinator")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            return BrowserResult(
                success=True,
                observation=observation,
                url=page.url,
                data={"title": await page.title()},
            )

    async def checkpoint(self, session_id: str) -> Path:
        session = self.get_session(session_id)
        state_path = self._state_path(session_id)
        async with session.lock:
            await session.context.storage_state(path=str(state_path))
        return state_path

    async def close_session(self, session_id: str) -> Optional[str]:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return None
        trace_path = self.artifacts.session_dir(session_id) / "trace.zip"
        trace_ref: Optional[str] = None
        try:
            await session.context.storage_state(path=str(self._state_path(session_id)))
            await session.context.tracing.stop(path=str(trace_path))
            trace_ref = f"/v1/artifacts/{session_id}/trace.zip"
        finally:
            try:
                await session.context.close()
            finally:
                self._capacity.release()
        return trace_ref

    async def close(self) -> None:
        for session_id in list(self._sessions):
            await self.close_session(session_id)
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._browser = None
        self._playwright = None
