import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))

import billing  # noqa: E402
import db  # noqa: E402
import metering  # noqa: E402
import scripts.migrate_to_auth_schema as migrate_auth_module  # noqa: E402
import scripts.migrate_to_billing_schema as migrate_billing_module  # noqa: E402
import scripts.migrate_to_tenant_schema as migrate_tenant_module  # noqa: E402
import scripts.migrate_to_usage_schema as migrate_usage_module  # noqa: E402


@pytest.fixture
def usage_schema():
    """稼働中のpublic.recordsとは隔離した専用スキーマに、tenants/records(第16回)＋
    users/tenant_memberships(第17回)＋tenant_subscriptions(第18回)＋
    tenant_usage(第20回)を用意する。
    """
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    schema_name = f"test_usage_{uuid.uuid4().hex}"
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    db.ensure_schema(conn)
    tenant_id = uuid.uuid4()
    migrate_tenant_module.migrate_to_tenant_schema(tenant_id, conn=conn)
    migrate_auth_module.migrate_to_auth_schema(conn=conn)
    migrate_billing_module.migrate_to_billing_schema(conn=conn)
    migrate_usage_module.migrate_to_usage_schema(conn=conn)
    conn.commit()

    try:
        yield conn, tenant_id
    finally:
        conn.rollback()
        conn.close()
        cleanup_conn = db.get_connection()
        try:
            with cleanup_conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()


def make_second_tenant(conn):
    """別世帯を1つ作成し、そのtenant_idを返す(他世帯への非干渉テスト用)。"""
    other_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s)", (other_id, "別世帯")
        )
    return other_id


# --- has_standard_access(): DB不要、plan/statusの組み合わせ ---


@pytest.mark.parametrize(
    "plan,status,expected",
    [
        ("free", "active", False),
        ("standard", "active", True),
        ("standard", "trialing", True),
        ("standard", "past_due", False),  # 支払い失敗中はFree相当(仕様書③参照)
        ("standard", "canceled", False),
        ("standard", "unpaid", False),
        ("free", "canceled", False),
    ],
)
def test_has_standard_access(plan, status, expected):
    plan_status = {"plan": plan, "status": status, "current_period_end": None}
    assert billing.has_standard_access(plan_status) is expected


# --- db.get_tenant_usage_count / increment_tenant_usage_if_under_limit ---


def test_get_usage_count_defaults_to_zero_when_no_row(usage_schema):
    conn, tenant_id = usage_schema
    count = db.get_tenant_usage_count(
        conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=date(2026, 9, 1)
    )
    assert count == 0


def test_increment_creates_row_and_increments(usage_schema):
    conn, tenant_id = usage_schema
    period = date(2026, 9, 1)
    first = db.increment_tenant_usage_if_under_limit(
        conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=period, limit=3
    )
    second = db.increment_tenant_usage_if_under_limit(
        conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=period, limit=3
    )
    conn.commit()
    assert first == 1
    assert second == 2
    assert (
        db.get_tenant_usage_count(
            conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=period
        )
        == 2
    )


def test_increment_rejects_at_limit(usage_schema):
    conn, tenant_id = usage_schema
    period = date(2026, 9, 1)
    for _ in range(3):
        result = db.increment_tenant_usage_if_under_limit(
            conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=period, limit=3
        )
        assert result is not None
    fourth = db.increment_tenant_usage_if_under_limit(
        conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=period, limit=3
    )
    conn.commit()
    assert fourth is None
    assert (
        db.get_tenant_usage_count(
            conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=period
        )
        == 3
    )


def test_increment_with_none_limit_is_unlimited(usage_schema):
    conn, tenant_id = usage_schema
    period = date(2026, 9, 1)
    for _ in range(10):
        result = db.increment_tenant_usage_if_under_limit(
            conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=period, limit=None
        )
        assert result is not None
    conn.commit()
    assert (
        db.get_tenant_usage_count(
            conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=period
        )
        == 10
    )


def test_increment_does_not_affect_other_tenant(usage_schema):
    conn, tenant_id = usage_schema
    other_tenant_id = make_second_tenant(conn)
    period = date(2026, 9, 1)
    db.increment_tenant_usage_if_under_limit(
        conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=period, limit=3
    )
    conn.commit()
    assert (
        db.get_tenant_usage_count(
            conn, tenant_id=other_tenant_id, metric_key="monthly_reflection", period_start=period
        )
        == 0
    )


