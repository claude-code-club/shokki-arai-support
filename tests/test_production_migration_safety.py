"""第16〜20回の本番migrationスクリプト5本(migrate_to_tenant_schema.py・
migrate_to_auth_schema.py・migrate_to_billing_schema.py・
migrate_to_webhook_schema.py・migrate_to_usage_schema.py)に追加した
接続先安全確認(scripts/production_target_identity.py)の統合テスト。

DATABASE_URLが指す接続先とは別の使い捨てデータベースを作成して隔離する
(tests/test_least_privilege_schema.pyと同じ設計方針)。各スクリプトは
subprocessとして実際に起動し、[OK]/[NG]の標準出力と、実行後のテーブル
存在有無の両方で検証する(モジュール内の関数を直接呼ぶのではなく、
main()のCLIエントリポイントを経由することで、安全確認が実際に
DDL実行より前に効いていることを保証する)。
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))

import db  # noqa: E402

SCRIPTS_DIR = ROOT / "scripts"

requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします",
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


def _identity_env(dbname, project_id="ci-test-project", environment_id="ci-test-environment"):
    """すべて正しい接続先識別情報(正常系のベース)。個々のテストで
    一部を書き換えて異常系を作る。
    """
    return {
        "EXPECTED_TARGET_DBNAME": dbname,
        "EXPECTED_TARGET_USER": _connection_parts()["user"],
        "EXPECTED_RAILWAY_PROJECT_ID": project_id,
        "RAILWAY_PROJECT_ID": project_id,
        "EXPECTED_RAILWAY_ENVIRONMENT_ID": environment_id,
        "RAILWAY_ENVIRONMENT_ID": environment_id,
        "PRODUCTION_DDL_EXPLICITLY_ALLOWED": "true",
        "BACKUP_RESTORE_TEST_CONFIRMED": "true",
    }


def _run_script(script_name, dbname, argv_extra=None, extra_env=None):
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"postgresql://{_connection_parts()['user']}:{_connection_parts()['password']}"
        f"@{_connection_parts()['host']}:{_connection_parts()['port']}/{dbname}"
    )
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    argv = [sys.executable, str(SCRIPTS_DIR / script_name)]
    if argv_extra:
        argv.extend(argv_extra)
    return subprocess.run(
        argv, cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8"
    )


def _table_names(dbname):
    parts = _connection_parts()
    with psycopg.connect(dbname=dbname, **parts) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            return {r[0] for r in cur.fetchall()}


@pytest.fixture
def baseline_db():
    """recordsテーブルだけを持つ、第16回適用前の状態を模した使い捨てDB。"""
    dbname = f"test_prodmig_{uuid.uuid4().hex[:12]}"
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
                """
                CREATE TABLE records (
                    id          BIGSERIAL PRIMARY KEY,
                    record_date DATE NOT NULL UNIQUE,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
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
class TestSequentialApplication:
    """①→⑤を正しい順序・正しい安全確認情報で適用すると、最初から最後まで
    成功し、全7テーブルが揃うことを確認する(要求仕様#8)。
    """

    def test_full_sequence_succeeds_and_produces_expected_schema(self, baseline_db):
        env = _identity_env(baseline_db)
        tenant_id = str(uuid.uuid4())

        proc = _run_script("migrate_to_tenant_schema.py", baseline_db, [tenant_id], env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "[OK]" in proc.stdout

        proc = _run_script("migrate_to_auth_schema.py", baseline_db, extra_env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr

        proc = _run_script("migrate_to_billing_schema.py", baseline_db, extra_env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr

        proc = _run_script("migrate_to_webhook_schema.py", baseline_db, extra_env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr

        proc = _run_script("migrate_to_usage_schema.py", baseline_db, extra_env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr

        tables = _table_names(baseline_db)
        assert tables == {
            "records", "tenants", "tenant_memberships", "users",
            "tenant_subscriptions", "tenant_usage", "processed_stripe_events",
        }

    def test_tenant_schema_step_reports_sha256_verification(self, baseline_db):
        """第16回(JSON相当のrecords→tenant付きschemaへの移行)は、件数だけで
        なく正規化データのSHA-256でも移行前後を検証し、その値を[OK]
        メッセージへ出力すること(要求仕様#6)。
        """
        env = _identity_env(baseline_db)
        tenant_id = str(uuid.uuid4())

        proc = _run_script("migrate_to_tenant_schema.py", baseline_db, [tenant_id], env)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "SHA-256" in proc.stdout
        assert "移行前" in proc.stdout and "移行後" in proc.stdout

    def test_full_sequence_is_idempotent_when_rerun(self, baseline_db):
        """①→⑤を一度適用した後、同じ順序でもう一度流しても全て[OK]で
        終わり、スキーマが変化しないこと(再実行時の挙動)。
        """
        env = _identity_env(baseline_db)
        tenant_id = str(uuid.uuid4())
        scripts = [
            ("migrate_to_tenant_schema.py", [tenant_id]),
            ("migrate_to_auth_schema.py", None),
            ("migrate_to_billing_schema.py", None),
            ("migrate_to_webhook_schema.py", None),
            ("migrate_to_usage_schema.py", None),
        ]
        for name, argv_extra in scripts:
            proc = _run_script(name, baseline_db, argv_extra, env)
            assert proc.returncode == 0, proc.stdout + proc.stderr

        tables_after_first = _table_names(baseline_db)

        for name, argv_extra in scripts:
            proc = _run_script(name, baseline_db, argv_extra, env)
            assert proc.returncode == 0, f"{name} 再実行が失敗: {proc.stdout + proc.stderr}"

        assert _table_names(baseline_db) == tables_after_first


@requires_db
class TestOutOfOrderIsBlocked:
    """依存する前段の回を飛ばして実行すると、DDLを一切実行せずに[NG]で
    停止すること(実行前条件の確認)。
    """

    def test_auth_schema_without_tenant_schema_is_blocked(self, baseline_db):
        env = _identity_env(baseline_db)
        proc = _run_script("migrate_to_auth_schema.py", baseline_db, extra_env=env)
        assert proc.returncode == 1
        assert "[NG]" in proc.stdout
        assert "第17回" in proc.stdout
        assert _table_names(baseline_db) == {"records"}

    def test_billing_schema_without_tenant_schema_is_blocked(self, baseline_db):
        env = _identity_env(baseline_db)
        proc = _run_script("migrate_to_billing_schema.py", baseline_db, extra_env=env)
        assert proc.returncode == 1
        assert "[NG]" in proc.stdout
        assert _table_names(baseline_db) == {"records"}

    def test_webhook_schema_without_billing_schema_is_blocked(self, baseline_db):
        # 第16回だけ適用し、第18回(billing)を飛ばして第19回を実行する。
        env = _identity_env(baseline_db)
        tenant_id = str(uuid.uuid4())
        proc = _run_script("migrate_to_tenant_schema.py", baseline_db, [tenant_id], env)
        assert proc.returncode == 0, proc.stdout + proc.stderr

        proc = _run_script("migrate_to_webhook_schema.py", baseline_db, extra_env=env)
        assert proc.returncode == 1
        assert "[NG]" in proc.stdout
        assert "processed_stripe_events" not in _table_names(baseline_db)

    def test_usage_schema_without_billing_schema_is_blocked(self, baseline_db):
        env = _identity_env(baseline_db)
        tenant_id = str(uuid.uuid4())
        proc = _run_script("migrate_to_tenant_schema.py", baseline_db, [tenant_id], env)
        assert proc.returncode == 0, proc.stdout + proc.stderr

        proc = _run_script("migrate_to_usage_schema.py", baseline_db, extra_env=env)
        assert proc.returncode == 1
        assert "[NG]" in proc.stdout
        assert "tenant_usage" not in _table_names(baseline_db)


@requires_db
class TestConnectionIdentityIsEnforced:
    """接続先識別情報が欠落・不一致の場合、正しい実行順序であっても
    DDLを一切実行せずに[NG]で停止すること。
    """

    @pytest.mark.parametrize(
        "env_override",
        [
            {"PRODUCTION_DDL_EXPLICITLY_ALLOWED": ""},
            {"PRODUCTION_DDL_EXPLICITLY_ALLOWED": "false"},
            {"BACKUP_RESTORE_TEST_CONFIRMED": ""},
            {"BACKUP_RESTORE_TEST_CONFIRMED": "false"},
            {"EXPECTED_TARGET_DBNAME": "wrong-database-name"},
            {"EXPECTED_TARGET_USER": "wrong-user"},
            {"EXPECTED_RAILWAY_PROJECT_ID": "wrong-project"},
            {"EXPECTED_RAILWAY_ENVIRONMENT_ID": "wrong-environment"},
            {"RAILWAY_PROJECT_ID": ""},
            {"RAILWAY_ENVIRONMENT_ID": ""},
        ],
        ids=[
            "flag-unset", "flag-false",
            "backup-restore-unconfirmed", "backup-restore-false",
            "wrong-dbname", "wrong-user",
            "wrong-project-id", "wrong-environment-id",
            "railway-project-id-missing", "railway-environment-id-missing",
        ],
    )
    def test_mismatched_or_missing_identity_blocks_first_migration(self, baseline_db, env_override):
        env = _identity_env(baseline_db)
        env.update(env_override)
        tenant_id = str(uuid.uuid4())

        proc = _run_script("migrate_to_tenant_schema.py", baseline_db, [tenant_id], env)

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "[NG]" in proc.stdout
        assert _table_names(baseline_db) == {"records"}
