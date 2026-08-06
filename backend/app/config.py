"""Application settings loaded from environment / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SYSTEM_PROMPT = (
    "你是一个乐于助人的智能助理。你可以查询当前时间、做数学计算、查询示例天气。"
    "请用中文回答，回答尽量简洁清晰。"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM provider: "openai" | "anthropic" | "mock"
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"

    openai_api_key: str = ""
    openai_base_url: str = ""

    anthropic_api_key: str = ""

    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
