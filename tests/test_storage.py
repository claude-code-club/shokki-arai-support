import sys
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


requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします",
)


@pytest.fixture(autouse=True)
def _isolate_json_data_file(monkeypatch, tmp_path):
    """storage.pyのjson分岐が本番のrecords.jsonへ触れないよう、常にtmp_pathへ差し替える。"""
    monkeypatch.setattr(logic, "DATA_FILE", tmp_path / "records.json")


@pytest.fixture(autouse=True)
def _clean_records_table():
    """DATABASE_URLが設定されている場合のみ、各テストの前後でrecordsテーブルを空にする。"""
    if not db.is_configured():
        yield
        return
    conn = db.get_connection()
    db.ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM records")
    conn.commit()
    conn.close()
    yield
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM records")
    conn.commit()
    conn.close()


# --- 1〜4: STORAGE_BACKENDの判定(DB接続不要) ---


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


# --- 5〜12: PostgreSQL連携(DATABASE_URL設定時のみ実行) ---


@requires_db
def test_db_functions_do_not_commit_by_themselves():
    """項目5: db.py単体では勝手にcommitしない。"""
    conn1 = db.get_connection()
    try:
        db.insert_dates({"2026-08-01"}, conn=conn1)  # commitしない

        conn2 = db.get_connection()
        try:
            assert db.load_dates(conn2) == set()  # 別コネクションからはまだ見えない
        finally:
            conn2.close()
    finally:
        conn1.rollback()
        conn1.close()


@requires_db
def test_storage_load_dates_and_save_dates_use_postgres(monkeypatch):
    """項目6: STORAGE_BACKEND=postgresでstorage経由の読み書きがPostgreSQLの内容と一致する。"""
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")

    storage.save_dates({"2026-08-01", "2026-08-02"})

    assert storage.load_dates() == {"2026-08-01", "2026-08-02"}


@requires_db
def test_storage_save_dates_commits_on_success(monkeypatch):
    """項目7: storage.save_dates()が正常終了した場合、確かにcommitされている(別コネクションから見える)。"""
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")

    storage.save_dates({"2026-08-03"})

    conn = db.get_connection()
    try:
        assert db.load_dates(conn) == {"2026-08-03"}
    finally:
        conn.close()


@requires_db
def test_storage_save_dates_rolls_back_on_error(monkeypatch):
    """項目8: storage.save_dates()の途中でエラーが起きた場合、rollbackされ、状態が変わらない。"""
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")
    storage.save_dates({"2026-08-01"})  # 既存の記録を1件作っておく

    def failing_save_dates(dates, conn):
        # 実際に書き込みを行った直後に失敗させ、中途半端な状態を模擬する
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO records (record_date) VALUES ('2099-01-01') ON CONFLICT DO NOTHING"
            )
        raise psycopg.errors.OperationalError("疑似的な書き込み失敗")

    monkeypatch.setattr(db, "save_dates", failing_save_dates)

    with pytest.raises(storage.StorageUnavailableError):
        storage.save_dates({"2026-08-01", "2099-01-01"})

    # rollbackされているので'2099-01-01'は残らず、既存の'2026-08-01'だけが保持される
    conn = db.get_connection()
    try:
        assert db.load_dates(conn) == {"2026-08-01"}
    finally:
        conn.close()


@requires_db
def test_storage_save_dates_unavailable_does_not_touch_json(monkeypatch):
    """項目9: PostgreSQL接続自体が失敗する場合StorageUnavailableErrorを送出し、JSON側は一切書き込まれない。"""
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://baduser:badpass@localhost:1/nonexistent?connect_timeout=2",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("logic.save_dates()が呼ばれた(jsonへフォールバックしている)")

    monkeypatch.setattr(logic, "save_dates", fail_if_called)

    with pytest.raises(storage.StorageUnavailableError):
        storage.save_dates({"2026-08-01"})

    assert not logic.DATA_FILE.exists()


@requires_db
def test_migrate_commits_only_on_full_match(tmp_path):
    """項目10: migrate()は日付集合が完全一致する場合のみcommitし、実際にDBへ反映される。"""
    data_file = tmp_path / "records.json"
    json_save_dates({"2026-08-01", "2026-08-02"}, data_file=data_file)

    result = migrate_module.migrate(data_file=data_file)
    assert result["match"] is True

    conn = db.get_connection()
    try:
        assert db.load_dates(conn) == {"2026-08-01", "2026-08-02"}
    finally:
        conn.close()


@requires_db
def test_migrate_rolls_back_insert_on_mismatch(tmp_path):
    """項目11(今回の修正の核心): 照合不一致時、移行中のDB書き込みはrollbackされる。"""
    data_file = tmp_path / "records.json"
    json_save_dates({"2026-08-01"}, data_file=data_file)

    # DB側に、JSONには無い日付を先にcommit済みで混入させておく
    conn = db.get_connection()
    try:
        db.ensure_schema(conn)
        db.insert_dates({"2026-08-09"}, conn=conn)
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(migrate_module.MigrationVerificationError):
        migrate_module.migrate(data_file=data_file)

    # migrate()が試みた"2026-08-01"の書き込みはrollbackされ、
    # 事前にcommit済みだった"2026-08-09"だけが残っている
    conn = db.get_connection()
    try:
        assert db.load_dates(conn) == {"2026-08-09"}
    finally:
        conn.close()


@requires_db
def test_all_entry_points_close_self_owned_connections(monkeypatch, tmp_path):
    """項目12: storage / migrate_to_postgres、いずれの経路でも接続が確実にcloseされる。

    restore_json_from_postgres.pyは第16回(マルチテナント設計)でtenant_idが必須に
    なり、tenant_id列を持つ移行後スキーマ前提の関数になったため、ここでは対象外。
    その接続close確認はtests/test_tenant_migration.py側で行う。
    """
    captured = []
    real_get_connection = db.get_connection

    def spy_get_connection():
        conn = real_get_connection()
        captured.append(conn)
        return conn

    monkeypatch.setattr(db, "get_connection", spy_get_connection)

    # storage.py経由(成功パス)
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")
    storage.save_dates({"2026-08-01"})
    assert captured[-1].closed

    # storage.py経由(異常パス)
    real_db_save_dates = db.save_dates

    def failing_save_dates(dates, conn):
        raise psycopg.errors.OperationalError("疑似的な失敗")

    monkeypatch.setattr(db, "save_dates", failing_save_dates)
    with pytest.raises(storage.StorageUnavailableError):
        storage.save_dates({"2026-08-02"})
    assert captured[-1].closed
    monkeypatch.setattr(db, "save_dates", real_db_save_dates)  # 本物のdb.save_datesへ戻す

    # migrate_to_postgres.py(自前でconnを開くケース)
    data_file = tmp_path / "records_for_migrate.json"
    json_save_dates({"2026-08-01"}, data_file=data_file)
    migrate_module.migrate(data_file=data_file)
    assert captured[-1].closed


# --- 13: production想定(STORAGE_BACKEND未設定)の回帰確認 ---


def test_unset_backend_matches_existing_logic_behavior():
    """項目13: STORAGE_BACKEND未設定(production想定)時、既存logic.pyの挙動と完全に一致する(回帰確認)。

    logic.DATA_FILEは_isolate_json_data_fileフィクスチャによりtmp_pathへ差し替え済み。
    """
    assert storage.load_dates() == set()

    storage.save_dates({"2026-08-01", "2026-08-02"})

    assert storage.load_dates() == json_load_dates(data_file=logic.DATA_FILE)
    assert json_load_dates(data_file=logic.DATA_FILE) == {"2026-08-01", "2026-08-02"}
