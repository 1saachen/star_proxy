import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.main import create_app
from app.volcengine import VolcengineClient
from conftest import build_settings, create_key, create_project


def test_asset_key_account_isolation_auth_scope_and_restart(tmp_path: Path) -> None:
    database = tmp_path / "scope.db"
    settings = build_settings(database)
    app = create_app(settings)
    requests = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"Result": {"Items": []}})

    with TestClient(app) as client:
        create_project(client, "neibumanju")
        create_project(client, "other_project")
        key_id, key = create_key(client, "neibumanju", "migration-target")
        _, same_project_key = create_key(client, "neibumanju", "unchanged-client")
        _, other_key = create_key(client, "other_project", "other-client")
        settings.volcengine_asset_accounts_json = SecretStr(json.dumps({key_id: {
            "project_name": "neibumanju", "access_key_id": "migration-ak", "secret_access_key": "migration-sk",
        }}))
        app.state.volcengine = VolcengineClient(settings, app.state.database, httpx.MockTransport(upstream))
        assert client.get("/api/asset-group/list").status_code == 401
        assert not requests
        for secret, expected_ak, expected_project in [
            (key, "migration-ak", "neibumanju"),
            (same_project_key, "test-ak", "neibumanju"),
            (other_key, "test-ak", "other_project"),
        ]:
            response = client.get("/api/asset-group/list", headers={"Authorization": "Bearer " + secret})
            assert response.status_code == 200
            assert "Credential=" + expected_ak + "/" in requests[-1].headers["authorization"]
            assert json.loads(requests[-1].content)["ProjectName"] == expected_project
        # A caller's supplied project cannot select another credential.
        response = client.post("/api/asset-group/create", headers={"Authorization": "Bearer " + key},
                               json={"name": "migration-scope", "projectName": "other_project"})
        assert response.status_code == 200
        assert json.loads(requests[-1].content)["ProjectName"] == "neibumanju"
        assert "Credential=migration-ak/" in requests[-1].headers["authorization"]
        # TOS and IAM remain on their original identities.
        assert settings.effective_tos_access_key == "test-ak"
        iam_url, iam_headers = app.state.volcengine._signed_get_request(
            "GetProject", {"ProjectName": "neibumanju"}, service="iam", version="2021-08-01", host="iam.volcengineapi.com")
        assert "Credential=test-ak/" in iam_headers["authorization"]
        assert "GetProject" in iam_url

    # Open an actual second app/database connection with the same existing Key.
    restored = create_app(settings)
    with TestClient(restored) as client:
        restored.state.volcengine = VolcengineClient(settings, restored.state.database, httpx.MockTransport(upstream))
        assert client.get("/api/asset-group/list", headers={"Authorization": "Bearer " + key}).status_code == 200
        assert "Credential=migration-ak/" in requests[-1].headers["authorization"]
        settings.volcengine_asset_accounts_json = SecretStr(json.dumps({key_id: {
            "project_name": "other_project", "access_key_id": "migration-ak", "secret_access_key": "migration-sk",
        }}))
        before = len(requests)
        response = client.get("/api/asset-group/list", headers={"Authorization": "Bearer " + key})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "asset_account_scope_mismatch"
        assert len(requests) == before
        assert "migration-ak" not in response.text and "migration-sk" not in response.text
        # Removing the opt-in restores the original account without rewriting Keys.
        settings.volcengine_asset_accounts_json = None
        assert client.get("/api/asset-group/list", headers={"Authorization": "Bearer " + key}).status_code == 200
        assert "Credential=test-ak/" in requests[-1].headers["authorization"]

    assert b"migration-sk" not in database.read_bytes()
    assert "migration-sk" not in settings.model_dump_json()


@pytest.mark.parametrize("raw", ["not-json", "[]", '{"key":{}}', '{"key":{"project_name":"p","access_key_id":"ak","secret_access_key":""}}'])
def test_bad_asset_account_configuration_is_masked_and_rejected(tmp_path: Path, raw: str) -> None:
    with pytest.raises(ValidationError) as error:
        build_settings(tmp_path / "invalid.db", volcengine_asset_accounts_json=raw)
    assert raw not in str(error.value)


def test_blank_asset_account_override_keeps_default(tmp_path: Path) -> None:
    assert build_settings(tmp_path / "empty.db", volcengine_asset_accounts_json=" ").volcengine_asset_accounts_json is None
