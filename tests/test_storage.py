import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))

import db  # noqa: E402
import logic  # noqa: E402
import psycopg  # noqa: E402
import storage  # noqa: E402
from logic import load_dates as json_load_dates  # noqa: E402
from logic import save_dates as json_save_dates  # noqa: E402
import scripts.migrate_to_postgres as migrate_module  # noqa: E402
import scripts.migrate_to_tenant_schema as migrate_tenant_module  # noqa: E402


requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします",
)


@pytest.fixture(autouse=True)
def _isolate_json_data_file(monkeypatch, tmp_path):
    """storage.pyのjson分岐が本番のrecords.jsonへ触れないよう、常にtmp_pathへ差し替える。"""
    monkeypatch.setattr(logic, "DATA_FILE", tmp_path / "records.json")


@pytest.fixture
def tenant_env(monkeypatch):
    """storage.py経由のpostgres系テストを、稼働中のpublic.recordsとは完全に隔離した
    専用スキーマ(search_pathをpublicから切り離す)で実行する。

    db.get_connection()をスパイし、storage.pyが自前で開くすべての接続にも
    このスキーマのsearch_pathを設定してから返すことで、public.recordsには
    一切触れずにテナント対応スキーマでのstorage.py挙動を検証できる。
    STORAGE_BACKEND=postgres・DEFAULT_TENANT_ID(生成した世帯Aのtenant_id)を
    設定し、yield値として世帯Aのtenant_idを返す。
    """
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    # テスト本体がDATABASE_URLをmonkeypatchで書き換えるケース(接続失敗の疑似)があるため、
    # 後始末は「今」の環境変数ではなく、フィクスチャ開始時点の正しい接続先を使う。
    good_database_url = os.environ["DATABASE_URL"]

    def _connect_with_good_url():
        return psycopg.connect(good_database_url)

    schema_name = f"test_storage_{uuid.uuid4().hex}"

    setup_conn = _connect_with_good_url()
    with setup_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    db.ensure_schema(setup_conn)
    tenant_id = uuid.uuid4()
    migrate_tenant_module.migrate_to_tenant_schema(tenant_id, conn=setup_conn)
    setup_conn.close()

    real_get_connection = db.get_connection  # テスト中の実際の接続はこちら経由(監視対象)

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


@pytest.fixture
def legacy_schema_conn():
    """tenant_id列を持たない旧スキーマ(scripts/migrate_to_postgres.py用)を、
    public.recordsとは隔離した専用スキーマに用意する。
    """
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    schema_name = f"test_legacy_{uuid.uuid4().hex}"
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    db.ensure_schema(conn)
    conn.commit()

    try:
        yield conn
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


# --- 1〜5: STORAGE_BACKENDの判定(DB接続不要) ---


def test_get_backend_name_defaults_to_json_when_unset(monkeypatch):
    monkeypatch.delenv(storage.STORAGE_BACKEND_ENV, raising=False)
    assert storage.get_backend_name() == "json"


def test_get_backend_name_json_explicit(monkeypatch):
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "json")
    assert storage.get_backend_name() == "json"


def test_get_backend_name_postgres_explicit_with_database_url(monkeypatch):
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/dummy")
    assert storage.get_backend_name() == "postgres"


def test_invalid_backend_value_raises_and_does_not_fall_back_to_json(monkeypatch):
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgre")  # 誤字を想定

    def fail_if_called(*args, **kwargs):
        raise AssertionError("logic.load_dates()が呼ばれた(jsonへフォールバックしている)")

    monkeypatch.setattr(logic, "load_dates", fail_if_called)

    with pytest.raises(storage.StorageConfigError):
        storage.get_backend_name()
    with pytest.raises(storage.StorageConfigError):
        storage.load_dates()


def test_postgres_backend_without_database_url_raises_storage_config_error(monkeypatch):
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(storage.StorageConfigError):
        storage.get_backend_name()


# --- 6〜9: get_tenant_id()の判定(DB接続不要。DATABASE_URLはis_configured()の
#     env変数チェックのみに使われ、実接続は発生しない) ---


def test_get_tenant_id_returns_none_for_json_backend(monkeypatch):
    monkeypatch.delenv(storage.STORAGE_BACKEND_ENV, raising=False)
    assert storage.get_tenant_id() is None


def test_get_tenant_id_postgres_without_default_tenant_id_raises(monkeypatch):
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/dummy")
    monkeypatch.delenv(storage.DEFAULT_TENANT_ID_ENV, raising=False)

    with pytest.raises(storage.StorageConfigError):
        storage.get_tenant_id()


