from functools import lru_cache
from pathlib import Path
from typing import List, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FARAFLOW_",
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    database_url: str = "sqlite+aiosqlite:///./data/faraflow.db"
    artifact_root: Path = Path("./artifacts")
    browser_state_root: Path = Path("./browser-state")
    code_work_root: Path = Path("./data/code-workspaces")

    enable_local_workspaces: bool = False
    workspace_allowed_roots: List[Path] = Field(default_factory=list)

    browser_headless: bool = True
    browser_channel: str = "chromium"
    browser_viewport_width: int = 1440
    browser_viewport_height: int = 900
    browser_timeout_ms: int = 30_000

    fara_base_url: str = "http://127.0.0.1:5000/v1"
    fara_api_key: str = "not-needed"
    fara_model: str = "microsoft/Fara1.5-9B"
    fara_timeout_seconds: float = 120.0
    fara_max_tokens: int = 2048
    fara_max_screenshots: int = 3
    fara_coordinate_space: int = 1000
    fara_coordinate_mode: Literal["pixel", "normalized_1000"] = "normalized_1000"

    # Leave both URL and model empty to reuse the Fara endpoint for chat.
    chat_base_url: str = ""
    chat_api_key: str = ""
    chat_model: str = ""
    chat_timeout_seconds: float = Field(default=120.0, gt=0)
    chat_max_tokens: int = Field(default=2048, ge=1)
    chat_disable_thinking: bool = False

    code_base_url: str = ""
    code_api_key: str = "not-needed"
    code_model: str = ""
    code_timeout_seconds: float = Field(default=120.0, gt=0)
    code_max_tokens: int = Field(default=4096, ge=1)
    code_max_steps: int = Field(default=50, ge=1)
    code_max_runtime_minutes: int = Field(default=20, ge=1)
    code_max_concurrent_runs: int = Field(default=1, ge=1)

    # Desktop control is opt-in and intentionally separate from browser automation.
    enable_desktop_control: bool = False
    desktop_base_url: str = ""
    desktop_api_key: str = "not-needed"
    desktop_model: str = ""
    desktop_timeout_seconds: float = Field(default=120.0, gt=0)
    desktop_max_tokens: int = Field(default=2048, ge=1)
    desktop_max_screenshots: int = Field(default=3, ge=1)
    desktop_max_steps: int = Field(default=50, ge=1)
    desktop_max_runtime_minutes: int = Field(default=15, ge=1)
    desktop_max_concurrent_runs: int = Field(default=1, ge=1)
    desktop_capture_mode: str = "window"
    desktop_allowed_apps: List[str] = Field(default_factory=list)
    desktop_require_confirmation: bool = True
    desktop_unattended_mode: bool = False

    default_allowed_domains: List[str] = Field(default_factory=lambda: ["bing.com"])
    allow_private_networks: bool = False
    max_concurrent_sessions: int = 2
    cors_origins: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    )

    def prepare_directories(self) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.browser_state_root.mkdir(parents=True, exist_ok=True)
        self.code_work_root.mkdir(parents=True, exist_ok=True)
        self.workspace_allowed_roots = [
            path.expanduser().resolve() for path in self.workspace_allowed_roots
        ]
        if self.enable_local_workspaces and not self.workspace_allowed_roots:
            raise ValueError(
                "FARAFLOW_WORKSPACE_ALLOWED_ROOTS must not be empty when "
                "local workspaces are enabled"
            )
        if self.enable_local_workspaces and self.api_host not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError(
                "FARAFLOW_API_HOST must be a loopback address when local workspaces are enabled"
            )
        if self.database_url.startswith("sqlite"):
            database_path = self.database_url.rsplit("///", 1)[-1]
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare_directories()
    return settings
