"""records.json（JSON版）の記録日をPostgreSQLへ移行する。

書き込み(insert_dates)と日付集合の完全一致照合を同一トランザクション内で行い、
**完全一致した場合だけcommitする**。不一致・例外時はrollbackし、この移行による
書き込みを一切確定させない(仕様書/保存方式切り替え設計.md ②-c参照)。
insert_datesはON CONFLICT DO NOTHINGのため、再実行しても安全(冪等)。

同一トランザクション内では、insert_dates(未コミット)の直後にload_datesで
読み直しても、自分自身が書いた未コミットの変更は見える(read-your-own-writes、
PostgreSQLの標準的な挙動)ため、「書き込み→同一トランザクションで照合」が
正しく機能する。

実行方法は仕様書/PostgreSQL移行設計.mdを参照。
Railway上ではPre-deploy Commandではなく、railway sshでコンテナ内から実行する想定。
本番のrecords.jsonへは、operatorがrailway volume files downloadで取得したローカルのコピー
または、コンテナ内で実行する場合はそのコンテナのVolumeマウント先を読み込む。
このスクリプト自体はrecords.jsonを一切書き換えない(読み取り専用)。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402
import logic  # noqa: E402


class MigrationVerificationError(Exception):
    """移行後の日付集合がJSONとPostgreSQLで一致しない場合に送出される。この移行による書き込みはrollback済み。"""


def migrate(data_file=None, conn=None):
    """JSONの記録日をPostgreSQLへ移行し、検証結果を返す。

    書き込みと照合を同一トランザクションで行い、完全一致した場合だけcommitする。

    data_file: 省略時はlogic.DATA_FILE(streamlit/data/records.json)を使う。
    conn: 省略時はdb.get_connection()で新規接続する(呼び出し元が閉じること)。
          テストではダミーのconnを渡して検証する。
    """
    data_file = data_file or logic.DATA_FILE
    json_dates = logic.load_dates(data_file=data_file)

    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        db.ensure_schema(conn)
        db.insert_dates(json_dates, conn=conn)  # まだcommitしない
        db_dates = db.load_dates(conn)  # 同一トランザクション内なので未コミット分も見える
        result = db.compare_date_sets(json_dates, db_dates)
        if result["match"]:
            conn.commit()  # 完全一致した場合だけ確定
        else:
            conn.rollback()  # 不一致ならこの移行によるINSERTを取り消す
            raise MigrationVerificationError(
                f"移行後の日付集合が一致しません: "
                f"JSONのみ={result['only_in_json']}, DBのみ={result['only_in_db']}"
            )
        return result
    except psycopg.Error:
        conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()


def main(argv):
    if len(argv) not in (1, 2):
        print("使い方: python scripts/migrate_to_postgres.py [records.jsonのパス]")
        return 1

    data_file = Path(argv[1]) if len(argv) == 2 else None
    try:
        result = migrate(data_file=data_file)
    except MigrationVerificationError as e:
        print(f"[NG] {e}")
        return 1
    except db.DatabaseNotConfiguredError as e:
        print(f"[NG] {e}")
        return 1
    except psycopg.Error:
        print("[NG] PostgreSQLへの接続または操作に失敗しました。")
        return 1

    diff = len(result["only_in_json"]) + len(result["only_in_db"])
    print(f"[OK] 移行完了。日付集合が完全に一致しました（差分{diff}件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
