import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True)

    volcengine_access_key: str = ""
    volcengine_secret_key: str = ""
    # Optional, server-only asset-account overrides by business API Key ID.
    # Whole JSON is SecretStr so diagnostics never expose its AK/SK contents.
    volcengine_asset_accounts_json: SecretStr | None = None
    multi_provider_enabled: bool = False
    provider_credential_encryption_key: SecretStr | None = None
    tos_access_key: str = ""
    tos_secret_key: str = ""
    tos_endpoint: str = "tos-cn-beijing.volces.com"
    tos_region: str = "cn-beijing"
    tos_bucket: str = ""
    tos_public_base_url: str = ""
    upload_max_bytes: int = Field(default=200 * 1024 * 1024, gt=0, le=512 * 1024 * 1024)
    ffprobe_path: str = "ffprobe"
    admin_cookie_secure: bool = True
    admin_session_idle_seconds: int = Field(default=30 * 60, ge=60, le=24 * 60 * 60)
    admin_session_absolute_seconds: int = Field(default=12 * 60 * 60, ge=300, le=7 * 24 * 60 * 60)
    admin_login_window_seconds: int = Field(default=15 * 60, ge=60, le=24 * 60 * 60)
    admin_login_lock_seconds: int = Field(default=15 * 60, ge=60, le=24 * 60 * 60)
    admin_login_max_failures: int = Field(default=5, ge=2, le=20)
    admin_argon2_time_cost: int = Field(default=3, ge=1, le=10)
    admin_argon2_memory_cost: int = Field(default=65536, ge=8192, le=262144)
    admin_argon2_parallelism: int = Field(default=4, ge=1, le=16)
    admin_totp_issuer: str = Field(default="Star Proxy", min_length=1, max_length=64)
    admin_totp_encryption_key: SecretStr | None = None
    admin_totp_valid_window: int = Field(default=1, ge=0, le=2)
    admin_backup_enabled: bool = True
    admin_backup_interval_seconds: int = Field(default=24 * 60 * 60, ge=60, le=7 * 24 * 60 * 60)
    admin_backup_retention: int = Field(default=30, ge=2, le=365)
    admin_backup_directory: Path | None = None
    system_monitor_enabled: bool = True
    system_monitor_path: Path | None = None
    system_monitor_sample_interval_seconds: int = Field(default=60, ge=10, le=3600)
    system_monitor_persist_interval_seconds: int = Field(default=5 * 60, ge=60, le=24 * 60 * 60)
    system_monitor_retention_days: int = Field(default=30, ge=1, le=365)
    # Legacy import only. After the first migration, the encrypted panel settings win.
    smtp_host: str = ""
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: SecretStr | None = None
    smtp_from_email: str = ""
    alert_email_recipients: str = ""
    smtp_security: Literal["ssl", "starttls"] = "ssl"
    smtp_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    database_path: Path = Path("./data/avatar_proxy.db")
    cors_origins: str = (
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:3001,http://127.0.0.1:3001,"
        "http://localhost:3002,http://127.0.0.1:3002"
    )
    cors_origin_regex: str = ""
    enable_api_docs: bool = False
    upstream_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    # Model execution is not an ordinary metadata request. Never let a legacy
    # UPSTREAM_TIMEOUT_SECONDS=30 deployment truncate normal generation.
    model_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    model_generation_timeout_seconds: float = Field(default=1800.0, gt=0, le=3600)
    model_stream_idle_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    model_write_timeout_seconds: float = Field(default=120.0, gt=0, le=300)
    model_image_max_inflight: int = Field(default=16, ge=1, le=128)

    @field_validator("volcengine_asset_accounts_json")
    @classmethod
    def validate_asset_accounts(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        try:
            accounts = json.loads(value.get_secret_value())
            valid = isinstance(accounts, dict) and all(
                isinstance(key_id, str) and bool(key_id.strip())
                and isinstance(account, dict)
                and {"project_name", "access_key_id", "secret_access_key"}.issubset(account)
                and set(account).issubset({"project_name", "access_key_id", "secret_access_key", "inference_channel_id"})
                and all(isinstance(field, str) and bool(field.strip()) for field in account.values())
                for key_id, account in accounts.items()
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("asset accounts require project_name/access_key_id/secret_access_key and optional inference_channel_id")
        return value

    def asset_account_for(self, key_id: str, project_name: str) -> dict[str, str] | None:
        configured = self.volcengine_asset_accounts_json
        account = json.loads(configured.get_secret_value()).get(key_id) if configured else None
        if account is not None and account["project_name"] != project_name:
            from .errors import ApiError
            raise ApiError("素材账号配置与业务 Key 的项目不一致", 503, "asset_account_scope_mismatch")
        return account

    @field_validator("system_monitor_path", mode="before")
    @classmethod
    def blank_system_monitor_path_uses_database_directory(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def effective_tos_access_key(self) -> str:
        return self.tos_access_key or self.volcengine_access_key

    @property
    def effective_tos_secret_key(self) -> str:
        return self.tos_secret_key or self.volcengine_secret_key

    @property
    def effective_system_monitor_path(self) -> Path:
        return self.system_monitor_path or self.database_path.parent

    @property
    def alert_email_recipient_list(self) -> list[str]:
        return [
            address.strip()
            for address in self.alert_email_recipients.replace(";", ",").split(",")
            if address.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
