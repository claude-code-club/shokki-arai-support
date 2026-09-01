"""tenant_usageテーブルを追加する（第20回: プラン制限とメータリング）。

migrate_to_billing_schema.pyと同じく、CREATE TABLE IF NOT EXISTSによる冪等な追加のみを
行う。既存のtenant_subscriptions等には一切変更を加えない。

実行方法は仕様書/プラン制限・メータリング設計.md④を参照。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402


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
        migrate_to_usage_schema()
    except db.DatabaseNotConfiguredError as e:
        print(f"[NG] {e}")
        return 1
    except psycopg.Error:
        print("[NG] PostgreSQLへの接続または操作に失敗しました。")
        return 1
    print("[OK] tenant_usageテーブルを作成しました(既に存在する場合は変更なし)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