def test_get_tenant_id_postgres_with_invalid_uuid_raises(monkeypatch):
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/dummy")
    monkeypatch.setenv(storage.DEFAULT_TENANT_ID_ENV, "not-a-uuid")

    with pytest.raises(storage.StorageConfigError):
        storage.get_tenant_id()


def test_get_tenant_id_postgres_with_valid_uuid_returns_it(monkeypatch):
    tenant_id = uuid.uuid4()
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/dummy")
    monkeypatch.setenv(storage.DEFAULT_TENANT_ID_ENV, str(tenant_id))

    assert storage.get_tenant_id() == tenant_id


# --- 10〜16: storage.py経由のPostgreSQL連携(DB接続必要、隔離スキーマで実行) ---


@requires_db
def test_storage_load_add_cancel_round_trip(tenant_env):
    tenant_id = tenant_env

    assert storage.load_dates(tenant_id=tenant_id) == set()

    storage.add_date("2026-08-01", tenant_id=tenant_id)
    storage.add_date("2026-08-02", tenant_id=tenant_id)
    assert storage.load_dates(tenant_id=tenant_id) == {"2026-08-01", "2026-08-02"}

    storage.cancel_date("2026-08-01", tenant_id=tenant_id)
    assert storage.load_dates(tenant_id=tenant_id) == {"2026-08-02"}


@requires_db
def test_storage_add_date_commits_on_success(tenant_env):
    """storage.add_date()が正常終了した場合、確かにcommitされている(別コネクションから見える)。"""
    tenant_id = tenant_env

    storage.add_date("2026-08-03", tenant_id=tenant_id)

    conn = db.get_connection()  # tenant_envによりこの接続も同じ隔離スキーマへ向く
    try:
        assert db.load_dates_for_tenant(conn, tenant_id=tenant_id) == {"2026-08-03"}
    finally:
        conn.close()


@requires_db
def test_storage_add_date_rolls_back_on_error(monkeypatch, tenant_env):
    """storage.add_date()の途中でエラーが起きた場合、rollbackされ、状態が変わらない。"""
    tenant_id = tenant_env
    storage.add_date("2026-08-01", tenant_id=tenant_id)  # 既存の記録を1件作っておく

    def failing_insert(record_date, conn, *, tenant_id):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO records (tenant_id, record_date) VALUES (%s, %s) "
                "ON CONFLICT (tenant_id, record_date) DO NOTHING",
                (tenant_id, "2099-01-01"),
            )
        raise psycopg.errors.OperationalError("疑似的な書き込み失敗")

    monkeypatch.setattr(db, "insert_date_for_tenant", failing_insert)

    with pytest.raises(storage.StorageUnavailableError):
        storage.add_date("2099-01-01", tenant_id=tenant_id)

    # rollbackされているので'2099-01-01'は残らず、既存の'2026-08-01'だけが保持される
    assert storage.load_dates(tenant_id=tenant_id) == {"2026-08-01"}


@requires_db
def test_storage_unavailable_does_not_touch_json(tenant_env, monkeypatch):
    """項目: PostgreSQL接続自体が失敗する場合StorageUnavailableErrorを送出し、JSON側は一切書き込まれない。"""
    tenant_id = tenant_env
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://baduser:badpass@localhost:1/nonexistent?connect_timeout=2",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("logic.save_dates()が呼ばれた(jsonへフォールバックしている)")

    monkeypatch.setattr(logic, "save_dates", fail_if_called)

    with pytest.raises(storage.StorageUnavailableError):
        storage.add_date("2026-08-01", tenant_id=tenant_id)

    assert not logic.DATA_FILE.exists()


@requires_db
def test_storage_cross_tenant_isolation(tenant_env):
    """世帯A・Bによる越境防止テスト(storage.py層): Aの操作がBへ一切影響しない。"""
    tenant_a = tenant_env

    # 同じ隔離スキーマ内に世帯Bを追加で作成する(db.get_connection()はtenant_envで
    # 既にこのスキーマへ向くようパッチ済み)
    tenant_b = uuid.uuid4()
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant_b, "世帯B"))
        conn.commit()
    finally:
        conn.close()

    storage.add_date("2026-08-01", tenant_id=tenant_a)
    storage.add_date("2026-08-01", tenant_id=tenant_b)  # 同じ日付をBにも追加(衝突しない)
    storage.add_date("2026-08-09", tenant_id=tenant_b)

    assert storage.load_dates(tenant_id=tenant_a) == {"2026-08-01"}
    assert storage.load_dates(tenant_id=tenant_b) == {"2026-08-01", "2026-08-09"}

    # Aの取り消し操作がBの同じ日付を消さない
    storage.cancel_date("2026-08-01", tenant_id=tenant_a)
    assert storage.load_dates(tenant_id=tenant_a) == set()
    assert storage.load_dates(tenant_id=tenant_b) == {"2026-08-01", "2026-08-09"}

    # Aのsave_dates(全体置き換え)がBへ影響しない
    storage.save_dates({"2026-08-20"}, tenant_id=tenant_a)
    assert storage.load_dates(tenant_id=tenant_a) == {"2026-08-20"}
    assert storage.load_dates(tenant_id=tenant_b) == {"2026-08-01", "2026-08-09"}


