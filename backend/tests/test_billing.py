import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.billing import (
    BillingManager,
    _loaded,
    current_month,
    micros_to_yuan,
    month_bounds_utc,
    next_month,
    normalize_resolution,
    previous_month,
    signed_yuan_to_micros,
    timestamp_month,
    validate_month,
    yuan_to_micros,
)
from app.admin_auth import AdminPrincipal
from app.errors import ApiError
from app.database import Database
from app.main import create_app
from app.official_rates import (
    BYTEPLUS_RATE_EFFECTIVE_MONTH,
    BYTEPLUS_RATE_SOURCE_URL,
    BYTEPLUS_RATE_VERSION,
    USD_CNY_FX,
)
from conftest import ADMIN_HEADERS, build_settings, create_key, create_project


PASSWORD = "Test-admin-password!2026"


SUPER_ACTOR = AdminPrincipal(
    id="super-test", username="super-test", display_name="Super Test", role="super_admin",
    session_id="super-session", csrf_token="csrf", must_change_password=False,
    mfa_verified=True, totp_enabled=True,
)


def _set_rate(client: TestClient, month: str, alias: str, prices: dict):
    return client.app.state.billing.set_rate(alias, month, prices, SUPER_ACTOR)


def _put_text_rate(client: TestClient, month: str, input_price: str | None, output_price: str | None):
    return _set_rate(
        client, month, "glm-5.2",
        {"inputPerMillionYuan": input_price, "outputPerMillionYuan": output_price},
    )


def _enable_project(client: TestClient, month: str, discount_bps: int = 10000):
    return client.put(
        "/api/internal/billing/projects/drama_prod",
        headers=ADMIN_HEADERS,
        json={
            "effectiveMonth": month,
            "enabled": True,
            "discountBps": discount_bps,
            "currentPassword": PASSWORD,
        },
    )


def _insert_relay_usage(app, key_id: str, *, usage_id: str = "usage-1", input_tokens=500_000, output_tokens=250_000):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with app.state.database.connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO provider_channels(id,project_name,name,provider) VALUES (?,?,?,?)",
            ("billing-channel", "drama_prod", "billing", "volcengine_ark"),
        )
        connection.execute(
            "INSERT INTO inference_usage(id,request_id,api_key_id,project_name,model_alias,channel_id,status,"
            "input_tokens,output_tokens,total_tokens,created_at,settled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                usage_id, f"request-{usage_id}", key_id, "drama_prod", "glm-5.2", "billing-channel",
                "succeeded", input_tokens, output_tokens,
                None if input_tokens is None or output_tokens is None else input_tokens + output_tokens,
                now, now,
            ),
        )


def test_billing_defaults_disabled_and_rates_require_reauthentication(tmp_path: Path) -> None:
    app = create_app(build_settings(tmp_path / "billing.db"))
    with TestClient(app) as client:
        create_project(client)
        month = current_month()
        terms = client.get(
            f"/api/internal/billing/projects/drama_prod?month={month}", headers=ADMIN_HEADERS
        )
        wrong = client.put(
            "/api/internal/billing/rates/glm-5.2",
            headers=ADMIN_HEADERS,
            json={
                "effectiveMonth": month,
                "prices": {"inputPerMillionYuan": "1", "outputPerMillionYuan": "2"},
                "currentPassword": "wrong",
            },
        )

    assert terms.status_code == 200
    assert terms.json()["billing"]["enabled"] is False
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "super_admin_required"


def test_text_usage_is_rated_with_project_discount_and_is_idempotent(tmp_path: Path) -> None:
    app = create_app(build_settings(tmp_path / "billing.db"))
    with TestClient(app) as client:
        create_project(client)
        key_id, _ = create_key(client)
        month = current_month()
        _put_text_rate(client, month, "1.20", "4.00")
        assert _enable_project(client, month, 8000).status_code == 200
        _insert_relay_usage(app, key_id)
        _insert_relay_usage(app, key_id, usage_id="older-usage")
        with app.state.database.connect() as connection:
            connection.execute(
                "UPDATE inference_usage SET created_at=?,settled_at=? WHERE id='older-usage'",
                (f"{previous_month(month)}-15 12:00:00", f"{previous_month(month)}-15 12:00:00"),
            )

        preview = client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={month}",
            headers=ADMIN_HEADERS,
        )
        repeated = client.post(
            f"/api/internal/billing/statements/{preview.json()['statement']['id']}/recalculate",
            headers=ADMIN_HEADERS,
        )

    statement = repeated.json()["statement"]
    assert preview.status_code == repeated.status_code == 200
    assert statement["subtotalYuan"] == "1.600000"
    assert statement["discountYuan"] == "0.320000"
    assert statement["totalYuan"] == "1.280000"
    assert statement["pendingCount"] == 0
    with sqlite3.connect(tmp_path / "billing.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM billing_usage_items").fetchone()[0] == 1


