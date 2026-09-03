"""public.records.memo列を追加する（第22課題: 検索できるDB）。

ChatGPT監査(2026-09-03、Highの指摘)を反映し、次を追加している。

- PR #29(scripts/target_identity.py)と同じ接続先識別確認を、main()実行時に
  必ず行う(接続先データベース名・ユーザー・Railwayプロジェクト/環境ID・
  明示許可フラグのすべてが一致しない限り停止する。DDLは一切実行しない)
- ALTER TABLEの対象を無条件の"records"ではなく、完全修飾の"public.records"に
  固定する(search_path経由で想定外のテーブルを変更しないため)
- 実行前後にpublic.records.memoの列定義(型・NULL許容・デフォルト値)を検証する。
  既にmemo列が存在するが想定と異なる定義(例: 型がTEXTでない)の場合は、
  IF NOT EXISTSによる「サイレントな成功」を許さずエラーにする

migrate_to_usage_schema.pyと同じくALTER TABLE ... ADD COLUMN IF NOT EXISTSに
よる冪等な追加が基本方針であることは変わらない。既存行はmemo=NULL(メモ無し)の
まま、他の列には一切変更を加えない。第16回(マルチテナント設計)のtenant_id列が
既に存在する前提（migrate_to_tenant_schema.py実行後）。

実行方法・実行順序(★PR #30のマージより前に実行すること)は
仕様書/検索できるDB設計.mdを参照。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402

import db  # noqa: E402
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity  # noqa: E402


class UnexpectedColumnDefinitionError(Exception):
    """public.records.memoが既に存在するが、想定と異なる定義(型・NULL許容・
    デフォルト値)の場合に送出される。IF NOT EXISTSがサイレントに成功したと
    誤認しないための安全装置。
    """


EXPECTED_MEMO_DEFINITION = {"data_type": "text", "is_nullable": "YES", "column_default": None}


def _fetch_memo_column_definition(cur):
    """public.records.memoの現在の定義(data_type, is_nullable, column_default)を
    辞書で返す。列が存在しなければNoneを返す。
    """
    cur.execute(
        "SELECT data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'records' AND column_name = 'memo'"
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"data_type": row[0], "is_nullable": row[1], "column_default": row[2]}


def migrate_to_records_memo_schema(conn=None):
    """public.records.memo列を冪等に追加する(接続先識別は行わない、呼び出し元の責務)。

    実行前にpublic.records.memoの現在の定義を確認し、既に存在する場合は
    EXPECTED_MEMO_DEFINITIONと一致するかを検証する(型違い等の想定外の既存列を
    「変更不要」と誤認しないため)。ALTER TABLE実行後にも同じ検証を行い、
    最終的な列定義が想定どおりであることを保証してからcommitする。
    """
    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        with conn.cursor() as cur:
            before = _fetch_memo_column_definition(cur)
            if before is not None and before != EXPECTED_MEMO_DEFINITION:
                raise UnexpectedColumnDefinitionError(
                    f"public.records.memoが既に存在しますが、想定と異なる定義です: "
                    f"実際={before} 期待={EXPECTED_MEMO_DEFINITION}。DDLは実行していません。"
                )

            cur.execute("ALTER TABLE public.records ADD COLUMN IF NOT EXISTS memo TEXT")

            after = _fetch_memo_column_definition(cur)
            if after != EXPECTED_MEMO_DEFINITION:
                raise UnexpectedColumnDefinitionError(
                    f"ALTER TABLE後もpublic.records.memoの定義が想定と一致しません: "
                    f"実際={after} 期待={EXPECTED_MEMO_DEFINITION}"
                )
        conn.commit()
    except (psycopg.Error, UnexpectedColumnDefinitionError):
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
        conn = db.get_connection()
    except db.DatabaseNotConfiguredError as e:
        print(f"[NG] {e}")
        return 1

    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)
        migrate_to_records_memo_schema(conn=conn)
    except TargetDatabaseMismatchError as e:
        conn.rollback()
        print(f"[NG] 接続先確認に失敗しました。DDLは実行していません: {e}")
        return 1
    except UnexpectedColumnDefinitionError as e:
        print(f"[NG] {e}")
        return 1
    except psycopg.Error:
        print("[NG] PostgreSQLへの接続または操作に失敗しました。")
        return 1
    finally:
        conn.close()

    print(
        "[OK] public.records.memo列を追加しました(既に存在する場合は変更なし、"
        "接続先・型・NULL許容を検証済み)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
