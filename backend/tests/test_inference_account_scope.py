import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from conftest import create_key, create_project
from test_provider_relay import provision, relay_client


MODEL = "doubao-seedance-2.5"
BODY = {"model": MODEL, "content": [{"type": "text", "text": "isolated account scope"}], "duration": 5}


def override(settings, key_id, channel_id):
    settings.volcengine_asset_accounts_json = SecretStr(json.dumps({key_id: {
        "project_name": "relay_project", "access_key_id": "new-asset-ak",
        "secret_access_key": "new-asset-sk", "inference_channel_id": channel_id,
    }}))


def test_key_channel_override_preserves_other_keys_and_old_tasks_after_restart(tmp_path: Path) -> None:
    requests = []
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        requests.append(request)
        if request.method == "POST":
            post_count += 1
            return httpx.Response(200, json={"id": f"upstream-{post_count}"})
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1], "status": "running", "progress": 10})

    with relay_client(tmp_path) as client:
        key_id, key, original = provision(client, provider="volcengine_ark", alias=MODEL, upstream_model="ignored-model")
        _, other_key = create_key(client, "relay_project", "unchanged-client")
        relay = client.app.state.provider_relay
        relay.transport = httpx.MockTransport(handler)
        settings = client.app.state.settings
        new_channel = relay.create_channel(project_name="relay_project", name="isolated new account", provider="volcengine_ark",
                                           config={}, secret="secret-new-account", actor_id="super-admin")

        def submit(secret, identity):
            return client.post("/api/v3/contents/generations/tasks", json=BODY,
                               headers={"Authorization": "Bearer " + secret, "Idempotency-Key": identity})

        old = submit(key, "before-account-switch")
        assert old.status_code == 200
        old_id = old.json()["id"]
        override(settings, key_id, new_channel["id"])
        new = submit(key, "after-account-switch")
        other = submit(other_key, "other-client-keeps-old-account")
        assert new.status_code == other.status_code == 200
        submitted = [r for r in requests if r.method == "POST"]
        assert [r.headers["authorization"] for r in submitted] == [
            "Bearer secret-volcengine_ark-abcdefgh", "Bearer secret-new-account", "Bearer secret-volcengine_ark-abcdefgh",
        ]
        with client.app.state.database.connect() as connection:
            old_row = dict(connection.execute("SELECT * FROM inference_tasks WHERE id=?", (old_id,)).fetchone())
            new_row = dict(connection.execute("SELECT * FROM inference_tasks WHERE id=?", (new.json()["id"],)).fetchone())
            binding = connection.execute("SELECT channel_id FROM project_model_bindings WHERE project_name=? AND model_alias=?", ("relay_project", MODEL)).fetchone()
        assert old_row["channel_id"] == original["id"]
        assert new_row["channel_id"] == new_channel["id"]
        assert old_row["credential_id"] != new_row["credential_id"]
        assert binding["channel_id"] == original["id"]
        # Polling an old task must ignore the new routing default.
        refreshed = client.get("/api/v3/contents/generations/tasks/" + old_id, headers={"Authorization": "Bearer " + key})
        assert refreshed.status_code == 200
        assert requests[-1].headers["authorization"] == "Bearer secret-volcengine_ark-abcdefgh"
        before = len(requests)
        assert client.get("/api/v3/contents/generations/tasks/" + old_id, headers={"Authorization": "Bearer " + other_key}).status_code == 404
        assert len(requests) == before

    restored = create_app(settings)
    with TestClient(restored) as client:
        client.app.state.provider_relay.transport = httpx.MockTransport(handler)
        assert client.get("/api/v3/contents/generations/tasks/" + old_id, headers={"Authorization": "Bearer " + key}).status_code == 200
        assert requests[-1].headers["authorization"] == "Bearer secret-volcengine_ark-abcdefgh"
        created = client.post("/api/v3/contents/generations/tasks", json=BODY,
                              headers={"Authorization": "Bearer " + key, "Idempotency-Key": "after-restart"})
        assert created.status_code == 200
        assert [r for r in requests if r.method == "POST"][-1].headers["authorization"] == "Bearer secret-new-account"
        # Rolling back defaults also leaves already-created new-account tasks pinned.
        settings.volcengine_asset_accounts_json = None
        assert client.get("/api/v3/contents/generations/tasks/" + new.json()["id"], headers={"Authorization": "Bearer " + key}).status_code == 200
        assert requests[-1].headers["authorization"] == "Bearer secret-new-account"


@pytest.mark.parametrize("invalid", ["missing", "disabled", "foreign_project", "wrong_provider"])
def test_invalid_key_override_never_falls_back_to_another_account(tmp_path: Path, invalid: str) -> None:
    with relay_client(tmp_path) as client:
        key_id, key, _ = provision(client, provider="volcengine_ark", alias=MODEL, upstream_model="ignored-model")
        relay = client.app.state.provider_relay
        calls = []
        relay.transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json={"id": "must-not-submit"}))
        channel_id = "missing-channel"
        if invalid != "missing":
            project = "other_project" if invalid == "foreign_project" else "relay_project"
            if project == "other_project":
                create_project(client, project)
            channel = relay.create_channel(project_name=project, name="invalid target", provider="openai" if invalid == "wrong_provider" else "volcengine_ark",
                                           config={}, secret="secret-unused-account", actor_id="super-admin")
            channel_id = channel["id"]
            if invalid == "disabled":
                with client.app.state.database.connect() as connection:
                    connection.execute("UPDATE provider_channels SET status='disabled' WHERE id=?", (channel_id,))
        override(client.app.state.settings, key_id, channel_id)
        response = client.post("/api/v3/contents/generations/tasks", json=BODY, headers={"Authorization": "Bearer " + key})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "account_channel_unavailable"
        assert not calls


def test_ark_account_override_does_not_change_other_supplier_requests(tmp_path: Path) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 200, "msg": "", "request_id": "unchanged-supplier",
            "data": {"created": 1, "data": [{"url": "https://cdn.example.com/image.png"}],
                     "usage": {"input_tokens": 3, "output_tokens": 7, "total_tokens": 10}}})

    with relay_client(tmp_path) as client:
        key_id, key, _ = provision(client, provider="maxmodel", alias="gpt-image-2", upstream_model="ignored-model")
        override(client.app.state.settings, key_id, "unavailable-ark-channel")
        client.app.state.provider_relay.transport = httpx.MockTransport(handler)
        response = client.post("/v1/images/generations", json={"model": "gpt-image-2", "prompt": "isolated fixture"},
                               headers={"Authorization": "Bearer " + key, "Idempotency-Key": "unchanged-other-supplier"})
        assert response.status_code == 200
        assert len(requests) == 1
        assert requests[0].headers["authorization"] == "Bearer secret-maxmodel-abcdefgh"
