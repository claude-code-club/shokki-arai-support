"""processed_stripe_eventsテーブルを追加する（第19回: 継続課金・Webhook）。

migrate_to_billing_schema.pyと同じく、CREATE TABLE IF NOT EXISTSによる冪等な追加のみを
行う。既存のtenant_subscriptions等には一切変更を加えない。

実行方法・切り替え手順は仕様書/Webhook設計.md③④を参照。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402


def migrate_to_webhook_schema(conn=None):
    """processed_stripe_eventsテーブルを冪等に作成する。"""
    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_stripe_events (
                    stripe_event_id TEXT PRIMARY KEY,
                    event_type      TEXT NOT NULL,
                    processed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
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
        print("使い方: python scripts/migrate_to_webhook_schema.py")
        return 1
    try:
        migrate_to_webhook_schema()
    except db.DatabaseNotConfiguredError as e:
        print(f"[NG] {e}")
        return 1
    except psycopg.Error:
        print("[NG] PostgreSQLへの接続または操作に失敗しました。")
        return 1
    print("[OK] processed_stripe_eventsテーブルを作成しました(既に存在する場合は変更なし)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
