import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from fastapi.responses import JSONResponse, Response

from .config import Settings
from .database import Database
from .errors import ApiError
from .security import ApiPrincipal


REGION = "cn-beijing"
SERVICE = "ark"
VERSION = "2024-01-01"
HOST = "ark.cn-beijing.volcengineapi.com"
IAM_SERVICE = "iam"
IAM_VERSION = "2021-08-01"
IAM_HOST = "iam.volcengineapi.com"
logger = logging.getLogger(__name__)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


class VolcengineClient:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.transport = transport
        self._client: httpx.AsyncClient | None = None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.upstream_timeout_seconds,
                transport=self.transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _credentials(self) -> tuple[str, str]:
        access_key = self.settings.volcengine_access_key
        secret_key = self.settings.volcengine_secret_key
        if not access_key or not secret_key:
            raise ApiError("服务端尚未配置火山引擎 AK/SK", 503, "upstream_credentials_missing")
        return access_key, secret_key

    def _asset_credentials(self, principal: ApiPrincipal) -> tuple[str, str]:
        account = self.settings.asset_account_for(principal.id, principal.project_name)
        if account is None:
            return self._credentials()
        return account["access_key_id"].strip(), account["secret_access_key"].strip()

    def _signed_request(
        self,
        action: str,
        payload: dict[str, Any],
        project_name: str | None,
        *,
        host: str = HOST,
        credentials: tuple[str, str] | None = None,
    ) -> tuple[str, bytes, dict[str, str]]:
        access_key, secret_key = credentials if credentials is not None else self._credentials()

        request_payload = dict(payload)
        if project_name:
            request_payload["ProjectName"] = project_name
        body = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        payload_hash = _sha256(body)
        x_date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        short_date = x_date[:8]
        query = f"Action={quote(action, safe='')}&Version={quote(VERSION, safe='')}"
        canonical_headers = (
            f"content-type:application/json\nhost:{host}\n"
            f"x-content-sha256:{payload_hash}\nx-date:{x_date}\n"
        )
        signed_headers = "content-type;host;x-content-sha256;x-date"
        canonical_request = f"POST\n/\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        scope = f"{short_date}/{REGION}/{SERVICE}/request"
        string_to_sign = f"HMAC-SHA256\n{x_date}\n{scope}\n{_sha256(canonical_request.encode('utf-8'))}"
        k_date = _hmac(secret_key.encode("utf-8"), short_date)
        k_region = _hmac(k_date, REGION)
        k_service = _hmac(k_region, SERVICE)
        k_signing = _hmac(k_service, "request")
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            f"HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        headers = {
            "content-type": "application/json",
            "host": host,
            "x-content-sha256": payload_hash,
            "x-date": x_date,
            "authorization": authorization,
        }
        return f"https://{host}/?{query}", body, headers

    def _signed_get_request(
        self,
        action: str,
        params: dict[str, str],
        *,
        service: str,
        version: str,
        host: str,
    ) -> tuple[str, dict[str, str]]:
        access_key, secret_key = self._credentials()
        query_params = {"Action": action, "Version": version, **params}
        query = "&".join(
            f"{quote(key, safe='-_.~')}={quote(str(value), safe='-_.~')}"
            for key, value in sorted(query_params.items())
        )
        payload_hash = _sha256(b"")
        x_date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        short_date = x_date[:8]
        canonical_headers = f"host:{host}\nx-date:{x_date}\n"
        signed_headers = "host;x-date"
        canonical_request = f"GET\n/\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        scope = f"{short_date}/{REGION}/{service}/request"
        string_to_sign = f"HMAC-SHA256\n{x_date}\n{scope}\n{_sha256(canonical_request.encode('utf-8'))}"
        k_date = _hmac(secret_key.encode("utf-8"), short_date)
        k_region = _hmac(k_date, REGION)
        k_service = _hmac(k_region, service)
        k_signing = _hmac(k_service, "request")
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            f"HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return f"https://{host}/?{query}", {
            "host": host,
            "x-date": x_date,
            "authorization": authorization,
        }

    async def get_project(self, project_name: str) -> dict[str, Any] | None:
        """Return the exact Volcengine resource project, or None when it does not exist."""
        url, headers = self._signed_get_request(
            "GetProject",
            {"ProjectName": project_name},
            service=IAM_SERVICE,
            version=IAM_VERSION,
            host=IAM_HOST,
        )
        try:
            upstream = await self._http_client().get(url, headers=headers)
        except httpx.RequestError as error:
            logger.warning(
                "Volcengine project validation failed: project=%s error=%s",
                project_name,
                type(error).__name__,
            )
            raise ApiError(
                "无法连接火山项目校验服务，请稍后重试",
                502,
                "volcengine_project_validation_unreachable",
            ) from error

        try:
            content = upstream.json()
        except ValueError as error:
            raise ApiError(
                "火山项目校验服务返回了无法识别的响应",
                502,
                "volcengine_project_validation_invalid_response",
            ) from error

        metadata = content.get("ResponseMetadata") if isinstance(content, dict) else None
        upstream_error = metadata.get("Error") if isinstance(metadata, dict) else None
        upstream_code = upstream_error.get("Code") if isinstance(upstream_error, dict) else None
        if upstream.status_code == 404 or upstream_code == "EntityNotFound":
            return None
        if upstream.status_code in {401, 403}:
            raise ApiError(
                "当前火山 AK/SK 无权读取资源项目，请授予 iam:GetProject 权限",
                502,
                "volcengine_project_validation_forbidden",
                details={"upstreamCode": upstream_code or "AccessDenied"},
            )
        if upstream.status_code >= 400 or upstream_error:
            raise ApiError(
                "火山项目校验失败，请检查 AK/SK、项目读取权限和火山服务状态",
                502,
                "volcengine_project_validation_failed",
                details={"upstreamCode": upstream_code or f"HTTP_{upstream.status_code}"},
            )

        result = content.get("Result", content) if isinstance(content, dict) else {}
        if not isinstance(result, dict) or not isinstance(result.get("ProjectName"), str):
            raise ApiError(
                "火山项目校验响应缺少 ProjectName",
                502,
                "volcengine_project_validation_invalid_response",
            )
        return result

    async def call(self, action: str, payload: dict[str, Any], principal: ApiPrincipal) -> Response:
        started = time.monotonic()
        url, body, headers = self._signed_request(
            action, payload, principal.project_name, credentials=self._asset_credentials(principal),
        )
        try:
            upstream = await self._http_client().post(url, content=body, headers=headers)
        except httpx.RequestError as error:
            duration_ms = round((time.monotonic() - started) * 1000)
            self.database.log_request(
                principal.id,
                principal.project_name,
                action,
                502,
                duration_ms,
            )
            logger.warning(
                "Volcengine request failed: action=%s project=%s error=%s duration_ms=%s",
                action,
                principal.project_name,
                type(error).__name__,
                duration_ms,
            )
            raise ApiError("无法连接火山引擎服务", 502, "upstream_unreachable") from error

        self.database.log_request(
            principal.id,
            principal.project_name,
            action,
            upstream.status_code,
            round((time.monotonic() - started) * 1000),
        )
        try:
            content = upstream.json()
            return JSONResponse(
                status_code=upstream.status_code,
                content=content,
                headers={"x-upstream-service": "volcengine-ark"},
            )
        except ValueError:
            return Response(
                status_code=upstream.status_code,
                content=upstream.content,
                media_type=upstream.headers.get("content-type", "text/plain"),
                headers={"x-upstream-service": "volcengine-ark"},
            )
