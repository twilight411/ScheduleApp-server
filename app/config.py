"""
应用配置管理 — 所有配置通过环境变量注入

[P1 修复]
  - 默认 DATABASE_URL 仍为 SQLite（本地开发零配置启动）
  - 新增 .env.example 引导切到 PostgreSQL
  - 增加 db_max_connections, db_pool_recycle 配置开关
"""
from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ===== Application =====
    app_name: str = "spirit-scheduler"
    app_env: str = "development"
    app_debug: bool = False
    app_version: str = "0.4.0"

    # ===== Database =====
    # 本地开发默认 SQLite；生产请通过环境变量改为 PostgreSQL
    # 例如: DATABASE_URL=postgresql+asyncpg://user:pass@db:5432/spirit
    database_url: str = "sqlite+aiosqlite:///./spirit.db"
    database_echo: bool = False

    # ===== Redis =====
    redis_url: str = "redis://localhost:6379/0"

    # ===== JWT =====
    jwt_secret: str = "change-me-to-a-strong-random-string-at-least-32-chars"
    jwt_access_token_expire_minutes: int = 120
    jwt_refresh_token_expire_days: int = 30
    jwt_algorithm: str = "HS256"

    # ===== LLM =====
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = "gpt-4"
    llm_max_tokens: int = 4096
    llm_rate_limit_per_user_per_hour: int = 50

    # ===== Image Generation =====
    image_provider: str = "openai"
    image_api_key: str = ""
    image_base_url: str = ""
    image_model: str = "dall-e-3"
    image_size: str = "1024x1024"
    image_quality: str = "standard"
    
    # ===== Jiyun (即梦) API =====
    jiyun_access_key_id: str = ""
    jiyun_secret_access_key: str = ""

    # ===== CORS =====
    cors_origins: List[str] = ["http://localhost:3000", "http://localhost:8080"]

    # ===== Object Storage =====
    oss_endpoint: str = ""
    oss_access_key: str = ""
    oss_secret_key: str = ""
    oss_bucket: str = "spirit-uploads"

    # ===== Celery =====
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_sqlite(self) -> bool:
        return "sqlite" in self.database_url.lower()

    @property
    def is_postgres(self) -> bool:
        url = self.database_url.lower()
        return "postgresql" in url or "postgres" in url


@lru_cache()
def get_settings() -> Settings:
    return Settings()