from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunables come from env vars (WX_*) so each environment configures itself."""

    model_config = SettingsConfigDict(env_prefix="WX_", env_file=".env", extra="ignore")

    # LLM
    model: str = "claude-haiku-4-5-20251001"  # small/cheap model is enough for this task
    max_tokens: int = 500
    llm_timeout_s: float = 20.0
    llm_max_retries: int = 2  # handled by the SDK (429 / 5xx / network)

    # Agent limits
    max_steps: int = 5
    request_deadline_s: float = 30.0
    max_question_chars: int = 500

    # Weather tool
    weather_timeout_s: float = 8.0
    weather_attempts: int = 3
    cache_ttl_s: float = 600.0
    cache_max_entries: int = 1000

    # Service security
    api_keys: str = ""  # comma-separated; empty means every request is rejected
    rate_limit_per_min: int = 30

    @property
    def api_key_set(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.api_keys.split(",") if k.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
