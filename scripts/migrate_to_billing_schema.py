"""tenant_subscriptionsテーブルを追加する（第18回: 課金① — Stripeサブスク決済）。

migrate_to_auth_schema.pyと同じく、CREATE TABLE IF NOT EXISTSによる冪等な追加のみを
行う。既存のtenants/records/users/tenant_membershipsには一切変更を加えない。

行が存在しない世帯は自動的にfree扱いとする（このスクリプト自身は行を一切INSERTしない。
全世帯へfree行を作る必要がない最小構成。仕様書/Stripe課金設計.md④参照）。

実行方法・切り替え手順は仕様書/Stripe課金設計.md⑧を参照。

--- 本番migration安全化(2026-09-07制定) ---
実行前条件:
    - tenantsテーブルが既に存在すること(第16回が先に適用済みであること)
    - 接続先識別と本番DDL明示許可フラグがすべて一致・設定されていること
実行後状態:
    - tenant_subscriptionsテーブルが存在する(行は0件のまま。行が無い
      世帯は自動的にfree扱い)
再実行時の挙動:
    - 完全に冪等(CREATE TABLE IF NOT EXISTSのみ)
途中失敗時の復旧:
    - psycopg.Error発生時はrollbackし、部分的な変更は確定しない
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402

import db  # noqa: E402
from production_target_identity import (  # noqa: E402
    ProductionTargetMismatchError,
    verify_expected_tables_exist,
    verify_production_migration_target,
)


def migrate_to_billing_schema(conn=None):
    """tenant_subscriptionsテーブルを冪等に作成する。"""
    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_subscriptions (
                    tenant_id                  UUID PRIMARY KEY REFERENCES tenants(id),
                    plan                       TEXT NOT NULL DEFAULT 'free'
                                                   CHECK (plan IN ('free', 'standard')),
                    status                     TEXT NOT NULL DEFAULT 'active',
                    stripe_customer_id         TEXT,
                    stripe_subscription_id     TEXT,
                    stripe_checkout_session_id TEXT UNIQUE,
                    current_period_end         TIMESTAMPTZ,
                    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()
    except psycopg.Error:
        conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()


def main(argv):
    if len(argv) != 1:
        print("使い方: python scripts/migrate_to_billing_schema.py")
        return 1
    try:
        conn = db.get_connection()
    except db.DatabaseNotConfiguredError as e:
        print(f"[NG] {e}")
        return 1

    try:
        with conn.cursor() as cur:
            verify_production_migration_target(cur)
            verify_expected_tables_exist(cur, {"tenants"}, "第18回(Stripeサブスク決済)")
        migrate_to_billing_schema(conn=conn)
    except ProductionTargetMismatchError as e:
        print(f"[NG] {e}")
        return 1
    except psycopg.Error:
        print("[NG] PostgreSQLへの接続または操作に失敗しました。")
        return 1
    finally:
        conn.close()

    print("[OK] tenant_subscriptionsテーブルを作成しました(既に存在する場合は変更なし)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
