"""Stripeを使った世帯単位のサブスクリプション課金窓口。

第18回(課金①: Stripeサブスク決済)。Stripeがカード情報・顧客・サブスクリプションの
契約状態そのものを保持し(真実の源)、アプリDBは表示・アクセス制御用のキャッシュ
(現在のプラン・状態・Stripe側IDへの参照)だけを持つ(仕様書/Stripe課金設計.md③参照)。

BILLING_ENABLED(既定は未設定=無効)がtrueの場合のみ課金UI・課金フローが有効になる
(仕様書/Stripe課金設計.md⑧参照)。

Checkout Sessionは必ずサーバー側(このモジュール)で作成・検証する。success_urlへ
戻ってきたことの表示だけでStandardへ変更することは一切行わない。session_idを使って
Stripe APIからCheckout Sessionを取得し直し、mode・状態・metadataのtenant_id一致を
サーバー側で検証してから、初めてDBへ反映する(仕様書/Stripe課金設計.md⑤参照)。

Stripe通信はstripe_client引数として注入可能にしており(省略時のみ実際のstripeパッケージを
使う)、実際のStripe接続なしでテストできる(仕様書/Stripe課金設計.md⑦参照)。
"""

import os
from datetime import datetime, timezone

import psycopg

import db

BILLING_ENABLED_ENV = "BILLING_ENABLED"
STRIPE_SECRET_KEY_ENV = "STRIPE_SECRET_KEY"
STRIPE_PRICE_ID_STANDARD_ENV = "STRIPE_PRICE_ID_STANDARD"

_ACTIVE_SUBSCRIPTION_STATUSES = ("active", "trialing")


class BillingConfigError(Exception):
    """BILLING_ENABLED=trueなのに必要な環境変数(秘密鍵・Price ID)が未設定の場合。"""


class PermissionDeniedError(Exception):
    """admin以外が世帯プランの購入・変更操作を行おうとした場合。"""


class InvalidSessionError(Exception):
    """Checkout Sessionのmode・状態が不正で、Standardへ反映できない場合。

    未払い・subscriptionモードでない・状態が有効でない等、すべてこの例外にまとめる
    (success_urlの表示だけを信用しないための検証。仕様書/Stripe課金設計.md⑤参照)。
    """


class TenantMismatchError(Exception):
    """Checkout Sessionのmetadataのtenant_idが、現在ログイン中の世帯と一致しない場合。"""


class StripeApiError(Exception):
    """Stripe API呼び出し自体が失敗した場合(ネットワークエラー等)。DBには一切書き込まない。"""


class BillingUnavailableError(Exception):
    """PostgreSQLへの接続・読み書きに失敗した場合。"""


def is_billing_enabled():
    return os.environ.get(BILLING_ENABLED_ENV, "").strip().lower() == "true"


def _get_stripe_client():
    """実際のstripeパッケージをSTRIPE_SECRET_KEYで初期化して返す(テスト以外の既定経路)。"""
    api_key = os.environ.get(STRIPE_SECRET_KEY_ENV, "").strip()
    if not api_key:
        raise BillingConfigError(f"{STRIPE_SECRET_KEY_ENV}が設定されていません。")
    import stripe  # 遅延import(BILLING_ENABLED=false環境ではstripeパッケージ未使用のため)

    stripe.api_key = api_key
    return stripe


def _get_price_id():
    price_id = os.environ.get(STRIPE_PRICE_ID_STANDARD_ENV, "").strip()
    if not price_id:
        raise BillingConfigError(f"{STRIPE_PRICE_ID_STANDARD_ENV}が設定されていません。")
    return price_id


def _subscription_current_period_end(subscription):
    """current_period_endを取得する。Stripe APIの一部バージョンではSubscription直下、
    それ以外ではsubscription.items.data[0]配下に移動しているため、両方に対応する。
    表示用の情報であり、課金状態の判定(active/trialing)には使わない。
    """
    top_level = _attr(subscription, "current_period_end")
    if top_level is not None:
        return top_level
    items = _attr(subscription, "items")
    items_data = _attr(items, "data") or []
    if items_data:
        return _attr(items_data[0], "current_period_end")
    return None


