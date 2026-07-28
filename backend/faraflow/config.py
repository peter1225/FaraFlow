from functools import lru_cache
from pathlib import Path
from typing import List

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

    default_allowed_domains: List[str] = Field(default_factory=lambda: ["bing.com"])
    allow_private_networks: bool = False
    max_concurrent_sessions: int = 2
    cors_origins: List[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"]
    )

    def prepare_directories(self) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.browser_state_root.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite"):
            database_path = self.database_url.rsplit("///", 1)[-1]
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare_directories()
    return settings
