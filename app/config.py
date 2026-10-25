from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
LOOPBACK_HOST = "127.0.0.1"


def bind_host(requested: str | None = None) -> str:
    """The studio always listens on loopback. A public bind is ignored."""
    if requested and requested not in {LOOPBACK_HOST, "localhost", "::1"}:
        import logging

        logging.getLogger("studio").warning(
            "Ignoring requested bind host %s; using %s", requested, LOOPBACK_HOST
        )
    return LOOPBACK_HOST


def _absolute(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return str(path)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_port: int = 8000
    database_path: str = "data/app.db"
    log_dir: str = "data/logs"
    backup_dir: str = "data/backups"
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_timeout: float = 120.0
    generation_attempts: int = Field(default=3, ge=1, le=5)
    start_scheduler: bool = True
    timezone: str = "Asia/Seoul"
    meta_graph_version: str = "v26.0"
    facebook_page_id: str = ""
    facebook_page_token: str = ""
    instagram_user_id: str = ""
    instagram_access_token: str = ""
    instagram_image_url: str = ""
    instagram_daily_cap: int = Field(default=50, ge=1, le=100)
    naver_cafe_access_token: str = ""
    naver_cafe_club_id: str = ""
    naver_cafe_menu_id: str = ""
    naver_cafe_daily_cap: int = Field(default=200, ge=1, le=200)

    @field_validator("database_path", "log_dir", "backup_dir")
    @classmethod
    def absolute_paths(cls, value: str) -> str:
        return _absolute(value)


def get_settings() -> Settings:
    return Settings()