def test_missing_rate_is_pending_but_explicit_zero_is_billable(tmp_path: Path) -> None:
    app = create_app(build_settings(tmp_path / "billing.db"))
    with TestClient(app) as client:
        create_project(client)
        key_id, _ = create_key(client)
        month = current_month()
        # This scenario needs a genuinely missing price, not an inherited
        # bundled price from an earlier month.
        with app.state.database.connect() as connection:
            connection.execute("DELETE FROM billing_model_rates WHERE model_alias='glm-5.2'")
        _put_text_rate(client, month, "0", None)
        assert _enable_project(client, month).status_code == 200
        _insert_relay_usage(app, key_id, input_tokens=10, output_tokens=0)
        first = client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={month}", headers=ADMIN_HEADERS
        ).json()["statement"]
        assert first["pendingCount"] == 1

        _put_text_rate(client, month, "0", "0")
        second = client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={month}", headers=ADMIN_HEADERS
        ).json()["statement"]

    assert second["pendingCount"] == 0
    assert second["totalYuan"] == "0.000000"


def test_unknown_usage_stays_pending_and_current_month_cannot_be_confirmed(tmp_path: Path) -> None:
    app = create_app(build_settings(tmp_path / "billing.db"))
    with TestClient(app) as client:
        create_project(client)
        key_id, _ = create_key(client)
        month = current_month()
        _put_text_rate(client, month, "1", "1")
        _enable_project(client, month)
        _insert_relay_usage(app, key_id, input_tokens=10, output_tokens=None)
        statement = client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={month}", headers=ADMIN_HEADERS
        ).json()["statement"]
        confirmed = client.post(
            f"/api/internal/billing/statements/{statement['id']}/confirm",
            headers=ADMIN_HEADERS,
            json={"currentPassword": PASSWORD},
        )

    assert statement["pendingCount"] == 1
    assert confirmed.status_code == 409
    assert confirmed.json()["error"]["code"] == "billing_current_month_open"


def test_adjustment_export_and_project_deletion_protection(tmp_path: Path) -> None:
    app = create_app(build_settings(tmp_path / "billing.db"))
    with TestClient(app) as client:
        create_project(client)
        month = current_month()
        _enable_project(client, month)
        preview = client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={month}", headers=ADMIN_HEADERS
        ).json()["statement"]
        adjusted = client.post(
            f"/api/internal/billing/statements/{preview['id']}/adjustments",
            headers=ADMIN_HEADERS,
            json={"amountYuan": "12.345678", "reason": "人工服务补收", "currentPassword": PASSWORD},
        )
        exported = client.get(
            f"/api/internal/billing/statements/{preview['id']}/export.csv", headers=ADMIN_HEADERS
        )
        deletion = client.request(
            "DELETE", "/api/internal/project/delete", headers=ADMIN_HEADERS, json={"name": "drama_prod"}
        )

    assert adjusted.status_code == 200
    assert adjusted.json()["statement"]["totalYuan"] == "12.345678"
    assert exported.status_code == 200
    assert exported.content.startswith(b"\xef\xbb\xbf")
    assert "人工服务补收" in exported.content.decode("utf-8-sig")
    assert deletion.status_code == 409
    assert deletion.json()["error"]["code"] == "project_has_billing_history"


def test_money_helpers_and_month_boundaries() -> None:
    assert micros_to_yuan(1) == "0.000001"
    assert next_month("2026-12") == "2027-01"
    assert previous_month("2026-01") == "2025-12"