def test_increment_does_not_affect_other_metric_or_period(usage_schema):
    conn, tenant_id = usage_schema
    db.increment_tenant_usage_if_under_limit(
        conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=date(2026, 8, 1), limit=3
    )
    conn.commit()
    # 別の期間(月が変わった)は0から始まる
    assert (
        db.get_tenant_usage_count(
            conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=date(2026, 9, 1)
        )
        == 0
    )
    # 別の指標も0から始まる
    assert (
        db.get_tenant_usage_count(
            conn, tenant_id=tenant_id, metric_key="other_metric", period_start=date(2026, 8, 1)
        )
        == 0
    )


def test_increment_rejects_unknown_tenant_via_foreign_key(usage_schema):
    conn, tenant_id = usage_schema
    unknown_tenant_id = uuid.uuid4()
    with pytest.raises(Exception):
        db.increment_tenant_usage_if_under_limit(
            conn,
            tenant_id=unknown_tenant_id,
            metric_key="monthly_reflection",
            period_start=date(2026, 9, 1),
            limit=3,
        )
    conn.rollback()


# --- metering.check_and_increment_usage() ---


def test_check_and_increment_usage_raises_when_limit_exceeded(usage_schema):
    conn, tenant_id = usage_schema
    period = date(2026, 9, 1)
    for _ in range(3):
        metering.check_and_increment_usage(
            conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=period, limit=3
        )
    conn.commit()
    with pytest.raises(metering.UsageLimitExceededError):
        metering.check_and_increment_usage(
            conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=period, limit=3
        )
    # 上限到達時はDBへ書き込まれない(3のまま)
    assert (
        db.get_tenant_usage_count(
            conn, tenant_id=tenant_id, metric_key="monthly_reflection", period_start=period
        )
        == 3
    )


# --- metering.fetch_monthly_reflection_status() / use_monthly_reflection() ---


def test_use_monthly_reflection_free_allows_up_to_limit_then_rejects(usage_schema):
    conn, tenant_id = usage_schema

    for i in range(1, 4):
        count = metering.use_monthly_reflection_with_conn(conn, tenant_id=tenant_id)
        conn.commit()
        assert count == i

    with pytest.raises(metering.UsageLimitExceededError):
        metering.use_monthly_reflection_with_conn(conn, tenant_id=tenant_id)
    conn.rollback()


def test_use_monthly_reflection_standard_is_unlimited(usage_schema):
    conn, tenant_id = usage_schema
    db.upsert_subscription_if_new_session(
        conn,
        tenant_id=tenant_id,
        plan="standard",
        status="active",
        stripe_customer_id="cus_test_metering",
        stripe_subscription_id="sub_test_metering",
        stripe_checkout_session_id="cs_test_metering",
        current_period_end=None,
    )
    conn.commit()

    for _ in range(5):
        metering.use_monthly_reflection_with_conn(conn, tenant_id=tenant_id)
        conn.commit()

    status = metering.get_monthly_reflection_status(conn, tenant_id=tenant_id)
    assert status["has_standard_access"] is True
    assert status["usage_count"] == 5
    assert status["limit"] is None


def test_fetch_monthly_reflection_status_free_defaults(usage_schema):
    conn, tenant_id = usage_schema
    status = metering.get_monthly_reflection_status(conn, tenant_id=tenant_id)
    assert status["has_standard_access"] is False
    assert status["usage_count"] == 0
    assert status["limit"] == metering.MONTHLY_REFLECTION_FREE_LIMIT


def test_past_due_is_treated_as_free_for_metering(usage_schema):
    conn, tenant_id = usage_schema
    db.upsert_subscription_if_new_session(
        conn,
        tenant_id=tenant_id,
        plan="standard",
        status="past_due",
        stripe_customer_id="cus_test_pastdue",
        stripe_subscription_id="sub_test_pastdue",
        stripe_checkout_session_id="cs_test_pastdue",
        current_period_end=None,
    )
    conn.commit()

    status = metering.get_monthly_reflection_status(conn, tenant_id=tenant_id)
    assert status["has_standard_access"] is False
    assert status["limit"] == metering.MONTHLY_REFLECTION_FREE_LIMIT

    for _ in range(3):
        metering.use_monthly_reflection_with_conn(conn, tenant_id=tenant_id)
        conn.commit()
    with pytest.raises(metering.UsageLimitExceededError):
        metering.use_monthly_reflection_with_conn(conn, tenant_id=tenant_id)
    conn.rollback()


def test_metering_unavailable_when_database_not_configured(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(metering.MeteringUnavailableError):
        metering.use_monthly_reflection(uuid.uuid4())
    with pytest.raises(metering.MeteringUnavailableError):
        metering.fetch_monthly_reflection_status(uuid.uuid4())


def test_current_period_start_is_first_of_jst_month():
    period = metering.current_period_start()
    assert period.day == 1
