"""本番migration(第16〜20回、scripts/migrate_to_tenant_schema.py等)実行前の
必須事前条件として、対象データベースのバックアップを取得し、別の使い捨て
データベースへ実際に復元して整合性を検証するツール(要求仕様#7)。

使い方:
    python scripts/backup_and_restore_test.py <バックアップ対象DBのDATABASE_URL>

pg_dump(カスタム形式、-Fc)でバックアップファイルを作成し、続けて同じ
PostgreSQLサーバー上に新規の使い捨てデータベースを作成してpg_restoreで
復元する。復元後、元のDBと復元先DBの両方について、全テーブルの行数と
正規化データ(全列を文字列化し、決定的な順序でJSON化したもの)のSHA-256を
比較し、完全一致することを確認する。

このツール自体はDDL(CREATE/DROP DATABASE)を実行するが、対象は常に
「新規に作成する使い捨てデータベース」のみであり、バックアップ対象DB・
既存の他のデータベースには一切書き込みを行わない(読み取り専用)。

pg_dump・pg_restoreの実行ファイルは既定でPATH上のものを使う。ローカルの
ポータブルPostgreSQL環境等、PATHに無い場合はPG_BIN_DIR環境変数で
bin/ディレクトリを指定する。

成功時、バックアップファイルのパス・SHA-256、テーブルごとの検証結果を
標準出力へ表示する。この出力を人間(および必要ならChatGPT監査)が確認した
うえで、操作者がBACKUP_RESTORE_TEST_CONFIRMED=trueを設定してから本番
migrationを実行する、という運用を想定する
(scripts/production_target_identity.pyがこのフラグを必須条件として検証する)。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

import psycopg


class BackupRestoreVerificationError(Exception):
    """バックアップ・復元・検証のいずれかの段階で失敗した場合に送出される。"""


def _pg_bin(name):
    bin_dir = os.environ.get("PG_BIN_DIR", "").strip()
    exe = f"{name}.exe" if os.name == "nt" else name
    if bin_dir:
        return str(Path(bin_dir) / exe)
    return name


def _connection_parts(database_url):
    parsed = urlparse(database_url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "user": parsed.username,
        "password": parsed.password or "",
        "dbname": parsed.path.lstrip("/"),
    }


def _run(cmd, env):
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    if result.returncode != 0:
        raise BackupRestoreVerificationError(
            f"コマンド失敗(終了コード{result.returncode}): {cmd[0]}\nstderr: {result.stderr}"
        )
    return result


def _admin_connect(parts):
    return psycopg.connect(
        dbname="postgres", host=parts["host"], port=parts["port"],
        user=parts["user"], password=parts["password"], autocommit=True,
    )


def _all_table_names(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        )
        return [r[0] for r in cur.fetchall()]


def _canonical_table_hash(conn, table_name):
    """1テーブルの全行を、決定的な順序(全列でORDER BY)で取得し、
    正規化されたJSON文字列のSHA-256を返す。列の値はすべて文字列化してから
    比較する(日付・UUID等の型がソースDBと復元先DBで同じPythonの型変換
    ロジックを通ることで、DBインスタンスが異なっても正規化結果は一致する)。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
            (table_name,),
        )
        columns = [r[0] for r in cur.fetchall()]
        if not columns:
            return hashlib.sha256(b"[]").hexdigest(), 0
        order_by = ", ".join(f'"{c}"' for c in columns)
        cur.execute(f'SELECT * FROM "{table_name}" ORDER BY {order_by}')
        rows = cur.fetchall()

    normalized_rows = [[None if v is None else str(v) for v in row] for row in rows]
    canonical = json.dumps(normalized_rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), len(rows)