def test_billing_value_validation_and_normalization() -> None:
    assert month_bounds_utc("2026-09") == ("2026-08-31 16:00:00", "2026-09-30 16:00:00")
    assert timestamp_month("2026-08-31T16:00:00Z") == "2026-09"
    assert timestamp_month(1_788_192_000_000) == "2026-09"
    assert normalize_resolution("720p") == "720p"
    assert normalize_resolution(None, 1920, 1080) == "1080p"
    assert normalize_resolution(None, "bad", None) is None
    assert yuan_to_micros("1.2345678") == 1_234_568
    assert signed_yuan_to_micros("-2.5") == -2_500_000
    assert _loaded("{broken", {"safe": True}) == {"safe": True}
    for invalid in ("2026-13", "2026-1", "not-a-month"):
        with pytest.raises(ApiError):
            validate_month(invalid)
    for invalid in ("NaN", "-1", "100000001"):
        with pytest.raises(ApiError):
            yuan_to_micros(invalid)
    for invalid in ("0", "Infinity", "bad"):
        with pytest.raises(ApiError):
            signed_yuan_to_micros(invalid)


def test_image_and_video_rate_books_and_validation(tmp_path: Path) -> None:
    app = create_app(build_settings(tmp_path / "billing.db"))
    with TestClient(app) as client:
        create_project(client)
        month = current_month()
        image = _set_rate(client, month, "doubao-seedream-5.0-pro", {"perImageYuan": "0.75"})
        video = _set_rate(client, month, "doubao-seedance-2.5", {
            "perSecondByResolution": {"480p": "0.10", "720p": "0.20", "768p": None}
        })
        listed = client.get(f"/api/internal/billing/rates?month={month}", headers=ADMIN_HEADERS)
        with pytest.raises(ApiError) as invalid_resolution:
            _set_rate(client, month, "doubao-seedance-2.5", {"perSecondByResolution": {"4k": "1"}})
        with pytest.raises(ApiError) as missing_model:
            _set_rate(client, month, "not-a-model", {})

    assert image["prices"]["perImageYuan"] == "0.750000"
    assert video["prices"]["perSecondByResolution"]["720p"] == "0.200000"
    assert any(item["model"] == "doubao-seedream-5.0-pro" for item in listed.json()["rates"])
    assert invalid_resolution.value.code == "billing_resolution_invalid"
    assert missing_model.value.code == "billing_model_not_found"


def test_image_video_and_legacy_video_usage_are_rated_but_failed_tasks_are_excluded(tmp_path: Path) -> None:
    app = create_app(build_settings(tmp_path / "billing.db"))
    with TestClient(app) as client:
        create_project(client)
        key_id, _ = create_key(client)
        month = current_month()
        _enable_project(client, month, 9000)
        for alias, prices in (
            ("doubao-seedream-4.5", {"perImageYuan": "1.00"}),
            ("doubao-seedance-2.5", {"rules": [{
                "metric": "output_tokens", "dimension": "720p:no_video",
                "unitSize": 1_000_000, "unitPriceYuan": "0.50",
            }]}),
        ):
            _set_rate(client, month, alias, prices)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        with app.state.database.connect() as connection:
            connection.execute(
                "INSERT INTO provider_channels(id,project_name,name,provider) VALUES (?,?,?,?)",
                ("media-channel", "drama_prod", "media", "volcengine_ark"),
            )
            for values in (
                ("img-use", "img-request", "doubao-seedream-4.5", 2, None, None, None, None),
                ("vid-use", "vid-request", "doubao-seedance-2.5", None, 4.0, 1280, 720, 4_000_000),
            ):
                connection.execute(
                    "INSERT INTO inference_usage(id,request_id,api_key_id,project_name,model_alias,channel_id,status,"
                    "generated_images,video_seconds,video_width,video_height,output_tokens,created_at,settled_at) VALUES (?,?,?,?,?,?,?, ?,?,?,?,?,?,?)",
                    (values[0], values[1], key_id, "drama_prod", values[2], "media-channel", "succeeded", *values[3:], now, now),
                )
            for task_id, status, duration, resolution in (
                ("legacy-ok", "succeeded", 3, "720p"),
                ("legacy-failed", "failed", 3, "720p"),
                ("legacy-bad-duration", "succeeded", "bad", "720p"),
                ("legacy-no-resolution", "succeeded", 3, None),
            ):
                record = json.dumps({"status": status, "duration": duration, "resolution": resolution})
                connection.execute(
                    "INSERT INTO video_tasks(api_key_id,project_name,task_id,record_json,status,created_at) VALUES (?,?,?,?,?,?)",
                    (key_id, "drama_prod", task_id, record, status, now_ms),
                )
                output_tokens = 0 if task_id == "legacy-bad-duration" else 3_000_000
                connection.execute(
                    "INSERT INTO video_usage(api_key_id,project_name,task_id,model,output_tokens,created_at) VALUES (?,?,?,?,?,?)",
                    (key_id, "drama_prod", task_id, "doubao-seedance-2-5-260628", output_tokens, now),
                )
        statement = client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={month}", headers=ADMIN_HEADERS
        ).json()["statement"]
        detail = client.get(
            f"/api/internal/billing/statements/{statement['id']}", headers=ADMIN_HEADERS
        ).json()["statement"]

    # (2 images * 1 + (4M + 3M) video output tokens * .5/M) * 90%
    assert statement["subtotalYuan"] == "5.500000"
    assert statement["totalYuan"] == "4.950000"
    assert statement["pendingCount"] == 2
    assert {line["metric"] for line in detail["lines"]} == {"image", "output_tokens"}
    with sqlite3.connect(tmp_path / "billing.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM billing_usage_items").fetchone()[0] == 5


