"""Application settings loaded from environment / .env."""

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Always resolve .env relative to the project root (not the process cwd).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"

# Load .env into os.environ before Settings is constructed.
# override=False keeps real env vars (Docker/K8s) authoritative.
load_dotenv(_ENV_FILE, override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str = ""
    schedule_extraction_model: str = "gpt-4o-mini"
    schedule_extraction_max_attempts: int = 3
    schedule_extraction_timeout: int = 120
    schedule_extraction_max_chars: int = 120_000

    scrape_wait_seconds: float = 2.0
    scrape_wait_until: str = "domcontentloaded"
    scrape_navigation_timeout_ms: int = 30_000

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """Drop the cache and re-read env / .env (e.g. after editing .env)."""
    get_settings.cache_clear()
    load_dotenv(_ENV_FILE, override=True)
    return get_settings()
