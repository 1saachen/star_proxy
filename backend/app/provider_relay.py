from __future__ import annotations

import base64
import asyncio
import binascii
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .database import Database
from .errors import ApiError
from .modeltop import MODELTOP_MODELS
from .security import ApiPrincipal
from .token_usage import token_count, token_summary
from . import vega
from . import vega_image, vega_vl
from . import seedance_draft


PROVIDERS = {
    "openai",
    "maxmodel",
    "modeltop",
    "volcengine_ark",
    "byteplus_modelark",
    "volcengine_speech",
    "aliyun_bailian",
    "minimax",
    "vega",
}
ARK_PROVIDERS = frozenset({"volcengine_ark", "byteplus_modelark", "modeltop"})
TERMINAL_TASK_STATUSES = {"succeeded", "failed", "canceled"}
ACTIVE_TASK_STATUSES = {"queued", "running"}
ALIYUN_REGIONS = {
    "cn-beijing": "cn-beijing",
    "ap-southeast-1": "ap-southeast-1",
    "ap-northeast-1": "ap-northeast-1",
    "eu-central-1": "eu-central-1",
    "us-east-1": "us-east-1",
}
ARK_IMAGE_FIELDS = {
    "prompt",
    "image",
    "size",
    "response_format",
    "output_format",
    "sequential_image_generation",
    "sequential_image_generation_options",
    "watermark",
    "optimize_prompt_options",
    "tools",
    "guidance_scale",
    "seed",
    "stream",
}


def _now_epoch() -> int:
    return int(time.time())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _channel_model(alias: str, provider: str, upstream: str, capabilities: str) -> tuple[str, dict[str, Any]]:
    if provider == "modeltop" and alias in MODELTOP_MODELS:
        model = MODELTOP_MODELS[alias]
        return model[5], dict(model[6])
    return upstream, _loaded(capabilities, {})


def _model_providers(alias: str, default: str) -> list[str]:
    return list(dict.fromkeys([default] + (["modeltop"] if alias in MODELTOP_MODELS else [])))


