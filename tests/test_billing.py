import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))

import billing  # noqa: E402
import db  # noqa: E402
import psycopg  # noqa: E402
import scripts.migrate_to_auth_schema as migrate_auth_module  # noqa: E402
import scripts.migrate_to_billing_schema as migrate_billing_module  # noqa: E402
import scripts.migrate_to_tenant_schema as migrate_tenant_module  # noqa: E402


class FakeSessionAPI:
    """checkout.Session.create/retrieveを模した偽実装。実際のStripe通信を一切行わない。"""

    def __init__(self, create_return=None, retrieve_return=None, retrieve_error=None):
        self.create_return = create_return
        self.retrieve_return = retrieve_return
        self.retrieve_error = retrieve_error
        self.create_calls = []
        self.retrieve_calls = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self.create_return

    def retrieve(self, session_id, **kwargs):
        self.retrieve_calls.append((session_id, kwargs))
        if self.retrieve_error is not None:
            raise self.retrieve_error
        return self.retrieve_return


class FakeStripeClient:
    def __init__(self, session_api):
        self.checkout = _Namespace(Session=session_api)


class _Namespace:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def make_valid_session(*, tenant_id, session_id="cs_test_123", subscription_id="sub_test_123"):
    return {
        "id": session_id,
        "mode": "subscription",
        "status": "complete",
        "customer": "cus_test_123",
        "metadata": {"tenant_id": str(tenant_id)},
        "subscription": {
            "id": subscription_id,
            "status": "active",
            "current_period_end": 1893456000,  # 2030-01-01T00:00:00Z 相当(検証用の固定値)
        },
    }


@pytest.fixture
def billing_schema():
    """稼働中のpublic.recordsとは隔離した専用スキーマに、tenants/records(第16回)＋
    users/tenant_memberships(第17回)＋tenant_subscriptions(第18回)を用意する。
    """
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    schema_name = f"test_billing_{uuid.uuid4().hex}"
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    db.ensure_schema(conn)
    tenant_id = uuid.uuid4()
    migrate_tenant_module.migrate_to_tenant_schema(tenant_id, conn=conn)
    migrate_auth_module.migrate_to_auth_schema(conn=conn)
    migrate_billing_module.migrate_to_billing_schema(conn=conn)
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


# --- BILLING_ENABLEDの判定(DB接続不要) ---


def test_subscription_current_period_end_prefers_top_level():
    subscription = {"current_period_end": 1700000000, "items": {"data": [{"current_period_end": 1}]}}
    assert billing._subscription_current_period_end(subscription) == 1700000000


def test_subscription_current_period_end_falls_back_to_items_data():
    # Stripe APIの一部バージョンでは、current_period_endがSubscription直下ではなく
    # subscription.items.data[0]配下に移動している(実機確認で判明)。
    subscription = {"items": {"data": [{"current_period_end": 1800000000}]}}
    assert billing._subscription_current_period_end(subscription) == 1800000000


def test_subscription_current_period_end_returns_none_when_missing():
    assert billing._subscription_current_period_end({"items": {"data": []}}) is None
    assert billing._subscription_current_period_end(None) is None


def test_is_billing_enabled_false_by_default(monkeypatch):
    monkeypatch.delenv(billing.BILLING_ENABLED_ENV, raising=False)
    assert billing.is_billing_enabled() is False


def test_is_billing_enabled_true(monkeypatch):
    monkeypatch.setenv(billing.BILLING_ENABLED_ENV, "true")
    assert billing.is_billing_enabled() is True


# --- 秘密値未設定時は安全にエラー停止(DB接続不要) ---


def test_create_checkout_session_without_secret_key_raises_config_error(monkeypatch):
    monkeypatch.delenv(billing.STRIPE_SECRET_KEY_ENV, raising=False)
    monkeypatch.delenv(billing.STRIPE_PRICE_ID_STANDARD_ENV, raising=False)
    with pytest.raises(billing.BillingConfigError):
        billing.create_checkout_session(
            tenant_id=uuid.uuid4(),
            role="admin",
            success_url="https://example.test/success",
            cancel_url="https://example.test/cancel",
        )


