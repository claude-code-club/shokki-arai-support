"""第22回・PostgreSQL最小権限化とRLS導入(仕様書/PostgreSQL最小権限化・
RLS設計.md 第13次改訂版)の統合テスト。

このテストが検証するスクリプト群(scripts/migrate_to_least_privilege_
schema.py・scripts/migrate_to_stripe_subscription_id_unique_schema.py・
scripts/rollback_*.py)は、`public`スキーマに固定した(search_path乗っ取り
防止のため)SECURITY DEFINER関数を作成する。既存テストのようなスキーマ
分離(CREATE SCHEMA test_x/SET search_path)では検証できないため、
CIジョブが提供するPostgreSQLサーバー上に、DATABASE_URLが指す接続先とは
別の使い捨てデータベースを作成して隔離する(仕様書15-1章のCI設計方針)。

このファイルは、db.py・auth.pyをまだ新関数呼び出しへ切り替えていない
段階のもの(実装フェーズの前半)。db.py/auth.pyの切替とその際の既存
202件のテストへの影響は、別の段階で扱う。
"""
import json
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

BASELINE_SCHEMA_SQL = """
CREATE TABLE users (
    id             UUID PRIMARY KEY,
    auth_subject   TEXT UNIQUE NOT NULL,
    email          TEXT,
    email_verified BOOLEAN NOT NULL DEFAULT false,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tenants (
    id         UUID PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tenant_memberships (
    tenant_id  UUID NOT NULL REFERENCES tenants(id),
    user_id    UUID NOT NULL REFERENCES users(id),
    role       TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE records (
    id          BIGSERIAL PRIMARY KEY,
    record_date DATE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id)
);
CREATE UNIQUE INDEX records_tenant_id_record_date_unique ON records (tenant_id, record_date);

CREATE TABLE tenant_subscriptions (
    tenant_id                  UUID PRIMARY KEY REFERENCES tenants(id),
    plan                       TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'standard')),
    status                     TEXT NOT NULL DEFAULT 'active',
    stripe_customer_id         TEXT,
    stripe_subscription_id     TEXT,
    stripe_checkout_session_id TEXT UNIQUE,
    current_period_end         TIMESTAMPTZ,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tenant_usage (
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    metric_key   TEXT NOT NULL,
    period_start DATE NOT NULL,
    usage_count  INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, metric_key, period_start)
);

CREATE TABLE processed_stripe_events (
    stripe_event_id TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

PASSWORD_ENV = {
    "LEAST_PRIVILEGE_APP_RUNTIME_PASSWORD": "ci-test-only-not-a-real-secret",
    "LEAST_PRIVILEGE_APP_WEBHOOK_PASSWORD": "ci-test-only-not-a-real-secret",
}


def _connection_parts():
    """DATABASE_URLをホスト/ポート/ユーザー/パスワードへ分解する
    (dbnameだけをテストごとに差し替えるため)。
    """
    parsed = urlparse(os.environ["DATABASE_URL"])
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "user": parsed.username,
        "password": parsed.password or "",
    }


def _admin_connect(dbname="postgres"):
    parts = _connection_parts()
    conn = psycopg.connect(dbname=dbname, autocommit=True, **parts)
    return conn


def _database_url_for(dbname):
    parts = _connection_parts()
    return f"postgresql://{parts['user']}:{parts['password']}@{parts['host']}:{parts['port']}/{dbname}"


def _execute_as_role(dbname, role, sql, params):
    """新しい接続でSET ROLEしてから1文だけ実行し、結果行(SELECTの場合)を返す。

    非LOCALのSET ROLEはトランザクション内で実行するとROLLBACKで巻き戻る
    (PostgreSQLの仕様)ため、同じ接続でエラー後にrollbackして次のチェックを
    続けると、意図せず接続元のロール(postgres)で実行されてしまう
    (2026-09-04の監査で発見・記録済みの方法論上のミス、
    仕様書のitem_methodology_note相当)。この関数は呼び出しごとに新しい接続を
    張ることでこの問題を避ける。
    """
    parts = _connection_parts()
    conn = psycopg.connect(dbname=dbname, **parts)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET ROLE {role}")
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description is not None else None
        conn.commit()
        return rows
    finally:
        conn.close()


def _identity_env(dbname, project_id="ci-test-project", environment_id="ci-test-environment"):
    return {
        "EXPECTED_TARGET_DBNAME": dbname,
        "EXPECTED_TARGET_USER": _connection_parts()["user"],
        "EXPECTED_RAILWAY_PROJECT_ID": project_id,
        "RAILWAY_PROJECT_ID": project_id,
        "EXPECTED_RAILWAY_ENVIRONMENT_ID": environment_id,
        "RAILWAY_ENVIRONMENT_ID": environment_id,
        "STAGING_DDL_EXPLICITLY_ALLOWED": "true",
    }


def _run_script(script_name, dbname, extra_env=None):
    env = os.environ.copy()
    env["DATABASE_URL"] = _database_url_for(dbname)
    # スクリプトの[OK]/[NG]メッセージは日本語を含むため、コンソールの既定
    # コードページがUTF-8でない環境でもsubprocess側の出力を確実にUTF-8として
    # デコードできるよう明示する(tests/test_records_search.pyの
    # _run_migrate_memo_script()と同じ対応)。
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script_name)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc


def _query_full_state(dbname):
    parts = _connection_parts()
    with psycopg.connect(dbname=dbname, **parts) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            tables = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT sequencename FROM pg_sequences WHERE schemaname='public'")
            sequences = {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public' AND p.prokind='f'"
            )
            functions = cur.fetchone()[0]
            cur.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'app\\_%'")
            roles = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname='public'")
            policies = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND rowsecurity")
            rls = cur.fetchone()[0]
            cur.execute("SELECT to_regclass('public.schema_migration_log') IS NOT NULL")
            log_exists = cur.fetchone()[0]
            cur.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='tenant_subscriptions' "
                "AND column_name='stripe_subscription_id'"
            )
            row = cur.fetchone()
            is_nullable = row[0] if row else None
    return {
        "tables": tables, "sequences": sequences, "functions": functions,
        "roles": roles, "policies": policies, "rls": rls,
        "log_exists": log_exists, "is_nullable": is_nullable,
    }


EXPECTED_BASELINE_TABLES = {
    "records", "tenants", "tenant_memberships", "users",
    "tenant_subscriptions", "tenant_usage", "processed_stripe_events",
}


def _is_round21_baseline(state):
    return (
        state["tables"] == EXPECTED_BASELINE_TABLES
        and state["sequences"] == {"records_id_seq"}
        and state["functions"] == 0
        and state["roles"] == set()
        and state["policies"] == 0
        and state["rls"] == 0
        and state["is_nullable"] == "YES"
        and not state["log_exists"]
    )


@pytest.fixture
def lp_db():
    """`DATABASE_URL`が指す接続先とは別の、使い捨てデータベースを1つ
    用意する。テスト終了時にデータベース・関連ロールを削除する。
    """
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    dbname = f"test_lp_{uuid.uuid4().hex[:16]}"
    admin = _admin_connect()
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        admin.close()

    try:
        yield dbname
    finally:
        admin = _admin_connect()
        try:
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
                for role in ("app_runtime", "app_webhook", "app_data_owner"):
                    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
                    if cur.fetchone():
                        cur.execute(f'DROP ROLE "{role}"')
        finally:
            admin.close()


def _apply_baseline(dbname):
    parts = _connection_parts()
    with psycopg.connect(dbname=dbname, **parts) as conn:
        with conn.cursor() as cur:
            cur.execute(BASELINE_SCHEMA_SQL)
        conn.commit()


def _build_full_state(dbname):
    _apply_baseline(dbname)
    env = {**PASSWORD_ENV, **_identity_env(dbname)}
    p1 = _run_script("migrate_to_least_privilege_schema.py", dbname, env)
    assert p1.returncode == 0, p1.stdout + p1.stderr
    p2 = _run_script(
        "migrate_to_stripe_subscription_id_unique_schema.py", dbname, _identity_env(dbname)
    )
    assert p2.returncode == 0, p2.stdout + p2.stderr


def _log_archive_env(dbname):
    proc = _run_script("export_and_hash_migration_log.py", dbname, _identity_env(dbname))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    row_count = sha256 = None
    for line in proc.stdout.splitlines():
        if "SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNT=" in line:
            row_count = line.split("=", 1)[1].strip()
        if "SCHEMA_MIGRATION_LOG_ARCHIVE_SHA256=" in line:
            sha256 = line.split("=", 1)[1].strip()
    if row_count is None or sha256 is None:
        return {"SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED": "true"}
    return {
        "SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED": "true",
        "SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNT": row_count,
        "SCHEMA_MIGRATION_LOG_ARCHIVE_SHA256": sha256,
    }


@requires_db
class TestLeastPrivilegeMigration:
    def test_initial_apply_succeeds(self, lp_db):
        _apply_baseline(lp_db)
        proc = _run_script(
            "migrate_to_least_privilege_schema.py", lp_db, {**PASSWORD_ENV, **_identity_env(lp_db)}
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "[OK]" in proc.stdout

    def test_reapply_is_idempotent(self, lp_db):
        _apply_baseline(lp_db)
        env = {**PASSWORD_ENV, **_identity_env(lp_db)}
        first = _run_script("migrate_to_least_privilege_schema.py", lp_db, env)
        second = _run_script("migrate_to_least_privilege_schema.py", lp_db, env)
        assert first.returncode == 0, first.stdout + first.stderr
        assert second.returncode == 0, second.stdout + second.stderr

    def test_acl_verification_detects_unknown_grantee_and_public_regrant(self, lp_db):
        _build_full_state(lp_db)
        sys.path.insert(0, str(SCRIPTS_DIR))
        import least_privilege_lib  # noqa: E402

        parts = _connection_parts()
        with psycopg.connect(dbname=lp_db, **parts) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE ROLE test_intruder NOLOGIN")
                cur.execute("GRANT EXECUTE ON FUNCTION public.get_subscription(uuid) TO test_intruder")
            conn.commit()
            with pytest.raises(least_privilege_lib.UnexpectedGranteeError):
                with conn.cursor() as cur:
                    least_privilege_lib.verify_all_function_grants(cur)
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("REVOKE EXECUTE ON FUNCTION public.get_subscription(uuid) FROM test_intruder")
                cur.execute("DROP ROLE test_intruder")
                cur.execute("GRANT EXECUTE ON FUNCTION public.get_subscription(uuid) TO PUBLIC")
            conn.commit()
            with pytest.raises(least_privilege_lib.UnexpectedGranteeError):
                with conn.cursor() as cur:
                    least_privilege_lib.verify_all_function_grants(cur)
            conn.rollback()

    def test_rls_isolates_tenants(self, lp_db):
        _build_full_state(lp_db)
        parts = _connection_parts()
        with psycopg.connect(dbname=lp_db, **parts) as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO public.tenants (id, name) VALUES (gen_random_uuid(), 'A') RETURNING id")
                tenant_a = cur.fetchone()[0]
                cur.execute("INSERT INTO public.tenants (id, name) VALUES (gen_random_uuid(), 'B') RETURNING id")
            conn.commit()
        with psycopg.connect(dbname=lp_db, **parts) as conn:
            with conn.cursor() as cur:
                cur.execute("SET ROLE app_data_owner")
                cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant_a),))
                cur.execute("SELECT id FROM public.tenants")
                rows = cur.fetchall()
            conn.rollback()
        assert {str(r[0]) for r in rows} == {str(tenant_a)}

    def test_memo_search_functions_work_as_app_runtime_and_isolate_tenants(self, lp_db):
        """第22課題(検索できるDB、PR #30)との統合対応(案A)。app_runtimeとして
        接続し、record_with_memo_for_tenant()・search_records_for_tenant()を
        実際に呼び出し、保存・検索・世帯分離が最小権限下で機能することを実測する
        (項目⑪で指摘された不整合の解消を検証する回帰テスト)。
        """
        _build_full_state(lp_db)
        parts = _connection_parts()

        with psycopg.connect(dbname=lp_db, **parts) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.tenants (id, name) VALUES (gen_random_uuid(), 'A') RETURNING id"
                )
                tenant_a = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO public.tenants (id, name) VALUES (gen_random_uuid(), 'B') RETURNING id"
                )
                tenant_b = cur.fetchone()[0]
            conn.commit()

        with psycopg.connect(dbname=lp_db, **parts) as conn:
            with conn.cursor() as cur:
                cur.execute("SET ROLE app_runtime")
                cur.execute(
                    "SELECT public.record_with_memo_for_tenant(%s, %s, %s)",
                    (tenant_a, "2026-09-01", "世帯Aのメモ"),
                )
                cur.execute(
                    "SELECT public.record_with_memo_for_tenant(%s, %s, %s)",
                    (tenant_b, "2026-09-01", "世帯Bのメモ"),
                )
                cur.execute(
                    "SELECT record_date, memo FROM public.search_records_for_tenant(%s, %s, %s)",
                    (tenant_a, None, "desc"),
                )
                rows_a = cur.fetchall()
                cur.execute(
                    "SELECT record_date, memo FROM public.search_records_for_tenant(%s, %s, %s)",
                    (tenant_b, None, "desc"),
                )
                rows_b = cur.fetchall()
            conn.commit()

        assert [(r[0].isoformat(), r[1]) for r in rows_a] == [("2026-09-01", "世帯Aのメモ")]
        assert [(r[0].isoformat(), r[1]) for r in rows_b] == [("2026-09-01", "世帯Bのメモ")]

    def test_app_runtime_cannot_query_records_table_directly(self, lp_db):
        """app_runtimeはrecordsテーブルへの直接GRANTを持たず、SECURITY DEFINER
        関数のEXECUTE権限だけでmemo保存・検索ができることの裏付け(素のSQLでの
        直接SELECT・INSERT・UPDATEがいずれも拒否されることを確認する。
        統合監査round 3で指摘されたINSERT・UPDATEの拒否確認を追加)。
        """
        _build_full_state(lp_db)
        parts = _connection_parts()
        with psycopg.connect(dbname=lp_db, **parts) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.tenants (id, name) VALUES (gen_random_uuid(), 'A') RETURNING id"
                )
                tenant_a = cur.fetchone()[0]
            conn.commit()

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _execute_as_role(lp_db, "app_runtime", "SELECT * FROM public.records", None)

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _execute_as_role(
                lp_db, "app_runtime",
                "INSERT INTO public.records (tenant_id, record_date) VALUES (%s, %s)",
                (tenant_a, "2026-09-01"),
            )

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _execute_as_role(
                lp_db, "app_runtime",
                "UPDATE public.records SET memo = %s WHERE tenant_id = %s",
                ("不正な直接更新", tenant_a),
            )

    def test_app_webhook_cannot_execute_memo_search_functions(self, lp_db):
        """app_webhookはrecord_with_memo_for_tenant・search_records_for_tenantの
        EXECUTE権限を持たない(この2関数はapp_runtime専用)ことを確認する
        (統合監査round 3で指摘、EXPECTED_FUNCTION_GRANTSの静的定義だけでなく
        実際にapp_webhookとして呼び出して拒否されることを実測する)。
        """
        _build_full_state(lp_db)
        parts = _connection_parts()
        with psycopg.connect(dbname=lp_db, **parts) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.tenants (id, name) VALUES (gen_random_uuid(), 'A') RETURNING id"
                )
                tenant_a = cur.fetchone()[0]
            conn.commit()

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _execute_as_role(
                lp_db, "app_webhook",
                "SELECT public.record_with_memo_for_tenant(%s, %s, %s)",
                (tenant_a, "2026-09-01", "x"),
            )

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _execute_as_role(
                lp_db, "app_webhook",
                "SELECT * FROM public.search_records_for_tenant(%s, %s, %s)",
                (tenant_a, None, "desc"),
            )

    def test_memo_validation_rejects_invalid_input_and_preserves_existing_data(self, lp_db):
        """record_with_memo_for_tenant()自身がメモの長さ・制御文字を検証し
        (呼び出し元のPython層を信頼しない設計)、拒否時に既存行が一切
        変更されないことをapp_runtimeとして実測する(監査指摘②)。
        """
        _build_full_state(lp_db)
        parts = _connection_parts()
        with psycopg.connect(dbname=lp_db, **parts) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.tenants (id, name) VALUES (gen_random_uuid(), 'A') RETURNING id"
                )
                tenant_a = cur.fetchone()[0]
            conn.commit()

        _execute_as_role(
            lp_db, "app_runtime",
            "SELECT public.record_with_memo_for_tenant(%s, %s, %s)",
            (tenant_a, "2026-09-01", "既存メモ"),
        )

        with pytest.raises(psycopg.errors.InvalidParameterValue):
            _execute_as_role(
                lp_db, "app_runtime",
                "SELECT public.record_with_memo_for_tenant(%s, %s, %s)",
                (tenant_a, "2026-09-01", "a" * 201),
            )

        with pytest.raises(psycopg.errors.InvalidParameterValue):
            _execute_as_role(
                lp_db, "app_runtime",
                "SELECT public.record_with_memo_for_tenant(%s, %s, %s)",
                (tenant_a, "2026-09-02", "a" + chr(1) + "b"),
            )

        rows = _execute_as_role(
            lp_db, "app_runtime",
            "SELECT record_date, memo FROM public.search_records_for_tenant(%s, %s, %s)",
            (tenant_a, None, "asc"),
        )
        assert [(r[0].isoformat(), r[1]) for r in rows] == [("2026-09-01", "既存メモ")]

    def test_memo_resave_same_date_updates_memo(self, lp_db):
        """同日に再度record_with_memo_for_tenant()を呼ぶと、日付は変わらず
        memoだけが上書きされることをapp_runtimeとして実測する。
        """
        _build_full_state(lp_db)
        parts = _connection_parts()
        with psycopg.connect(dbname=lp_db, **parts) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.tenants (id, name) VALUES (gen_random_uuid(), 'A') RETURNING id"
                )
                tenant_a = cur.fetchone()[0]
            conn.commit()

        _execute_as_role(
            lp_db, "app_runtime",
            "SELECT public.record_with_memo_for_tenant(%s, %s, %s)",
            (tenant_a, "2026-09-01", "最初のメモ"),
        )
        _execute_as_role(
            lp_db, "app_runtime",
            "SELECT public.record_with_memo_for_tenant(%s, %s, %s)",
            (tenant_a, "2026-09-01", "書き直したメモ"),
        )
        rows = _execute_as_role(
            lp_db, "app_runtime",
            "SELECT record_date, memo FROM public.search_records_for_tenant(%s, %s, %s)",
            (tenant_a, None, "asc"),
        )
        assert [(r[0].isoformat(), r[1]) for r in rows] == [("2026-09-01", "書き直したメモ")]

    def test_search_validation_rejects_invalid_input(self, lp_db):
        """search_records_for_tenant()自身が検索キーワードの長さ・制御文字・
        orderのNULL/不正値を検証することをapp_runtimeとして実測する
        (監査指摘②③)。
        """
        _build_full_state(lp_db)
        parts = _connection_parts()
        with psycopg.connect(dbname=lp_db, **parts) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.tenants (id, name) VALUES (gen_random_uuid(), 'A') RETURNING id"
                )
                tenant_a = cur.fetchone()[0]
            conn.commit()

        for keyword, order in [
            ("k" * 101, "desc"),
            ("a" + chr(2) + "b", "desc"),
            (None, None),
            (None, "sideways"),
        ]:
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                _execute_as_role(
                    lp_db, "app_runtime",
                    "SELECT * FROM public.search_records_for_tenant(%s, %s, %s)",
                    (tenant_a, keyword, order),
                )

    def test_search_keyword_escapes_wildcards_and_both_orders_work(self, lp_db):
        """%・_・バックスラッシュを含むmemoが、search_records_for_tenant()で
        ワイルドカードとしてではなく文字どおりに検索されること、asc/desc
        両方の並び順が機能することをapp_runtimeとして実測する。
        (統合監査round 3で、docstringはバックスラッシュに言及していたが
        実際のテスト本体には含まれていなかった不整合を指摘され、追加した)
        """
        _build_full_state(lp_db)
        parts = _connection_parts()
        with psycopg.connect(dbname=lp_db, **parts) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.tenants (id, name) VALUES (gen_random_uuid(), 'A') RETURNING id"
                )
                tenant_a = cur.fetchone()[0]
            conn.commit()

        for record_date, memo in [
            ("2026-09-01", "50%オフ"),
            ("2026-09-02", "under_score"),
            ("2026-09-03", "back\\slash"),
            ("2026-09-04", "no special chars"),
        ]:
            _execute_as_role(
                lp_db, "app_runtime",
                "SELECT public.record_with_memo_for_tenant(%s, %s, %s)",
                (tenant_a, record_date, memo),
            )

        percent_rows = _execute_as_role(
            lp_db, "app_runtime",
            "SELECT record_date, memo FROM public.search_records_for_tenant(%s, %s, %s)",
            (tenant_a, "%", "asc"),
        )
        assert [r[0].isoformat() for r in percent_rows] == ["2026-09-01"]

        underscore_rows = _execute_as_role(
            lp_db, "app_runtime",
            "SELECT record_date, memo FROM public.search_records_for_tenant(%s, %s, %s)",
            (tenant_a, "_", "asc"),
        )
        assert [r[0].isoformat() for r in underscore_rows] == ["2026-09-02"]

        backslash_rows = _execute_as_role(
            lp_db, "app_runtime",
            "SELECT record_date, memo FROM public.search_records_for_tenant(%s, %s, %s)",
            (tenant_a, "\\", "asc"),
        )
        assert [r[0].isoformat() for r in backslash_rows] == ["2026-09-03"]

        asc_rows = _execute_as_role(
            lp_db, "app_runtime",
            "SELECT record_date, memo FROM public.search_records_for_tenant(%s, %s, %s)",
            (tenant_a, None, "asc"),
        )
        desc_rows = _execute_as_role(
            lp_db, "app_runtime",
            "SELECT record_date, memo FROM public.search_records_for_tenant(%s, %s, %s)",
            (tenant_a, None, "desc"),
        )
        assert [r[0].isoformat() for r in asc_rows] == [
            "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
        ]
        assert [r[0].isoformat() for r in desc_rows] == [
            "2026-09-04", "2026-09-03", "2026-09-02", "2026-09-01",
        ]


@requires_db
class TestRollbackLifecycle:
    def test_tier2_then_resume_reaches_baseline(self, lp_db):
        _build_full_state(lp_db)
        p_tier2 = _run_script("rollback_tier2_remove_roles.py", lp_db, _identity_env(lp_db))
        assert p_tier2.returncode == 0, p_tier2.stdout + p_tier2.stderr

        env_resume = {**_identity_env(lp_db), **_log_archive_env(lp_db)}
        p_resume = _run_script("rollback_resume_to_full_restore.py", lp_db, env_resume)
        assert p_resume.returncode == 0, p_resume.stdout + p_resume.stderr
        assert _is_round21_baseline(_query_full_state(lp_db))

    def test_tier3_direct_reaches_baseline(self, lp_db):
        _build_full_state(lp_db)
        env = {**_identity_env(lp_db), **_log_archive_env(lp_db)}
        proc = _run_script("rollback_tier3_full_restore.py", lp_db, env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _is_round21_baseline(_query_full_state(lp_db))

    def test_tier3_after_tier2_is_rejected_by_precondition(self, lp_db):
        _build_full_state(lp_db)
        _run_script("rollback_tier2_remove_roles.py", lp_db, _identity_env(lp_db))
        proc = _run_script("rollback_tier3_full_restore.py", lp_db, _identity_env(lp_db))
        assert proc.returncode == 1
        assert "RollbackPreconditionError" in proc.stdout

    def test_cross_database_dependency_degrades_then_resumes_to_completion(self, lp_db):
        """クロスDB依存でTier2がDEGRADED(終了コード2)になり、依存解消後は
        rollback_resume_to_full_restore.pyで完全復帰できることを確認する
        (仕様書 第13次改訂版・点A/E)。
        """
        _build_full_state(lp_db)
        secondary = f"{lp_db}_secondary"
        admin = _admin_connect()
        try:
            with admin.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{secondary}"')
        finally:
            admin.close()
        parts = _connection_parts()
        with psycopg.connect(dbname=secondary, **parts) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE t (id int)")
                cur.execute("GRANT SELECT ON t TO app_data_owner")
            conn.commit()

        try:
            p_degraded = _run_script("rollback_tier2_remove_roles.py", lp_db, _identity_env(lp_db))
            assert p_degraded.returncode == 2, p_degraded.stdout + p_degraded.stderr
            assert "[DEGRADED]" in p_degraded.stdout and "[OK]" not in p_degraded.stdout

            with psycopg.connect(dbname=secondary, **parts) as conn:
                with conn.cursor() as cur:
                    cur.execute("REVOKE SELECT ON t FROM app_data_owner")
                conn.commit()

            env_resume = {**_identity_env(lp_db), **_log_archive_env(lp_db)}
            p_resume = _run_script("rollback_resume_to_full_restore.py", lp_db, env_resume)
            assert p_resume.returncode == 0, p_resume.stdout + p_resume.stderr
            assert _is_round21_baseline(_query_full_state(lp_db))
        finally:
            admin = _admin_connect()
            try:
                with admin.cursor() as cur:
                    cur.execute(f'DROP DATABASE IF EXISTS "{secondary}" WITH (FORCE)')
            finally:
                admin.close()

    def test_baseline_mismatch_rolls_back(self, lp_db):
        _build_full_state(lp_db)
        parts = _connection_parts()
        with psycopg.connect(dbname=lp_db, **parts) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE public.unexpected_extra_table (id int)")
            conn.commit()
        before = _query_full_state(lp_db)
        env = {**_identity_env(lp_db), **_log_archive_env(lp_db)}
        proc = _run_script("rollback_tier3_full_restore.py", lp_db, env)
        after = _query_full_state(lp_db)
        assert proc.returncode == 1
        assert "BaselineStateMismatchError" in proc.stdout
        assert before["roles"] == after["roles"]
        assert "unexpected_extra_table" in after["tables"]

    def test_migration_log_not_archived_blocks_tier3(self, lp_db):
        _build_full_state(lp_db)
        proc = _run_script("rollback_tier3_full_restore.py", lp_db, _identity_env(lp_db))
        assert proc.returncode == 1
        assert "SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED" in proc.stdout


@requires_db
class TestTargetIdentity:
    def test_missing_baseline_tables_blocks_migration(self, lp_db):
        # baselineスキーマを適用しない = 想定7テーブルが無い状態
        proc = _run_script(
            "migrate_to_least_privilege_schema.py", lp_db, {**PASSWORD_ENV, **_identity_env(lp_db)}
        )
        assert proc.returncode == 1
        assert "TargetDatabaseMismatchError" in proc.stdout
        assert "missing_expected_tables" in proc.stdout

    def test_missing_expected_dbname_blocks_migration(self, lp_db):
        _apply_baseline(lp_db)
        env = {**PASSWORD_ENV, **_identity_env(lp_db)}
        del env["EXPECTED_TARGET_DBNAME"]
        proc = _run_script("migrate_to_least_privilege_schema.py", lp_db, env)
        assert proc.returncode == 1
        assert "EXPECTED_TARGET_DBNAME" in proc.stdout

    def test_railway_project_id_mismatch_blocks_migration(self, lp_db):
        _apply_baseline(lp_db)
        env = {**PASSWORD_ENV, **_identity_env(lp_db)}
        env["RAILWAY_PROJECT_ID"] = "different-project-id"
        proc = _run_script("migrate_to_least_privilege_schema.py", lp_db, env)
        assert proc.returncode == 1
        assert "RAILWAY_PROJECT_ID" in proc.stdout


@requires_db
class TestConstraintMigration:
    def test_apply_then_skip(self, lp_db):
        _apply_baseline(lp_db)
        env = {**PASSWORD_ENV, **_identity_env(lp_db)}
        _run_script("migrate_to_least_privilege_schema.py", lp_db, env)
        first = _run_script(
            "migrate_to_stripe_subscription_id_unique_schema.py", lp_db, _identity_env(lp_db)
        )
        assert first.returncode == 0
        assert "'result': 'applied'" in first.stdout
        second = _run_script(
            "migrate_to_stripe_subscription_id_unique_schema.py", lp_db, _identity_env(lp_db)
        )
        assert second.returncode == 0
        assert "[SKIP]" in second.stdout