def _attr(obj, name, default=None):
    """辞書(テストの偽オブジェクト)・Stripeオブジェクト(属性アクセスのみ)の両方から
    安全に値を取り出す。新しいstripeパッケージ(v15以降)のSession/Subscriptionは
    dictの.get()をサポートしないため(属性アクセスのみ)、テストではdictを使い続けつつ
    実際のstripeオブジェクトにも対応できるようにする。
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def get_plan_status(conn, *, tenant_id):
    """指定世帯の現在のプラン状態を返す({"plan", "status", "current_period_end"})。"""
    return db.get_subscription(conn, tenant_id=tenant_id)


def has_standard_access(plan_status):
    """Standard限定機能を使える契約状態かどうかを返す(第20回: プラン制限とメータリング)。

    第19回のcustomer.subscription.updatedハンドラ(webhook.py)と同じ判定
    (status in _ACTIVE_SUBSCRIPTION_STATUSES)を再利用し、別の判定を新設しない。
    status="past_due"(支払い失敗中)はplan="standard"のまま残るが、ここではFalseになる
    (仕様書/プラン制限・メータリング設計.md③参照。支払いが回復しWebhookでstatusが
    active等へ戻るまでFree相当の制限を適用する)。
    """
    return (
        plan_status["plan"] == "standard"
        and plan_status["status"] in _ACTIVE_SUBSCRIPTION_STATUSES
    )


def create_checkout_session(*, tenant_id, role, success_url, cancel_url, stripe_client=None):
    """Standardプランの契約用Checkout Sessionをサーバー側で作成する(admin専用)。

    tenant_id・roleは呼び出し側(app.py)がログインセッションから確定済みの値を渡すこと
    (ブラウザ入力・URLパラメータからは一切受け取らない)。
    """
    if role != "admin":
        raise PermissionDeniedError("世帯プランの購入にはadmin権限が必要です。")

    stripe_client = stripe_client or _get_stripe_client()
    price_id = _get_price_id()

    try:
        return stripe_client.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"tenant_id": str(tenant_id)},
            # subscription_data.metadataにも同じtenant_idを載せる。作成されるSubscription
            # オブジェクト自身にtenant_idを持たせることで、Webhook側でも参照できるようにする
            # (仕様書/Webhook設計.md⑤参照。実際の状態同期はDB側のstripe_subscription_id
            # 逆引きを主経路とし、こちらは冗長化のための予備情報)。
            subscription_data={"metadata": {"tenant_id": str(tenant_id)}},
        )
    except (BillingConfigError, PermissionDeniedError):
        raise
    except Exception as e:
        raise StripeApiError("Stripeとの通信に失敗しました。") from e


def confirm_checkout_session(*, session_id, tenant_id, role, conn, stripe_client=None):
    """session_idをStripe APIで取得し直し、検証を通過した場合のみStandardへ反映する。

    - roleがadminでない場合: PermissionDeniedError(DB未接続、Stripe未呼び出し)
    - 取得したSessionのmodeがsubscriptionでない、状態が有効でない場合: InvalidSessionError
    - metadataのtenant_idが現在の世帯と一致しない場合: TenantMismatchError
    - 上記いずれの場合もDBへは一切書き込まない(Freeのまま)。
    """
    if role != "admin":
        raise PermissionDeniedError("世帯プランの購入にはadmin権限が必要です。")

    stripe_client = stripe_client or _get_stripe_client()
    try:
        session = stripe_client.checkout.Session.retrieve(session_id, expand=["subscription"])
    except (BillingConfigError, PermissionDeniedError):
        raise
    except Exception as e:
        raise StripeApiError("Stripeとの通信に失敗しました。") from e

    if _attr(session, "mode") != "subscription":
        raise InvalidSessionError("このCheckout Sessionはsubscriptionモードではありません。")

    subscription = _attr(session, "subscription")
    subscription_status = _attr(subscription, "status")
    if _attr(session, "status") != "complete" or subscription_status not in _ACTIVE_SUBSCRIPTION_STATUSES:
        raise InvalidSessionError("決済または契約状態が有効ではありません。")

    metadata = _attr(session, "metadata") or {}
    if _attr(metadata, "tenant_id") != str(tenant_id):
        raise TenantMismatchError("Checkout Sessionの世帯情報が一致しません。")

    try:
        applied = _apply_checkout_completion(
            conn, tenant_id=tenant_id, session=session, subscription=subscription
        )
        conn.commit()
    except psycopg.Error:
        conn.rollback()
        raise

    return applied


def _apply_checkout_completion(conn, *, tenant_id, session, subscription):
    """検証済みのsession/subscriptionをtenant_subscriptionsへ反映する(SQL操作のみ、
    commitはしない。呼び出し元がcommit/rollbackを行うこと)。

    confirm_checkout_session()(success_url確認)とwebhook.py側の
    checkout.session.completedハンドラの両方から共通で呼ばれる(仕様書/Webhook設計.md⑧参照)。
    呼び出し元がmode・状態・tenant_id一致の検証を済ませていることが前提で、この関数自体は
    検証を行わない。
    """
    subscription_status = _attr(subscription, "status")
    period_end_ts = _subscription_current_period_end(subscription)
    current_period_end = (
        datetime.fromtimestamp(period_end_ts, tz=timezone.utc) if period_end_ts else None
    )
    return db.upsert_subscription_if_new_session(
        conn,
        tenant_id=tenant_id,
        plan="standard",
        status=subscription_status,
        stripe_customer_id=_attr(session, "customer"),
        stripe_subscription_id=_attr(subscription, "id"),
        stripe_checkout_session_id=_attr(session, "id"),
        current_period_end=current_period_end,
    )


# --- app.pyから使う、DB接続を自前で管理するラッパー(storage.pyと同じ方針) ---


def _get_postgres_connection():
    try:
        return db.get_connection()
    except db.DatabaseNotConfiguredError as e:
        raise BillingConfigError(str(e)) from e
    except psycopg.Error as e:
        raise BillingUnavailableError("PostgreSQLへの接続に失敗しました。") from e


def fetch_plan_status(tenant_id):
    """app.pyから呼ぶ入口。指定世帯の現在のプラン状態を返す(接続は自前で開閉する)。"""
    conn = _get_postgres_connection()
    try:
        return get_plan_status(conn, tenant_id=tenant_id)
    except psycopg.Error as e:
        raise BillingUnavailableError("課金状態の取得に失敗しました。") from e
    finally:
        conn.close()


def start_checkout_session(*, tenant_id, role, success_url, cancel_url):
    """app.pyから呼ぶ入口。Checkout Sessionを作成し、遷移先URLを返す(DB接続は使わない)。"""
    return create_checkout_session(
        tenant_id=tenant_id, role=role, success_url=success_url, cancel_url=cancel_url
    )


def apply_checkout_session(*, session_id, tenant_id, role):
    """app.pyから呼ぶ入口。success_urlへ戻ってきたsession_idをサーバー側で検証・反映する。

    (接続は自前で開閉する。PermissionDeniedError・InvalidSessionError・
    TenantMismatchError・StripeApiError・BillingConfigErrorはそのまま伝播する)。
    """
    conn = _get_postgres_connection()
    try:
        return confirm_checkout_session(
            session_id=session_id, tenant_id=tenant_id, role=role, conn=conn
        )
    finally:
        conn.close()


class NoActiveSubscriptionError(Exception):
    """Standard契約がない世帯に対して、解約用のBilling Portalを開こうとした場合。"""


def create_billing_portal_session(*, tenant_id, role, conn, return_url, stripe_client=None):
    """解約・支払い方法変更用のStripe Billing Portal Sessionを作成する(admin専用)。

    実際の解約操作はStripeホスト型の画面で行われ、結果はcustomer.subscription.deleted
    Webhookで反映される(アプリ側は解約処理そのものを実装しない。仕様書/Webhook設計.md⑦参照)。
    """
    if role != "admin":
        raise PermissionDeniedError("サブスクの管理にはadmin権限が必要です。")

    subscription = get_plan_status(conn, tenant_id=tenant_id)
    customer_id = subscription.get("stripe_customer_id")
    if not customer_id:
        raise NoActiveSubscriptionError("この世帯には有効な契約がありません。")

    stripe_client = stripe_client or _get_stripe_client()
    try:
        return stripe_client.billing_portal.Session.create(
            customer=customer_id, return_url=return_url
        )
    except (BillingConfigError, PermissionDeniedError):
        raise
    except Exception as e:
        raise StripeApiError("Stripeとの通信に失敗しました。") from e


def start_billing_portal_session(*, tenant_id, role, return_url):
    """app.pyから呼ぶ入口。Billing Portal Sessionを作成し、遷移先URLを返す。"""
    conn = _get_postgres_connection()
    try:
        return create_billing_portal_session(
            tenant_id=tenant_id, role=role, conn=conn, return_url=return_url
        )
    finally:
        conn.close()
