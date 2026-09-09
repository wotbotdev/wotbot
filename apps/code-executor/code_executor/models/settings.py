from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    idle_timeout_seconds: int = 1800
    execution_timeout_seconds: int = 300
    max_sessions: int = 50
    log_level: str = "INFO"
    artifacts_dir: str = "/tmp/code-executor-artifacts"
    # Charts and images are referenced by links that outlive the chat turn
    # or A2A task that produced them, so they keep the same retention as
    # exported files rather than an hour.
    artifacts_ttl_seconds: int = 604800
    file_artifacts_ttl_seconds: int = Field(default=604800, gt=0)
    artifact_max_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    artifact_max_files_per_execution: int = Field(default=16, gt=0)
    artifact_max_execution_bytes: int = Field(default=256 * 1024 * 1024, gt=0)

    # Security
    internal_api_key: str = ""

    # WoT runtime access for sandbox WoT client
    wot_runtime_url: str = "http://localhost:3003"
    wot_runtime_api_token: str = ""