def _loaded(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _secret_hint(secret: str) -> str:
    stripped = secret.strip()
    if len(stripped) <= 8:
        return "****"
    return f"{stripped[:3]}****{stripped[-4:]}"


def _request_hash(operation: str, payload: dict[str, Any]) -> str:
    canonical = _json({"operation": operation, "payload": payload})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decoded_image_data_url_size(value: str, maximum: int | None) -> int:
    header, separator, encoded = value.partition(",")
    parameters = header[5:].split(";") if header.lower().startswith("data:") else []
    if (
        not separator
        or not parameters
        or not parameters[0].lower().startswith("image/")
        or "base64" not in {item.lower() for item in parameters[1:]}
        or not encoded
    ):
        raise ApiError("参考图片Data URL格式无效", 422, "image_input_invalid")

    # Reject obviously oversized payloads before decoding so an authenticated
    # caller cannot force a large temporary allocation. The exact decoded size
    # is checked again after strict Base64 validation.
    padding = min(len(encoded) - len(encoded.rstrip("=")), 2)
    estimated_size = max(0, (len(encoded) * 3) // 4 - padding)
    if maximum is not None and estimated_size > maximum:
        raise ApiError(
            "参考图片超过当前模型允许的单张大小",
            422,
            "image_input_too_large",
            details={"maxBytes": maximum, "actualBytes": estimated_size},
        )
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ApiError("参考图片Data URL格式无效", 422, "image_input_invalid") from error
    actual_size = len(decoded)
    if maximum is not None and actual_size > maximum:
        raise ApiError(
            "参考图片超过当前模型允许的单张大小",
            422,
            "image_input_too_large",
            details={"maxBytes": maximum, "actualBytes": actual_size},
        )
    return actual_size


def _contains_image_input(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("type") in {"image_url", "input_image"}:
            return True
        return any(_contains_image_input(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_image_input(item) for item in value)
    return False


def _provider_request_id(headers: httpx.Headers, data: dict[str, Any] | None = None) -> str | None:
    for name in ("x-request-id", "request-id", "x-tt-logid", "x-log-id"):
        value = headers.get(name)
        if value:
            return value[:256]
    if isinstance(data, dict):
        for key in ("request_id", "requestId", "RequestId", "id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value[:256]
    return None


class CredentialVault:
    KEY_FILE_NAME = "provider_credentials.key"

    def __init__(self, settings: Settings, database_path: Path | None = None) -> None:
        self.settings = settings
        self.key_path = Path(database_path or settings.database_path).parent / self.KEY_FILE_NAME
        self._cached_fernet: Fernet | None = None

    def _load_or_create_local_key(self) -> bytes:
        key_path = self.key_path
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.is_symlink():
            raise ApiError(
                "供应商凭证主密钥文件不能是符号链接",
                503,
                "provider_encryption_key_invalid",
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(key_path, flags, 0o600)
        except FileExistsError:
            try:
                return key_path.read_bytes().strip()
            except OSError as error:
                raise ApiError(
                    "无法读取供应商凭证主密钥文件",
                    503,
                    "provider_encryption_key_unavailable",
                ) from error
        except OSError as error:
            raise ApiError(
                "无法创建供应商凭证主密钥文件",
                503,
                "provider_encryption_key_unavailable",
            ) from error
        key = Fernet.generate_key()
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(key + b"\n")
                output.flush()
                os.fsync(output.fileno())
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                key_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return key

    def _fernet(self) -> Fernet:
        if self._cached_fernet is not None:
            return self._cached_fernet
        configured = self.settings.provider_credential_encryption_key
        try:
            key = (
                configured.get_secret_value().encode("ascii")
                if configured is not None
                else self._load_or_create_local_key()
            )
            self._cached_fernet = Fernet(key)
            return self._cached_fernet
        except (TypeError, ValueError, UnicodeError) as error:
            raise ApiError(
                "多供应商凭证加密密钥格式无效",
                503,
                "provider_encryption_key_invalid",
            ) from error

    def encrypt(self, secret: str) -> str:
        value = secret.strip()
        if len(value) < 8 or len(value) > 4096:
            raise ApiError("供应商凭证长度无效", 422, "provider_secret_invalid")
        return self._fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as error:
            raise ApiError(
                "供应商凭证无法解密，主密钥与创建渠道时不一致，请重新录入该渠道凭证",
                503,
                "provider_secret_decrypt_failed",
            ) from error


@dataclass(frozen=True)
class ModelRoute:
    alias: str
    display_name: str
    provider: str
    modality: str
    protocol: str
    capabilities: dict[str, Any]
    upstream_model: str
    channel_id: str
    channel_name: str
    channel_config: dict[str, Any]
    credential_id: str
    secret: str


class ProviderRelay:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.vault = CredentialVault(settings, database.path)
        self.transport: httpx.AsyncBaseTransport | None = None
        self._image_jobs: set[asyncio.Task] = set()

    def _model_timeout(self, *, generation: bool = False, stream: bool = False) -> httpx.Timeout:
        read = self.settings.upstream_timeout_seconds
        if generation:
            read = self.settings.model_generation_timeout_seconds
        elif stream:
            read = self.settings.model_stream_idle_timeout_seconds
        return httpx.Timeout(
            read, connect=self.settings.model_connect_timeout_seconds,
            write=self.settings.model_write_timeout_seconds, pool=10.0,
        )

    @staticmethod
    def _transport_error(error: httpx.RequestError, *, submitted: bool) -> ApiError:
        # Do not include str(error): it may contain credentials or signed URLs.
        uncertain = submitted and not isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
        phase = (
            "connect" if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)) else
            "pool" if isinstance(error, httpx.PoolTimeout) else
            "write" if isinstance(error, (httpx.WriteError, httpx.WriteTimeout)) else "read"
        )
        if isinstance(error, httpx.TimeoutException):
            code = "provider_connect_timeout" if phase == "connect" else "provider_response_timeout"
            message = "连接供应商超时" if phase == "connect" else "等待供应商响应超时"
            status = 504
        else:
            code, message, status = "provider_unreachable", "供应商连接或响应传输失败", 502
        if uncertain:
            message += "；上游可能仍在执行，请勿更换幂等键重新提交，费用需核对"
        return ApiError(message, status, code, details={
            "phase": phase, "outcomeUnknown": uncertain, "automaticRetry": False,
        })

    async def aclose(self) -> None:
        # Deployment must drain requests first. If shutdown still interrupts a
        # job, retain an ambiguous outcome rather than silently allowing a retry.
        jobs = list(self._image_jobs)
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    def recover_interrupted_images(self) -> None:
        """Single-instance restart recovery: never replay a possibly paid POST."""
        with self.database.connect() as connection:
            tasks = connection.execute(
                "SELECT * FROM inference_tasks WHERE operation='image' AND status IN ('queued','running') "
                "AND NOT (model_alias LIKE 'vega-image-%' AND upstream_task_id IS NOT NULL)"
            ).fetchall()
            for task in tasks:
                connection.execute(
                    "UPDATE inference_tasks SET status='failed',error_code='provider_outcome_unknown',"
                    "error_message='服务重启前的生图结果未确认，请联系管理员核对，勿重复提交',"
                    "metadata_json=?,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (_json({"outcomeUnknown": True}), task["id"]),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO inference_usage "
                    "(id,request_id,task_id,api_key_id,project_name,model_alias,channel_id,status) "
                    "VALUES (?,?,?,?,?,?,?,'unknown')",
                    (uuid.uuid4().hex, task["id"], task["id"], task["api_key_id"],
                     task["project_name"], task["model_alias"], task["channel_id"]),
                )

    def _image_job_done(self, job: asyncio.Task) -> None:
        self._image_jobs.discard(job)
        if not job.cancelled():
            job.exception()  # Retrieve errors even if the original client disconnected.

    def require_enabled(self) -> None:
        if not self.settings.multi_provider_enabled:
            raise ApiError("多供应商模型中转尚未启用", 503, "multi_provider_disabled")
        self.vault._fernet()

    @staticmethod
    def _validate_provider_config(provider: str, config: dict[str, Any]) -> dict[str, Any]:
        if provider not in PROVIDERS:
            raise ApiError("不支持的供应商类型", 422, "provider_not_supported")
        if not isinstance(config, dict):
            raise ApiError("渠道配置必须是对象", 422, "provider_config_invalid")
        if provider == "vega":
            return vega.channel_config(config)
        allowed: dict[str, set[str]] = {
            "openai": {"organization", "project"},
            "maxmodel": set(),
            "modeltop": set(),
            "volcengine_ark": {"projectName"},
            "byteplus_modelark": set(),
            "volcengine_speech": set(),
            "aliyun_bailian": {"workspaceId", "region"},
            "minimax": set(),
        }
        unknown = sorted(set(config) - allowed[provider])
        if unknown:
            raise ApiError(
                "渠道配置包含不允许的字段",
                422,
                "provider_config_field_forbidden",
                details={"fields": unknown},
            )
        normalized = {key: str(value).strip() for key, value in config.items() if str(value).strip()}
        if provider == "aliyun_bailian":
            workspace = normalized.get("workspaceId", "")
            if not workspace or len(workspace) > 128 or not all(ch.isalnum() or ch in "-_" for ch in workspace):
                raise ApiError("阿里百炼渠道必须填写有效的 Workspace ID", 422, "aliyun_workspace_invalid")
            region = normalized.get("region", "cn-beijing")
            if region not in ALIYUN_REGIONS:
                raise ApiError("不支持的阿里百炼地域", 422, "aliyun_region_invalid")
            normalized["region"] = region
        for value in normalized.values():
            if len(value) > 256:
                raise ApiError("渠道配置字段过长", 422, "provider_config_invalid")
        return normalized

    def _channel_view(self, row: Any) -> dict[str, Any]:
        result = dict(row)
        return {
            "id": result["id"],
            "projectName": result["project_name"],
            "name": result["name"],
            "provider": result["provider"],
            "config": _loaded(result.get("config_json"), {}),
            "status": result["status"],
            "secretHint": result.get("secret_hint"),
            "credentialId": result.get("credential_id"),
            "lastTestStatus": result.get("last_test_status"),
            "lastTestAt": result.get("last_test_at"),
            "lastTestLatencyMs": result.get("last_test_latency_ms"),
            "lastTestError": result.get("last_test_error"),
            "createdAt": result.get("created_at"),
            "updatedAt": result.get("updated_at"),
        }

    def list_channels(self, project_name: str | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT c.*, pc.id AS credential_id, pc.secret_hint
            FROM provider_channels c
            LEFT JOIN provider_credentials pc ON pc.channel_id=c.id AND pc.status='active'
            WHERE c.deleted_at IS NULL
        """
        params: tuple[Any, ...] = ()
        if project_name:
            sql += " AND c.project_name=?"
            params = (project_name,)
        sql += " ORDER BY c.created_at DESC"
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._channel_view(row) for row in rows]

    def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT c.*, pc.id AS credential_id, pc.secret_hint
                FROM provider_channels c
                LEFT JOIN provider_credentials pc ON pc.channel_id=c.id AND pc.status='active'
                WHERE c.id=? AND c.deleted_at IS NULL
                """,
                (channel_id,),
            ).fetchone()
        return self._channel_view(row) if row else None

    def create_channel(
        self,
        *,
        project_name: str,
        name: str,
        provider: str,
        config: dict[str, Any],
        secret: str,
        actor_id: str,
    ) -> dict[str, Any]:
        self.require_enabled()
        canonical_project = self.database.resolve_project_name(project_name)
        if canonical_project is None:
            raise ApiError("项目不存在", 404, "project_not_found")
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 100:
            raise ApiError("渠道名称长度无效", 422, "provider_channel_name_invalid")
        normalized_config = self._validate_provider_config(provider, config)
        if provider == "vega":
            vega.credentials(secret)
        # The customer project is already bound to the canonical Volcengine
        # ProjectName. Never trust or require a second client-supplied value.
        if provider in {"volcengine_ark", "volcengine_speech"}:
            normalized_config["projectName"] = canonical_project
        channel_id = f"pch_{uuid.uuid4().hex}"
        credential_id = f"pcr_{uuid.uuid4().hex}"
        ciphertext = self.vault.encrypt(secret)
        with self.database.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO provider_channels "
                    "(id,project_name,name,provider,config_json,created_by) VALUES (?,?,?,?,?,?)",
                    (channel_id, canonical_project, normalized_name, provider, _json(normalized_config), actor_id),
                )
                connection.execute(
                    "INSERT INTO provider_credentials "
                    "(id,channel_id,secret_ciphertext,secret_hint,created_by) VALUES (?,?,?,?,?)",
                    (credential_id, channel_id, ciphertext, "Vega凭证已加密" if provider == "vega" else _secret_hint(secret), actor_id),
                )
            except Exception as error:
                if "UNIQUE constraint" in str(error):
                    raise ApiError("项目中已存在同名渠道", 409, "provider_channel_exists") from error
                raise
        return self.get_channel(channel_id) or {}

    def rotate_channel_secret(self, channel_id: str, secret: str, actor_id: str) -> dict[str, Any]:
        self.require_enabled()
        new_id = f"pcr_{uuid.uuid4().hex}"
        with self.database.connect() as connection:
            channel = connection.execute(
                "SELECT id,provider FROM provider_channels WHERE id=? AND deleted_at IS NULL", (channel_id,)
            ).fetchone()
            if channel is None:
                raise ApiError("供应商渠道不存在", 404, "provider_channel_not_found")
            if channel["provider"] == "vega":
                try:
                    patch = json.loads(secret)
                except (ValueError, TypeError):
                    patch = None
                if not isinstance(patch, dict) or not patch or set(patch) - {"secretId", "secretKey", "apiToken"}:
                    raise ApiError("请至少提供一项有效的Vega新凭证", 422, "vega_credentials_invalid")
                active = connection.execute("SELECT secret_ciphertext FROM provider_credentials WHERE channel_id=? AND status='active'", (channel_id,)).fetchone()
                previous = vega.credentials(self.vault.decrypt(active[0])) if active else {}
                secret = _json(vega.credentials(_json({**previous, **patch})))
            ciphertext = self.vault.encrypt(secret)
            connection.execute(
                "UPDATE provider_credentials SET status='retired',retired_at=CURRENT_TIMESTAMP "
                "WHERE channel_id=? AND status='active'",
                (channel_id,),
            )
            connection.execute(
                "INSERT INTO provider_credentials "
                "(id,channel_id,secret_ciphertext,secret_hint,created_by) VALUES (?,?,?,?,?)",
                (new_id, channel_id, ciphertext, "Vega凭证已加密" if channel["provider"] == "vega" else _secret_hint(secret), actor_id),
            )
            connection.execute(
                "UPDATE provider_channels SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (channel_id,),
            )
        return self.get_channel(channel_id) or {}

    def rename_channel(self, channel_id: str, name: str) -> dict[str, Any]:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 100:
            raise ApiError("渠道名称长度无效", 422, "provider_channel_name_invalid")
        with self.database.connect() as connection:
            try:
                cursor = connection.execute(
                    "UPDATE provider_channels SET name=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND deleted_at IS NULL",
                    (normalized_name, channel_id),
                )
            except Exception as error:
                if "UNIQUE constraint" in str(error):
                    raise ApiError("项目中已存在同名渠道", 409, "provider_channel_exists") from error
                raise
            if cursor.rowcount != 1:
                raise ApiError("供应商渠道不存在", 404, "provider_channel_not_found")
        return self.get_channel(channel_id) or {}

    def set_channel_status(self, channel_id: str, enabled: bool) -> dict[str, Any]:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE provider_channels SET status=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND deleted_at IS NULL",
                ("active" if enabled else "disabled", channel_id),
            )
            if cursor.rowcount != 1:
                raise ApiError("供应商渠道不存在", 404, "provider_channel_not_found")
        return self.get_channel(channel_id) or {}

    def delete_channel(self, channel_id: str) -> None:
        with self.database.connect() as connection:
            channel = connection.execute(
                "SELECT id FROM provider_channels WHERE id=? AND deleted_at IS NULL", (channel_id,)
            ).fetchone()
            if channel is None:
                raise ApiError("供应商渠道不存在", 404, "provider_channel_not_found")
            bindings = connection.execute(
                "SELECT COUNT(*) FROM project_model_bindings WHERE channel_id=?",
                (channel_id,),
            ).fetchone()[0]
            active_tasks = connection.execute(
                "SELECT COUNT(*) FROM inference_tasks WHERE channel_id=? AND status IN ('queued','running')",
                (channel_id,),
            ).fetchone()[0]
            if bindings or active_tasks:
                raise ApiError(
                    "渠道仍有模型绑定或未完成任务，不能删除",
                    409,
                    "provider_channel_in_use",
                    details={"bindingCount": bindings, "activeTaskCount": active_tasks},
                )
            connection.execute(
                "UPDATE provider_credentials SET status='retired',retired_at=COALESCE(retired_at,CURRENT_TIMESTAMP) "
                "WHERE channel_id=? AND status='active'",
                (channel_id,),
            )
            connection.execute(
                "UPDATE provider_channels SET status='disabled',deleted_at=CURRENT_TIMESTAMP,"
                "name=name || '-deleted-' || substr(id,-8),updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (channel_id,),
            )

    def catalog(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_catalog WHERE enabled=1 ORDER BY modality,alias"
            ).fetchall()
        return [
            {
                "id": row["alias"],
                "displayName": row["display_name"],
                "provider": row["provider"],
                "modality": row["modality"],
                "protocol": row["protocol"],
                "providers": _model_providers(row["alias"], row["provider"]),
                "upstreamModel": row["upstream_model"],
                "capabilities": _loaded(row["capabilities_json"], {}),
                "enabled": bool(row["enabled"]),
            }
            for row in rows
        ]

    def project_models(self, project_name: str) -> list[dict[str, Any]]:
        canonical = self.database.resolve_project_name(project_name)
        if canonical is None:
            raise ApiError("项目不存在", 404, "project_not_found")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, b.channel_id, b.enabled AS binding_enabled,
                       c.name AS channel_name, c.status AS channel_status,c.provider AS bound_provider
                FROM model_catalog m
                LEFT JOIN project_model_bindings b
                  ON b.model_alias=m.alias AND b.project_name=?
                LEFT JOIN provider_channels c ON c.id=b.channel_id
                WHERE m.enabled=1
                ORDER BY m.modality,m.alias
                """,
                (canonical,),
            ).fetchall()
        return [
            {
                "model": row["alias"],
                "displayName": row["display_name"],
                "provider": row["bound_provider"] or row["provider"],
                "providers": _model_providers(row["alias"], row["provider"]),
                "upstreamModels": {provider: _channel_model(row["alias"],provider,row["upstream_model"],row["capabilities_json"])[0]
                                   for provider in _model_providers(row["alias"],row["provider"])},
                "modality": row["modality"],
                "channelId": row["channel_id"],
                "channelName": row["channel_name"],
                "channelStatus": row["channel_status"],
                "upstreamModel": _channel_model(row["alias"], row["bound_provider"] or row["provider"],row["upstream_model"],row["capabilities_json"])[0],
                "enabled": bool(row["binding_enabled"]) if row["binding_enabled"] is not None else False,
            }
            for row in rows
        ]

    def set_project_models(
        self, project_name: str, bindings: list[dict[str, Any]], actor_id: str
    ) -> list[dict[str, Any]]:
        canonical = self.database.resolve_project_name(project_name)
        if canonical is None:
            raise ApiError("项目不存在", 404, "project_not_found")
        seen: set[str] = set()
        with self.database.connect() as connection:
            for item in bindings:
                alias = str(item.get("model") or "").strip()
                channel_id = str(item.get("channelId") or "").strip()
                enabled = bool(item.get("enabled", True))
                if not alias or alias in seen:
                    raise ApiError("模型绑定存在空值或重复项", 422, "model_binding_invalid")
                seen.add(alias)
                model = connection.execute(
                    "SELECT provider,upstream_model FROM model_catalog WHERE alias=? AND enabled=1",
                    (alias,),
                ).fetchone()
                channel = connection.execute(
                    "SELECT project_name,provider FROM provider_channels "
                    "WHERE id=? AND deleted_at IS NULL", (channel_id,)
                ).fetchone()
                if model is None or channel is None:
                    raise ApiError("模型或渠道不存在", 404, "model_or_channel_not_found")
                if channel["project_name"] != canonical:
                    raise ApiError("不能绑定其他项目的渠道", 403, "cross_project_channel_forbidden")
                if channel["provider"] not in _model_providers(alias, model["provider"]):
                    raise ApiError("模型与渠道供应商不匹配", 422, "model_provider_mismatch")
                upstream_model = str(_channel_model(alias,channel["provider"],model["upstream_model"],"{}")[0] or "").strip()
                if not upstream_model or len(upstream_model) > 256:
                    raise ApiError("模型目录中的上游模型ID无效", 500, "model_catalog_invalid")
                connection.execute(
                    "INSERT INTO project_model_bindings "
                    "(project_name,model_alias,channel_id,upstream_model,enabled,updated_by) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(project_name,model_alias) DO UPDATE SET "
                    "channel_id=excluded.channel_id,upstream_model=excluded.upstream_model,"
                    "enabled=excluded.enabled,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP",
                    (canonical, alias, channel_id, upstream_model, int(enabled), actor_id),
                )
            if seen:
                placeholders = ",".join("?" for _ in seen)
                connection.execute(
                    f"DELETE FROM project_model_bindings WHERE project_name=? AND model_alias NOT IN ({placeholders})",
                    (canonical, *sorted(seen)),
                )
            else:
                connection.execute("DELETE FROM project_model_bindings WHERE project_name=?", (canonical,))
        return self.project_models(canonical)

    def resolve(self, principal: ApiPrincipal, alias: str) -> ModelRoute:
        self.require_enabled()
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT m.alias,m.display_name,c.provider,m.modality,m.protocol,m.capabilities_json,
                       m.upstream_model,c.id AS channel_id,c.name AS channel_name,c.config_json,
                       pc.id AS credential_id,pc.secret_ciphertext
                FROM model_catalog m
                JOIN project_model_bindings b
                  ON b.model_alias=m.alias AND b.project_name=? AND b.enabled=1
                JOIN provider_channels c
                  ON c.id=b.channel_id AND c.project_name=? AND c.status='active'
                JOIN provider_credentials pc ON pc.channel_id=c.id AND pc.status='active'
                WHERE m.alias=? AND m.enabled=1
                """,
                (principal.project_name, principal.project_name, alias),
            ).fetchone()
            if row is not None and row["provider"] == "volcengine_ark":
                account = self.settings.asset_account_for(principal.id, principal.project_name)
                channel_id = account.get("inference_channel_id") if account else None
                if channel_id:
                    override = connection.execute(
                        "SELECT c.provider,c.id AS channel_id,c.name AS channel_name,c.config_json,"
                        "pc.id AS credential_id,pc.secret_ciphertext FROM provider_channels c "
                        "JOIN provider_credentials pc ON pc.channel_id=c.id AND pc.status='active' "
                        "WHERE c.id=? AND c.project_name=? AND c.provider='volcengine_ark' "
                        "AND c.status='active' AND c.deleted_at IS NULL",
                        (channel_id, principal.project_name),
                    ).fetchone()
                    if override is None:
                        raise ApiError("当前 Key 的火山账号渠道不可用", 503, "account_channel_unavailable")
                    # Default project bindings and other Keys are untouched.
                    # Tasks persist this exact channel/credential; _task_route
                    # deliberately keeps using the stored pair after a switch.
                    row = {**dict(row), **dict(override)}
        if row is None:
            raise ApiError("当前项目未启用该模型或渠道不可用", 403, "model_not_allowed")
        secret = self.vault.decrypt(row["secret_ciphertext"])
        if row["provider"] == "vega":
            (vega.api_token if row["modality"] == "text" else vega.tc3_credentials)(secret)
        return ModelRoute(
            alias=row["alias"],
            display_name=row["display_name"],
            provider=row["provider"],
            modality=row["modality"],
            protocol=row["protocol"],
            capabilities=_channel_model(alias,row["provider"],row["upstream_model"],row["capabilities_json"])[1],
            upstream_model=_channel_model(alias,row["provider"],row["upstream_model"],row["capabilities_json"])[0],
            channel_id=row["channel_id"],
            channel_name=row["channel_name"],
            channel_config=_loaded(row["config_json"], {}),
            credential_id=row["credential_id"],
            secret=secret,
        )

    def available_models(self, principal: ApiPrincipal) -> list[dict[str, Any]]:
        self.require_enabled()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT m.alias,m.display_name,m.modality,m.capabilities_json,m.upstream_model,c.provider,c.config_json
                FROM model_catalog m
                JOIN project_model_bindings b
                  ON b.model_alias=m.alias AND b.project_name=? AND b.enabled=1
                JOIN provider_channels c
                  ON c.id=b.channel_id AND c.project_name=? AND c.status='active'
                JOIN provider_credentials pc ON pc.channel_id=c.id AND pc.status='active'
                WHERE m.enabled=1
                ORDER BY m.alias
                """,
                (principal.project_name, principal.project_name),
            ).fetchall()
        return [
            {
                "id": row["alias"],
                "object": "model",
                "created": 0,
                "owned_by": "richbest",
                "display_name": row["display_name"],
                "modality": row["modality"],
                "provider": row["provider"],
                "capabilities": {**_channel_model(row["alias"],row["provider"],row["upstream_model"],row["capabilities_json"])[1],
                                 **({"billingSite": _loaded(row["config_json"], {}).get("site")} if row["provider"] == "vega" else {})},
            }
            for row in rows
        ]

    def validate_text_operation(
        self, principal: ApiPrincipal, alias: str, operation: str, *, stream: bool
    ) -> ModelRoute:
        route = self.resolve(principal, alias)
        if route.modality != "text":
            raise ApiError("该模型不支持文本接口", 422, "model_modality_mismatch")
        if not route.capabilities.get(operation):
            raise ApiError(
                "该模型不支持当前文本接口",
                422,
                "model_operation_unsupported",
                details={"operation": operation},
            )
        if stream and not route.capabilities.get("stream"):
            raise ApiError("该模型不支持流式输出", 422, "model_stream_unsupported")
        return route

    def _base_url(self, route: ModelRoute) -> str:
        if route.provider == "vega" and route.modality == "text":
            return vega_vl.BASE_URL
        if route.provider == "modeltop":
            return "https://modeltop.ai/api/v3" if route.modality == "video" else "https://modeltop.ai/v1"
        if route.provider == "openai":
            return "https://api.openai.com/v1"
        if route.provider == "maxmodel":
            return "https://aiapi.maxmaas.com/v1"
        if route.provider == "volcengine_ark":
            return "https://ark.cn-beijing.volces.com/api/v3"
        if route.provider == "byteplus_modelark":
            return "https://ark.ap-southeast.bytepluses.com/api/v3"
        if route.provider == "volcengine_speech":
            return "https://openspeech.bytedance.com"
        if route.provider == "minimax":
            return "https://api.minimax.cn"
        if route.provider == "aliyun_bailian":
            workspace = route.channel_config.get("workspaceId", "")
            region = route.channel_config.get("region", "cn-beijing")
            if not workspace or region not in ALIYUN_REGIONS:
                raise ApiError("阿里百炼渠道配置不完整", 503, "aliyun_channel_invalid")
            return f"https://{workspace}.{region}.maas.aliyuncs.com/api/v1"
        raise ApiError("供应商适配器不存在", 503, "provider_adapter_missing")

    @staticmethod
    def _headers(route: ModelRoute) -> dict[str, str]:
        if route.provider == "vega" and route.modality == "text":
            return {"authorization": f"Bearer {vega.api_token(route.secret)}", "content-type": "application/json"}
        if route.provider == "volcengine_speech":
            return {"x-api-key": route.secret, "content-type": "application/json"}
        headers = {"authorization": f"Bearer {route.secret}", "content-type": "application/json"}
        if route.provider == "openai":
            organization = route.channel_config.get("organization")
            project = route.channel_config.get("project")
            if organization:
                headers["openai-organization"] = organization
            if project:
                headers["openai-project"] = project
        if route.provider == "aliyun_bailian":
            headers["x-dashscope-async"] = "enable"
        return headers

    async def _request(
        self, route: ModelRoute, method: str, path: str, payload: dict[str, Any] | None = None,
        *, extra_headers: dict[str, str] | None = None,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        if route.provider == "vega" and route.modality != "text":
            return await self._vega_request(route, path.removeprefix("/"), payload or {})
        try:
            async with httpx.AsyncClient(
                timeout=self._model_timeout(generation=method == "POST" and (
                    route.modality in {"text", "image", "audio"} or path == "/contents/generations/tasks"
                )),
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method,
                    f"{self._base_url(route)}{path}",
                    headers={**self._headers(route), **(extra_headers or {})},
                    json=payload,
                )
        except httpx.RequestError as error:
            raise self._transport_error(error, submitted=method == "POST") from error
        try:
            data = response.json()
        except ValueError:
            data = {}
            if response.status_code < 400 and response.content:
                raise ApiError("模型供应商返回了无效响应", 502, "provider_response_invalid")
        if response.status_code >= 400:
            message = "模型供应商拒绝了请求"
            if isinstance(data, dict):
                error_data = data.get("error")
                if isinstance(error_data, dict) and isinstance(error_data.get("message"), str):
                    message = error_data["message"][:500]
                elif isinstance(data.get("message"), str):
                    message = data["message"][:500]
                elif isinstance(data.get("msg"), str) and data["msg"]:
                    message = data["msg"][:500]
            if route.provider == "vega":
                for secret in vega.credentials(route.secret).values():
                    message = message.replace(secret, "[REDACTED]")
            raise ApiError(
                message,
                response.status_code if 400 <= response.status_code < 600 else 502,
                "provider_request_failed",
                details={"upstreamRequestId": _provider_request_id(response.headers, data)},
            )
        if not isinstance(data, dict):
            raise ApiError("模型供应商返回了无效响应", 502, "provider_response_invalid")
        return response, data

    async def _vega_request(self, route: ModelRoute, action: str, payload: dict[str, Any]):
        url, headers, body = vega.signed_request(route.secret, route.channel_config, action, payload, _now_epoch())
        try:
            async with httpx.AsyncClient(timeout=self._model_timeout(), transport=self.transport) as client:
                response = await client.post(url, headers=headers, content=body)
        except httpx.RequestError as error:
            raise self._transport_error(error, submitted=action.startswith("Create")) from error
        try:
            data = response.json().get("Response")
        except (ValueError, AttributeError):
            data = None
        if not isinstance(data, dict):
            raise ApiError("Vega供应商返回了无效响应", 502, "provider_response_invalid")
        error = data.get("Error")
        if response.status_code >= 400 or isinstance(error, dict):
            error = error if isinstance(error, dict) else {}
            message = str(error.get("Message") or "Vega供应商拒绝了请求")[:500]
            for secret in vega.credentials(route.secret).values():
                message = message.replace(secret, "[REDACTED]")
            code = str(error.get("Code") or "UpstreamError")
            status = 429 if code.startswith("LimitExceeded") or code.startswith("RequestLimitExceeded") else 502
            raise ApiError(message, status, "vega_request_failed", details={
                "upstreamCode": code, "upstreamRequestId": data.get("RequestId"), "automaticRetry": False})
        return response, data

    async def test_channel(self, channel_id: str) -> dict[str, Any]:
        self.require_enabled()
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT c.*,pc.id AS credential_id,pc.secret_ciphertext
                FROM provider_channels c JOIN provider_credentials pc
                  ON pc.channel_id=c.id AND pc.status='active'
                WHERE c.id=? AND c.deleted_at IS NULL
                """,
                (channel_id,),
            ).fetchone()
        if row is None:
            raise ApiError("供应商渠道不存在", 404, "provider_channel_not_found")
        route = ModelRoute(
            alias="channel-test",
            display_name="channel-test",
            provider=row["provider"],
            modality="text",
            protocol="test",
            capabilities={},
            upstream_model="",
            channel_id=row["id"],
            channel_name=row["name"],
            channel_config=_loaded(row["config_json"], {}),
            credential_id=row["credential_id"],
            secret=self.vault.decrypt(row["secret_ciphertext"]),
        )
        if route.provider in {"volcengine_speech", "maxmodel", "byteplus_modelark", "modeltop", "vega"}:
            provider_name = {
                "volcengine_speech": "豆包语音", "maxmodel": "MaxModel",
                "byteplus_modelark": "BytePlus ModelArk",
                "modeltop": "ModelTop",
                "vega": "Vega（腾讯云点播）",
            }[route.provider]
            message = f"{provider_name}没有可靠的免费凭证探测接口，请绑定模型后从用户端发起一次真实测试"
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE provider_channels SET last_test_status='manual',last_test_at=CURRENT_TIMESTAMP,"
                    "last_test_latency_ms=NULL,last_test_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (message, channel_id),
                )
            return {"status": "manual", "latencyMs": None, "message": message}
        path = "/v1/models" if route.provider == "minimax" else "/models"
        started = time.monotonic()
        status = "success"
        error_message: str | None = None
        try:
            await self._request(route, "GET", path)
        except ApiError as error:
            status = "failed"
            error_message = error.message[:500]
        latency = round((time.monotonic() - started) * 1000)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE provider_channels SET last_test_status=?,last_test_at=CURRENT_TIMESTAMP,"
                "last_test_latency_ms=?,last_test_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, latency, error_message, channel_id),
            )
        return {"status": status, "latencyMs": latency, "message": error_message}

    def _record_usage(
        self,
        *,
        request_id: str,
        principal: ApiPrincipal,
        route: ModelRoute,
        status: str,
        task_id: str | None = None,
        provider_request_id: str | None = None,
        usage: dict[str, Any] | None = None,
        generated_images: int | None = None,
        video_seconds: float | None = None,
        width: int | None = None,
        height: int | None = None,
        input_characters: int | None = None,
        audio_seconds: float | None = None,
        usage_dimension: str | None = None,
    ) -> None:
        usage = usage or {}
        if route.provider == "vega" and route.modality == "text":
            usage = vega_vl.normalized_usage(usage)
            usage_dimension = route.channel_config["site"]
        input_tokens = token_count(usage.get("prompt_tokens", usage.get("input_tokens")))
        output_tokens = token_count(usage.get("completion_tokens", usage.get("output_tokens")))
        total_tokens = token_count(usage.get("total_tokens"))
        token_details = usage.get("prompt_tokens_details")
        if not isinstance(token_details, dict):
            token_details = usage.get("input_tokens_details")
        token_details = token_details if isinstance(token_details, dict) else {}
        cached_input_tokens = token_count(token_details.get(
            "cached_tokens", usage.get("cached_input_tokens")
        ))
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO inference_usage
                (id,request_id,task_id,api_key_id,project_name,model_alias,channel_id,
                 provider_request_id,status,input_tokens,cached_input_tokens,output_tokens,total_tokens,usage_dimension,
                 generated_images,video_seconds,video_width,video_height,input_characters,
                 audio_seconds,settled_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                """,
                (
                    f"ius_{uuid.uuid4().hex}", request_id, task_id, principal.id,
                    principal.project_name, route.alias, route.channel_id,
                    provider_request_id, status,
                    input_tokens, cached_input_tokens, output_tokens, total_tokens,
                    usage_dimension, generated_images, video_seconds, width, height,
                    input_characters, audio_seconds,
                ),
            )

    def _speech_headers(
        self, route: ModelRoute, *, resource_id: str | None, request_id: str, require_usage: bool = False
    ) -> dict[str, str]:
        headers = self._headers(route)
        headers.update({
            "x-api-request-id": request_id,
            "x-api-sequence": "-1",
        })
        if resource_id:
            headers["x-api-resource-id"] = resource_id
        if require_usage:
            headers["x-control-require-usage-tokens-return"] = "*"
        return headers

    async def _speech_request(
        self,
        route: ModelRoute,
        path: str,
        payload: dict[str, Any],
        *,
        request_id: str,
        resource_id: str | None,
        require_usage: bool = False,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        try:
            async with httpx.AsyncClient(
                timeout=self._model_timeout(generation=not path.endswith("/query")),
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"{self._base_url(route)}{path}",
                    headers=self._speech_headers(
                        route,
                        resource_id=resource_id,
                        request_id=request_id,
                        require_usage=require_usage,
                    ),
                    json=payload,
                )
        except httpx.RequestError as error:
            raise self._transport_error(error, submitted=True) from error
        status_code = response.headers.get("x-api-status-code")
        status_ok = not status_code or status_code == "0" or status_code.startswith("2000000")
        if response.status_code >= 400 or not status_ok:
            message = response.headers.get("x-api-message")
            if not message:
                try:
                    error_body = response.json()
                except ValueError:
                    error_body = {}
                if isinstance(error_body, dict):
                    message = error_body.get("message") or error_body.get("error")
            message = str(message or "豆包语音服务拒绝了请求")
            raise ApiError(
                message[:500],
                response.status_code if response.status_code >= 400 else 502,
                "provider_request_failed",
                details={"upstreamRequestId": response.headers.get("x-tt-logid") or request_id},
            )
        try:
            data = response.json() if response.content else {}
        except ValueError as error:
            raise ApiError("豆包语音服务返回了无效响应", 502, "provider_response_invalid") from error
        if not isinstance(data, dict):
            raise ApiError("豆包语音服务返回了无效响应", 502, "provider_response_invalid")
        return response, data

    @staticmethod
    def _embedding_usage_dimension(payload: dict[str, Any]) -> str | None:
        kinds: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                kind = value.get("type")
                if isinstance(kind, str):
                    if kind == "text":
                        kinds.add("text")
                    elif kind in {"image", "image_url", "input_image"}:
                        kinds.add("image")
                    elif kind in {"video", "video_url", "input_video"}:
                        kinds.add("video")
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str) and not kinds:
                kinds.add("text")

        visit(payload.get("input"))
        return next(iter(kinds)) if len(kinds) == 1 else None

    async def embeddings(
        self, principal: ApiPrincipal, alias: str, payload: dict[str, Any], *, multimodal: bool
    ) -> tuple[dict[str, Any], str]:
        route = self.resolve(principal, alias)
        if route.modality != "embedding" or not route.capabilities.get("embeddings"):
            raise ApiError("该模型不支持向量化接口", 422, "model_modality_mismatch")
        if multimodal and not route.capabilities.get("multimodal"):
            raise ApiError("该模型不支持多模态向量化", 422, "model_operation_unsupported")
        upstream = dict(payload)
        upstream["model"] = route.upstream_model
        # Ark's current embedding-vision release accepts text through the
        # multimodal endpoint. Preserve an OpenAI-compatible text endpoint by
        # adapting a single text input and normalising the provider response.
        adapt_vision_text = not multimodal and route.upstream_model.startswith(
            "doubao-embedding-vision-"
        )
        if adapt_vision_text:
            input_value = upstream.get("input")
            if isinstance(input_value, list):
                if len(input_value) != 1 or not isinstance(input_value[0], str):
                    raise ApiError(
                        "当前模型的文本向量化一次仅支持一个字符串",
                        422,
                        "embedding_batch_unsupported",
                    )
                input_value = input_value[0]
            if not isinstance(input_value, str):
                raise ApiError(
                    "当前模型的文本向量化仅支持字符串输入",
                    422,
                    "embedding_input_invalid",
                )
            upstream["input"] = [{"type": "text", "text": input_value}]
            upstream.pop("sparse_embedding", None)
        response, data = await self._request(
            route,
            "POST",
            "/embeddings/multimodal" if multimodal or adapt_vision_text else "/embeddings",
            upstream,
        )
        if adapt_vision_text:
            provider_data = data.get("data")
            vector = provider_data.get("embedding") if isinstance(provider_data, dict) else None
            if not isinstance(vector, list):
                raise ApiError("模型供应商返回了无效向量", 502, "provider_response_invalid")
            data = {
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": vector}],
                "model": route.alias,
                "usage": data.get("usage") if isinstance(data.get("usage"), dict) else {},
            }
        else:
            data["model"] = route.alias
        request_id = _provider_request_id(response.headers, data) or f"req_{uuid.uuid4().hex}"
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        self._record_usage(
            request_id=request_id,
            principal=principal,
            route=route,
            status="succeeded",
            provider_request_id=_provider_request_id(response.headers, data),
            usage=usage,
            usage_dimension="text" if adapt_vision_text else self._embedding_usage_dimension(payload),
        )
        return data, request_id

    async def synthesize_speech(
        self, principal: ApiPrincipal, alias: str, payload: dict[str, Any]
    ) -> tuple[bytes, str, str]:
        route = self.resolve(principal, alias)
        if route.modality != "audio" or route.protocol != "speech_tts":
            raise ApiError("该模型不支持语音合成接口", 422, "model_modality_mismatch")
        text = str(payload.get("input") or "").strip()
        voice = str(payload.get("voice") or "").strip()
        if not text or len(text) > 10000:
            raise ApiError("input不能为空或过长", 422, "audio_input_invalid")
        if not voice or len(voice) > 128:
            raise ApiError("voice必须填写TTS 2.0音色ID", 422, "audio_voice_invalid")
        output_format = str(payload.get("response_format") or "mp3")
        if output_format not in {"mp3", "pcm", "ogg_opus"}:
            raise ApiError("response_format仅支持mp3、pcm或ogg_opus", 422, "audio_format_invalid")
        sample_rate = payload.get("sample_rate", 24000)
        speed = payload.get("speed", 0)
        if not isinstance(sample_rate, int) or isinstance(sample_rate, bool) or sample_rate not in {8000, 16000, 22050, 24000, 32000, 44100, 48000}:
            raise ApiError("sample_rate不受支持", 422, "audio_sample_rate_invalid")
        if not isinstance(speed, int) or isinstance(speed, bool) or not -50 <= speed <= 100:
            raise ApiError("speed必须是-50到100的整数", 422, "audio_speed_invalid")
        request_id = str(uuid.uuid4())
        body = {
            "user": {"uid": principal.id},
            "req_params": {
                "text": text,
                "speaker": voice,
                "sample_rate": sample_rate,
                "audio_params": {
                    "format": output_format,
                    "speech_rate": speed,
                },
                "additions": _json({"disable_markdown_filter": False}),
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self._model_timeout(generation=True), transport=self.transport) as client:
                response = await client.post(
                    f"{self._base_url(route)}/api/v3/tts/unidirectional/sse",
                    headers=self._speech_headers(
                        route, resource_id=route.upstream_model, request_id=request_id, require_usage=True
                    ),
                    json=body,
                )
        except httpx.RequestError as error:
            raise self._transport_error(error, submitted=True) from error
        if response.status_code >= 400:
            raise ApiError(
                response.headers.get("x-api-message") or "豆包语音服务拒绝了请求",
                response.status_code,
                "provider_request_failed",
            )
        chunks: list[bytes] = []
        provider_request_id = response.headers.get("x-tt-logid") or request_id
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            code = event.get("code")
            if code not in {None, 0, 20000000}:
                raise ApiError(str(event.get("message") or "语音合成失败")[:500], 502, "provider_request_failed")
            encoded = event.get("data")
            if isinstance(encoded, str) and encoded:
                try:
                    chunks.append(base64.b64decode(encoded, validate=True))
                except (ValueError, binascii.Error) as error:
                    raise ApiError("语音数据格式无效", 502, "provider_response_invalid") from error
        if not chunks:
            raise ApiError("语音服务未返回音频数据", 502, "provider_response_invalid")
        self._record_usage(
            request_id=f"aud_{uuid.uuid4().hex}", principal=principal, route=route,
            status="succeeded", provider_request_id=provider_request_id,
            input_characters=len(text),
        )
        media_types = {"mp3": "audio/mpeg", "pcm": "audio/L16", "ogg_opus": "audio/ogg"}
        return b"".join(chunks), media_types[output_format], provider_request_id

    async def generate_audio(
        self, principal: ApiPrincipal, alias: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        route = self.resolve(principal, alias)
        if route.modality != "audio" or route.protocol != "speech_audio_generation":
            raise ApiError("该模型不支持音频生成接口", 422, "model_modality_mismatch")
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt or len(prompt) > 4000:
            raise ApiError("prompt不能为空或过长", 422, "audio_prompt_invalid")
        request_id = str(uuid.uuid4())
        upstream = {key: value for key, value in payload.items() if key in {
            "speaker", "audio_url", "audio_data", "image_url", "image_data", "duration", "format"
        }}
        upstream["text_prompt"] = prompt
        upstream["model"] = route.upstream_model
        response, data = await self._speech_request(
            route, "/api/v3/tts/create", upstream,
            request_id=request_id, resource_id=None,
        )
        content = data.get("data") if isinstance(data.get("data"), dict) else data
        duration = content.get("original_duration") if isinstance(content, dict) else None
        try:
            seconds = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            seconds = None
        provider_id = response.headers.get("x-tt-logid") or request_id
        self._record_usage(
            request_id=f"aud_{uuid.uuid4().hex}", principal=principal, route=route,
            status="succeeded", provider_request_id=provider_id, audio_seconds=seconds,
        )
        item = content if isinstance(content, dict) else {}
        return {
            "created": _now_epoch(), "model": route.alias,
            "data": [{key: item[key] for key in ("url", "audio", "duration", "original_duration", "subtitle") if item.get(key) is not None}],
        }, provider_id

    async def create_transcription(
        self,
        principal: ApiPrincipal,
        alias: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        route = self.resolve(principal, alias)
        if route.modality != "audio" or route.protocol != "speech_asr":
            raise ApiError("该模型不支持录音文件识别接口", 422, "model_modality_mismatch")
        audio_url = str(payload.get("url") or "").strip()
        if not audio_url.startswith("https://") or len(audio_url) > 2048:
            raise ApiError("url必须是公网可访问的HTTPS音频地址", 422, "audio_url_invalid")
        normalized = {
            "model": alias,
            "url": audio_url,
            "language": str(payload.get("language") or "").strip() or None,
            "enable_speaker_info": bool(payload.get("enable_speaker_info", False)),
        }
        task, created = self._create_task(
            principal, route, "transcription", normalized, idempotency_key
        )
        if not created:
            return self._transcription_public(task)
        upstream_id = str(uuid.uuid4())
        request_body: dict[str, Any] = {
            "user": {"uid": principal.id},
            "audio": {"url": audio_url},
            "request": {
                "model_name": "bigmodel",
                "enable_punc": True,
                "enable_itn": True,
                "enable_speaker_info": normalized["enable_speaker_info"],
            },
        }
        if normalized["language"]:
            request_body["request"]["language"] = normalized["language"]
        try:
            response, _ = await self._speech_request(
                route,
                "/api/v3/auc/bigmodel/submit",
                request_body,
                request_id=upstream_id,
                resource_id=route.upstream_model,
            )
        except Exception:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE inference_tasks SET status='failed',progress=100,error_code='provider_submit_failed',"
                    "error_message='录音文件识别任务提交失败',completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (task["id"],),
                )
            raise
        provider_id = response.headers.get("x-tt-logid") or upstream_id
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE inference_tasks SET upstream_task_id=?,status='queued',provider_request_id=?,"
                "metadata_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (upstream_id, provider_id, _json({"audio_url": audio_url}), task["id"]),
            )
        return self._transcription_public(self.get_local_task(principal, task["id"]))

    @staticmethod
    def _transcription_public(task: dict[str, Any]) -> dict[str, Any]:
        metadata = _loaded(task.get("metadata_json"), {})
        result: dict[str, Any] = {
            "id": task["id"],
            "object": "audio.transcription",
            "model": task.get("model_alias"),
            "status": task.get("status", "queued"),
            "created_at": task.get("created_at"),
        }
        if isinstance(metadata.get("text"), str):
            result["text"] = metadata["text"]
        if isinstance(metadata.get("duration"), (int, float)):
            result["duration"] = metadata["duration"]
        if task.get("error_message"):
            result["error"] = {"code": task.get("error_code"), "message": task["error_message"]}
        return result

    async def refresh_transcription(
        self, principal: ApiPrincipal, task_id: str
    ) -> dict[str, Any]:
        task = self.get_local_task(principal, task_id)
        if task.get("operation") != "transcription":
            raise ApiError("录音识别任务不存在", 404, "transcription_task_not_found")
        if task["status"] in TERMINAL_TASK_STATUSES or not task.get("upstream_task_id"):
            return self._transcription_public(task)
        route = self._task_route(task)
        response, data = await self._speech_request(
            route,
            "/api/v3/auc/bigmodel/query",
            {},
            request_id=task["upstream_task_id"],
            resource_id=route.upstream_model,
        )
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        text = result.get("text") if isinstance(result.get("text"), str) else None
        audio_info = data.get("audio_info") if isinstance(data.get("audio_info"), dict) else {}
        duration_ms = audio_info.get("duration")
        try:
            seconds = float(duration_ms) / 1000 if duration_ms is not None else None
        except (TypeError, ValueError):
            seconds = None
        header_status = response.headers.get("x-api-status-code")
        message = response.headers.get("x-api-message") or ""
        complete = bool(text is not None or result) and header_status == "20000000"
        running = header_status in {"20000001", "20000002"} or "processing" in message.lower()
        status = "succeeded" if complete else "running" if running or header_status == "20000000" else "failed"
        metadata = {"text": text, "duration": seconds}
        provider_id = response.headers.get("x-tt-logid") or task.get("provider_request_id")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE inference_tasks SET status=?,progress=?,metadata_json=?,provider_request_id=?,"
                "error_code=?,error_message=?,completed_at=CASE WHEN ? IN ('succeeded','failed') "
                "THEN COALESCE(completed_at,CURRENT_TIMESTAMP) ELSE completed_at END,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (
                    status, 100 if status in TERMINAL_TASK_STATUSES else 50, _json(metadata), provider_id,
                    "provider_task_failed" if status == "failed" else None,
                    message[:500] if status == "failed" else None,
                    status, task_id,
                ),
            )
        if status == "succeeded":
            self._record_usage(
                request_id=task_id, task_id=task_id, principal=principal, route=route,
                status="succeeded", provider_request_id=provider_id, audio_seconds=seconds,
            )
        return self._transcription_public(self.get_local_task(principal, task_id))

    async def text_json(
        self, principal: ApiPrincipal, alias: str, operation: str, payload: dict[str, Any],
        *, user_session_id: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        route = self.validate_text_operation(principal, alias, operation, stream=False)
        if route.provider == "vega":
            payload = vega_vl.validate_payload(payload, pro=not route.capabilities.get("textOnly", True))
        elif {"thinking_enabled", "reasoning_split"} & set(payload):
            raise ApiError("该模型不支持Vega-VL推理参数", 422, "text_parameter_unsupported")
        if _contains_image_input(payload) and not route.capabilities.get("imageInput"):
            raise ApiError("该模型不支持图片理解", 422, "model_image_input_unsupported")
        path = "/chat/completions" if operation == "chat" else "/responses"
        upstream_payload = dict(payload)
        upstream_payload["model"] = route.upstream_model
        upstream_payload["stream"] = False
        response, data = await self._request(route, "POST", path, upstream_payload,
            extra_headers=vega_vl.session_header(principal, user_session_id) if route.provider == "vega" else None)
        data["model"] = route.alias
        request_id = _provider_request_id(response.headers, data) or f"req_{uuid.uuid4().hex}"
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        self._record_usage(
            request_id=request_id,
            principal=principal,
            route=route,
            status="succeeded",
            provider_request_id=_provider_request_id(response.headers, data),
            usage=usage,
        )
        return data, request_id

    async def text_stream(
        self, principal: ApiPrincipal, alias: str, operation: str, payload: dict[str, Any],
        *, user_session_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        route = self.validate_text_operation(principal, alias, operation, stream=True)
        if route.provider == "vega":
            payload = vega_vl.validate_payload(payload, pro=not route.capabilities.get("textOnly", True))
        elif {"thinking_enabled", "reasoning_split"} & set(payload):
            raise ApiError("该模型不支持Vega-VL推理参数", 422, "text_parameter_unsupported")
        if _contains_image_input(payload) and not route.capabilities.get("imageInput"):
            raise ApiError("该模型不支持图片理解", 422, "model_image_input_unsupported")
        path = "/chat/completions" if operation == "chat" else "/responses"
        upstream_payload = dict(payload)
        upstream_payload["model"] = route.upstream_model
        upstream_payload["stream"] = True
        client = httpx.AsyncClient(
            timeout=self._model_timeout(stream=True),
            transport=self.transport,
        )
        provider_request_id: str | None = None
        usage: dict[str, Any] = {}
        request_id = f"req_{uuid.uuid4().hex}"
        stream_completed = False
        stream_error = False
        try:
            async with client.stream(
                "POST",
                f"{self._base_url(route)}{path}",
                headers={**self._headers(route), **(vega_vl.session_header(principal, user_session_id) if route.provider == "vega" else {})},
                json=upstream_payload,
            ) as response:
                provider_request_id = _provider_request_id(response.headers)
                if response.status_code >= 400:
                    body = await response.aread()
                    try:
                        data = json.loads(body)
                    except ValueError:
                        data = {}
                    message = data.get("message") if isinstance(data, dict) else None
                    if route.provider == "vega" and isinstance(data, dict):
                        upstream_error = data.get("error")
                        if isinstance(upstream_error, dict):
                            message = upstream_error.get("message") or message
                        message = str(message or "模型供应商拒绝了流式请求")
                        for secret in vega.credentials(route.secret).values():
                            message = message.replace(secret, "[REDACTED]")
                    raise ApiError(
                        str(message or "模型供应商拒绝了流式请求")[:500],
                        response.status_code,
                        "provider_request_failed",
                    )
                async for line in response.aiter_lines():
                    if not line:
                        yield b"\n"
                        continue
                    if line.startswith("data:") and line[5:].strip() == "[DONE]":
                        stream_completed = True
                        yield b"data: [DONE]\n"
                    elif line.startswith("data:"):
                        try:
                            event = json.loads(line[5:].strip())
                        except ValueError:
                            stream_error = True
                            if route.provider == "vega":
                                raise ApiError("Vega-VL返回了无效流式数据", 502, "provider_response_invalid")
                            yield f"{line}\n".encode("utf-8")
                            continue
                        if isinstance(event, dict):
                            if event.get("error"):
                                stream_error = True
                                if route.provider == "vega":
                                    safe_event = _json(event)
                                    for secret in vega.credentials(route.secret).values():
                                        safe_event = safe_event.replace(_json(secret)[1:-1], "[REDACTED]")
                                    event = json.loads(safe_event)
                            if "model" in event:
                                event["model"] = route.alias
                            if isinstance(event.get("usage"), dict):
                                usage = event["usage"]
                            nested = event.get("response")
                            request_id = str(event.get("id") or (nested.get("id") if isinstance(nested, dict) else None) or request_id)
                        yield f"data: {_json(event)}\n".encode("utf-8")
                    else:
                        yield f"{line}\n".encode("utf-8")
                if route.provider == "vega" and not stream_completed:
                    raise ApiError("Vega-VL流式响应未完成，请核对结果，勿盲目重试", 502, "provider_stream_incomplete")
        except httpx.RequestError as error:
            raise self._transport_error(error, submitted=True) from error
        finally:
            await client.aclose()
            self._record_usage(
                request_id=request_id,
                principal=principal,
                route=route,
                status="succeeded" if usage and (route.provider != "vega" or stream_completed and not stream_error) else "unknown",
                provider_request_id=provider_request_id,
                usage=usage,
            )

    def _find_idempotent_task(
        self, principal: ApiPrincipal, operation: str, idempotency_key: str, request_hash: str
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM inference_tasks WHERE api_key_id=? AND operation=? AND idempotency_key=?",
                (principal.id, operation, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result["request_hash"] != request_hash:
            raise ApiError(
                "同一个Idempotency-Key不能用于不同请求",
                409,
                "idempotency_key_conflict",
            )
        return result

    def _create_task(
        self,
        principal: ApiPrincipal,
        route: ModelRoute,
        operation: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
        *,
        billing_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        request_hash = _request_hash(operation, payload)
        if idempotency_key:
            existing = self._find_idempotent_task(principal, operation, idempotency_key, request_hash)
            if existing:
                return existing, False
        prefix = {"video": "vid", "image": "img", "transcription": "asr"}.get(operation, "tsk")
        task_id = f"{prefix}_{uuid.uuid4().hex}"
        billing_metadata: dict[str, Any] = {}
        if operation == "video":
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            resolution = metadata.get("resolution")
            if resolution is None and route.provider == "aliyun_bailian":
                resolution = "1080p"
            elif resolution is None and route.provider == "minimax":
                resolution = "768p"
            if resolution is not None:
                billing_metadata["resolution"] = str(resolution).lower()
            duration = payload.get("duration")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
                billing_metadata["requestedDuration"] = duration
            content = metadata.get("content") if isinstance(metadata.get("content"), list) else []
            billing_metadata["containsVideoInput"] = any(
                isinstance(item, dict) and item.get("type") == "video_url" for item in content
            )
            if route.provider == "vega":
                billing_metadata["vegaSite"] = route.channel_config["site"]
                billing_metadata["resolution"] = str(resolution or route.capabilities["defaultResolution"]).lower()
        elif operation == "image":
            if route.provider == "vega":
                billing_metadata["vegaSite"] = route.channel_config["site"]
            image = payload.get("image")
            billing_metadata["inputImageCount"] = len(image) if isinstance(image, list) else int(
                isinstance(image, str) and bool(image.strip())
            )
            if payload.get("size") is not None:
                billing_metadata["requestedSize"] = str(payload["size"])
        if billing_context:
            billing_metadata.update(billing_context)
        with self.database.connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO inference_tasks
                    (id,api_key_id,project_name,model_alias,channel_id,credential_id,upstream_model,
                     operation,status,request_hash,idempotency_key,billing_metadata_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?,'queued',?,?,?,?)
                    """,
                    (
                        task_id, principal.id, principal.project_name, route.alias,
                        route.channel_id, route.credential_id, route.upstream_model, operation, request_hash,
                        idempotency_key, _json(billing_metadata), _now_epoch(),
                    ),
                )
            except Exception as error:
                if idempotency_key and "UNIQUE constraint" in str(error):
                    existing = self._find_idempotent_task(
                        principal, operation, idempotency_key, request_hash
                    )
                    if existing:
                        return existing, False
                raise
        return {
            "id": task_id,
            "status": "queued",
            "request_hash": request_hash,
            "metadata_json": "{}",
            "billing_metadata_json": _json(billing_metadata),
        }, True

    @staticmethod
    def _validated_image_sources(route: ModelRoute, value: Any) -> str | list[str] | None:
        if value is None:
            return None
        if not route.capabilities.get("imageInput"):
            raise ApiError("该模型不支持参考图片", 422, "model_image_input_unsupported")
        if isinstance(value, str):
            sources = [value]
            normalized: str | list[str] = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            sources = value
            normalized = value
        else:
            raise ApiError("image必须是图片URL或URL数组", 422, "image_input_invalid")
        maximum = int(route.capabilities.get("maxInputImages") or 1)
        if not sources or len(sources) > maximum:
            raise ApiError(
                f"该模型最多支持{maximum}张参考图片",
                422,
                "image_input_count_invalid",
                details={"maxImages": maximum},
            )
        configured_maximum = route.capabilities.get("maxInputImageBytes")
        maximum_bytes = int(configured_maximum) if configured_maximum is not None else None
        for source in sources:
            stripped = source.strip()
            if not stripped or not (
                stripped.startswith("https://") or stripped.startswith("http://") or stripped.startswith("data:image/")
            ):
                raise ApiError("参考图片必须是HTTP(S) URL或图片Data URL", 422, "image_input_invalid")
            if stripped.startswith("data:image/"):
                _decoded_image_data_url_size(stripped, maximum_bytes)
        return normalized

    def _image_upstream_payload(self, route: ModelRoute, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("stream") is True:
            raise ApiError("图片流式输出暂未开放", 422, "image_stream_unsupported")
        image = self._validated_image_sources(route, payload.get("image"))
        if route.capabilities.get("imageInputRequired") and image is None:
            raise ApiError("该模型必须提供一张参考图片", 422, "image_input_required")
        count = payload.get("n", 1)
        maximum = int(route.capabilities.get("maxN") or 1)
        if count > maximum:
            raise ApiError(
                f"该模型单次最多生成{maximum}张图片",
                422,
                "image_count_unsupported",
                details={"maxImages": maximum},
            )
        if route.provider == "modeltop":
            return self._modeltop_image_payload(route, payload)
        if route.provider not in ARK_PROVIDERS:
            upstream = dict(payload)
            upstream["model"] = route.upstream_model
            return upstream

        if any(key in payload for key in ("sequential_image_generation", "sequential_image_generation_options")) and not route.capabilities.get("sequentialImages"):
            raise ApiError("该模型不支持组图生成", 422, "image_sequence_unsupported")
        if "output_format" in payload and not route.capabilities.get("outputFormat"):
            raise ApiError("该模型不支持指定输出格式", 422, "image_output_format_unsupported")
        if "tools" in payload and not route.capabilities.get("webSearch"):
            raise ApiError("该模型不支持图片生成工具", 422, "image_tools_unsupported")
        if "seed" in payload and not route.capabilities.get("seed"):
            raise ApiError("该模型不支持指定随机种子", 422, "image_seed_unsupported")
        if "guidance_scale" in payload and not route.capabilities.get("guidanceScale"):
            raise ApiError("该模型不支持guidance_scale", 422, "image_guidance_scale_unsupported")

        upstream = {key: value for key, value in payload.items() if key in ARK_IMAGE_FIELDS}
        if image is not None:
            upstream["image"] = image
        if count > 1:
            if not route.capabilities.get("sequentialImages"):
                raise ApiError("该模型不支持多图输出", 422, "image_count_unsupported")
            upstream["sequential_image_generation"] = "auto"
            options = upstream.get("sequential_image_generation_options")
            options = dict(options) if isinstance(options, dict) else {}
            options["max_images"] = count
            upstream["sequential_image_generation_options"] = options
        upstream["stream"] = False
        upstream["model"] = route.upstream_model
        if route.provider == "byteplus_modelark":
            self._validate_byteplus_image_payload(route, payload, upstream, image, count)
        return upstream

    @staticmethod
    def _modeltop_image_payload(route: ModelRoute, payload: dict[str, Any]) -> dict[str, Any]:
        # Only the documented synchronous text-to-image contract. Reference
        # image editing is multipart /images/edits, not a JSON passthrough.
        allowed = {"model", "prompt", "n", "size", "quality", "background", "output_format", "output_compression", "response_format", "stream"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ApiError("ModelTop图片模型不支持部分参数", 422, "image_parameter_unsupported", details={"fields": unknown})
        size = payload.get("size", "auto")
        sizes = {"auto", "1024x1024", "1536x1024", "1024x1536", "2048x2048", "2048x1152", "3840x2160", "2160x3840"}
        if not isinstance(size, str):
            raise ApiError("ModelTop图片尺寸无效", 422, "image_size_invalid")
        if size not in sizes:
            match = re.fullmatch(r"(\d{1,5})x(\d{1,5})", str(size))
            width, height = (int(match[1]), int(match[2])) if match else (0, 0)
            if not (route.capabilities["customSizes"] and width and height and max(width, height) <= 3840
                    and width % 16 == height % 16 == 0 and max(width, height) <= 3 * min(width, height)
                    and 655360 <= width * height <= 8294400):
                raise ApiError("ModelTop图片尺寸无效", 422, "image_size_invalid")
        quality = payload.get("quality", "auto")
        if not isinstance(quality, str) or quality not in route.capabilities["qualityLevels"]:
            raise ApiError("ModelTop图片质量档位无效", 422, "image_quality_invalid")
        background = payload.get("background", "auto")
        if not isinstance(background, str) or background not in {"auto", "opaque", "transparent"} or (background == "transparent" and not route.capabilities["customSizes"]):
            raise ApiError("ModelTop图片背景参数无效", 422, "image_parameter_unsupported")
        output_format = payload.get("output_format", "png")
        if not isinstance(output_format, str) or output_format not in {"png", "jpeg", "webp"} or ("output_format" in payload and not route.capabilities["outputFormat"]):
            raise ApiError("ModelTop图片输出格式无效", 422, "image_output_format_unsupported")
        if background == "transparent" and output_format == "jpeg":
            raise ApiError("透明背景需要png或webp格式", 422, "image_output_format_unsupported")
        compression = payload.get("output_compression")
        if "output_compression" in payload and (not route.capabilities["outputFormat"] or output_format == "png"
                or isinstance(compression, bool) or not isinstance(compression, int) or not 0 <= compression <= 100):
            raise ApiError("ModelTop图片压缩参数无效", 422, "image_parameter_unsupported")
        # GPT Image returns Base64 natively. Do not send the legacy DALL-E
        # response_format flag; expose the returned b64_json without uploading it.
        upstream = {key: value for key, value in payload.items() if key not in {"response_format", "stream"}}
        upstream.update(model=route.upstream_model, size=size, quality=quality)
        return upstream

    @staticmethod
    def _validate_byteplus_image_payload(
        route: ModelRoute, payload: dict[str, Any], upstream: dict[str, Any],
        image: str | list[str] | None, count: int,
    ) -> None:
        unsupported = sorted(set(payload) - ARK_IMAGE_FIELDS - {"n", "model"})
        if unsupported:
            raise ApiError("当前图片模型不支持部分参数", 422, "image_parameter_unsupported", details={"fields": unsupported})
        size = payload.get("size", route.capabilities["defaultSize"])
        if size not in route.capabilities["sizes"]:
            match = re.fullmatch(r"(\d{1,5})x(\d{1,5})", size) if isinstance(size, str) else None
            width, height = (int(match[1]), int(match[2])) if match else (0, 0)
            if (
                not width or not height or width > height * 16 or height > width * 16
                or not route.capabilities["minPixels"] <= width * height <= route.capabilities["maxPixels"]
            ):
                raise ApiError("当前模型不支持该图片尺寸", 422, "image_size_invalid")
        upstream["size"] = size
        if "output_format" in payload and payload["output_format"] not in ("png", "jpeg"):
            raise ApiError("图片输出格式仅支持png或jpeg", 422, "image_output_format_unsupported")
        if "watermark" in payload and not isinstance(payload["watermark"], bool):
            raise ApiError("watermark必须是布尔值", 422, "image_parameter_unsupported")
        options = payload.get("optimize_prompt_options")
        if options is not None and (
            not isinstance(options, dict) or set(options) - {"mode"}
            or options.get("mode", "standard") not in route.capabilities["optimizePromptModes"]
        ):
            raise ApiError("当前模型不支持该提示词优化模式", 422, "image_parameter_unsupported")
        if route.capabilities.get("sequentialImages"):
            mode = upstream.get("sequential_image_generation", "disabled")
            if mode not in ("auto", "disabled"):
                raise ApiError("组图模式无效", 422, "image_sequence_unsupported")
            original_options = payload.get("sequential_image_generation_options", {})
            if not isinstance(original_options, dict) or set(original_options) - {"max_images"}:
                raise ApiError("组图参数无效", 422, "image_sequence_unsupported")
            options = upstream.get("sequential_image_generation_options", {})
            if not isinstance(options, dict) or set(options) - {"max_images"}:
                raise ApiError("组图参数无效", 422, "image_sequence_unsupported")
            input_count = len(image) if isinstance(image, list) else int(image is not None)
            maximum = options.get("max_images", 15 - input_count if mode == "auto" else count)
            if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 15:
                raise ApiError("组图数量无效", 422, "image_count_unsupported")
            if input_count + maximum > 15:
                raise ApiError("参考图片和生成图片合计不能超过15张", 422, "image_input_count_invalid")
            if mode == "auto":
                upstream["sequential_image_generation_options"] = {"max_images": maximum}

    @staticmethod
    def _maxmodel_image_response(
        response: httpx.Response, envelope: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        provider_id = _provider_request_id(response.headers, envelope)
        code = envelope.get("code")
        try:
            numeric_code = int(code)
        except (TypeError, ValueError):
            numeric_code = None
        if numeric_code != 200:
            message = envelope.get("msg") or envelope.get("message") or "MaxModel拒绝了图片请求"
            status_code = numeric_code if numeric_code is not None and 400 <= numeric_code < 600 else 502
            raise ApiError(
                str(message)[:500],
                status_code,
                "provider_request_failed",
                details={"upstreamRequestId": provider_id},
            )
        data = envelope.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ApiError("MaxModel返回了无效图片响应", 502, "provider_response_invalid")
        return data, provider_id

    async def generate_image(
        self,
        principal: ApiPrincipal,
        alias: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        route = self.resolve(principal, alias)
        if route.modality != "image":
            raise ApiError("该模型不支持图片接口", 422, "model_modality_mismatch")
        if route.provider == "vega":
            from .vega_image_relay import VegaImages
            return await VegaImages(self).generate(principal, alias, payload, idempotency_key)
        upstream = self._image_upstream_payload(route, payload)
        if len(self._image_jobs) >= self.settings.model_image_max_inflight and not (
            idempotency_key and self._find_idempotent_task(
                principal, "image", idempotency_key, _request_hash("image", payload)
            )
        ):
            raise ApiError("生图并发已满，本次尚未提交供应商，请稍后使用相同幂等键重试", 503,
                           "model_capacity_exceeded", details={"outcomeUnknown": False},
                           headers={"Retry-After": "10"})
        task, created = self._create_task(principal, route, "image", payload, idempotency_key)
        if not created:
            metadata = _loaded(task.get("metadata_json"), {})
            if task["status"] == "succeeded" and isinstance(metadata.get("response"), dict):
                return metadata["response"]
            if task["status"] in ACTIVE_TASK_STATUSES:
                raise ApiError("上次生图仍在处理中；稍后使用相同幂等键查询，不会重新生成", 409,
                               "idempotency_request_in_progress", details={"taskId": task["id"], "automaticRetry": False})
            if metadata.get("outcomeUnknown") or task.get("error_code") in {
                "provider_response_timeout", "provider_unreachable", "provider_outcome_unknown",
            }:
                raise ApiError("上次生图结果尚未确认，请联系管理员核对，勿更换幂等键重复付费生成", 409,
                               "idempotency_outcome_unknown", details={"taskId": task["id"], "outcomeUnknown": True})
            raise ApiError("相同幂等请求此前执行失败", 409, "idempotency_request_failed")
        job = asyncio.create_task(self._execute_image(principal, route, upstream, task))
        self._image_jobs.add(job)
        job.add_done_callback(self._image_job_done)
        # A closed browser connection must not abort an already submitted paid
        # generation. The same idempotency key can retrieve the cached response.
        return await asyncio.shield(job)

    async def _execute_image(
        self, principal: ApiPrincipal, route: ModelRoute, upstream: dict[str, Any], task: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            response, data = await self._request(route, "POST", "/images/generations", upstream)
            provider_id: str | None = None
            if route.provider == "maxmodel":
                data, provider_id = self._maxmodel_image_response(response, data)
        except (ApiError, asyncio.CancelledError) as error:
            if isinstance(error, asyncio.CancelledError):
                failure = ApiError("服务中断，生图结果未确认，请勿重复提交", 503, "provider_outcome_unknown",
                                   details={"outcomeUnknown": True})
            else:
                failure = error
            uncertain = failure.details.get("outcomeUnknown") is True
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE inference_tasks SET status='failed',error_code=?,error_message=?,"
                    "metadata_json=?,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (failure.code, failure.message, _json({"outcomeUnknown": uncertain}), task["id"]),
                )
            if uncertain:
                self._record_usage(request_id=task["id"], task_id=task["id"], principal=principal,
                                   route=route, status="unknown")
            raise
        data["model"] = route.alias
        provider_id = provider_id or _provider_request_id(response.headers, data)
        images = data.get("data") if isinstance(data.get("data"), list) else []
        if route.provider in {"byteplus_modelark", "modeltop"}:
            # A partial batch can contain error entries. Only actual outputs
            # count as successful images; an empty/malformed response is not free usage.
            images = [item for item in images if isinstance(item, dict) and (item.get("url") or item.get("b64_json")) and not item.get("error")]
            if not images:
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE inference_tasks SET status='failed',error_code='provider_response_invalid',"
                        "completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (task["id"],),
                    )
                raise ApiError("模型供应商未返回成功生成的图片", 502, "provider_response_invalid")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE inference_tasks SET status='succeeded',progress=100,provider_request_id=?,"
                "metadata_json=?,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (provider_id, _json({"response": data}), task["id"]),
            )
        self._record_usage(
            request_id=task["id"],
            task_id=task["id"],
            principal=principal,
            route=route,
            status="succeeded",
            provider_request_id=provider_id,
            usage=usage,
            generated_images=len(images),
        )
        return data

    @staticmethod
    def _video_public(task: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": task["id"],
            "object": "video",
            "model": task["model_alias"],
            "status": task["status"],
            "progress": int(task.get("progress") or 0),
            "created_at": int(task["created_at"]),
        }
        if task.get("completed_at"):
            result["completed_at"] = task["completed_at"]
        if task.get("result_url"):
            result["url"] = task["result_url"]
        if task.get("result_format"):
            result["format"] = task["result_format"]
        if task.get("error_code") or task.get("error_message"):
            result["error"] = {
                "code": task.get("error_code") or "provider_error",
                "message": task.get("error_message") or "视频生成失败",
            }
        metadata = _loaded(task.get("metadata_json"), {})
        metadata = seedance_draft.public_metadata(task, metadata if isinstance(metadata, dict) else {})
        if metadata:
            result["metadata"] = {key: value for key, value in metadata.items() if key != "response"}
        return result

    def _video_submit_payload(self, route: ModelRoute, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if route.provider == "vega":
            return "/CreateAigcVideoTask", vega.submit_payload(route, payload)
        if route.provider in ARK_PROVIDERS:
            return self._volcengine_video_submit_payload(route, payload)
        if route.provider == "aliyun_bailian":
            return self._aliyun_video_submit_payload(route, payload)
        if route.provider == "minimax":
            return self._minimax_video_submit_payload(route, payload)
        raise ApiError("该渠道不支持异步视频", 422, "video_provider_unsupported")

    def _volcengine_video_submit_payload(
        self, route: ModelRoute, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        seedance_draft.validate(route, payload)
        allowed_metadata = {
            "resolution", "ratio", "generate_audio", "watermark", "camera_fixed",
            "return_last_frame", "service_tier", "content", "draft", "frames",
            "execution_expires_after", "task_type",
            "output_format",
        }
        forbidden = sorted(set(metadata) - allowed_metadata)
        if forbidden:
            raise ApiError(
                "metadata包含当前版本不允许的字段",
                422,
                "video_metadata_forbidden",
                details={"fields": forbidden},
            )
        prompt = str(payload.get("prompt") or "").strip()
        image = str(payload.get("image") or "").strip()
        if not prompt and not image and not metadata.get("content"):
            raise ApiError("视频请求至少需要prompt或参考素材", 422, "video_input_required")
        duration = payload.get("duration")
        unsupported = [name for name in ("width", "height", "fps") if payload.get(name) is not None]
        if unsupported:
            raise ApiError(
                "当前视频模型不支持部分兼容字段",
                422,
                "video_parameter_unsupported",
                details={"fields": unsupported},
            )
        content = metadata.get("content")
        if content is None:
            content = []
            if prompt:
                content.append({"type": "text", "text": prompt})
            if image:
                content.append(
                    {"type": "image_url", "image_url": {"url": image}, "role": "first_frame"}
                )
        safe_content = self._ark_video_content(route, content)
        frames = metadata.get("frames")
        if duration is not None and frames is not None:
            raise ApiError("duration和frames不能同时设置", 422, "video_duration_conflict")
        body: dict[str, Any] = {"model": route.upstream_model, "content": safe_content}
        if duration is not None:
            if isinstance(duration, bool) or not float(duration).is_integer():
                raise ApiError("火山视频时长必须为整数秒", 422, "video_duration_invalid")
            normalized_duration = int(duration)
            minimum = int(route.capabilities.get("durationMin", 2))
            maximum = int(route.capabilities.get("durationMax", 12))
            smart = bool(route.capabilities.get("smartDuration"))
            if normalized_duration != -1 and not minimum <= normalized_duration <= maximum:
                raise ApiError(
                    f"当前模型视频时长范围为{minimum}到{maximum}秒",
                    422,
                    "video_duration_invalid",
                )
            if normalized_duration == -1 and not smart:
                raise ApiError("当前模型不支持智能时长", 422, "video_duration_invalid")
            body["duration"] = normalized_duration
        if frames is not None:
            if (
                not route.capabilities.get("frames")
                or isinstance(frames, bool)
                or not isinstance(frames, int)
                or not 29 <= frames <= 289
                or (frames - 25) % 4 != 0
            ):
                raise ApiError("当前模型的frames参数无效", 422, "video_frames_invalid")
            body["frames"] = frames
        resolution = metadata.get("resolution")
        if resolution is not None:
            supported = route.capabilities.get("resolutions", [])
            if resolution not in supported:
                raise ApiError("当前模型不支持该分辨率", 422, "video_resolution_invalid")
            body["resolution"] = resolution
        ratio = metadata.get("ratio")
        if ratio is not None:
            if ratio not in {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"}:
                raise ApiError("视频比例无效", 422, "video_ratio_invalid")
            body["ratio"] = ratio
        generate_audio = metadata.get("generate_audio")
        if generate_audio is not None:
            if not isinstance(generate_audio, bool) or not route.capabilities.get("generateAudio"):
                raise ApiError("当前模型不支持生成音频", 422, "video_audio_unsupported")
            body["generate_audio"] = generate_audio
        draft = metadata.get("draft")
        if draft is not None:
            if not isinstance(draft, bool) or not route.capabilities.get("draft"):
                raise ApiError("当前模型不支持样片模式", 422, "video_draft_unsupported")
            body["draft"] = draft
        for name in ("watermark", "return_last_frame"):
            if name in metadata:
                if not isinstance(metadata[name], bool):
                    raise ApiError(f"{name}必须是布尔值", 422, "video_parameter_invalid")
                if name == "return_last_frame" and metadata[name] and route.capabilities.get("returnLastFrame") is False:
                    raise ApiError("当前模型不支持返回末帧", 422, "video_last_frame_unsupported")
                body[name] = metadata[name]
        output_format = metadata.get("output_format")
        if output_format is not None:
            if output_format not in route.capabilities.get("outputFormats", []):
                raise ApiError("当前模型不支持该输出格式", 422, "video_output_format_invalid")
            body["output_format"] = output_format
        camera_fixed = metadata.get("camera_fixed")
        if camera_fixed is not None:
            if not isinstance(camera_fixed, bool) or route.alias.startswith("doubao-seedance-2.0") or route.capabilities.get("cameraFixed") is False:
                raise ApiError("当前模型不支持固定摄像头", 422, "video_camera_unsupported")
            if any(item["type"] != "text" for item in safe_content):
                raise ApiError("参考素材场景不支持固定摄像头", 422, "video_camera_unsupported")
            body["camera_fixed"] = camera_fixed
        service_tier = metadata.get("service_tier")
        if service_tier is not None:
            if service_tier not in {"default", "flex"}:
                raise ApiError("service_tier无效", 422, "video_service_tier_invalid")
            if service_tier == "flex" and route.alias.startswith("doubao-seedance-2.0"):
                raise ApiError("Seedance 2.0不支持离线推理", 422, "video_service_tier_invalid")
            if "serviceTiers" in route.capabilities and service_tier not in route.capabilities["serviceTiers"]:
                raise ApiError("当前模型不支持该服务层级", 422, "video_service_tier_invalid")
            body["service_tier"] = service_tier
        expires_after = metadata.get("execution_expires_after")
        if expires_after is not None:
            if isinstance(expires_after, bool) or not isinstance(expires_after, int) or not 3600 <= expires_after <= 259200:
                raise ApiError("execution_expires_after无效", 422, "video_expiration_invalid")
            body["execution_expires_after"] = expires_after
        task_type = metadata.get("task_type")
        if task_type is not None:
            if task_type not in {"t2v", "i2v"}:
                raise ApiError("task_type无效", 422, "video_task_type_invalid")
            body["task_type"] = task_type
        if payload.get("seed") is not None:
            if route.capabilities.get("seed") is False:
                raise ApiError("当前模型不支持seed", 422, "video_seed_unsupported")
            body["seed"] = payload["seed"]
        return "/contents/generations/tasks", body

    @staticmethod
    def _external_video_content(content: Any, provider_name: str) -> tuple[str, list[dict[str, str]]]:
        if not isinstance(content, list) or not content or len(content) > 20:
            raise ApiError(
                f"{provider_name} content必须是1到20项的数组",
                422,
                "video_content_invalid",
            )
        text_parts: list[str] = []
        images: list[dict[str, str]] = []
        for item in content:
            if not isinstance(item, dict):
                raise ApiError(f"{provider_name} content包含无效条目", 422, "video_content_invalid")
            if item.get("type") == "text":
                if (
                    set(item) - {"type", "text"}
                    or not isinstance(item.get("text"), str)
                    or not item["text"].strip()
                ):
                    raise ApiError(f"{provider_name}文本条目格式无效", 422, "video_content_invalid")
                text_parts.append(item["text"].strip())
                continue
            if item.get("type") != "image_url":
                raise ApiError(f"{provider_name} content包含不支持的条目", 422, "video_content_unsupported")
            image_value = item.get("image_url")
            role = item.get("role", "first_frame")
            url = image_value.get("url") if isinstance(image_value, dict) else None
            if (
                set(item) - {"type", "image_url", "role"}
                or not isinstance(image_value, dict)
                or set(image_value) - {"url"}
                or not isinstance(url, str)
                or not url.startswith(("https://", "http://"))
                or role not in {"first_frame", "last_frame", "reference_image"}
            ):
                raise ApiError(f"{provider_name}图片条目格式无效", 422, "video_content_invalid")
            images.append({"url": url, "role": role})
        return "\n".join(text_parts)[:40000], images

    def _aliyun_video_submit_payload(
        self, route: ModelRoute, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        allowed_metadata = {"content", "resolution", "ratio", "generate_audio", "watermark"}
        forbidden = sorted(set(metadata) - allowed_metadata)
        if forbidden:
            raise ApiError(
                "当前阿里视频模型不支持部分火山兼容字段",
                422,
                "video_parameter_unsupported",
                details={"fields": forbidden},
            )
        if payload.get("seed") is not None:
            raise ApiError(
                "当前阿里视频模型不支持seed",
                422,
                "video_parameter_unsupported",
                details={"fields": ["seed"]},
            )
        prompt, images = self._external_video_content(metadata.get("content"), "阿里百炼")
        if len(images) > 1:
            raise ApiError("当前阿里视频模型最多支持一张参考图片", 422, "video_content_unsupported")
        if images and images[0]["role"] != "first_frame":
            raise ApiError("当前阿里视频模型仅支持首帧图片", 422, "video_content_unsupported")
        input_data: dict[str, Any] = {}
        if prompt:
            input_data["prompt"] = prompt
        if images:
            input_data["media"] = [{"type": "first_frame", "url": images[0]["url"]}]
        parameters: dict[str, Any] = {
            "resolution": metadata.get("resolution", "1080P"),
            "ratio": metadata.get("ratio", "adaptive"),
            "prompt_extend": True,
            "audio": metadata.get("generate_audio", True),
        }
        for name in ("generate_audio", "watermark"):
            if name in metadata and not isinstance(metadata[name], bool):
                raise ApiError(f"{name}必须是布尔值", 422, "video_parameter_invalid")
        if "watermark" in metadata:
            parameters["aigc_watermark"] = metadata["watermark"]
        if payload.get("duration") is not None:
            parameters["duration"] = payload["duration"]
        return "/services/aigc/video-generation/video-synthesis", {
            "model": route.upstream_model,
            "input": input_data,
            "parameters": parameters,
        }

    def _minimax_video_submit_payload(
        self, route: ModelRoute, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        allowed_metadata = {"content", "resolution", "ratio", "watermark"}
        forbidden = sorted(set(metadata) - allowed_metadata)
        if forbidden:
            raise ApiError(
                "当前MiniMax视频模型不支持部分火山兼容字段",
                422,
                "video_parameter_unsupported",
                details={"fields": forbidden},
            )
        prompt, images = self._external_video_content(metadata.get("content"), "MiniMax")
        safe_content: list[dict[str, Any]] = []
        if prompt:
            safe_content.append({"type": "text", "text": prompt})
        safe_content.extend(
            {
                "type": "image_url",
                "image_url": {"url": image["url"]},
                "role": image["role"],
            }
            for image in images
        )
        body: dict[str, Any] = {
            "model": route.upstream_model,
            "content": safe_content,
            "resolution": metadata.get("resolution", "768P"),
            "ratio": metadata.get("ratio", "adaptive"),
        }
        if payload.get("duration") is not None:
            body["duration"] = payload["duration"]
        if payload.get("seed") is not None:
            body["seed"] = payload["seed"]
        if "watermark" in metadata:
            if not isinstance(metadata["watermark"], bool):
                raise ApiError("watermark必须是布尔值", 422, "video_parameter_invalid")
            body["aigc_watermark"] = metadata["watermark"]
        return "/v2/video_generation", body

    @staticmethod
    def _ark_video_content(route: ModelRoute, content: Any) -> list[dict[str, Any]]:
        maximum = int(route.capabilities.get("maxContent", 20))
        if not isinstance(content, list) or not content or len(content) > maximum:
            raise ApiError(
                f"火山content必须是1到{maximum}项的数组",
                422,
                "video_content_invalid",
            )
        media_fields = {
            "image_url": ("image_url", "image", {"first_frame", "last_frame", "reference_image"}),
            "video_url": ("video_url", "video", {"reference_video"}),
            "audio_url": ("audio_url", "audio", {"reference_audio"}),
        }
        safe_content: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                raise ApiError("火山content包含无效条目", 422, "video_content_invalid")
            content_type = item.get("type")
            if content_type == "text":
                if set(item) - {"type", "text"} or not isinstance(item.get("text"), str) or not item["text"].strip():
                    raise ApiError("火山文本条目格式无效", 422, "video_content_invalid")
                safe_content.append({"type": "text", "text": item["text"][:40000]})
                continue
            if content_type == "draft_task":
                parent = seedance_draft.reference_id(route, content)
                safe_content.append({"type": "draft_task", "draft_task": {"id": parent}})
                continue
            spec = media_fields.get(str(content_type))
            if spec is None:
                raise ApiError("火山content包含不支持的条目", 422, "video_content_invalid")
            value_field, capability, roles = spec
            if not route.capabilities.get(capability):
                raise ApiError("当前模型不支持该参考素材类型", 422, "video_content_unsupported")
            value = item.get(value_field)
            role = item.get("role")
            url = value.get("url") if isinstance(value, dict) else None
            allowed_prefixes = ("https://", "http://", "asset://")
            if content_type == "image_url":
                allowed_prefixes += ("data:image/",)
            elif content_type == "audio_url":
                allowed_prefixes += ("data:audio/",)
            if (
                set(item) - {"type", value_field, "role"}
                or not isinstance(value, dict)
                or set(value) - {"url"}
                or not isinstance(url, str)
                or not url.startswith(allowed_prefixes)
                or role not in roles
            ):
                raise ApiError("火山参考素材条目格式无效", 422, "video_content_invalid")
            safe_content.append({"type": content_type, value_field: {"url": url}, "role": role})
        if route.capabilities.get("imageRequired") and not any(item["type"] == "image_url" for item in safe_content):
            raise ApiError("当前模型必须提供参考图片", 422, "video_image_required")
        if route.provider in {"byteplus_modelark", "modeltop"}:
            counts = {kind: sum(item["type"] == kind for item in safe_content) for kind in media_fields}
            for kind, limit_name in (("image_url", "maxImages"), ("video_url", "maxVideos"), ("audio_url", "maxAudios")):
                if counts[kind] > route.capabilities[limit_name]:
                    raise ApiError("参考素材数量超过当前模型上限", 422, "video_content_limit")
            roles = [item.get("role") for item in safe_content]
            if "last_frame" in roles and "first_frame" not in roles:
                raise ApiError("指定末帧时必须同时提供首帧", 422, "video_content_invalid")
            if roles.count("first_frame") > 1 or roles.count("last_frame") > 1:
                raise ApiError("首帧和末帧各最多一张图片", 422, "video_content_invalid")
            if any(role in {"first_frame", "last_frame"} for role in roles) and any(
                role in {"reference_image", "reference_video", "reference_audio"} for role in roles
            ):
                raise ApiError("首末帧不能与全能参考素材混用", 422, "video_content_invalid")
            if counts["audio_url"] and not counts["image_url"] and not counts["video_url"] and not route.capabilities["audioOnly"]:
                raise ApiError("当前模型的参考音频必须搭配参考图片或视频", 422, "video_content_invalid")
        return safe_content

    async def create_video(
        self,
        principal: ApiPrincipal,
        alias: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        route = self.resolve(principal, alias)
        if route.modality != "video":
            raise ApiError("该模型不支持视频接口", 422, "model_modality_mismatch")
        parent_id = seedance_draft.validate(route, payload)
        billing_context = None
        parent = None
        if parent_id:
            if idempotency_key:
                existing = self._find_idempotent_task(principal, "video", idempotency_key, _request_hash("video", payload))
                if existing:
                    return self._video_public(existing)
            try:
                parent = self.get_local_task(principal, parent_id)
            except ApiError as error:
                if error.code != "video_task_not_found":
                    raise
                raise ApiError("样片任务不存在或不属于当前业务Key", 404, "video_draft_not_found") from error
            billing_context = seedance_draft.source_context(parent, route, _now_epoch())
            # Keep the accepted Draft's account/credential version. Current
            # resolve() above still enforces active channel and Key permission.
            route = self._task_route(parent)
        elif seedance_draft.supported(route):
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            billing_context = {"draft": metadata.get("draft", False)}
        path, upstream_payload = self._video_submit_payload(route, payload)
        if parent is not None:
            upstream_payload["content"][0]["draft_task"]["id"] = parent["upstream_task_id"]
        if route.provider == "vega":
            from .vega_resources import VegaResources
            resources = VegaResources(self)
            for item in upstream_payload.get("FileInfos", []):
                if item["Url"].startswith("asset://"):
                    resources.owns(principal, route, "AssetId", item["Url"][8:].rstrip("/"))
        task, created = self._create_task(principal, route, "video", payload, idempotency_key, billing_context=billing_context)
        if not created:
            return self._video_public(task)
        try:
            response, data = await self._request(route, "POST", path, upstream_payload)
            if route.provider == "aliyun_bailian":
                output = data.get("output") if isinstance(data.get("output"), dict) else {}
                upstream_task_id = output.get("task_id")
            elif route.provider == "vega":
                upstream_task_id = data.get("TaskId")
            elif route.provider in ARK_PROVIDERS:
                upstream_task_id = data.get("id")
            else:
                upstream_task_id = data.get("task_id")
                if not upstream_task_id and isinstance(data.get("task"), dict):
                    upstream_task_id = data["task"].get("id")
            if not isinstance(upstream_task_id, (str, int)) or not str(upstream_task_id):
                raise ApiError("供应商未返回任务ID", 502, "provider_task_id_missing")
            provider_id = data.get("RequestId") if route.provider == "vega" else _provider_request_id(response.headers, data)
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE inference_tasks SET upstream_task_id=?,status='queued',provider_request_id=?,"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (str(upstream_task_id), provider_id, task["id"]),
                )
        except ApiError as error:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE inference_tasks SET status='failed',error_code=?,error_message=?,"
                    "completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (error.code, error.message, task["id"]),
                )
            raise
        return self._video_public(self.get_local_task(principal, task["id"]))

    async def create_ark_video(
        self,
        principal: ApiPrincipal,
        alias: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        route = self.resolve(principal, alias)
        if route.modality != "video":
            raise ApiError("该模型不支持视频接口", 422, "model_modality_mismatch")
        metadata = {
            key: value
            for key, value in payload.items()
            if key not in {"model", "duration", "seed"}
        }
        normalized: dict[str, Any] = {"model": alias, "metadata": metadata}
        if "duration" in payload:
            normalized["duration"] = payload["duration"]
        if "seed" in payload:
            normalized["seed"] = payload["seed"]
        return await self.create_video(principal, alias, normalized, idempotency_key)

    @staticmethod
    def _ark_video_public(task: dict[str, Any]) -> dict[str, Any]:
        status = "cancelled" if task.get("status") == "canceled" else task.get("status", "queued")
        result: dict[str, Any] = {
            "id": task["id"],
            "model": task.get("model") or task.get("model_alias"),
            "status": status,
            "created_at": task.get("created_at"),
        }
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        for key in ("resolution", "ratio", "duration", "frames", "framespersecond", "generate_audio", "output_format", "draft", "draft_task_id", "draft_expires_at"):
            if metadata.get(key) is not None:
                result[key] = metadata[key]
        content: dict[str, Any] = {}
        video_url = task.get("url")
        if video_url:
            content["video_url"] = video_url
        if metadata.get("last_frame_url"):
            content["last_frame_url"] = metadata["last_frame_url"]
        if content:
            result["content"] = content
        if isinstance(metadata.get("usage"), dict):
            result["usage"] = metadata["usage"]
        if isinstance(task.get("error"), dict):
            result["error"] = task["error"]
        return result

    async def refresh_ark_video(
        self, principal: ApiPrincipal, task_id: str
    ) -> dict[str, Any]:
        return self._ark_video_public(await self.refresh_video(principal, task_id))

    async def cancel_ark_video(self, principal: ApiPrincipal, task_id: str) -> None:
        task = self.get_local_task(principal, task_id)
        route = self._task_route(task)
        if route.capabilities.get("cancelSupported") is False:
            raise ApiError("该供应商未开放任务取消接口", 422, "video_cancel_unsupported")
        if route.provider not in ARK_PROVIDERS:
            raise ApiError(
                "当前视频供应商不支持通过中转站取消任务",
                422,
                "video_cancel_unsupported",
            )
        upstream_id = task.get("upstream_task_id")
        if upstream_id:
            await self._request(route, "DELETE", f"/contents/generations/tasks/{upstream_id}")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE inference_tasks SET status='canceled',progress=100,"
                "completed_at=COALESCE(completed_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND api_key_id=? AND project_name=?",
                (task_id, principal.id, principal.project_name),
            )

    def get_local_task(self, principal: ApiPrincipal, task_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM inference_tasks WHERE id=? AND api_key_id=? AND project_name=?",
                (task_id, principal.id, principal.project_name),
            ).fetchone()
        if row is None:
            raise ApiError("视频任务不存在", 404, "video_task_not_found")
        return dict(row)

    def _task_route(self, task: dict[str, Any]) -> ModelRoute:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT m.display_name,c.provider,m.modality,m.protocol,m.capabilities_json,
                       c.name AS channel_name,c.config_json,pc.secret_ciphertext
                FROM model_catalog m
                JOIN provider_channels c ON c.id=?
                JOIN provider_credentials pc ON pc.id=?
                WHERE m.alias=?
                """,
                (task["channel_id"], task["credential_id"], task["model_alias"]),
            ).fetchone()
        if row is None:
            raise ApiError("任务绑定的供应商配置不存在", 503, "task_channel_missing")
        return ModelRoute(
            alias=task["model_alias"],
            display_name=row["display_name"],
            provider=row["provider"],
            modality=row["modality"],
            protocol=row["protocol"],
            capabilities=_channel_model(task["model_alias"],row["provider"],task["upstream_model"],row["capabilities_json"])[1],
            upstream_model=task["upstream_model"],
            channel_id=task["channel_id"],
            channel_name=row["channel_name"],
            channel_config=_loaded(row["config_json"], {}),
            credential_id=task["credential_id"],
            secret=self.vault.decrypt(row["secret_ciphertext"]),
        )

    async def refresh_video(self, principal: ApiPrincipal, task_id: str) -> dict[str, Any]:
        task = self.get_local_task(principal, task_id)
        if task.get("operation") != "video":
            raise ApiError("视频任务不存在", 404, "video_task_not_found")
        if task["status"] in TERMINAL_TASK_STATUSES or not task.get("upstream_task_id"):
            return self._video_public(task)
        route = self._task_route(task)
        upstream_id = task["upstream_task_id"]
        error_code_value: str | None = None
        if route.provider == "vega":
            response, data = await self._request(route, "POST", "/DescribeTaskDetail", {"TaskId": upstream_id})
            result = vega.video_result(data)
            status, result_url, usage = result["status"], result["url"], result["usage"]
            seconds_value = result.get("seconds")
            error, error_code_value = result.get("error"), result.get("error_code")
            metadata = result["metadata"]
        elif route.provider == "aliyun_bailian":
            response, data = await self._request(route, "GET", f"/tasks/{upstream_id}")
            output = data.get("output") if isinstance(data.get("output"), dict) else {}
            raw_status = str(output.get("task_status") or "PENDING").upper()
            status_map = {
                "PENDING": "queued", "RUNNING": "running", "SUCCEEDED": "succeeded",
                "FAILED": "failed", "CANCELED": "canceled", "CANCELLED": "canceled",
            }
            status = status_map.get(raw_status, "running")
            result_url = output.get("video_url") or output.get("url")
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            seconds_value = (
                output.get("video_duration")
                or usage.get("output_video_duration")
                or usage.get("duration")
                or usage.get("video_duration")
            )
            error = output.get("message") or data.get("message")
            error_code_value = str(output.get("code") or data.get("code") or "provider_task_failed")
            metadata = {
                "duration": seconds_value,
                **({"usage": usage} if usage else {}),
            }
        elif route.provider in ARK_PROVIDERS:
            response, data = await self._request(
                route, "GET", f"/contents/generations/tasks/{upstream_id}"
            )
            raw_status = str(data.get("status") or "queued").lower()
            status_map = {
                "queued": "queued", "running": "running", "succeeded": "succeeded",
                "failed": "failed", "cancelled": "canceled", "canceled": "canceled",
                "expired": "failed",
            }
            status = status_map.get(raw_status, "running")
            content = data.get("content") if isinstance(data.get("content"), dict) else {}
            result_url = content.get("video_url") or content.get("url")
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            seconds_value = data.get("duration")
            error_data = data.get("error") if isinstance(data.get("error"), dict) else {}
            error = error_data.get("message")
            error_code_value = str(error_data.get("code") or "provider_task_failed")
            if raw_status == "expired" and not error:
                error = "火山视频任务已过期"
            metadata = {
                key: data[key]
                for key in ("resolution", "ratio", "duration", "frames", "framespersecond", "generate_audio", "output_format", "draft")
                if data.get(key) is not None
            }
            if content.get("last_frame_url"):
                metadata["last_frame_url"] = content["last_frame_url"]
            if usage:
                metadata["usage"] = usage
        elif route.provider == "minimax":
            response, data = await self._request(
                route, "GET", f"/v2/query/video_generation/{upstream_id}"
            )
            upstream_task = data.get("task") if isinstance(data.get("task"), dict) else {}
            raw_status = str(upstream_task.get("status") or "queued").lower()
            status = "canceled" if raw_status in {"cancelled", "canceled"} else raw_status
            if status not in TERMINAL_TASK_STATUSES | ACTIVE_TASK_STATUSES:
                status = "running"
            content = upstream_task.get("content") if isinstance(upstream_task.get("content"), dict) else {}
            result_url = content.get("url") or content.get("video_url")
            usage = upstream_task.get("usage") if isinstance(upstream_task.get("usage"), dict) else {}
            seconds_value = usage.get("output_seconds") or upstream_task.get("duration")
            error_data = upstream_task.get("error") if isinstance(upstream_task.get("error"), dict) else {}
            error = error_data.get("message")
            error_code_value = str(error_data.get("code") or "provider_task_failed")
            metadata = {
                key: upstream_task[key]
                for key in ("resolution", "duration", "ratio")
                if upstream_task.get(key) is not None
            }
            if usage:
                metadata["usage"] = usage
        else:
            raise ApiError("该渠道不支持异步视频", 422, "video_provider_unsupported")
        try:
            seconds = float(seconds_value) if seconds_value is not None else None
        except (TypeError, ValueError):
            seconds = None
        provider_id = data.get("RequestId") if route.provider == "vega" else _provider_request_id(response.headers, data)
        provider_id = provider_id or task.get("provider_request_id")
        progress = 100 if status in TERMINAL_TASK_STATUSES else (50 if status == "running" else 0)
        error_code = error_code_value if status == "failed" else None
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE inference_tasks SET status=?,progress=?,result_url=?,result_format=?,
                    error_code=?,error_message=?,provider_request_id=?,metadata_json=?,
                    completed_at=CASE WHEN ? IN ('succeeded','failed','canceled')
                        THEN COALESCE(completed_at,CURRENT_TIMESTAMP) ELSE completed_at END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    status, progress, result_url, (metadata.get("output_format") or "mp4") if result_url else None,
                    error_code, str(error)[:500] if error else None, provider_id,
                    _json(metadata), status, task_id,
                ),
            )
        if status == "succeeded":
            self._record_usage(
                request_id=task_id,
                task_id=task_id,
                principal=principal,
                route=route,
                status="succeeded",
                provider_request_id=provider_id,
                usage=usage,
                video_seconds=float(seconds) if isinstance(seconds, (int, float)) else None,
            )
        return self._video_public(self.get_local_task(principal, task_id))

    def usage(
        self,
        *,
        project_name: str | None,
        key_id: str | None,
        model: str | None,
        provider: str | None,
        start: str | None,
        end: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_name:
            clauses.append("u.project_name=?")
            params.append(project_name)
        if key_id:
            clauses.append("u.api_key_id=?")
            params.append(key_id)
        if model:
            clauses.append("u.model_alias=?")
            params.append(model)
        if provider:
            clauses.append("c.provider=?")
            params.append(provider)
        if start:
            clauses.append("u.created_at>=?")
            params.append(start)
        if end:
            clauses.append("u.created_at<?")
            params.append(end)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT u.*,c.provider,c.name AS channel_name FROM inference_usage u "
                "JOIN provider_channels c ON c.id=u.channel_id" + where +
                " ORDER BY u.created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "id": row["id"], "requestId": row["request_id"], "taskId": row["task_id"],
                "projectName": row["project_name"], "apiKeyId": row["api_key_id"],
                "model": row["model_alias"], "provider": row["provider"],
                "channelName": row["channel_name"], "status": row["status"],
                "inputTokens": row["input_tokens"], "outputTokens": row["output_tokens"],
                "totalTokens": row["total_tokens"], "generatedImages": row["generated_images"],
                "knownTokens": token_summary(row["input_tokens"], row["output_tokens"], row["total_tokens"])[0],
                "tokenUsageComplete": token_summary(row["input_tokens"], row["output_tokens"], row["total_tokens"])[1],
                "videoSeconds": row["video_seconds"], "inputCharacters": row["input_characters"],
                "audioSeconds": row["audio_seconds"], "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def tasks(self, *, project_name: str | None, key_id: str | None, model: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_name:
            clauses.append("project_name=?")
            params.append(project_name)
        if key_id:
            clauses.append("api_key_id=?")
            params.append(key_id)
        if model:
            clauses.append("model_alias=?")
            params.append(model)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM inference_tasks" + where + " ORDER BY created_at DESC LIMIT ?", params
            ).fetchall()
        return [self._video_public(dict(row)) if row["operation"] == "video" else self._transcription_public(dict(row)) if row["operation"] == "transcription" else {
            "id": row["id"], "object": "image", "model": row["model_alias"],
            "status": row["status"], "created_at": row["created_at"],
        } if not row["operation"].startswith("vega_") else {
            "id": row["id"], "object": "vega_resource", "model": row["model_alias"],
            "operation": row["operation"], "status": row["status"], "created_at": row["created_at"],
        } for row in rows]
