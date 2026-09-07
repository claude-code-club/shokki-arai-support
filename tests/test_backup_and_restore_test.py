"""scripts/backup_and_restore_test.py(要求仕様#7: 本番migration実行前の
バックアップ・使い捨て環境への復元テスト)の自動テスト。

pg_dump/pg_restoreの実行ファイルが見つからない環境(CIランナーの構成に
よっては存在しない場合がある)では、このファイルのテストをすべてスキップ
する。DBそのものへの疎通は他のテストと同じrequires_dbガードで判定する。
"""
import os
import shutil
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
from backup_and_restore_test import (  # noqa: E402
    _canonical_table_hash,
    backup_and_restore_test,
)

requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします",
)


def _pg_dump_available():
    bin_dir = os.environ.get("PG_BIN_DIR", "").strip()
    exe = "pg_dump.exe" if os.name == "nt" else "pg_dump"
    if bin_dir:
        return (Path(bin_dir) / exe).exists()
    return shutil.which("pg_dump") is not None


requires_pg_dump = pytest.mark.skipif(
    not _pg_dump_available(),
    reason="pg_dumpが見つからないため、バックアップ・復元テストをスキップします",
)


def _connection_parts():
    parsed = urlparse(os.environ["DATABASE_URL"])
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "user": parsed.username,
        "password": parsed.password or "",
    }


def _admin_connect(dbname="postgres"):
    parts = _connection_parts()
    return psycopg.connect(dbname=dbname, autocommit=True, **parts)


def _database_url_for(dbname):
    parts = _connection_parts()
    return f"postgresql://{parts['user']}:{parts['password']}@{parts['host']}:{parts['port']}/{dbname}"


@pytest.fixture
def sample_db():
    """records相当のサンプルデータを持つ使い捨てDB(バックアップ対象役)。"""
    dbname = f"test_backupsrc_{uuid.uuid4().hex[:12]}"
    admin = _admin_connect()
    try:
        with admin.cursor() as cur:
            cur.execute(f"CREATE DATABASE {dbname}")
    finally:
        admin.close()

    parts = _connection_parts()
    conn = psycopg.connect(dbname=dbname, **parts)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE records (id BIGSERIAL PRIMARY KEY, "
                "record_date DATE NOT NULL UNIQUE, memo TEXT)"
            )
            cur.execute(
                "INSERT INTO records (record_date, memo) VALUES "
                "('2026-09-01', 'test1'), ('2026-09-02', NULL), ('2026-09-03', 'テスト3')"
            )
        conn.commit()
    finally:
        conn.close()

    yield dbname

    admin = _admin_connect()
    try:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {dbname} WITH (FORCE)")
    finally:
        admin.close()


@requires_db
@requires_pg_dump
class TestBackupAndRestoreTest:
    def test_successful_backup_restore_reports_full_match(self, sample_db):
        result = backup_and_restore_test(_database_url_for(sample_db))
        try:
            assert result["all_match"] is True
            assert set(result["tables"].keys()) == {"records"}
            assert result["tables"]["records"]["row_count"] == 3
            assert result["tables"]["records"]["match"] is True
            assert (
                result["tables"]["records"]["source_hash"]
                == result["tables"]["records"]["restored_hash"]
            )
            assert len(result["backup_sha256"]) == 64
        finally:
            Path(result["backup_file"]).unlink(missing_ok=True)

    def test_restored_db_is_dropped_by_default(self, sample_db):
        result = backup_and_restore_test(_database_url_for(sample_db))
        try:
            assert result["restored_dbname"] is None
        finally:
            Path(result["backup_file"]).unlink(missing_ok=True)

    def test_keep_restored_db_option_leaves_it_in_place(self, sample_db):
        result = backup_and_restore_test(_database_url_for(sample_db), keep_restored_db=True)
        try:
            assert result["restored_dbname"] is not None
            parts = _connection_parts()
            conn = psycopg.connect(dbname=result["restored_dbname"], **parts)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM records")
                    assert cur.fetchone()[0] == 3
            finally:
                conn.close()
        finally:
            Path(result["backup_file"]).unlink(missing_ok=True)
            admin = _admin_connect()
            try:
                with admin.cursor() as cur:
                    cur.execute(f"DROP DATABASE IF EXISTS {result['restored_dbname']} WITH (FORCE)")
            finally:
                admin.close()


@requires_db
class TestCanonicalTableHash:
    """pg_dump/pg_restoreを使わない、正規化ハッシュ計算そのものの単体テスト。"""

    def test_identical_data_produces_identical_hash(self, sample_db):
        parts = _connection_parts()
        conn_a = psycopg.connect(dbname=sample_db, **parts)
        conn_b = psycopg.connect(dbname=sample_db, **parts)
        try:
            hash_a, count_a = _canonical_table_hash(conn_a, "records")
            hash_b, count_b = _canonical_table_hash(conn_b, "records")
            assert hash_a == hash_b
            assert count_a == count_b == 3
        finally:
            conn_a.close()
            conn_b.close()

    def test_different_data_produces_different_hash(self, sample_db):
        parts = _connection_parts()
        conn = psycopg.connect(dbname=sample_db, **parts)
        try:
            hash_before, _ = _canonical_table_hash(conn, "records")
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO records (record_date, memo) VALUES ('2026-09-04', 'added')"
                )
            conn.commit()
            hash_after, count_after = _canonical_table_hash(conn, "records")
            assert hash_after != hash_before
            assert count_after == 4
        finally:
            conn.close()
