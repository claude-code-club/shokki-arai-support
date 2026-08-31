"""records.tenant_id列を追加し、既存の全記録を指定した初期テナントへ紐付ける
（第16回: マルチテナント設計。仕様書/マルチテナント設計.md④参照）。

書き込み(CREATE TABLE/ALTER/UPDATE等)と検証(移行前後の日付集合の完全一致)を
同一トランザクション内で行い、完全一致した場合だけcommitする。不一致・例外時は
rollbackし、この移行によるスキーマ変更・データ変更を一切確定させない。

tenant_idは呼び出し側が明示的に渡すことを必須とする(デフォルト値・自動生成・
「先頭テナントを自動選択」等のフォールバックは一切行わない。
仕様書/マルチテナント設計.md①参照)。UUIDはGitへハードコードしない。

再実行しても安全(冪等)。CREATE TABLE IF NOT EXISTS(tenants作成)、
ON CONFLICT DO NOTHING(tenants挿入)、WHERE tenant_id IS NULL(backfillは
未移行の行だけが対象)、IF NOT EXISTS(列・インデックス・制約)により、
既に移行済みの行や制約には触れない。

実行方法・段階的な移行手順は仕様書/マルチテナント設計.md⑧を参照。
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402


class MigrationVerificationError(Exception):
    """移行後の日付集合が移行前と一致しない場合に送出される。この移行による変更はrollback済み。"""


def migrate_to_tenant_schema(tenant_id, conn=None, tenant_name="初期世帯"):
    """recordsへtenant_id列を追加し、既存の全記録をtenant_idへ紐付ける。

    tenant_id: uuid.UUIDのインスタンス。呼び出し側が明示的に生成・指定すること
        (デフォルト値なし。呼び忘れるとTypeErrorになる)。
    conn: 省略時はdb.get_connection()で新規接続する(呼び出し元が閉じること)。
        テストでは、隔離したスキーマへsearch_pathを設定済みのconnを渡して検証する。
    tenant_name: 個人情報を含まない仮の世帯名(デフォルト「初期世帯」)。
    """
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_idはuuid.UUIDのインスタンスを明示的に渡してください。")

    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        before_dates = db.load_dates(conn)  # tenant_id非対応の既存の読み込み(全行が対象)

        with conn.cursor() as cur:
            # 0. tenantsテーブルを冪等に作成
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    id          UUID PRIMARY KEY,
                    name        TEXT NOT NULL,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            # 1. 初期世帯を冪等に作成(既に存在すれば何もしない)
            cur.execute(
                "INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (tenant_id, tenant_name),
            )
            # 2. recordsへnullableなtenant_id列を追加(既に存在すれば何もしない)
            cur.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS tenant_id UUID")
            # 3. 既存レコードだけをtenant_idでbackfill(NULLの行だけが対象、再実行しても安全)
            cur.execute(
                "UPDATE records SET tenant_id = %s WHERE tenant_id IS NULL",
                (tenant_id,),
            )
            # 4. NULL件数が0であることを確認
            cur.execute("SELECT COUNT(*) FROM records WHERE tenant_id IS NULL")
            null_count = cur.fetchone()[0]
            if null_count != 0:
                conn.rollback()
                raise MigrationVerificationError(
                    f"tenant_id未設定の行が残っています: {null_count}件"
                )

            # 5. 外部キーを追加(既に存在すれば何もしない)
            # conname(制約名)はスキーマ横断で重複しうるため、conrelidで対象テーブルを
            # 明示的に絞り込む('records'::regclassはsearch_pathに従って解決される)。
            cur.execute(
                """
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'records_tenant_id_fkey'
                          AND conrelid = 'records'::regclass
                    ) THEN
                        ALTER TABLE records ADD CONSTRAINT records_tenant_id_fkey
                            FOREIGN KEY (tenant_id) REFERENCES tenants(id);
                    END IF;
                END $$;
                """
            )
            # 6. tenant_idをNOT NULL化
            cur.execute("ALTER TABLE records ALTER COLUMN tenant_id SET NOT NULL")
            # 7. record_date単独のUNIQUE INDEXを削除(実際の名前。db.ensure_schema()参照)
            cur.execute("DROP INDEX IF EXISTS records_record_date_unique")
            # 8. UNIQUE(tenant_id, record_date)を追加
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS records_tenant_id_record_date_unique
                ON records (tenant_id, record_date)
                """
            )

            # 9. 指定したtenant_idの移行後の日付集合を取得(同一トランザクション内、read-your-own-writes)
            cur.execute(
                "SELECT record_date FROM records WHERE tenant_id = %s",
                (tenant_id,),
            )
            after_dates = {row[0].isoformat() for row in cur.fetchall()}

        if after_dates == before_dates:
            conn.commit()  # 10. 完全一致した場合だけ確定
            return {"tenant_id": tenant_id, "match": True, "count": len(after_dates)}
        else:
            conn.rollback()  # 不一致ならこの移行による変更をすべて取り消す
            raise MigrationVerificationError(
                f"移行後の日付集合が一致しません: 移行前={before_dates}, 移行後={after_dates}"
            )
    except psycopg.Error:
        conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()


def main(argv):
    if len(argv) != 2:
        print("使い方: python scripts/migrate_to_tenant_schema.py <初期世帯のUUID>")
        print("UUIDは事前にPythonのuuid.uuid4()等で1回だけ生成し、明示的に渡してください。")
        print("再実行のたびに新しいUUIDを生成しないこと(同じ値を使い回すこと)。")
        return 1

    try:
        tenant_id = uuid.UUID(argv[1])
    except ValueError:
        print(f"[NG] UUID形式ではありません: {argv[1]!r}")
        return 1

    try:
        result = migrate_to_tenant_schema(tenant_id)
    except MigrationVerificationError as e:
        print(f"[NG] {e}")
        return 1
    except db.DatabaseNotConfiguredError as e:
        print(f"[NG] {e}")
        return 1
    except psycopg.Error:
        print("[NG] PostgreSQLへの接続または操作に失敗しました。")
        return 1

    print(f"[OK] 移行完了。tenant_id={result['tenant_id']}、記録件数={result['count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