def test_statement_lifecycle_freezes_history_and_moves_late_usage_forward(tmp_path: Path) -> None:
    app = create_app(build_settings(tmp_path / "billing.db"))
    with TestClient(app) as client:
        create_project(client)
        key_id, _ = create_key(client)
        old_month = previous_month(current_month())
        with app.state.database.connect() as connection:
            connection.execute("DELETE FROM billing_model_rates WHERE model_alias='glm-5.2'")
            connection.execute(
                "INSERT INTO project_billing_terms(id,project_name,effective_month,enabled,discount_bps,updated_by) VALUES (?,?,?,?,?,?)",
                ("old-terms", "drama_prod", old_month, 1, 10000, "admin"),
            )
            for metric in ("input_tokens", "output_tokens"):
                connection.execute(
                    "INSERT INTO billing_model_rates(id,model_alias,metric,resolution,effective_month,unit_size,unit_price_micros,created_by) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (f"old-{metric}", "glm-5.2", metric, "", old_month, 1_000_000, 1_000_000, "admin"),
                )
        draft = client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={old_month}", headers=ADMIN_HEADERS
        ).json()["statement"]
        old_created_ms = int(
            datetime.fromisoformat(f"{old_month}-15T12:00:00+00:00").timestamp() * 1000
        )
        with app.state.database.connect() as connection:
            connection.execute(
                "INSERT INTO video_tasks(api_key_id,project_name,task_id,record_json,status,created_at) VALUES (?,?,?,?,?,?)",
                (key_id, "drama_prod", "still-running", "{}", "running", old_created_ms),
            )
        active_blocked = client.post(
            f"/api/internal/billing/statements/{draft['id']}/confirm",
            headers=ADMIN_HEADERS,
            json={"currentPassword": PASSWORD},
        )
        with app.state.database.connect() as connection:
            connection.execute(
                "UPDATE video_tasks SET status='failed' WHERE api_key_id=? AND task_id='still-running'",
                (key_id,),
            )
        confirmed = client.post(
            f"/api/internal/billing/statements/{draft['id']}/confirm",
            headers=ADMIN_HEADERS,
            json={"currentPassword": PASSWORD},
        )
        invalid_paid = client.post(
            f"/api/internal/billing/statements/{draft['id']}/mark-paid",
            headers=ADMIN_HEADERS,
            json={"currentPassword": PASSWORD, "paidAt": "not-a-date"},
        )
        paid = client.post(
            f"/api/internal/billing/statements/{draft['id']}/mark-paid",
            headers=ADMIN_HEADERS,
            json={"currentPassword": PASSWORD, "paidAt": "2026-09-03T08:00:00+08:00", "reference": "PAY-1", "note": "全额"},
        )
        occurred = f"{old_month}-15 12:00:00"
        with app.state.database.connect() as connection:
            connection.execute(
                "INSERT INTO provider_channels(id,project_name,name,provider) VALUES (?,?,?,?)",
                ("late-channel", "drama_prod", "late", "volcengine_ark"),
            )
            connection.execute(
                "INSERT INTO inference_usage(id,request_id,api_key_id,project_name,model_alias,channel_id,status,input_tokens,output_tokens,total_tokens,created_at,settled_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("late-use", "late-request", key_id, "drama_prod", "glm-5.2", "late-channel", "succeeded", 1_000_000, 0, 1_000_000, occurred, occurred),
            )
        current_preview = client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={current_month()}", headers=ADMIN_HEADERS
        ).json()["statement"]
        current_detail = client.get(
            f"/api/internal/billing/statements/{current_preview['id']}", headers=ADMIN_HEADERS
        ).json()["statement"]
        locked_adjustment = client.post(
            f"/api/internal/billing/statements/{draft['id']}/adjustments",
            headers=ADMIN_HEADERS,
            json={"amountYuan": "1", "reason": "不应允许", "currentPassword": PASSWORD},
        )

    assert active_blocked.status_code == 409
    assert active_blocked.json()["error"]["code"] == "billing_tasks_active"
    assert confirmed.json()["statement"]["status"] == "confirmed"
    assert invalid_paid.status_code == 422
    assert invalid_paid.json()["error"]["code"] == "billing_paid_at_invalid"
    assert paid.json()["statement"]["status"] == "paid"
    assert current_preview["totalYuan"] == "1.000000"
    assert current_detail["adjustments"][0]["type"] == "late_usage"
    assert locked_adjustment.status_code == 409