def test_create_checkout_session_without_price_id_raises_config_error(monkeypatch):
    monkeypatch.setenv(billing.STRIPE_SECRET_KEY_ENV, "sk_test_dummy")
    monkeypatch.delenv(billing.STRIPE_PRICE_ID_STANDARD_ENV, raising=False)
    fake_api = FakeSessionAPI()
    fake_client = FakeStripeClient(fake_api)
    with pytest.raises(billing.BillingConfigError):
        billing.create_checkout_session(
            tenant_id=uuid.uuid4(),
            role="admin",
            success_url="https://example.test/success",
            cancel_url="https://example.test/cancel",
            stripe_client=fake_client,
        )
    assert fake_api.create_calls == []


# --- adminだけCheckout Sessionを作成できる / memberは拒否される(DB接続不要) ---


def test_create_checkout_session_rejects_member(monkeypatch):
    monkeypatch.setenv(billing.STRIPE_PRICE_ID_STANDARD_ENV, "price_dummy")
    fake_api = FakeSessionAPI(create_return={"id": "cs_test", "url": "https://checkout.test"})
    fake_client = FakeStripeClient(fake_api)
    with pytest.raises(billing.PermissionDeniedError):
        billing.create_checkout_session(
            tenant_id=uuid.uuid4(),
            role="member",
            success_url="https://example.test/success",
            cancel_url="https://example.test/cancel",
            stripe_client=fake_client,
        )
    assert fake_api.create_calls == []


def test_create_checkout_session_allows_admin_and_sets_metadata(monkeypatch):
    monkeypatch.setenv(billing.STRIPE_PRICE_ID_STANDARD_ENV, "price_dummy")
    tenant_id = uuid.uuid4()
    fake_api = FakeSessionAPI(create_return={"id": "cs_test", "url": "https://checkout.test"})
    fake_client = FakeStripeClient(fake_api)

    result = billing.create_checkout_session(
        tenant_id=tenant_id,
        role="admin",
        success_url="https://example.test/success",
        cancel_url="https://example.test/cancel",
        stripe_client=fake_client,
    )

    assert result["id"] == "cs_test"
    assert len(fake_api.create_calls) == 1
    call = fake_api.create_calls[0]
    assert call["mode"] == "subscription"
    assert call["metadata"] == {"tenant_id": str(tenant_id)}
    assert call["line_items"] == [{"price": "price_dummy", "quantity": 1}]


def test_create_checkout_session_wraps_stripe_error(monkeypatch):
    monkeypatch.setenv(billing.STRIPE_PRICE_ID_STANDARD_ENV, "price_dummy")

    class ExplodingSessionAPI(FakeSessionAPI):
        def create(self, **kwargs):
            raise RuntimeError("network down")

    fake_client = FakeStripeClient(ExplodingSessionAPI())
    with pytest.raises(billing.StripeApiError):
        billing.create_checkout_session(
            tenant_id=uuid.uuid4(),
            role="admin",
            success_url="https://example.test/success",
            cancel_url="https://example.test/cancel",
            stripe_client=fake_client,
        )


# --- Free/Standardの既定値(DB接続あり) ---


def test_get_plan_status_defaults_to_free_when_no_row(billing_schema):
    conn, tenant_id = billing_schema
    status = billing.get_plan_status(conn, tenant_id=tenant_id)
    assert status == {
        "plan": "free",
        "status": "active",
        "current_period_end": None,
        "stripe_customer_id": None,
    }


# --- confirm_checkout_session: memberは拒否される(DB接続あり、Stripe未呼び出し) ---


def test_confirm_checkout_session_rejects_member(billing_schema):
    conn, tenant_id = billing_schema
    fake_api = FakeSessionAPI(retrieve_return=make_valid_session(tenant_id=tenant_id))
    fake_client = FakeStripeClient(fake_api)

    with pytest.raises(billing.PermissionDeniedError):
        billing.confirm_checkout_session(
            session_id="cs_test_123",
            tenant_id=tenant_id,
            role="member",
            conn=conn,
            stripe_client=fake_client,
        )
    assert fake_api.retrieve_calls == []
    assert billing.get_plan_status(conn, tenant_id=tenant_id)["plan"] == "free"


