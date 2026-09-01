import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))

import billing  # noqa: E402
import db  # noqa: E402
import scripts.migrate_to_auth_schema as migrate_auth_module  # noqa: E402
import scripts.migrate_to_billing_schema as migrate_billing_module  # noqa: E402
import scripts.migrate_to_tenant_schema as migrate_tenant_module  # noqa: E402
import scripts.migrate_to_webhook_schema as migrate_webhook_module  # noqa: E402
import webhook  # noqa: E402


class _Namespace:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class FakeResourceAPI:
    """retrieve()を模した偽実装。retrieve_map: {id: return_value}。"""

    def __init__(self, retrieve_map=None, retrieve_error=None):
        self.retrieve_map = retrieve_map or {}
        self.retrieve_error = retrieve_error
        self.retrieve_calls = []

    def retrieve(self, resource_id, **kwargs):
        self.retrieve_calls.append((resource_id, kwargs))
        if self.retrieve_error is not None:
            raise self.retrieve_error
        return self.retrieve_map[resource_id]


def make_fake_client(*, session_api=None, subscription_api=None, invoice_api=None):
    return _Namespace(
        checkout=_Namespace(Session=session_api or FakeResourceAPI()),
        Subscription=subscription_api or FakeResourceAPI(),
        Invoice=invoice_api or FakeResourceAPI(),
    )


def make_event(*, event_id, event_type, object_id):
    return {"id": event_id, "type": event_type, "data": {"object": {"id": object_id}}}


def make_valid_session(*, tenant_id, session_id, subscription_id):
    return {
        "id": session_id,
        "mode": "subscription",
        "status": "complete",
        "customer": "cus_test_123",
        "metadata": {"tenant_id": str(tenant_id)},
        "subscription": {
            "id": subscription_id,
            "status": "active",
            "current_period_end": 1893456000,
        },
    }


@pytest.fixture
def webhook_schema():
    """稼働中のpublic.recordsとは隔離した専用スキーマに、tenants/records(第16回)＋
    users/tenant_memberships(第17回)＋tenant_subscriptions(第18回)＋
    processed_stripe_events(第19回)を用意する。
    """
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    schema_name = f"test_webhook_{uuid.uuid4().hex}"
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    db.ensure_schema(conn)
    tenant_id = uuid.uuid4()
    migrate_tenant_module.migrate_to_tenant_schema(tenant_id, conn=conn)
    migrate_auth_module.migrate_to_auth_schema(conn=conn)
    migrate_billing_module.migrate_to_billing_schema(conn=conn)
    migrate_webhook_module.migrate_to_webhook_schema(conn=conn)
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


# --- checkout.session.completed ---


def test_checkout_session_completed_applies_standard(webhook_schema):
    conn, tenant_id = webhook_schema
    session_id = "cs_test_wh_1"
    subscription_id = "sub_test_wh_1"
    session_api = FakeResourceAPI(
        retrieve_map={
            session_id: make_valid_session(
                tenant_id=tenant_id, session_id=session_id, subscription_id=subscription_id
            )
        }
    )
    client = make_fake_client(session_api=session_api)
    event = make_event(
        event_id="evt_1", event_type="checkout.session.completed", object_id=session_id
    )

    result = webhook.process_event(conn, event, stripe_client=client)

    assert result["handled"] is True
    status = billing.get_plan_status(conn, tenant_id=tenant_id)
    assert status["plan"] == "standard"


def test_checkout_session_completed_skips_missing_tenant_metadata(webhook_schema):
    conn, tenant_id = webhook_schema
    session_id = "cs_test_wh_2"
    session = make_valid_session(
        tenant_id=tenant_id, session_id=session_id, subscription_id="sub_test_wh_2"
    )
    session["metadata"] = {}
    session_api = FakeResourceAPI(retrieve_map={session_id: session})
    client = make_fake_client(session_api=session_api)
    event = make_event(
        event_id="evt_2", event_type="checkout.session.completed", object_id=session_id
    )

    result = webhook.process_event(conn, event, stripe_client=client)

    assert result["result"]["skipped"] == "missing_tenant_id"
    assert billing.get_plan_status(conn, tenant_id=tenant_id)["plan"] == "free"


# --- 重複イベント・未対応イベント ---