def test_draft_adjustments_filters_and_disabled_project_rebuild(tmp_path: Path) -> None:
    app = create_app(build_settings(tmp_path / "billing.db"))
    with TestClient(app) as client:
        create_project(client)
        month = current_month()
        assert client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={month}", headers=ADMIN_HEADERS
        ).json()["statement"] is None
        _enable_project(client, month)
        draft = client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={month}", headers=ADMIN_HEADERS
        ).json()["statement"]
        zero = client.post(
            f"/api/internal/billing/statements/{draft['id']}/adjustments",
            headers=ADMIN_HEADERS,
            json={"amountYuan": "0", "reason": "零金额", "currentPassword": PASSWORD},
        )
        blank = client.post(
            f"/api/internal/billing/statements/{draft['id']}/adjustments",
            headers=ADMIN_HEADERS,
            json={"amountYuan": "1", "reason": "   ", "currentPassword": PASSWORD},
        )
        added = client.post(
            f"/api/internal/billing/statements/{draft['id']}/adjustments",
            headers=ADMIN_HEADERS,
            json={"amountYuan": "-1.25", "reason": "=内部减免", "currentPassword": PASSWORD},
        ).json()["statement"]
        adjustment_id = added["adjustments"][0]["id"]
        exported = client.get(
            f"/api/internal/billing/statements/{draft['id']}/export.csv", headers=ADMIN_HEADERS
        ).content.decode("utf-8-sig")
        deleted = client.request(
            "DELETE",
            f"/api/internal/billing/statements/{draft['id']}/adjustments/{adjustment_id}",
            headers=ADMIN_HEADERS,
            json={"currentPassword": PASSWORD},
        )
        missing_delete = client.request(
            "DELETE",
            f"/api/internal/billing/statements/{draft['id']}/adjustments/{adjustment_id}",
            headers=ADMIN_HEADERS,
            json={"currentPassword": PASSWORD},
        )
        filtered = client.get(
            f"/api/internal/billing/statements?projectName=drama_prod&month={month}&status=draft",
            headers=ADMIN_HEADERS,
        )
        not_paid = client.post(
            f"/api/internal/billing/statements/{draft['id']}/mark-paid",
            headers=ADMIN_HEADERS,
            json={"currentPassword": PASSWORD},
        )
        disabled = client.put(
            "/api/internal/billing/projects/drama_prod",
            headers=ADMIN_HEADERS,
            json={"effectiveMonth": month, "enabled": False, "discountBps": 10000, "currentPassword": PASSWORD},
        )
        rebuilt = client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={month}", headers=ADMIN_HEADERS
        ).json()["statement"]

    assert zero.status_code == blank.status_code == 422
    assert "'=内部减免" in exported
    assert deleted.status_code == 200
    assert missing_delete.status_code == 404
    assert len(filtered.json()["statements"]) == 1
    assert not_paid.status_code == 409
    assert disabled.json()["billing"]["enabled"] is False
    assert rebuilt["totalYuan"] == "0.000000"


