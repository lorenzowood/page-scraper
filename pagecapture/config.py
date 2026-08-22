from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8081
    data_dir: Path = Path("/data")
    output_root: Path = Path("/output")
    api_token: str = ""

    concurrency: int = 1
    recycle_every: int = 25
    viewport_width: int = 1440
    viewport_height: int = 900
    max_height_px: int = 50_000
    stable_ms: int = 5_000
    timeout_ms: int = 30_000
    dismiss_cookies: bool = True
    video: bool = True
    video_fps: int = 5
    video_seconds: int = 30
    on_complete_hook: str = ""

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        fresh = self.data_dir / "page-scraper.sqlite3"
        legacy = self.data_dir / "pagecapture.sqlite3"
        if fresh.exists() or not legacy.exists():
            return fresh
        return legacy


settings = Settings()