def test_process_event_is_idempotent_for_same_event_id(webhook_schema):
    conn, tenant_id = webhook_schema
    session_id = "cs_test_wh_3"
    subscription_id = "sub_test_wh_3"
    session_api = FakeResourceAPI(
        retrieve_map={
            session_id: make_valid_session(
                tenant_id=tenant_id, session_id=session_id, subscription_id=subscription_id
            )
        }
    )
    client = make_fake_client(session_api=session_api)
    event = make_event(
        event_id="evt_3", event_type="checkout.session.completed", object_id=session_id
    )

    first = webhook.process_event(conn, event, stripe_client=client)
    second = webhook.process_event(conn, event, stripe_client=client)

    assert first["handled"] is True
    assert second == {"handled": False, "reason": "duplicate"}
    # 2回目はStripe APIすら呼ばれていない(先にmark_stripe_event_processedで検知するため)
    assert len(session_api.retrieve_calls) == 1


def test_process_event_unhandled_type_is_marked_but_not_processed(webhook_schema):
    conn, tenant_id = webhook_schema
    client = make_fake_client()
    event = make_event(event_id="evt_4", event_type="customer.created", object_id="cus_x")

    result = webhook.process_event(conn, event, stripe_client=client)

    assert result == {"handled": False, "reason": "unhandled_event_type"}
    # 同じevent_idはもう一度渡しても重複として扱われる(処理はされない)
    second = webhook.process_event(conn, event, stripe_client=client)
    assert second == {"handled": False, "reason": "duplicate"}


# --- customer.subscription.updated ---


def test_subscription_updated_syncs_active_status(webhook_schema):
    conn, tenant_id = webhook_schema
    subscription_id = "sub_test_wh_5"
    # まず初回契約状態を作る
    db.upsert_subscription_if_new_session(
        conn,
        tenant_id=tenant_id,
        plan="standard",
        status="active",
        stripe_customer_id="cus_test_wh_5",
        stripe_subscription_id=subscription_id,
        stripe_checkout_session_id="cs_test_wh_5",
        current_period_end=None,
    )
    conn.commit()

    subscription_api = FakeResourceAPI(
        retrieve_map={
            subscription_id: {
                "id": subscription_id,
                "status": "trialing",
                "items": {"data": [{"current_period_end": 1893456000}]},
            }
        }
    )
    client = make_fake_client(subscription_api=subscription_api)
    event = make_event(
        event_id="evt_5", event_type="customer.subscription.updated", object_id=subscription_id
    )

    result = webhook.process_event(conn, event, stripe_client=client)

    assert result["handled"] is True
    status = billing.get_plan_status(conn, tenant_id=tenant_id)
    assert status["plan"] == "standard"
    assert status["current_period_end"] is not None


def test_subscription_updated_downgrades_to_free_on_inactive_status(webhook_schema):
    conn, tenant_id = webhook_schema
    subscription_id = "sub_test_wh_6"
    db.upsert_subscription_if_new_session(
        conn,
        tenant_id=tenant_id,
        plan="standard",
        status="active",
        stripe_customer_id="cus_test_wh_6",
        stripe_subscription_id=subscription_id,
        stripe_checkout_session_id="cs_test_wh_6",
        current_period_end=None,
    )
    conn.commit()

    subscription_api = FakeResourceAPI(
        retrieve_map={
            subscription_id: {
                "id": subscription_id,
                "status": "unpaid",
                "items": {"data": []},
            }
        }
    )
    client = make_fake_client(subscription_api=subscription_api)
    event = make_event(
        event_id="evt_6", event_type="customer.subscription.updated", object_id=subscription_id
    )

    webhook.process_event(conn, event, stripe_client=client)

    assert billing.get_plan_status(conn, tenant_id=tenant_id)["plan"] == "free"


def test_subscription_updated_unknown_subscription_is_skipped(webhook_schema):
    conn, tenant_id = webhook_schema
    subscription_api = FakeResourceAPI(
        retrieve_map={"sub_unknown": {"id": "sub_unknown", "status": "active", "items": {"data": []}}}
    )
    client = make_fake_client(subscription_api=subscription_api)
    event = make_event(
        event_id="evt_7", event_type="customer.subscription.updated", object_id="sub_unknown"
    )

    result = webhook.process_event(conn, event, stripe_client=client)

    assert result["result"]["skipped"] == "unknown_subscription"


# --- customer.subscription.deleted ---


def test_subscription_deleted_downgrades_to_free(webhook_schema):
    conn, tenant_id = webhook_schema
    subscription_id = "sub_test_wh_8"
    db.upsert_subscription_if_new_session(
        conn,
        tenant_id=tenant_id,
        plan="standard",
        status="active",
        stripe_customer_id="cus_test_wh_8",
        stripe_subscription_id=subscription_id,
        stripe_checkout_session_id="cs_test_wh_8",
        current_period_end=None,
    )
    conn.commit()

    client = make_fake_client()
    event = make_event(
        event_id="evt_8", event_type="customer.subscription.deleted", object_id=subscription_id
    )

    webhook.process_event(conn, event, stripe_client=client)

    status = billing.get_plan_status(conn, tenant_id=tenant_id)
    assert status["plan"] == "free"
    assert status["status"] == "canceled"