def test_rate_term_and_statement_error_boundaries(tmp_path: Path) -> None:
    app = create_app(build_settings(tmp_path / "billing.db"))
    with TestClient(app) as client:
        create_project(client)
        month = current_month()
        old_month = previous_month(month)
        missing_project = client.get(
            f"/api/internal/billing/projects/absent?month={month}", headers=ADMIN_HEADERS
        )
        with pytest.raises(ApiError) as bad_video_shape:
            _set_rate(client, month, "doubao-seedance-2.5", {"perSecondByResolution": ["0.1"]})
        with pytest.raises(ApiError) as past_rate:
            _set_rate(client, old_month, "glm-5.2", {"inputPerMillionYuan": "1"})
        past_terms = client.put(
            "/api/internal/billing/projects/drama_prod",
            headers=ADMIN_HEADERS,
            json={"effectiveMonth": old_month, "enabled": True, "discountBps": 10000, "currentPassword": PASSWORD},
        )
        _enable_project(client, month)
        draft = client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={month}", headers=ADMIN_HEADERS
        ).json()["statement"]
        with app.state.database.connect() as connection:
            connection.execute(
                "UPDATE billing_statements SET status='confirmed' WHERE id=?", (draft["id"],)
            )
        with pytest.raises(ApiError) as closed_rate:
            _put_text_rate(client, month, "1", "1")
        closed_terms = _enable_project(client, month)
        missing_statement = client.get(
            "/api/internal/billing/statements/not-found", headers=ADMIN_HEADERS
        )

    assert missing_project.status_code == 404
    assert bad_video_shape.value.code == "billing_rate_invalid"
    assert past_rate.value.status_code == 409
    assert past_terms.status_code == 409
    assert closed_rate.value.status_code == 409
    assert closed_terms.status_code == 409
    assert missing_statement.status_code == 404


def test_previous_month_pending_usage_blocks_confirmation(tmp_path: Path) -> None:
    app = create_app(build_settings(tmp_path / "billing.db"))
    with TestClient(app) as client:
        create_project(client)
        key_id, _ = create_key(client)
        month = previous_month(current_month())
        with app.state.database.connect() as connection:
            connection.execute("DELETE FROM billing_model_rates WHERE model_alias='glm-5.2'")
            connection.execute(
                "INSERT INTO project_billing_terms(id,project_name,effective_month,enabled,discount_bps,updated_by) VALUES (?,?,?,?,?,?)",
                ("pending-terms", "drama_prod", month, 1, 10000, "admin"),
            )
            connection.execute(
                "INSERT INTO billing_model_rates(id,model_alias,metric,resolution,effective_month,unit_size,unit_price_micros,created_by) VALUES (?,?,?,?,?,?,?,?)",
                ("pending-rate", "glm-5.2", "input_tokens", "", month, 1_000_000, 1_000_000, "admin"),
            )
        _insert_relay_usage(app, key_id, usage_id="pending-use", input_tokens=10, output_tokens=10)
        with app.state.database.connect() as connection:
            connection.execute(
                "UPDATE inference_usage SET created_at=?,settled_at=? WHERE id='pending-use'",
                (f"{month}-15 12:00:00", f"{month}-15 12:00:00"),
            )
        statement = client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={month}", headers=ADMIN_HEADERS
        ).json()["statement"]
        blocked = client.post(
            f"/api/internal/billing/statements/{statement['id']}/confirm",
            headers=ADMIN_HEADERS,
            json={"currentPassword": PASSWORD},
        )

    assert statement["pendingCount"] == 1
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "billing_usage_pending"


