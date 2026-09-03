import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))

import db  # noqa: E402
import psycopg  # noqa: E402
import storage  # noqa: E402
import scripts.memo_search_functions as memo_search_functions_module  # noqa: E402
import scripts.migrate_to_records_memo_schema as migrate_memo_module  # noqa: E402

SCRIPTS_DIR = ROOT / "scripts"

requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします",
)


def _setup_memo_functions_database(dbname):
    """使い捨てデータベースへ、tenants/records(memo込み)を作り、
    PostgreSQL側のrecord_with_memo_for_tenant()・search_records_for_tenant()
    (scripts/memo_search_functions.py)を作成する。作成した世帯のtenant_idを返す。
    """
    with psycopg.connect(dbname=dbname, **_connection_parts()) as conn:
        with conn.cursor() as cur:
            cur.execute(MIGRATION_BASELINE_SQL)
            migrate_memo_module.ensure_records_memo_column(cur)
            memo_search_functions_module.create_or_replace_memo_search_functions(cur)
            tenant_id = uuid.uuid4()
            cur.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant_id, "テスト世帯"))
        conn.commit()
    return tenant_id


@pytest.fixture
def memo_schema():
    """record_with_memo_for_tenant()・search_records_for_tenant()(streamlit/db.py)は
    案A対応(PR #29のPostgreSQL最小権限化との統合)により、public.records固定の
    PostgreSQL関数(SECURITY DEFINER、SET search_path='')を呼ぶようになったため、
    スキーマ分離(CREATE SCHEMA/SET search_path)では検証できない(呼び出し先の
    PG関数が常にpublic.recordsを直接参照するため)。使い捨てデータベース
    (tests/test_least_privilege_schema.pyのlp_dbと同じ設計)を使い、db.py関数を
    直接検証する用途。(conn, tenant_id)を返す。
    """
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    dbname = f"test_memo_{uuid.uuid4().hex[:16]}"
    admin = _admin_connect()
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        admin.close()

    tenant_id = _setup_memo_functions_database(dbname)
    conn = psycopg.connect(dbname=dbname, **_connection_parts())

    try:
        yield conn, tenant_id
    finally:
        conn.rollback()
        conn.close()
        admin = _admin_connect()
        try:
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            admin.close()


@pytest.fixture
def memo_env(monkeypatch):
    """storage.py経由のadd_date_with_memo()/search_records()を、memo_schemaと同じ
    使い捨てデータベース(PG関数込み)で検証する。tests/test_storage.pyの
    tenant_envと同じ方式(db.get_connection()をスパイして接続先を固定する)。
    """
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    dbname = f"test_memo_env_{uuid.uuid4().hex[:16]}"
    admin = _admin_connect()
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        admin.close()

    tenant_id = _setup_memo_functions_database(dbname)
    disposable_url = _database_url_for(dbname)

    def patched_get_connection():
        return psycopg.connect(disposable_url)

    monkeypatch.setattr(db, "get_connection", patched_get_connection)
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")
    monkeypatch.setenv(storage.DEFAULT_TENANT_ID_ENV, str(tenant_id))

    try:
        yield tenant_id
    finally:
        admin = _admin_connect()
        try:
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            admin.close()


def _second_tenant(conn):
    other_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (other_id, "別世帯"))
    return other_id


# --- migrate_to_records_memo_schema.py: 接続先識別・列定義検証(監査項目③⑤⑥⑪) ---
#
# migrate_to_records_memo_schema()はpublic.recordsを完全修飾(search_path非依存)で
# 扱うため、上記のスキーマ分離フィクスチャ(memo_schema/memo_env)では検証できない。
# tests/test_least_privilege_schema.pyのlp_dbフィクスチャと同じ理由・同じ設計で、
# 使い捨てデータベースを使う。