def backup_and_restore_test(source_database_url, keep_restored_db=False):
    """source_database_urlが指すDBをpg_dumpでバックアップし、新規の
    使い捨てDBへpg_restoreで復元、両者の全テーブルを比較検証する。

    戻り値: {
        "backup_file": str, "backup_sha256": str,
        "restored_dbname": str (keep_restored_db=Falseの場合はNone),
        "tables": {テーブル名: {"source_hash", "restored_hash", "row_count", "match"}},
        "all_match": bool,
    }
    """
    parts = _connection_parts(source_database_url)
    restored_dbname = f"restore_test_{uuid.uuid4().hex[:12]}"

    backup_dir = Path(tempfile.gettempdir()) / "shokki_arai_backup_restore_test"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_file = backup_dir / f"{parts['dbname']}_{uuid.uuid4().hex[:8]}.dump"

    env = os.environ.copy()
    env["PGPASSWORD"] = parts["password"]

    # 1. pg_dumpでバックアップ(カスタム形式)。ソースDBへは読み取りのみ。
    _run(
        [
            _pg_bin("pg_dump"), "-h", str(parts["host"]), "-p", str(parts["port"]),
            "-U", parts["user"], "-Fc", "-f", str(backup_file), parts["dbname"],
        ],
        env,
    )
    backup_sha256 = hashlib.sha256(backup_file.read_bytes()).hexdigest()

    # 2. 使い捨てDBを新規作成
    admin_conn = _admin_connect(parts)
    try:
        with admin_conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE {restored_dbname}")
    finally:
        admin_conn.close()

    try:
        # 3. pg_restoreで復元(使い捨てDBへのみ書き込む)
        _run(
            [
                _pg_bin("pg_restore"), "-h", str(parts["host"]), "-p", str(parts["port"]),
                "-U", parts["user"], "-d", restored_dbname, str(backup_file),
            ],
            env,
        )

        # 4. ソースDBと復元先DBの全テーブルを比較
        source_conn = psycopg.connect(
            dbname=parts["dbname"], host=parts["host"], port=parts["port"],
            user=parts["user"], password=parts["password"],
        )
        restored_conn = psycopg.connect(
            dbname=restored_dbname, host=parts["host"], port=parts["port"],
            user=parts["user"], password=parts["password"],
        )
        try:
            source_tables = set(_all_table_names(source_conn))
            restored_tables = set(_all_table_names(restored_conn))
            if source_tables != restored_tables:
                raise BackupRestoreVerificationError(
                    f"テーブル集合が一致しません: ソース={source_tables} "
                    f"復元先={restored_tables}"
                )

            table_results = {}
            all_match = True
            for table in sorted(source_tables):
                source_hash, source_count = _canonical_table_hash(source_conn, table)
                restored_hash, restored_count = _canonical_table_hash(restored_conn, table)
                match = source_hash == restored_hash and source_count == restored_count
                all_match = all_match and match
                table_results[table] = {
                    "source_hash": source_hash,
                    "restored_hash": restored_hash,
                    "row_count": source_count,
                    "match": match,
                }
        finally:
            source_conn.close()
            restored_conn.close()
    finally:
        if not keep_restored_db:
            admin_conn = _admin_connect(parts)
            try:
                with admin_conn.cursor() as cur:
                    cur.execute(f"DROP DATABASE IF EXISTS {restored_dbname} WITH (FORCE)")
            finally:
                admin_conn.close()

    return {
        "backup_file": str(backup_file),
        "backup_sha256": backup_sha256,
        "restored_dbname": restored_dbname if keep_restored_db else None,
        "tables": table_results,
        "all_match": all_match,
    }


def main(argv):
    parser = argparse.ArgumentParser(
        prog="backup_and_restore_test.py",
        description=(
            "対象DBをバックアップし、使い捨て環境へ復元・検証する"
            "(本番migration実行前の必須事前条件)。"
        ),
    )
    parser.add_argument("database_url", help="バックアップ対象DBの接続URL")
    parser.add_argument(
        "--keep-restored-db",
        action="store_true",
        help="検証後も復元先の使い捨てDBを削除せず残す(追加確認用)",
    )
    args = parser.parse_args(argv[1:])

    try:
        result = backup_and_restore_test(
            args.database_url, keep_restored_db=args.keep_restored_db
        )
    except BackupRestoreVerificationError as e:
        print(f"[NG] {e}")
        return 1

    print(f"[バックアップ] ファイル: {result['backup_file']}")
    print(f"[バックアップ] SHA-256: {result['backup_sha256']}")
    for table, info in sorted(result["tables"].items()):
        status = "[OK]" if info["match"] else "[NG]"
        print(
            f"{status} テーブル{table}: {info['row_count']}件、"
            f"source={info['source_hash'][:16]}... restored={info['restored_hash'][:16]}..."
        )

    if result["all_match"]:
        print(
            "[OK] バックアップ・復元テストに成功しました。全テーブルのデータが"
            "完全一致しています。このログを確認したうえで、"
            "BACKUP_RESTORE_TEST_CONFIRMED=trueを設定してください。"
        )
        return 0

    print("[NG] 一部テーブルのデータが復元後に一致しませんでした。本番migrationを実行しないでください。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
