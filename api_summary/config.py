from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: Literal["production", "development"] = "production"
    auth_enabled: bool = True
    oidc_issuer: str = ""
    oidc_audience: str = "api-summary"
    oidc_jwks_url: str = ""
    oidc_algorithms: list[str] = ["RS256"]
    oidc_scopes: str = "summary:read summary:write"
    public_base_url: str = "http://localhost:8080"
    data_dir: Path = Path("/data")
    openai_api_key: str = ""
    openai_base_url: str = "https://llm.example.org/v1"
    openai_api_model: str = "ai-summary"
    request_timeout: float = Field(default=120, gt=0)
    async_concurrency: int = Field(default=4, ge=1, le=32)
    job_timeout: float = Field(default=7200, gt=0)
    retention_seconds: int = Field(default=86400, ge=1)
    max_file_bytes: int = Field(default=2 * 1024**3, ge=1)
    max_owner_bytes: int = Field(default=8 * 1024**3, ge=1)
    max_storage_bytes: int = Field(default=64 * 1024**3, ge=1)
    max_chunk_bytes: int = Field(default=256 * 1024, ge=1, le=1024**2)
    max_files: int = Field(default=20, ge=1)
    max_jobs_per_owner: int = Field(default=4, ge=1)
    max_queued_jobs: int = Field(default=100, ge=1)
    max_input_chars: int = Field(default=2_000_000, ge=1)
    max_pdf_pages: int = Field(default=2000, ge=1)
    max_zip_entries: int = Field(default=500, ge=1)
    max_zip_expanded_bytes: int = Field(default=1024**3, ge=1)
    max_zip_ratio: int = Field(default=200, ge=1)
    chunk_tokens: int = Field(default=6000, ge=256)
    reduce_tokens: int = Field(default=16000, ge=512)
    summary_max_tokens: int = Field(default=1000, ge=64)

    @model_validator(mode="after")
    def secure_configuration(self):
        if not self.auth_enabled and self.app_env != "development":
            raise ValueError("AUTH_ENABLED=false is only allowed with APP_ENV=development")
        if self.auth_enabled and not self.oidc_issuer:
            raise ValueError("OIDC_ISSUER is required")
        for name in ("oidc_issuer", "oidc_jwks_url", "public_base_url", "openai_base_url"):
            value = getattr(self, name)
            if not value:
                continue
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
                raise ValueError(f"Invalid {name}")
            if self.app_env == "production" and url.scheme != "https":
                raise ValueError(f"{name} must use HTTPS in production")
        if not self.oidc_algorithms or any(a.startswith("HS") or a == "none" for a in self.oidc_algorithms):
            raise ValueError("Use asymmetric JWT algorithms")
        if self.summary_max_tokens * 2 >= self.reduce_tokens:
            raise ValueError("REDUCE_TOKENS must exceed twice SUMMARY_MAX_TOKENS")
        return self
