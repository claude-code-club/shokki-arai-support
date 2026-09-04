"""事前に手動実行し、schema_migration_logの内容を確定させ、行数・
SHA-256を出力する。削除・変更は一切行わない(読み取り専用)。

操作者はこの出力を保存し、Tier 3・17-4(resume)実行時に
SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNT・SCHEMA_MIGRATION_LOG_ARCHIVE_
SHA256として設定する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402

import db  # noqa: E402
from rollback_helpers import _canonical_migration_log_export  # noqa: E402
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity  # noqa: E402


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)
            cur.execute("SELECT to_regclass('public.schema_migration_log') IS NOT NULL")
            if not cur.fetchone()[0]:
                print("[OK] schema_migration_logは存在しません(エクスポート不要)。")
                return 0
            _, canonical_text, digest, row_count = _canonical_migration_log_export(cur)
        conn.rollback()  # 読み取りのみ、DBへの変更は一切無い

        export_dir = Path("audit/schema_migration_log_archive")
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / f"schema_migration_log_export_{digest[:12]}.json"
        export_path.write_text(canonical_text, encoding="utf-8")

        print(f"[OK] エクスポート完了。 row_count={row_count} sha256={digest} path={export_path}")
        print("この内容を永続的な保存先(git・S3等)へ保存したうえで、")
        print("Tier 3・17-4(resume)実行時に次の環境変数を設定してください:")
        print(f"  SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNT={row_count}")
        print(f"  SCHEMA_MIGRATION_LOG_ARCHIVE_SHA256={digest}")
        return 0
    except (psycopg.Error, TargetDatabaseMismatchError) as e:
        conn.rollback()
        print(f"[FAILED] エクスポート中にエラーが発生しました: {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
