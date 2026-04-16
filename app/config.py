from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://glacis:glacis@localhost:5432/glacis"
    redis_url: str = "redis://localhost:6379/0"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # "openai" or "mock"
    llm_mode: str = "mock"

    # Mock LLM simulation (only used when llm_mode="mock")
    # Set non-zero values in dev to simulate real LLM behaviour.
    mock_delay_min: float = 0.0   # seconds; e.g. 0.3 for dev
    mock_delay_max: float = 0.0   # seconds; e.g. 1.5 for dev
    mock_error_rate: float = 0.0  # 0.0–1.0; e.g. 0.1 to simulate 10% failures

    # Dedup TTL in seconds
    dedup_ttl: int = 3600

    # Worker: LLM retry (how many times to re-call the LLM before giving up)
    max_retries: int = 3
    job_timeout: int = 60

    # Worker: DB retry on transient OperationalError (e.g. DB restart)
    # Delay doubles each attempt: delay, 2×delay, 4×delay, …
    db_retry_attempts: int = 3
    db_retry_delay: float = 1.0  # seconds


settings = Settings()
