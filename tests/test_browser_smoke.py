import os
from pathlib import Path

import pytest
from faraflow.browser.playwright_runner import BrowserPool
from faraflow.config import Settings
from faraflow.infra.artifacts import ArtifactStore
from faraflow.model.fara_protocol import ComputerAction

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1",
    reason="set RUN_BROWSER_TESTS=1 to run the installed-browser smoke test",
)


@pytest.mark.asyncio
async def test_browser_coordinate_execution(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "artifacts",
        browser_state_root=tmp_path / "browser-state",
        browser_channel=os.environ.get("FARAFLOW_TEST_BROWSER_CHANNEL", "msedge"),
        browser_headless=True,
    )
    pool = BrowserPool(settings, ArtifactStore(settings.artifact_root))
    try:
        session = await pool.ensure_session("sess_smoke", "about:blank", ["example.com"])
        await session.page.set_content(
            """
            <button id="target"
              style="position:absolute;left:100px;top:100px;width:100px;height:50px"
              onclick="document.body.dataset.clicked='yes'">Click me</button>
            """
        )
        await pool.execute(
            "sess_smoke",
            ComputerAction(action="left_click", coordinate=(104, 139)),
        )
        assert await session.page.get_attribute("body", "data-clicked") == "yes"
        _, reference = await pool.screenshot("sess_smoke", "smoke.png")
        assert reference.endswith("/screenshots/smoke.png")
    finally:
        await pool.close()
