"""tenant_usageテーブルを追加する（第20回: プラン制限とメータリング）。

migrate_to_billing_schema.pyと同じく、CREATE TABLE IF NOT EXISTSによる冪等な追加のみを
行う。既存のtenant_subscriptions等には一切変更を加えない。

実行方法は仕様書/プラン制限・メータリング設計.md④を参照。

--- 本番migration安全化(2026-09-07制定) ---
実行前条件:
    - tenants・tenant_subscriptionsテーブルが既に存在すること(第16回・
      第18回が先に適用済みであること。tenant_usage.tenant_idはtenantsを
      FK参照する。tenant_subscriptionsはプラン判定に使うため実行順序として
      明示的に確認する)
    - 接続先識別と本番DDL明示許可フラグがすべて一致・設定されていること
実行後状態:
    - tenant_usageテーブルが存在する(行は0件のまま)
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


def migrate_to_usage_schema(conn=None):
    """tenant_usageテーブルを冪等に作成する。"""
    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_usage (
                    tenant_id    UUID NOT NULL REFERENCES tenants(id),
                    metric_key   TEXT NOT NULL,
                    period_start DATE NOT NULL,
                    usage_count  INTEGER NOT NULL DEFAULT 0,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, metric_key, period_start)
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
        print("使い方: python scripts/migrate_to_usage_schema.py")
        return 1
    try:
        conn = db.get_connection()
    except db.DatabaseNotConfiguredError as e:
        print(f"[NG] {e}")
        return 1

    try:
        with conn.cursor() as cur:
            verify_production_migration_target(cur)
            verify_expected_tables_exist(
                cur, {"tenants", "tenant_subscriptions"}, "第20回(プラン制限とメータリング)"
            )
        migrate_to_usage_schema(conn=conn)
    except ProductionTargetMismatchError as e:
        print(f"[NG] {e}")
        return 1
    except psycopg.Error:
        print("[NG] PostgreSQLへの接続または操作に失敗しました。")
        return 1
    finally:
        conn.close()

    print("[OK] tenant_usageテーブルを作成しました(既に存在する場合は変更なし)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