@requires_db
def test_storage_postgres_backend_requires_tenant_id(tenant_env):
    """postgresバックエンドでtenant_id未指定(None)の操作はStorageConfigErrorになる。"""
    with pytest.raises(storage.StorageConfigError):
        storage.load_dates(tenant_id=None)
    with pytest.raises(storage.StorageConfigError):
        storage.add_date("2026-08-01", tenant_id=None)
    with pytest.raises(storage.StorageConfigError):
        storage.cancel_date("2026-08-01", tenant_id=None)
    with pytest.raises(storage.StorageConfigError):
        storage.save_dates({"2026-08-01"}, tenant_id=None)


@requires_db
def test_storage_entry_points_close_self_owned_connections(monkeypatch, tenant_env):
    """storage.load_dates/add_date/cancel_date/save_dates、いずれの経路でも接続が確実にcloseされる。"""
    tenant_id = tenant_env
    captured = []
    real_get_connection = db.get_connection  # tenant_env適用済み(隔離スキーマへ向く)

    def spy_get_connection():
        conn = real_get_connection()
        captured.append(conn)
        return conn

    monkeypatch.setattr(db, "get_connection", spy_get_connection)

    storage.add_date("2026-08-01", tenant_id=tenant_id)
    assert captured[-1].closed

    storage.load_dates(tenant_id=tenant_id)
    assert captured[-1].closed

    storage.cancel_date("2026-08-01", tenant_id=tenant_id)
    assert captured[-1].closed

    storage.save_dates({"2026-08-05"}, tenant_id=tenant_id)
    assert captured[-1].closed

    # 異常パスでも確実にcloseされる
    real_insert = db.insert_date_for_tenant

    def failing_insert(record_date, conn, *, tenant_id):
        raise psycopg.errors.OperationalError("疑似的な失敗")

    monkeypatch.setattr(db, "insert_date_for_tenant", failing_insert)
    with pytest.raises(storage.StorageUnavailableError):
        storage.add_date("2026-08-02", tenant_id=tenant_id)
    assert captured[-1].closed
    monkeypatch.setattr(db, "insert_date_for_tenant", real_insert)


# --- 17〜18: scripts/migrate_to_postgres.py(旧スキーマ専用、隔離スキーマで実行) ---


@requires_db
def test_migrate_commits_only_on_full_match(tmp_path, legacy_schema_conn):
    conn = legacy_schema_conn
    data_file = tmp_path / "records.json"
    json_save_dates({"2026-08-01", "2026-08-02"}, data_file=data_file)

    result = migrate_module.migrate(data_file=data_file, conn=conn)
    assert result["match"] is True

    assert db.load_dates(conn) == {"2026-08-01", "2026-08-02"}


@requires_db
def test_migrate_rolls_back_insert_on_mismatch(tmp_path, legacy_schema_conn):
    conn = legacy_schema_conn
    data_file = tmp_path / "records.json"
    json_save_dates({"2026-08-01"}, data_file=data_file)
    db.insert_dates({"2026-08-09"}, conn=conn)  # JSONには無い日付をDB側に先に混入させておく
    conn.commit()

    with pytest.raises(migrate_module.MigrationVerificationError):
        migrate_module.migrate(data_file=data_file, conn=conn)

    assert db.load_dates(conn) == {"2026-08-09"}


# --- 19: production想定(STORAGE_BACKEND未設定)の回帰確認 ---


def test_unset_backend_matches_existing_logic_behavior():
    """項目: STORAGE_BACKEND未設定(production想定)時、既存logic.pyの挙動と完全に一致する(回帰確認)。

    logic.DATA_FILEは_isolate_json_data_fileフィクスチャによりtmp_pathへ差し替え済み。
    """
    assert storage.load_dates() == set()

    storage.add_date("2026-08-01")
    storage.add_date("2026-08-02")

    assert storage.load_dates() == json_load_dates(data_file=logic.DATA_FILE)
    assert json_load_dates(data_file=logic.DATA_FILE) == {"2026-08-01", "2026-08-02"}
