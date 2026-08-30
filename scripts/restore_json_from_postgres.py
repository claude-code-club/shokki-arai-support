"""PostgreSQLの最新データをrecords.json（JSON版）へ書き戻す（DB→JSON復元手順）。

PostgreSQL移行後に切り戻す場合、JSONを移行時点のまま残すだけでは
移行後に追加された記録を含めて戻すことができない。
そのため、切り替えをJSONへ戻す前に、このスクリプトでPostgreSQLの
最新データをJSONへ書き戻す。

書き戻し前に、既存のrecords.jsonをlogic.backup_data()で退避してから上書きする
(既存のrestore_data()と同じ安全設計)。

実行方法は仕様書/PostgreSQL移行設計.mdを参照。railway sshでコンテナ内から実行する想定。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import db  # noqa: E402
import logic  # noqa: E402


def restore_json_from_postgres(data_file=None, conn=None):
    """PostgreSQLの記録日をJSONへ書き戻し、書き戻した日付集合を返す。"""
    data_file = data_file or logic.DATA_FILE

    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        db_dates = db.load_dates(conn)
    finally:
        if owns_conn:
            conn.close()

    if data_file.exists():
        logic.backup_data(data_file=data_file)

    payload = json.dumps(
        {"schema_version": logic.SCHEMA_VERSION, "dates": sorted(db_dates)},
        ensure_ascii=False,
        indent=2,
    )
    logic._atomic_write(data_file, payload)
    return db_dates


def main(argv):
    if len(argv) not in (1, 2):
        print("使い方: python scripts/restore_json_from_postgres.py [records.jsonのパス]")
        return 1

    data_file = Path(argv[1]) if len(argv) == 2 else None
    try:
        dates = restore_json_from_postgres(data_file=data_file)
    except db.DatabaseNotConfiguredError as e:
        print(f"[NG] {e}")
        return 1

    print(f"[OK] PostgreSQLから{len(dates)}件の記録日をJSONへ書き戻しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
