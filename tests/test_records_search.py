import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))

import db  # noqa: E402
import psycopg  # noqa: E402
import storage  # noqa: E402
import scripts.migrate_to_records_memo_schema as migrate_memo_module  # noqa: E402
import scripts.migrate_to_tenant_schema as migrate_tenant_module  # noqa: E402

requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします",
)


@pytest.fixture
def memo_schema():
    """稼働中のpublic.recordsとは隔離した専用スキーマに、tenants/records(第16回)＋
    records.memo(第22回)を用意する。db.py関数を直接検証する用途。
    """
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    schema_name = f"test_memo_{uuid.uuid4().hex}"
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    db.ensure_schema(conn)
    tenant_id = uuid.uuid4()
    migrate_tenant_module.migrate_to_tenant_schema(tenant_id, conn=conn)
    migrate_memo_module.migrate_to_records_memo_schema(conn=conn)
    conn.commit()

    try:
        yield conn, tenant_id
    finally:
        conn.rollback()
        conn.close()
        cleanup_conn = db.get_connection()
        try:
            with cleanup_conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()


@pytest.fixture
def memo_env(monkeypatch):
    """storage.py経由のadd_date_with_memo()/search_records()を、稼働中のpublic.records
    とは隔離した専用スキーマ(records.memo込み)で検証する。tests/test_storage.pyの
    tenant_envと同じ方式(db.get_connection()をスパイしてsearch_pathを固定する)。
    """
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    good_database_url = os.environ["DATABASE_URL"]

    def _connect_with_good_url():
        return psycopg.connect(good_database_url)

    schema_name = f"test_memo_env_{uuid.uuid4().hex}"

    setup_conn = _connect_with_good_url()
    with setup_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    db.ensure_schema(setup_conn)
    tenant_id = uuid.uuid4()
    migrate_tenant_module.migrate_to_tenant_schema(tenant_id, conn=setup_conn)
    migrate_memo_module.migrate_to_records_memo_schema(conn=setup_conn)
    setup_conn.close()

    real_get_connection = db.get_connection

    def patched_get_connection():
        conn = real_get_connection()
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema_name}")
        return conn

    monkeypatch.setattr(db, "get_connection", patched_get_connection)
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")
    monkeypatch.setenv(storage.DEFAULT_TENANT_ID_ENV, str(tenant_id))

    try:
        yield tenant_id
    finally:
        cleanup_conn = _connect_with_good_url()
        try:
            with cleanup_conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()


def _second_tenant(conn):
    other_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (other_id, "別世帯"))
    return other_id


# --- migrate_to_records_memo_schema(): 冪等性 ---


@requires_db
def test_migrate_to_records_memo_schema_is_idempotent(memo_schema):
    conn, _ = memo_schema
    migrate_memo_module.migrate_to_records_memo_schema(conn=conn)  # 2回目も安全
    with conn.cursor() as cur:
        cur.execute("SELECT memo FROM records LIMIT 0")  # 列が存在すればエラーにならない


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
