from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://opsai:opsai_dev@127.0.0.1:5432/opsai"
    redis_url: str = "redis://127.0.0.1:6379/0"

    dashscope_api_key: str = ""
    qwen_model: str = "qwen-plus"
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    jwt_secret: str = "local-dev-secret-change-me"
    jwt_expire_minutes: int = 120

    prometheus_url: str = "http://127.0.0.1:9090"
    elasticsearch_url: str = "http://127.0.0.1:9200"
    grafana_url: str = "http://127.0.0.1:3000"
    otel_endpoint: str = "http://127.0.0.1:4318"

    # 真实数据源未就绪时，使用带故障特征的模拟数据，保证排障链路可演示
    use_mock_datasource: bool = True

    # 演示模式：允许已登录用户在四端之间一键切换身份（生产环境应设为 false）
    demo_mode: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
