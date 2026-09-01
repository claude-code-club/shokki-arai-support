"""Stripe Webhookイベントの処理窓口（第19回: 継続課金・Webhook）。

署名検証はこのモジュールでは行わない。署名検証済みのeventオブジェクト
(stripe.Webhook.construct_event()の戻り値)を受け取り、重複防止・DB反映のみを担当する
(仕様書/Webhook設計.md⑥参照。署名検証はHTTPを受け取る側の責務)。

未知のtenant_id・subscription_idを含むイベントは、DBへ書き込まず無視する
(未登録の世帯を推測で作成しない。仕様書/認証基盤設計.md⑥と同じ方針)。
"""

import uuid
from datetime import datetime, timezone

import psycopg

import billing
import db


def _to_datetime(unix_ts):
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc) if unix_ts else None


def _handle_checkout_session_completed(conn, event, *, stripe_client):
    """success_urlへ戻れなかった場合の取りこぼしを防ぐ、確実な反映経路。

    イベントのペイロード内容は信用せず、session_idだけを使ってStripe APIから
    取得し直す(confirm_checkout_session()と同じ考え方。仕様書/Webhook設計.md⑧参照)。
    """
    session_obj = billing._attr(billing._attr(event, "data"), "object")
    session_id = billing._attr(session_obj, "id")
    session = stripe_client.checkout.Session.retrieve(session_id, expand=["subscription"])

    if billing._attr(session, "mode") != "subscription":
        return {"skipped": "not_subscription_mode"}

    subscription = billing._attr(session, "subscription")
    subscription_status = billing._attr(subscription, "status")
    if (
        billing._attr(session, "status") != "complete"
        or subscription_status not in billing._ACTIVE_SUBSCRIPTION_STATUSES
    ):
        return {"skipped": "not_active"}

    metadata = billing._attr(session, "metadata") or {}
    tenant_id_str = billing._attr(metadata, "tenant_id")
    if not tenant_id_str:
        return {"skipped": "missing_tenant_id"}
    try:
        tenant_id = uuid.UUID(tenant_id_str)
    except ValueError:
        return {"skipped": "invalid_tenant_id"}

    applied = billing._apply_checkout_completion(
        conn, tenant_id=tenant_id, session=session, subscription=subscription
    )
    return {"applied": applied}


def _handle_subscription_updated(conn, event, *, stripe_client):
    sub_obj = billing._attr(billing._attr(event, "data"), "object")
    subscription_id = billing._attr(sub_obj, "id")
    subscription = stripe_client.Subscription.retrieve(subscription_id)

    tenant_id = db.find_tenant_id_by_subscription(conn, stripe_subscription_id=subscription_id)
    if tenant_id is None:
        return {"skipped": "unknown_subscription"}

    status = billing._attr(subscription, "status")
    plan = "standard" if status in billing._ACTIVE_SUBSCRIPTION_STATUSES else "free"
    current_period_end = _to_datetime(billing._subscription_current_period_end(subscription))

    updated = db.update_subscription_status(
        conn, tenant_id=tenant_id, plan=plan, status=status, current_period_end=current_period_end
    )
    return {"updated": updated}


def _handle_subscription_deleted(conn, event, *, stripe_client):
    sub_obj = billing._attr(billing._attr(event, "data"), "object")
    subscription_id = billing._attr(sub_obj, "id")

    tenant_id = db.find_tenant_id_by_subscription(conn, stripe_subscription_id=subscription_id)
    if tenant_id is None:
        return {"skipped": "unknown_subscription"}

    updated = db.update_subscription_status(
        conn, tenant_id=tenant_id, plan="free", status="canceled", current_period_end=None
    )
    return {"updated": updated}


def _handle_invoice_payment_failed(conn, event, *, stripe_client):
    """支払い失敗をstatus='past_due'として記録するのみ。planは変更しない
    (自動的なFree降格・猶予期間の設計は将来課題。仕様書/Webhook設計.md⑤⑩参照)。
    """
    invoice_obj = billing._attr(billing._attr(event, "data"), "object")
    invoice_id = billing._attr(invoice_obj, "id")
    invoice = stripe_client.Invoice.retrieve(invoice_id)

    subscription_id = billing._attr(invoice, "subscription")
    if not subscription_id:
        return {"skipped": "not_subscription_invoice"}

    tenant_id = db.find_tenant_id_by_subscription(conn, stripe_subscription_id=subscription_id)
    if tenant_id is None:
        return {"skipped": "unknown_subscription"}

    current = billing.get_plan_status(conn, tenant_id=tenant_id)
    updated = db.update_subscription_status(
        conn,
        tenant_id=tenant_id,
        plan=current["plan"],
        status="past_due",
        current_period_end=current["current_period_end"],
    )
    return {"updated": updated}


_HANDLERS = {
    "checkout.session.completed": _handle_checkout_session_completed,
    "customer.subscription.updated": _handle_subscription_updated,
    "customer.subscription.deleted": _handle_subscription_deleted,
    "invoice.payment_failed": _handle_invoice_payment_failed,
}


def process_event(conn, event, *, stripe_client):
    """署名検証済みのeventを1件処理する。

    重複イベント対策(mark_stripe_event_processed)と、実際のハンドラ処理を同一
    トランザクションにまとめてcommitする。途中で失敗した場合はイベント記録ごと
    rollbackされるため、Stripe側の再送で再度正しく処理できる。

    戻り値: {"handled": bool, ...}の辞書。DBエラー時はpsycopg.Errorをそのまま送出する。
    """
    event_id = billing._attr(event, "id")
    event_type = billing._attr(event, "type")
    handler = _HANDLERS.get(event_type)

    try:
        is_new = db.mark_stripe_event_processed(conn, event_id=event_id, event_type=event_type)
        if not is_new:
            conn.commit()
            return {"handled": False, "reason": "duplicate"}

        if handler is None:
            conn.commit()
            return {"handled": False, "reason": "unhandled_event_type"}

        result = handler(conn, event, stripe_client=stripe_client)
        conn.commit()
        return {"handled": True, "result": result}
    except psycopg.Error:
        conn.rollback()
        raise
