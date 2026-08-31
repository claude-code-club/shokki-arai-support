"""PostgreSQLの最新データをrecords.json（JSON版）へ書き戻す（DB→JSON復元手順）。

PostgreSQL移行後に切り戻す場合、JSONを移行時点のまま残すだけでは
移行後に追加された記録を含めて戻すことができない。
そのため、切り替えをJSONへ戻す前に、このスクリプトでPostgreSQLの
最新データをJSONへ書き戻す。

第16回（マルチテナント設計）のスキーマ移行（migrate_to_tenant_schema.py）は
段階的に反映されるため、このスクリプトは新旧どちらのスキーマにも対応する
「移行期間中の互換性」を持たせる（旧スキーマのstagingへ、tenant_id必須の
コードだけを先にデプロイすると、DB移行前の緊急JSON復元手段を失ってしまうため）。

- recordsにtenant_id列がまだ無い旧スキーマ：従来どおり、全recordsを単一の
  records.jsonへ復元する。この場合にtenant_idを指定するとエラー停止する
  （旧スキーマにテナントという概念はまだ存在しないため）
- recordsにtenant_id列がある新スキーマ：tenant_idの指定を必須とする。
  未指定・不正なUUID値ではエラー停止し、全テナントを暗黙に混ぜてJSONへ
  書き出すことは行わない（仕様書/マルチテナント設計.md⑨参照）

書き戻し前に、既存のrecords.jsonをlogic.backup_data()で退避してから上書きする
(既存のrestore_data()と同じ安全設計)。新旧どちらの経路でも、接続は必ずcloseする。

実行方法は仕様書/PostgreSQL移行設計.mdを参照。railway sshでコンテナ内から実行する想定。
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import db  # noqa: E402
import logic  # noqa: E402


class SchemaMismatchError(Exception):
    """旧スキーマ(tenant_id列なし)でtenant_idを指定した、または新スキーマ(tenant_id列あり)で
    tenant_idを指定しなかった場合に送出される。
    """


def _records_has_tenant_id_column(conn):
    """対象recordsテーブル(search_pathで解決される、無修飾の'records')が
    tenant_id列を持つかを確認する。'records'::regclassはsearch_pathに従って
    解決されるため、無関係な別スキーマの同名テーブルを誤って参照しない
    (仕様書/マルチテナント設計.md⑫参照)。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_attribute "
            "WHERE attrelid = 'records'::regclass AND attname = 'tenant_id' AND NOT attisdropped"
        )
        return cur.fetchone() is not None


def restore_json_from_postgres(tenant_id=None, data_file=None, conn=None):
    """PostgreSQLの記録日をJSONへ書き戻し、書き戻した日付集合を返す。

    tenant_id: 新スキーマ(tenant_id列あり)では必須。旧スキーマ(tenant_id列なし)では
        指定不可(Noneのみ許可)。Noneでなければuuid.UUIDのインスタンスであること。
    """
    if tenant_id is not None and not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_idを指定する場合はuuid.UUIDのインスタンスを渡してください。")

    data_file = data_file or logic.DATA_FILE

    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        has_tenant_id = _records_has_tenant_id_column(conn)

        if has_tenant_id:
            if tenant_id is None:
                raise SchemaMismatchError(
                    "recordsはテナント対応スキーマ(tenant_id列あり)です。"
                    "tenant_idを指定してください(全テナントを暗黙に混ぜて復元することはできません)。"
                )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT record_date FROM records WHERE tenant_id = %s",
                    (tenant_id,),
                )
                db_dates = {row[0].isoformat() for row in cur.fetchall()}
        else:
            if tenant_id is not None:
                raise SchemaMismatchError(
                    "recordsはまだテナント対応スキーマ(tenant_id列)を持っていません。"
                    "この旧スキーマではtenant_idを指定できません(全件を復元する形式のみ対応)。"
                )
            with conn.cursor() as cur:
                cur.execute("SELECT record_date FROM records")
                db_dates = {row[0].isoformat() for row in cur.fetchall()}
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
    parser = argparse.ArgumentParser(
        prog="restore_json_from_postgres.py",
        description="PostgreSQLの記録日をrecords.json(JSON版)へ書き戻す(DB→JSON復元)。",
    )
    parser.add_argument(
        "--tenant-id",
        default=None,
        help=(
            "復元対象のテナントUUID。recordsがtenant_id列を持つ新スキーマでは必須。"
            "まだtenant_id列がない旧スキーマでは指定しないこと。"
        ),
    )
    parser.add_argument(
        "data_file",
        nargs="?",
        default=None,
        help="復元先records.jsonのパス(省略時は既定パス)",
    )

    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 1

    tenant_id = None
    if args.tenant_id is not None:
        try:
            tenant_id = uuid.UUID(args.tenant_id)
        except ValueError:
            print(f"[NG] UUID形式ではありません: {args.tenant_id!r}")
            return 1

    data_file = Path(args.data_file) if args.data_file else None
    try:
        dates = restore_json_from_postgres(tenant_id, data_file=data_file)
    except SchemaMismatchError as e:
        print(f"[NG] {e}")
        return 1
    except db.DatabaseNotConfiguredError as e:
        print(f"[NG] {e}")
        return 1

    tenant_note = f"(tenant_id={tenant_id})" if tenant_id else "(旧スキーマ・全件)"
    print(f"[OK] PostgreSQLから{len(dates)}件の記録日をJSONへ書き戻しました{tenant_note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
