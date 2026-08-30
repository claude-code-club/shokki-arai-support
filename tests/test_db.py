import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))

import db  # noqa: E402
from logic import load_dates as json_load_dates  # noqa: E402
from logic import save_dates as json_save_dates  # noqa: E402
import scripts.migrate_to_postgres as migrate_module  # noqa: E402
import scripts.restore_json_from_postgres as restore_module  # noqa: E402


requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします",
)


# --- 純粋関数・DB未設定時の挙動のテスト(ダミーデータのみ、DB接続不要) ---


def test_compare_date_sets_match():
    result = db.compare_date_sets({"2026-08-01", "2026-08-02"}, {"2026-08-02", "2026-08-01"})
    assert result["match"] is True
    assert result["only_in_json"] == []
    assert result["only_in_db"] == []


def test_compare_date_sets_detects_missing_in_db():
    result = db.compare_date_sets({"2026-08-01", "2026-08-02"}, {"2026-08-01"})
    assert result["match"] is False
    assert result["only_in_json"] == ["2026-08-02"]
    assert result["only_in_db"] == []


def test_compare_date_sets_detects_extra_in_db():
    result = db.compare_date_sets({"2026-08-01"}, {"2026-08-01", "2026-08-09"})
    assert result["match"] is False
    assert result["only_in_json"] == []
    assert result["only_in_db"] == ["2026-08-09"]


def test_is_configured_false_without_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert db.is_configured() is False


def test_is_configured_true_with_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/dummy")
    assert db.is_configured() is True


def test_get_connection_raises_without_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(db.DatabaseNotConfiguredError):
        db.get_connection()


def test_migrate_raises_when_database_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    data_file = tmp_path / "records.json"
    json_save_dates({"2026-08-01"}, data_file=data_file)

    with pytest.raises(db.DatabaseNotConfiguredError):
        migrate_module.migrate(data_file=data_file)

    # DB未接続時、JSON側は一切変更されない
    assert json_load_dates(data_file=data_file) == {"2026-08-01"}


def test_restore_json_raises_when_database_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    data_file = tmp_path / "records.json"

    with pytest.raises(db.DatabaseNotConfiguredError):
        restore_module.restore_json_from_postgres(data_file=data_file)

    assert not data_file.exists()


def test_migrate_cli_reports_error_when_database_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    data_file = tmp_path / "records.json"
    json_save_dates({"2026-08-01"}, data_file=data_file)

    exit_code = migrate_module.main(["migrate_to_postgres.py", str(data_file)])

    assert exit_code == 1


def test_restore_json_cli_reports_error_when_database_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    data_file = tmp_path / "records.json"

    exit_code = restore_module.main(["restore_json_from_postgres.py", str(data_file)])

    assert exit_code == 1


# --- PostgreSQL連携テスト(DATABASE_URLが設定されている場合のみ実行。CIではpostgresサービスで実行される) ---


@requires_db
class TestWithRealDatabase:
    @pytest.fixture(autouse=True)
    def conn(self):
        connection = db.get_connection()
        db.ensure_schema(connection)
        with connection.cursor() as cur:
            cur.execute("DELETE FROM records")
        connection.commit()
        try:
            yield connection
        finally:
            with connection.cursor() as cur:
                cur.execute("DELETE FROM records")
            connection.commit()
            connection.close()

    def test_insert_dates_is_idempotent(self, conn):
        dates = {"2026-08-01", "2026-08-02"}
        db.insert_dates(dates, conn=conn)
        db.insert_dates(dates, conn=conn)  # 再実行しても重複しない(ON CONFLICT DO NOTHING)

        assert db.load_dates(conn) == dates

    def test_save_dates_syncs_additions_and_removals(self, conn):
        db.insert_dates({"2026-08-01", "2026-08-02"}, conn=conn)

        db.save_dates({"2026-08-02", "2026-08-03"}, conn=conn)

        assert db.load_dates(conn) == {"2026-08-02", "2026-08-03"}

    def test_save_dates_can_clear_all(self, conn):
        db.insert_dates({"2026-08-01"}, conn=conn)

        db.save_dates(set(), conn=conn)

        assert db.load_dates(conn) == set()

    def test_migrate_from_json_matches_and_is_idempotent(self, tmp_path, conn):
        data_file = tmp_path / "records.json"
        json_save_dates({"2026-08-01", "2026-08-02", "2026-08-03"}, data_file=data_file)

        result1 = migrate_module.migrate(data_file=data_file, conn=conn)
        assert result1["match"] is True

        result2 = migrate_module.migrate(data_file=data_file, conn=conn)  # 再実行
        assert result2["match"] is True
        assert db.load_dates(conn) == {"2026-08-01", "2026-08-02", "2026-08-03"}

    def test_migrate_detects_mismatch_and_raises(self, tmp_path, conn):
        data_file = tmp_path / "records.json"
        json_save_dates({"2026-08-01"}, data_file=data_file)
        db.insert_dates({"2026-08-09"}, conn=conn)  # JSONには無い日付をDB側に先に混入させておく

        with pytest.raises(migrate_module.MigrationVerificationError):
            migrate_module.migrate(data_file=data_file, conn=conn)

    def test_restore_json_from_postgres_writes_current_db_state(self, tmp_path, conn):
        db.insert_dates({"2026-08-05", "2026-08-06"}, conn=conn)
        data_file = tmp_path / "records.json"

        dates = restore_module.restore_json_from_postgres(data_file=data_file, conn=conn)

        assert dates == {"2026-08-05", "2026-08-06"}
        assert json_load_dates(data_file=data_file) == {"2026-08-05", "2026-08-06"}

    def test_restore_json_from_postgres_backs_up_existing_json(self, tmp_path, conn):
        data_file = tmp_path / "records.json"
        json_save_dates({"2026-07-01"}, data_file=data_file)
        db.insert_dates({"2026-08-05"}, conn=conn)

        restore_module.restore_json_from_postgres(data_file=data_file, conn=conn)

        from logic import list_backups
        backups = list_backups(data_file=data_file)
        assert len(backups) >= 1