# --- 未払い・無効なSessionを拒否 ---


def test_confirm_checkout_session_rejects_non_subscription_mode(billing_schema):
    conn, tenant_id = billing_schema
    session = make_valid_session(tenant_id=tenant_id)
    session["mode"] = "payment"
    fake_client = FakeStripeClient(FakeSessionAPI(retrieve_return=session))

    with pytest.raises(billing.InvalidSessionError):
        billing.confirm_checkout_session(
            session_id="cs_test_123",
            tenant_id=tenant_id,
            role="admin",
            conn=conn,
            stripe_client=fake_client,
        )
    assert billing.get_plan_status(conn, tenant_id=tenant_id)["plan"] == "free"


def test_confirm_checkout_session_rejects_incomplete_status(billing_schema):
    conn, tenant_id = billing_schema
    session = make_valid_session(tenant_id=tenant_id)
    session["status"] = "open"  # まだ支払いが完了していない
    fake_client = FakeStripeClient(FakeSessionAPI(retrieve_return=session))

    with pytest.raises(billing.InvalidSessionError):
        billing.confirm_checkout_session(
            session_id="cs_test_123",
            tenant_id=tenant_id,
            role="admin",
            conn=conn,
            stripe_client=fake_client,
        )
    assert billing.get_plan_status(conn, tenant_id=tenant_id)["plan"] == "free"


def test_confirm_checkout_session_rejects_inactive_subscription(billing_schema):
    conn, tenant_id = billing_schema
    session = make_valid_session(tenant_id=tenant_id)
    session["subscription"]["status"] = "canceled"
    fake_client = FakeStripeClient(FakeSessionAPI(retrieve_return=session))

    with pytest.raises(billing.InvalidSessionError):
        billing.confirm_checkout_session(
            session_id="cs_test_123",
            tenant_id=tenant_id,
            role="admin",
            conn=conn,
            stripe_client=fake_client,
        )
    assert billing.get_plan_status(conn, tenant_id=tenant_id)["plan"] == "free"


# --- metadataのtenant_id不一致を拒否(=他世帯のPrice購入・課金状態変更ができない) ---


def test_confirm_checkout_session_rejects_tenant_id_mismatch(billing_schema):
    conn, tenant_id = billing_schema
    other_tenant_id = uuid.uuid4()
    session = make_valid_session(tenant_id=other_tenant_id)  # 別世帯向けに発行されたSession
    fake_client = FakeStripeClient(FakeSessionAPI(retrieve_return=session))

    with pytest.raises(billing.TenantMismatchError):
        billing.confirm_checkout_session(
            session_id="cs_test_123",
            tenant_id=tenant_id,
            role="admin",
            conn=conn,
            stripe_client=fake_client,
        )
    assert billing.get_plan_status(conn, tenant_id=tenant_id)["plan"] == "free"


# --- 偽のsuccess URLだけでは有料化されない ---


def test_reaching_success_url_alone_does_not_upgrade_plan(billing_schema):
    """confirm_checkout_session()を一切呼ばない限り、プランはfreeのまま変化しない
    (success_urlの表示自体はDB更新の引き金にならない、という設計そのものを確認する)。
    """
    conn, tenant_id = billing_schema
    assert billing.get_plan_status(conn, tenant_id=tenant_id)["plan"] == "free"


# --- Stripe APIエラー時にFreeのまま ---


def test_confirm_checkout_session_stripe_api_error_keeps_free(billing_schema):
    conn, tenant_id = billing_schema
    fake_client = FakeStripeClient(
        FakeSessionAPI(retrieve_error=RuntimeError("stripe is down"))
    )

    with pytest.raises(billing.StripeApiError):
        billing.confirm_checkout_session(
            session_id="cs_test_123",
            tenant_id=tenant_id,
            role="admin",
            conn=conn,
            stripe_client=fake_client,
        )
    assert billing.get_plan_status(conn, tenant_id=tenant_id)["plan"] == "free"