MIGRATION_BASELINE_SQL = """
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


def _connection_parts():
    """DATABASE_URLをホスト/ポート/ユーザー/パスワードへ分解する
    (dbnameだけをテストごとに差し替えるため。tests/test_least_privilege_schema.py
    と同じヘルパー)。
    """
    parsed = urlparse(os.environ["DATABASE_URL"])
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "user": parsed.username,
        "password": parsed.password or "",
    }


def _admin_connect(dbname="postgres"):
    return psycopg.connect(dbname=dbname, autocommit=True, **_connection_parts())


def _database_url_for(dbname):
    parts = _connection_parts()
    return f"postgresql://{parts['user']}:{parts['password']}@{parts['host']}:{parts['port']}/{dbname}"


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


def _run_migrate_memo_script(dbname, extra_env=None):
    env = os.environ.copy()
    env["DATABASE_URL"] = _database_url_for(dbname)
    # スクリプトの[OK]/[NG]メッセージは日本語を含むため、Windows等でコンソールの
    # 既定コードページがUTF-8でない環境でもsubprocess側の出力を確実にUTF-8として
    # デコードできるよう明示する(指定が無いと環境依存でUnicodeDecodeErrorになりうる)。
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "migrate_to_records_memo_schema.py")],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _apply_migration_baseline(dbname, *, with_existing_record=True):
    """使い捨てデータベースへ、staging相当の7テーブルを作る。with_existing_record
    がTrueの場合、1件の既存記録(migration前=memo列が無い状態)も作り、その
    tenant_idを返す(既存データ保持の検証用)。
    """
    tenant_id = None
    with psycopg.connect(dbname=dbname, **_connection_parts()) as conn:
        with conn.cursor() as cur:
            cur.execute(MIGRATION_BASELINE_SQL)
            if with_existing_record:
                tenant_id = uuid.uuid4()
                cur.execute(
                    "INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant_id, "既存世帯")
                )
                cur.execute(
                    "INSERT INTO records (tenant_id, record_date) VALUES (%s, %s)",
                    (tenant_id, "2026-08-01"),
                )
        conn.commit()
    return tenant_id


@pytest.fixture
def memo_migration_db():
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    dbname = f"test_memo_migration_{uuid.uuid4().hex[:16]}"
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
        finally:
            admin.close()


@requires_db
def test_migrate_memo_main_rejects_when_identity_env_missing(memo_migration_db):
    """接続先識別env(EXPECTED_TARGET_DBNAME等)が無ければDDLを実行せず停止する
    (監査項目⑪、PR #29のtarget_identity.pyと同じ安全装置)。
    """
    dbname = memo_migration_db
    _apply_migration_baseline(dbname)

    proc = _run_migrate_memo_script(dbname, extra_env={})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "EXPECTED_TARGET_DBNAME" in proc.stdout

    with psycopg.connect(dbname=dbname, **_connection_parts()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='records' AND column_name='memo'"
            )
            assert cur.fetchone() is None  # DDLが実行されていないこと


@requires_db
def test_migrate_memo_main_rejects_when_dbname_mismatches(memo_migration_db):
    dbname = memo_migration_db
    _apply_migration_baseline(dbname)

    env = _identity_env(dbname)
    env["EXPECTED_TARGET_DBNAME"] = "some-other-database-not-this-one"
    proc = _run_migrate_memo_script(dbname, extra_env=env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "接続先データベース名が想定と異なります" in proc.stdout


@requires_db
def test_migrate_memo_main_succeeds_and_preserves_existing_data(memo_migration_db):
    """接続先識別が一致する場合、DDLが実行され、既存データ(既存1件の記録の
    tenant_id・record_date)が変更されず、memo=NULLで追加されることを確認する
    (監査項目⑥⑪、実際のCLIスクリプトを使い捨てDBに対してsubprocess実行)。
    """
    dbname = memo_migration_db
    tenant_id = _apply_migration_baseline(dbname)

    proc = _run_migrate_memo_script(dbname, extra_env=_identity_env(dbname))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[OK]" in proc.stdout

    with psycopg.connect(dbname=dbname, **_connection_parts()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='records' AND column_name='memo'"
            )
            assert cur.fetchone() == ("text", "YES", None)

            cur.execute("SELECT tenant_id, record_date, memo FROM records")
            rows = cur.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == tenant_id
            assert rows[0][2] is None  # 既存行はmemo=NULL


@requires_db
def test_migrate_memo_main_is_idempotent(memo_migration_db):
    dbname = memo_migration_db
    _apply_migration_baseline(dbname)
    env = _identity_env(dbname)

    proc1 = _run_migrate_memo_script(dbname, extra_env=env)
    assert proc1.returncode == 0, proc1.stdout + proc1.stderr
    proc2 = _run_migrate_memo_script(dbname, extra_env=env)  # 2回目も安全
    assert proc2.returncode == 0, proc2.stdout + proc2.stderr


@requires_db
def test_migrate_memo_rejects_preexisting_column_with_wrong_type(memo_migration_db):
    """memo列が既に別の型(INTEGER)で存在する場合、IF NOT EXISTSによる
    サイレントな成功と誤認せず、UnexpectedColumnDefinitionErrorで停止する
    (監査項目③、ChatGPT指摘のシナリオそのもの)。
    """
    dbname = memo_migration_db
    _apply_migration_baseline(dbname, with_existing_record=False)

    with psycopg.connect(dbname=dbname, **_connection_parts()) as setup_conn:
        with setup_conn.cursor() as cur:
            cur.execute("ALTER TABLE records ADD COLUMN memo INTEGER")
        setup_conn.commit()

    with psycopg.connect(dbname=dbname, **_connection_parts()) as conn:
        with pytest.raises(migrate_memo_module.UnexpectedColumnDefinitionError):
            migrate_memo_module.migrate_to_records_memo_schema(conn=conn)

        # エラー後も列の型が変更されていないこと(INTEGERのまま)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='records' AND column_name='memo'"
            )
            assert cur.fetchone() == ("integer",)


# --- db.record_with_memo_for_tenant() ---


@requires_db
def test_record_with_memo_inserts_new_record(memo_schema):
    conn, tenant_id = memo_schema
    db.record_with_memo_for_tenant("2026-09-01", "朝ごはんの後に", conn, tenant_id=tenant_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id)
    assert results == [{"date": "2026-09-01", "memo": "朝ごはんの後に"}]


@requires_db
def test_record_with_memo_updates_existing_memo(memo_schema):
    conn, tenant_id = memo_schema
    db.record_with_memo_for_tenant("2026-09-01", "最初のメモ", conn, tenant_id=tenant_id)
    db.record_with_memo_for_tenant("2026-09-01", "書き直したメモ", conn, tenant_id=tenant_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id)
    assert results == [{"date": "2026-09-01", "memo": "書き直したメモ"}]


@requires_db
def test_record_with_memo_none_stores_null(memo_schema):
    conn, tenant_id = memo_schema
    db.record_with_memo_for_tenant("2026-09-01", None, conn, tenant_id=tenant_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id)
    assert results == [{"date": "2026-09-01", "memo": None}]


# --- db.search_records_for_tenant(): 並び順 ---


@requires_db
def test_search_records_defaults_to_newest_first(memo_schema):
    conn, tenant_id = memo_schema
    for d in ("2026-09-01", "2026-09-03", "2026-09-02"):
        db.record_with_memo_for_tenant(d, None, conn, tenant_id=tenant_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id)
    assert [r["date"] for r in results] == ["2026-09-03", "2026-09-02", "2026-09-01"]


@requires_db
def test_search_records_oldest_first(memo_schema):
    conn, tenant_id = memo_schema
    for d in ("2026-09-01", "2026-09-03", "2026-09-02"):
        db.record_with_memo_for_tenant(d, None, conn, tenant_id=tenant_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id, order="asc")
    assert [r["date"] for r in results] == ["2026-09-01", "2026-09-02", "2026-09-03"]


@requires_db
def test_search_records_invalid_order_raises(memo_schema):
    conn, tenant_id = memo_schema
    with pytest.raises(ValueError):
        db.search_records_for_tenant(conn, tenant_id=tenant_id, order="sideways")


# --- db.search_records_for_tenant(): キーワード検索 ---


@requires_db
def test_search_records_keyword_filters_by_memo_substring(memo_schema):
    conn, tenant_id = memo_schema
    db.record_with_memo_for_tenant("2026-09-01", "食洗機が壊れた", conn, tenant_id=tenant_id)
    db.record_with_memo_for_tenant("2026-09-02", "手洗いで頑張った", conn, tenant_id=tenant_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id, keyword="食洗機")
    assert [r["date"] for r in results] == ["2026-09-01"]


@requires_db
def test_search_records_keyword_is_case_insensitive(memo_schema):
    conn, tenant_id = memo_schema
    db.record_with_memo_for_tenant("2026-09-01", "Dishwasher fixed", conn, tenant_id=tenant_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id, keyword="dishwasher")
    assert [r["date"] for r in results] == ["2026-09-01"]


# --- db.search_records_for_tenant(): 特殊文字(SQL LIKEワイルドカードの漏れ防止、監査項目⑦) ---


@requires_db
def test_search_records_keyword_percent_is_treated_literally(memo_schema):
    """keyword="%"はSQL LIKEのワイルドカードとしてではなく、memoに実際に
    "%"という文字が含まれる記録だけにマッチする(全件マッチしない)ことを確認する。
    """
    conn, tenant_id = memo_schema
    db.record_with_memo_for_tenant("2026-09-01", "10%オフだった", conn, tenant_id=tenant_id)
    db.record_with_memo_for_tenant("2026-09-02", "割引なし", conn, tenant_id=tenant_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id, keyword="%")
    assert [r["date"] for r in results] == ["2026-09-01"]


@requires_db
def test_search_records_keyword_underscore_is_treated_literally(memo_schema):
    """keyword="_"はSQL LIKEの単一文字ワイルドカードとしてではなく、memoに
    実際に"_"という文字が含まれる記録だけにマッチする(全件マッチしない)ことを確認する。
    """
    conn, tenant_id = memo_schema
    db.record_with_memo_for_tenant("2026-09-01", "under_score_here", conn, tenant_id=tenant_id)
    db.record_with_memo_for_tenant("2026-09-02", "no special chars", conn, tenant_id=tenant_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id, keyword="_")
    assert [r["date"] for r in results] == ["2026-09-01"]


@requires_db
def test_search_records_keyword_backslash_is_treated_literally(memo_schema):
    conn, tenant_id = memo_schema
    db.record_with_memo_for_tenant("2026-09-01", "back\\slash", conn, tenant_id=tenant_id)
    db.record_with_memo_for_tenant("2026-09-02", "no backslash", conn, tenant_id=tenant_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id, keyword="\\")
    assert [r["date"] for r in results] == ["2026-09-01"]


@requires_db
def test_search_records_keyword_percent_does_not_match_unrelated_records(memo_schema):
    """keyword="50%"のような具体的な文字列でも、"%"が正しく文字通り扱われ、
    無関係な記録を誤ってマッチさせないことを確認する。
    """
    conn, tenant_id = memo_schema
    db.record_with_memo_for_tenant("2026-09-01", "ちょうど50%引きだった", conn, tenant_id=tenant_id)
    db.record_with_memo_for_tenant("2026-09-02", "500円引きだった", conn, tenant_id=tenant_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id, keyword="50%")
    assert [r["date"] for r in results] == ["2026-09-01"]


@requires_db
def test_search_records_keyword_no_match_returns_empty(memo_schema):
    conn, tenant_id = memo_schema
    db.record_with_memo_for_tenant("2026-09-01", "手洗いで頑張った", conn, tenant_id=tenant_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id, keyword="該当なし")
    assert results == []


@requires_db
def test_search_records_no_keyword_returns_all_including_memoless(memo_schema):
    conn, tenant_id = memo_schema
    db.record_with_memo_for_tenant("2026-09-01", None, conn, tenant_id=tenant_id)
    db.record_with_memo_for_tenant("2026-09-02", "メモあり", conn, tenant_id=tenant_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id)
    assert len(results) == 2


# --- db.search_records_for_tenant(): テナント越境しないこと ---


@requires_db
def test_search_records_does_not_leak_other_tenant(memo_schema):
    conn, tenant_id = memo_schema
    other_id = _second_tenant(conn)
    db.record_with_memo_for_tenant("2026-09-01", "自分の世帯のメモ", conn, tenant_id=tenant_id)
    db.record_with_memo_for_tenant("2026-09-01", "別世帯のメモ", conn, tenant_id=other_id)
    conn.commit()

    results = db.search_records_for_tenant(conn, tenant_id=tenant_id)
    assert results == [{"date": "2026-09-01", "memo": "自分の世帯のメモ"}]


# --- storage._validate_memo() / _validate_search_keyword(): DB接続不要 ---


def test_validate_memo_rejects_too_long():
    with pytest.raises(storage.InvalidInputError):
        storage._validate_memo("あ" * (storage.MEMO_MAX_LENGTH + 1))


def test_validate_memo_rejects_control_characters():
    with pytest.raises(storage.InvalidInputError):
        storage._validate_memo("メモ\x00")


def test_validate_memo_treats_blank_as_none():
    assert storage._validate_memo("   ") is None
    assert storage._validate_memo(None) is None


def test_validate_memo_strips_surrounding_whitespace():
    assert storage._validate_memo("  食洗機  ") == "食洗機"


def test_validate_search_keyword_rejects_too_long():
    with pytest.raises(storage.InvalidInputError):
        storage._validate_search_keyword("あ" * (storage.KEYWORD_MAX_LENGTH + 1))


def test_validate_search_keyword_rejects_control_characters():
    with pytest.raises(storage.InvalidInputError):
        storage._validate_search_keyword("食洗機\x00")


def test_validate_search_keyword_treats_blank_as_none():
    assert storage._validate_search_keyword("") is None
    assert storage._validate_search_keyword(None) is None


# --- storage.add_date_with_memo() / search_records(): jsonバックエンドでは利用不可 ---


def test_add_date_with_memo_raises_on_json_backend(monkeypatch):
    monkeypatch.delenv(storage.STORAGE_BACKEND_ENV, raising=False)
    with pytest.raises(storage.StorageConfigError):
        storage.add_date_with_memo("2026-09-01", "メモ")


def test_search_records_raises_on_json_backend(monkeypatch):
    monkeypatch.delenv(storage.STORAGE_BACKEND_ENV, raising=False)
    with pytest.raises(storage.StorageConfigError):
        storage.search_records()


# --- storage.add_date_with_memo() / search_records(): postgresバックエンド実機 ---


@requires_db
def test_add_date_with_memo_then_search_roundtrip(memo_env):
    tenant_id = memo_env
    storage.add_date_with_memo("2026-09-01", "実機テストのメモ", tenant_id=tenant_id)

    results = storage.search_records(tenant_id=tenant_id)
    assert results == [{"date": "2026-09-01", "memo": "実機テストのメモ"}]


@requires_db
def test_add_date_with_memo_rejects_invalid_memo(memo_env):
    tenant_id = memo_env
    with pytest.raises(storage.InvalidInputError):
        storage.add_date_with_memo(
            "2026-09-01", "あ" * (storage.MEMO_MAX_LENGTH + 1), tenant_id=tenant_id
        )


@requires_db
def test_search_records_keyword_via_storage(memo_env):
    tenant_id = memo_env
    storage.add_date_with_memo("2026-09-01", "食洗機の調子が悪い", tenant_id=tenant_id)
    storage.add_date_with_memo("2026-09-02", "手洗い快調", tenant_id=tenant_id)

    results = storage.search_records(tenant_id=tenant_id, keyword="食洗機")
    assert [r["date"] for r in results] == ["2026-09-01"]


@requires_db
def test_add_date_with_memo_requires_tenant_id_on_postgres(memo_env):
    with pytest.raises(storage.StorageConfigError):
        storage.add_date_with_memo("2026-09-01", "メモ", tenant_id=None)


@requires_db
def test_search_records_requires_tenant_id_on_postgres(memo_env):
    with pytest.raises(storage.StorageConfigError):
        storage.search_records(tenant_id=None)


@requires_db
def test_search_records_too_long_keyword_raises_invalid_input_error_via_storage(memo_env):
    """app.pyが実際に呼ぶsearch_records()自体が、101文字超のkeywordに対して
    InvalidInputError(StorageConfigErrorのサブクラス)を送出することを確認する
    (監査項目②、呼び出し層でのテスト)。app.py側はこれをexcept storage.InvalidInputError
    で個別に捕捉し、専用の案内文を表示する(st.max_charsに加えた二重の防御)。
    """
    tenant_id = memo_env
    with pytest.raises(storage.InvalidInputError):
        storage.search_records(tenant_id=tenant_id, keyword="a" * (storage.KEYWORD_MAX_LENGTH + 1))


@requires_db
def test_search_records_empty_keyword_via_storage_returns_all(memo_env):
    """空文字のkeywordは絞り込みとして扱われず、全件返す(監査項目⑦)。"""
    tenant_id = memo_env
    storage.add_date_with_memo("2026-09-01", "何かメモ", tenant_id=tenant_id)

    results = storage.search_records(tenant_id=tenant_id, keyword="")
    assert results == [{"date": "2026-09-01", "memo": "何かメモ"}]


@requires_db
def test_search_records_via_storage_does_not_leak_other_tenant(memo_env):
    """storage層(app.pyから実際に呼ばれる関数)でも、世帯Aのメモ・検索結果に
    世帯Bの記録が一切混ざらないことを確認する(db.py層の検証に加えて、
    storage層でも同じ性質が保たれていることの二重確認。監査項目⑧)。
    """
    tenant_id = memo_env
    conn = db.get_connection()  # memo_envによりこのスキーマへ固定済み
    try:
        with conn.cursor() as cur:
            other_id = uuid.uuid4()
            cur.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (other_id, "別世帯"))
        conn.commit()
    finally:
        conn.close()

    storage.add_date_with_memo("2026-09-01", "世帯Aのメモ", tenant_id=tenant_id)
    storage.add_date_with_memo("2026-09-01", "世帯Bのメモ", tenant_id=other_id)

    results_a = storage.search_records(tenant_id=tenant_id)
    results_b = storage.search_records(tenant_id=other_id)
    assert results_a == [{"date": "2026-09-01", "memo": "世帯Aのメモ"}]
    assert results_b == [{"date": "2026-09-01", "memo": "世帯Bのメモ"}]
