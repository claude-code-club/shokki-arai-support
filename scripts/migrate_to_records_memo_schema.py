"""records.memo列を追加する（第22課題: 検索できるDB）。

migrate_to_usage_schema.pyと同じく、ALTER TABLE ... ADD COLUMN IF NOT EXISTSに
よる冪等な追加のみを行う。既存行はmemo=NULL（メモ無し）のまま、他の列には
一切変更を加えない。第16回(マルチテナント設計)のtenant_id列が既に存在する
前提（migrate_to_tenant_schema.py実行後）。

実行方法は仕様書/検索できるDB設計.mdを参照。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402


def migrate_to_records_memo_schema(conn=None):
    """records.memo列を冪等に追加する。"""
    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS memo TEXT")
        conn.commit()
    except psycopg.Error:
        conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()


def main(argv):
    if len(argv) != 1:
        print("使い方: python scripts/migrate_to_records_memo_schema.py")
        return 1
    try:
        migrate_to_records_memo_schema()
    except db.DatabaseNotConfiguredError as e:
        print(f"[NG] {e}")
        return 1
    except psycopg.Error:
        print("[NG] PostgreSQLへの接続または操作に失敗しました。")
        return 1
    print("[OK] records.memo列を追加しました(既に存在する場合は変更なし)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