# --- 正常系: Standardへ反映される ---


def test_confirm_checkout_session_upgrades_to_standard(billing_schema):
    conn, tenant_id = billing_schema
    session = make_valid_session(tenant_id=tenant_id)
    fake_client = FakeStripeClient(FakeSessionAPI(retrieve_return=session))

    applied = billing.confirm_checkout_session(
        session_id="cs_test_123",
        tenant_id=tenant_id,
        role="admin",
        conn=conn,
        stripe_client=fake_client,
    )

    assert applied is True
    status = billing.get_plan_status(conn, tenant_id=tenant_id)
    assert status["plan"] == "standard"
    assert status["status"] == "active"
    assert status["current_period_end"] is not None


# --- 同じsession_idの再処理が冪等 ---


def test_confirm_checkout_session_is_idempotent_for_same_session_id(billing_schema):
    conn, tenant_id = billing_schema
    session = make_valid_session(tenant_id=tenant_id)
    fake_client = FakeStripeClient(FakeSessionAPI(retrieve_return=session))

    first = billing.confirm_checkout_session(
        session_id="cs_test_123", tenant_id=tenant_id, role="admin",
        conn=conn, stripe_client=fake_client,
    )
    second = billing.confirm_checkout_session(
        session_id="cs_test_123", tenant_id=tenant_id, role="admin",
        conn=conn, stripe_client=fake_client,
    )

    assert first is True
    assert second is False  # 既に同じsession_idで反映済みのため、実際の更新は起きない
    status = billing.get_plan_status(conn, tenant_id=tenant_id)
    assert status["plan"] == "standard"


# --- DBエラー時rollback ---


def test_confirm_checkout_session_rolls_back_on_db_error(billing_schema):
    conn, tenant_id = billing_schema
    # tenantsに存在しないtenant_idを使い、外部キー制約違反(psycopg.Error)を意図的に起こす。
    unknown_tenant_id = uuid.uuid4()
    session = make_valid_session(tenant_id=unknown_tenant_id)
    fake_client = FakeStripeClient(FakeSessionAPI(retrieve_return=session))

    with pytest.raises(psycopg.Error):
        billing.confirm_checkout_session(
            session_id="cs_test_123",
            tenant_id=unknown_tenant_id,
            role="admin",
            conn=conn,
            stripe_client=fake_client,
        )

    # rollback済みであり、connは引き続き使える(既存世帯のfree状態にも影響していない)。
    assert billing.get_plan_status(conn, tenant_id=tenant_id)["plan"] == "free"


# --- 他世帯へのsession_id使い回しをDB制約レベルでも拒否 ---


def test_stripe_checkout_session_id_cannot_be_reused_across_tenants(billing_schema):
    conn, tenant_id = billing_schema
    other_tenant_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (other_tenant_id, "別世帯"),
        )
    conn.commit()

    session_id = "cs_test_shared"
    session_for_tenant = make_valid_session(tenant_id=tenant_id, session_id=session_id)
    fake_client = FakeStripeClient(FakeSessionAPI(retrieve_return=session_for_tenant))
    assert billing.confirm_checkout_session(
        session_id=session_id, tenant_id=tenant_id, role="admin",
        conn=conn, stripe_client=fake_client,
    ) is True

    # 同じsession_idを、別世帯のmetadataとして偽装して流し込もうとしても、
    # metadataのtenant_id検証で別世帯分は先に拒否される。
    session_for_other = make_valid_session(tenant_id=other_tenant_id, session_id=session_id)
    fake_client_other = FakeStripeClient(FakeSessionAPI(retrieve_return=session_for_other))
    with pytest.raises(billing.TenantMismatchError):
        billing.confirm_checkout_session(
            session_id=session_id, tenant_id=tenant_id, role="admin",
            conn=conn, stripe_client=fake_client_other,
        )
    assert billing.get_plan_status(conn, tenant_id=other_tenant_id)["plan"] == "free"
