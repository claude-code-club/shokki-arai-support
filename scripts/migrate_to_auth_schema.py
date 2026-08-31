"""users・tenant_membershipsテーブルを追加する（第17回: 認証基盤）。

migrate_to_tenant_schema.pyと同じく、CREATE TABLE IF NOT EXISTSによる冪等な
追加のみを行う。既存のtenants/recordsには一切変更を加えない。

usersへの初回admin紐付けは、このスクリプトではなく
scripts/bootstrap_admin_membership.py（一回限りの管理スクリプト）で行う
（仕様書/認証基盤設計.md⑥参照。未登録ユーザーを自動的にどこかの世帯へ
参加させないため、このスクリプト自身は行を一切INSERTしない）。

実行方法・切り替え手順は仕様書/認証基盤設計.md⑪を参照。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402


def migrate_to_auth_schema(conn=None):
    """users・tenant_membershipsテーブルを冪等に作成する。"""
    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id             UUID PRIMARY KEY,
                    auth_subject   TEXT UNIQUE NOT NULL,
                    email          TEXT,
                    email_verified BOOLEAN NOT NULL DEFAULT false,
                    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_memberships (
                    tenant_id  UUID NOT NULL REFERENCES tenants(id),
                    user_id    UUID NOT NULL REFERENCES users(id),
                    role       TEXT NOT NULL CHECK (role IN ('admin', 'member')),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (tenant_id, user_id)
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
        print("使い方: python scripts/migrate_to_auth_schema.py")
        return 1
    try:
        migrate_to_auth_schema()
    except db.DatabaseNotConfiguredError as e:
        print(f"[NG] {e}")
        return 1
    except psycopg.Error:
        print("[NG] PostgreSQLへの接続または操作に失敗しました。")
        return 1
    print("[OK] users・tenant_membershipsテーブルを作成しました(既に存在する場合は変更なし)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