def test_future_billing_configuration_without_history_does_not_block_project_deletion(tmp_path: Path) -> None:
    app = create_app(build_settings(tmp_path / "billing.db"))
    with TestClient(app) as client:
        create_project(client)
        configured = _enable_project(client, next_month(current_month()))
        deleted = client.request(
            "DELETE", "/api/internal/project/delete", headers=ADMIN_HEADERS, json={"name": "drama_prod"}
        )

    assert configured.status_code == 200
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_billing_internal_normalization_and_maintenance_failure_isolation(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "billing.db")
    database.initialize()
    manager = BillingManager(database)
    assert timestamp_month("") == current_month()
    with pytest.raises(ApiError):
        yuan_to_micros("bad")
    assert normalize_resolution("999p") == "999p"
    assert _loaded({"already": "decoded"}, {"fallback": True}) == {"fallback": True}
    assert BillingManager._statement_number("中文项目", "2026-09").startswith("RB-202609-PROJECT-")
    assert BillingManager._principal_actor(type("Actor", (), {"id": "admin-1"})()) == "admin-1"
    assert BillingManager._relay_measurements({"modality": "image", "generated_images": None}) == (None, "usage_unknown")
    assert BillingManager._relay_measurements({"modality": "image", "generated_images": 3})[0][0][2] == 3
    assert BillingManager._relay_measurements({"modality": "embedding", "input_tokens": 12})[0] == [
        ("input_tokens", "", 12)
    ]
    assert BillingManager._relay_measurements({
        "modality": "audio", "capabilities_json": '{"billingMetric":"characters"}',
        "input_characters": 8, "audio_seconds": None,
    })[0] == [("characters", "", 8)]
    assert BillingManager._relay_measurements({
        "modality": "audio", "capabilities_json": '{"billingMetric":"audio_second"}',
        "input_characters": None, "audio_seconds": 2.5,
    })[0] == [("audio_second", "", pytest.approx(2.5))]
    assert BillingManager._relay_measurements({"modality": "video", "video_seconds": 0}) == (None, "usage_unknown")
    unresolved_video = {
        "modality": "video", "video_seconds": 4, "billing_metadata_json": "{}",
        "metadata_json": "{}", "video_width": None, "video_height": None,
    }
    assert BillingManager._relay_measurements(unresolved_video) == (None, "resolution_unknown")
    database.create_project("edge_project", "Edge", "")
    database.create_api_key("edge-key", "Edge key", "vap_live_edge", "edge-hash", "edge_project")
    with database.connect() as connection:
        assert manager._model_alias(connection, "unknown-upstream") is None
        first = manager._ensure_statement(connection, "edge_project", current_month())
        assert manager._ensure_statement(connection, "edge_project", current_month())["id"] == first["id"]
        connection.execute(
            "INSERT INTO project_billing_terms(id,project_name,effective_month,enabled,discount_bps,updated_by) VALUES (?,?,?,?,?,?)",
            ("edge-terms", "edge_project", current_month(), 1, 10000, "admin"),
        )
        manager._upsert_item(
            connection,
            source_type="relay",
            source_id="unmapped-edge",
            api_key_id="edge-key",
            project_name="edge_project",
            model_alias=None,
            occurred_at=f"{current_month()}-15 12:00:00",
            measurements=None,
            pending_reason=None,
        )
        connection.execute(
            "UPDATE billing_usage_items SET statement_id=? WHERE source_id='unmapped-edge'", (first["id"],)
        )
        connection.execute("UPDATE billing_statements SET status='confirmed' WHERE id=?", (first["id"],))
        manager._upsert_item(
            connection,
            source_type="relay",
            source_id="unmapped-edge",
            api_key_id="edge-key",
            project_name="edge_project",
            model_alias=None,
            occurred_at=f"{current_month()}-15 12:00:00",
            measurements=None,
            pending_reason=None,
        )
        connection.execute(
            "INSERT INTO billing_statement_lines(id,statement_id,model_alias,metric,resolution,quantity,unit_size,"
            "unit_price_micros,list_amount_micros,net_amount_micros) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("formula-line", first["id"], "=cmd", "image", "", "1", 1, 1, 1, 1),
        )
    assert "'=cmd" in manager.csv_bytes(first["id"]).decode("utf-8-sig")
    with pytest.raises(ApiError):
        manager.delete_adjustment("missing-statement", "missing-adjustment")
    with pytest.raises(ApiError):
        manager.mark_paid("missing-statement", None, None, None, type("Actor", (), {"id": "admin-1"})())

    async def fail_reconcile() -> None:
        raise RuntimeError("simulated background failure")

    async def stop_after_retry(_: int) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(manager, "reconcile", fail_reconcile)
    monkeypatch.setattr("app.billing.asyncio.sleep", stop_after_retry)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager.maintenance_loop())