def test_subscription_deleted_unknown_subscription_does_not_touch_other_tenants(webhook_schema):
    conn, tenant_id = webhook_schema
    subscription_id = "sub_test_wh_9"
    db.upsert_subscription_if_new_session(
        conn,
        tenant_id=tenant_id,
        plan="standard",
        status="active",
        stripe_customer_id="cus_test_wh_9",
        stripe_subscription_id=subscription_id,
        stripe_checkout_session_id="cs_test_wh_9",
        current_period_end=None,
    )
    conn.commit()

    client = make_fake_client()
    event = make_event(
        event_id="evt_9", event_type="customer.subscription.deleted", object_id="sub_unrelated"
    )

    webhook.process_event(conn, event, stripe_client=client)

    # 無関係なsubscription_idのイベントは、既存世帯のStandard契約に影響しない
    assert billing.get_plan_status(conn, tenant_id=tenant_id)["plan"] == "standard"


# --- invoice.payment_failed ---


def test_invoice_payment_failed_marks_past_due_without_downgrading(webhook_schema):
    conn, tenant_id = webhook_schema
    subscription_id = "sub_test_wh_10"
    db.upsert_subscription_if_new_session(
        conn,
        tenant_id=tenant_id,
        plan="standard",
        status="active",
        stripe_customer_id="cus_test_wh_10",
        stripe_subscription_id=subscription_id,
        stripe_checkout_session_id="cs_test_wh_10",
        current_period_end=None,
    )
    conn.commit()

    invoice_id = "in_test_wh_10"
    invoice_api = FakeResourceAPI(
        retrieve_map={invoice_id: {"id": invoice_id, "subscription": subscription_id}}
    )
    client = make_fake_client(invoice_api=invoice_api)
    event = make_event(
        event_id="evt_10", event_type="invoice.payment_failed", object_id=invoice_id
    )

    webhook.process_event(conn, event, stripe_client=client)

    status = billing.get_plan_status(conn, tenant_id=tenant_id)
    assert status["plan"] == "standard"  # planは変更しない
    assert status["status"] == "past_due"


def test_invoice_payment_failed_unknown_subscription_is_skipped(webhook_schema):
    conn, tenant_id = webhook_schema
    invoice_id = "in_test_wh_11"
    invoice_api = FakeResourceAPI(
        retrieve_map={invoice_id: {"id": invoice_id, "subscription": "sub_unknown"}}
    )
    client = make_fake_client(invoice_api=invoice_api)
    event = make_event(
        event_id="evt_11", event_type="invoice.payment_failed", object_id=invoice_id
    )

    result = webhook.process_event(conn, event, stripe_client=client)

    assert result["result"]["skipped"] == "unknown_subscription"


# --- Billing Portal(解約導線) ---


def test_create_billing_portal_session_requires_admin(webhook_schema):
    conn, tenant_id = webhook_schema
    with pytest.raises(billing.PermissionDeniedError):
        billing.create_billing_portal_session(
            tenant_id=tenant_id,
            role="member",
            conn=conn,
            return_url="https://example.test/",
            stripe_client=make_fake_client(),
        )


def test_create_billing_portal_session_requires_existing_customer(webhook_schema):
    conn, tenant_id = webhook_schema
    with pytest.raises(billing.NoActiveSubscriptionError):
        billing.create_billing_portal_session(
            tenant_id=tenant_id,
            role="admin",
            conn=conn,
            return_url="https://example.test/",
            stripe_client=make_fake_client(),
        )


def test_create_billing_portal_session_succeeds_for_admin_with_subscription(webhook_schema):
    conn, tenant_id = webhook_schema
    db.upsert_subscription_if_new_session(
        conn,
        tenant_id=tenant_id,
        plan="standard",
        status="active",
        stripe_customer_id="cus_test_portal",
        stripe_subscription_id="sub_test_portal",
        stripe_checkout_session_id="cs_test_portal",
        current_period_end=None,
    )
    conn.commit()

    class FakePortalAPI:
        def __init__(self):
            self.create_calls = []

        def create(self, **kwargs):
            self.create_calls.append(kwargs)
            return {"url": "https://billing.stripe.test/session/abc"}

    portal_api = FakePortalAPI()
    client = make_fake_client()
    client.billing_portal = _Namespace(Session=portal_api)

    result = billing.create_billing_portal_session(
        tenant_id=tenant_id,
        role="admin",
        conn=conn,
        return_url="https://example.test/",
        stripe_client=client,
    )

    assert result["url"] == "https://billing.stripe.test/session/abc"
    assert portal_api.create_calls[0]["customer"] == "cus_test_portal"