def test_byteplus_official_usd_list_prices_are_seeded_as_cny_rates(tmp_path: Path) -> None:
    """Overseas list prices are published in USD, so one frozen FX snapshot seeds the CNY ledger."""
    app = create_app(build_settings(tmp_path / "byteplus-rates.db"))
    with TestClient(app) as client:
        billing = client.app.state.billing
        month = BYTEPLUS_RATE_EFFECTIVE_MONTH
        source = {
            "version": BYTEPLUS_RATE_VERSION,
            "url": BYTEPLUS_RATE_SOURCE_URL,
            "currency": "USD",
            "fxRateToCny": str(USD_CNY_FX),
        }

        video = billing.get_rate("dreamina-seedance-2.5", month, "byteplus_modelark")
        assert video["configured"] and video["officialSource"] == source
        # USD/M tokens * 6.705025, e.g. 10.70 -> 71.743768 CNY/M tokens.
        assert {(rule["dimension"], rule["unitPriceYuan"]) for rule in video["rules"]} == {
            ("480p:no_video", "71.743768"),
            ("480p:video", "42.912160"),
            ("720p:no_video", "71.743768"),
            ("720p:video", "42.912160"),
            ("1080p:no_video", "78.448793"),
            ("1080p:video", "46.935175"),
        }

        # The video-input variant is always cheaper than the text-only variant.
        prices = {rule["dimension"]: rule["unitPriceYuan"] for rule in video["rules"]}
        assert prices["720p:video"] < prices["720p:no_video"]

        # Image tiers follow the published 2.61 million pixel boundary.
        image = billing.get_rate("dola-seedream-5.0-pro", month)
        assert {(rule["dimension"], rule["unitPriceYuan"]) for rule in image["rules"]} == {
            ("input:after_first", "0.020115"),
            ("output:le2610000", "0.301726"),
            ("output:gt2610000", "0.603452"),
        }
        assert [rule["dimension"] for rule in image["editableRules"]] == [
            rule["dimension"] for rule in image["rules"]
        ]
        flat = billing.get_rate("dola-seedream-5.0-lite", month)
        assert [(rule["dimension"], rule["unitPriceYuan"]) for rule in flat["rules"]] == [("", "0.234676")]

        # Re-running startup keeps the same rows instead of duplicating them.
        with app.state.database.connect() as connection:
            before = {row["id"] for row in connection.execute(
                "SELECT id FROM billing_model_rates WHERE model_alias='dola-seedream-5.0-lite'")}
        app.state.database.initialize()
        with app.state.database.connect() as connection:
            after = {row["id"] for row in connection.execute(
                "SELECT id FROM billing_model_rates WHERE model_alias='dola-seedream-5.0-lite'")}

    assert before and before == after


def test_byteplus_project_discount_uses_the_seeded_official_prices(tmp_path: Path) -> None:
    """A project discount applies to the seeded CNY card, not to the USD list price."""
    app = create_app(build_settings(tmp_path / "byteplus-discount.db"))
    with TestClient(app) as client:
        create_project(client)
        key_id, _ = create_key(client)
        billing = client.app.state.billing
        month = current_month()
        billing.set_project_terms("drama_prod", month, True, 5000, SUPER_ACTOR)
        with app.state.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO provider_channels(id,project_name,name,provider) VALUES (?,?,?,?)",
                ("byteplus-channel", "drama_prod", "byteplus", "byteplus_modelark"),
            )
            # 854x480 is the 480p class, which is the cheapest published tier.
            connection.execute(
                "INSERT INTO inference_usage(id,request_id,api_key_id,project_name,model_alias,channel_id,status,"
                "output_tokens,video_width,video_height,created_at,settled_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "usage-byteplus-1", "request-byteplus-1", key_id, "drama_prod",
                    "dreamina-seedance-2.0-mini", "byteplus-channel", "succeeded", 1_000_000,
                    854, 480, f"{month}-14 10:41:05", f"{month}-14 10:41:05",
                ),
            )
        asyncio.run(billing.reconcile())
        statement = client.get(
            f"/api/internal/billing/preview?projectName=drama_prod&month={month}", headers=ADMIN_HEADERS
        ).json()["statement"]

    assert statement["pendingCount"] == 0
    # 3.50 USD/M tokens -> 23.467588 CNY, halved by the 5,000 bps project discount.
    assert statement["totalYuan"] == "11.733794"
