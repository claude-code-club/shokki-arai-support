import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))

import db  # noqa: E402
import psycopg  # noqa: E402
import scripts.migrate_to_tenant_schema as migrate_tenant_module  # noqa: E402
import scripts.restore_json_from_postgres as restore_module  # noqa: E402

MigrationVerificationError = migrate_tenant_module.MigrationVerificationError

requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします",
)


@pytest.fixture
def tenant_schema():
    """既存のpublic.records(test_db.py/test_storage.pyが使う本来のテーブル)とは
    完全に隔離された、テスト専用スキーマ内にrecordsテーブルを用意する。

    search_pathをテスト専用スキーマだけに絞る(publicを含めない)ため、
    migrate_to_tenant_schema()・restore_json_from_postgres()が実行する
    無修飾のSQL(records/tenants)は、このスキーマ内だけに作用する。
    第16回のスキーマ移行が、稼働中のpublic.recordsへ一切影響しないことの前提。
    """
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    schema_name = f"test_tenant_{uuid.uuid4().hex}"
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    db.ensure_schema(conn)  # このスキーマ内にrecordsテーブルを作成(publicには一切触れない)
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


def _insert_legacy_date(conn, record_date):
    """tenant_id列がまだ無い(または未設定の)前提で、記録日を1件追加する(検証用)。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO records (record_date) VALUES (%s) ON CONFLICT (record_date) DO NOTHING",
            (record_date,),
        )


# --- tenant_idの型チェック(DB接続不要) ---


def test_migrate_requires_uuid_instance():
    with pytest.raises(TypeError):
        migrate_tenant_module.migrate_to_tenant_schema("not-a-uuid-instance")


def test_restore_requires_uuid_or_none(tmp_path):
    with pytest.raises(TypeError):
        restore_module.restore_json_from_postgres(
            "not-a-uuid-instance", data_file=tmp_path / "records.json"
        )


def test_restore_accepts_none_tenant_id_type_wise(tmp_path, monkeypatch):
    """tenant_id=None(省略、旧スキーマでの正当な指定)はTypeErrorにならない。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(db.DatabaseNotConfiguredError):
        restore_module.restore_json_from_postgres(None, data_file=tmp_path / "records.json")


# --- migrate_to_tenant_schema() の移行・検証・冪等性 ---


@requires_db
def test_migrate_backfills_existing_rows_and_commits_on_match(tenant_schema):
    conn = tenant_schema
    _insert_legacy_date(conn, "2026-08-01")
    _insert_legacy_date(conn, "2026-08-02")
    conn.commit()

    tenant_id = uuid.uuid4()
    result = migrate_tenant_module.migrate_to_tenant_schema(tenant_id, conn=conn)

    assert result["match"] is True
    assert result["count"] == 2

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM records WHERE tenant_id IS NULL")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT record_date FROM records WHERE tenant_id = %s", (tenant_id,))
        assert {row[0].isoformat() for row in cur.fetchall()} == {"2026-08-01", "2026-08-02"}


@requires_db
def test_migrate_is_idempotent(tenant_schema):
    """同じtenant_idで2回実行しても、エラーにならず結果が変わらない(冪等性)。"""
    conn = tenant_schema
    _insert_legacy_date(conn, "2026-08-01")
    conn.commit()

    tenant_id = uuid.uuid4()
    first = migrate_tenant_module.migrate_to_tenant_schema(tenant_id, conn=conn)
    second = migrate_tenant_module.migrate_to_tenant_schema(tenant_id, conn=conn)

    assert first == second

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM tenants WHERE id = %s", (tenant_id,))
        assert cur.fetchone()[0] == 1  # tenantsに重複行が増えていない
        cur.execute("SELECT COUNT(*) FROM records")
        assert cur.fetchone()[0] == 1  # recordsも重複していない


@requires_db
def test_migrate_enforces_not_null_after_success(tenant_schema):
    """移行成功後は、tenant_idを指定しないINSERTが失敗する(NOT NULL制約の直接確認)。"""
    conn = tenant_schema
    _insert_legacy_date(conn, "2026-08-01")
    conn.commit()

    migrate_tenant_module.migrate_to_tenant_schema(uuid.uuid4(), conn=conn)

    with pytest.raises(psycopg.errors.NotNullViolation):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO records (record_date) VALUES ('2026-09-01')")
    conn.rollback()


@requires_db
def test_migrate_allows_same_date_for_different_tenants(tenant_schema):
    """複合UNIQUE(tenant_id, record_date)により、異なるテナントは同じ日付を共存できる。"""
    conn = tenant_schema
    _insert_legacy_date(conn, "2026-08-01")
    conn.commit()
    tenant_a = uuid.uuid4()
    migrate_tenant_module.migrate_to_tenant_schema(tenant_a, conn=conn)

    tenant_b = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant_b, "世帯B")
        )
        cur.execute(
            "INSERT INTO records (tenant_id, record_date) VALUES (%s, '2026-08-01')",
            (tenant_b,),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT tenant_id FROM records WHERE record_date = '2026-08-01'")
        tenants_for_date = {row[0] for row in cur.fetchall()}
    assert tenants_for_date == {tenant_a, tenant_b}


@requires_db
def test_migrate_rejects_unknown_tenant_id_via_foreign_key(tenant_schema):
    """外部キー制約により、tenantsに存在しないtenant_idでのINSERTは失敗し、孤立行が残らない。"""
    conn = tenant_schema
    _insert_legacy_date(conn, "2026-08-01")
    conn.commit()
    migrate_tenant_module.migrate_to_tenant_schema(uuid.uuid4(), conn=conn)

    unknown_tenant = uuid.uuid4()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO records (tenant_id, record_date) VALUES (%s, '2026-09-01')",
                (unknown_tenant,),
            )
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM records WHERE tenant_id = %s", (unknown_tenant,))
        assert cur.fetchone()[0] == 0


@requires_db
def test_migrate_rolls_back_everything_on_mismatch(tenant_schema):
    """検証不一致(今回の修正の核心): 移行によるスキーマ変更・データ変更を全てrollbackする。

    「他のテナントに既に割り当て済みの行」と「未移行(NULLのまま)の行」が混在する
    状態(過去に別のtenant_idで部分的に移行を試みた形跡、を想定した現実的なシナリオ)で、
    新しいtenant_idを対象に実行すると、backfill対象は未移行の行だけになり、
    移行前後の日付集合が一致しなくなる。この場合、tenant_id列の変更・NOT NULL化・
    インデックス変更を含め、この呼び出しで行った変更を全てrollbackする。
    """
    conn = tenant_schema
    other_tenant = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id UUID PRIMARY KEY, name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS tenant_id UUID")
        cur.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (other_tenant, "別世帯"))
        cur.execute(
            "INSERT INTO records (tenant_id, record_date) VALUES (%s, '2026-08-01')",
            (other_tenant,),
        )
        cur.execute("INSERT INTO records (record_date) VALUES ('2026-08-02')")  # tenant_id NULLのまま
    conn.commit()

    new_tenant = uuid.uuid4()
    with pytest.raises(MigrationVerificationError):
        migrate_tenant_module.migrate_to_tenant_schema(new_tenant, conn=conn)

    # rollbackされているため、tenant_id列はNOT NULLになっておらず、
    # 未移行だった行はNULLのまま、other_tenantの行も変化していない
    # (information_schemaはスキーマ横断で同名テーブルを返しうるため、current_schema()で絞り込む)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'records' "
            "AND column_name = 'tenant_id'"
        )
        assert cur.fetchone()[0] == "YES"
        cur.execute("SELECT tenant_id FROM records WHERE record_date = '2026-08-02'")
        assert cur.fetchone()[0] is None
        cur.execute("SELECT tenant_id FROM records WHERE record_date = '2026-08-01'")
        assert cur.fetchone()[0] == other_tenant
        cur.execute("SELECT COUNT(*) FROM tenants WHERE id = %s", (new_tenant,))
        assert cur.fetchone()[0] == 0  # 新しいtenantsの挿入も取り消されている


def test_migrate_closes_self_owned_connection_on_failure(monkeypatch):
    """conn省略時(自前で接続を開く場合)、DB未設定などの例外時も接続を確実にcloseする。

    db.get_connection()はis_configured()ではなく環境変数を直接読むため、
    DATABASE_URLそのものを削除して未設定状態を再現する(DB接続不要)。
    """
    captured = []
    real_get_connection = db.get_connection

    def spy_get_connection():
        conn = real_get_connection()
        captured.append(conn)
        return conn

    monkeypatch.setattr(db, "get_connection", spy_get_connection)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(db.DatabaseNotConfiguredError):
        migrate_tenant_module.migrate_to_tenant_schema(uuid.uuid4())
    # DatabaseNotConfiguredErrorはget_connection内部で送出されるため、captured自体は空でよい
    assert captured == []


@requires_db
def test_migrate_closes_self_owned_connection_on_success(monkeypatch, tenant_schema):
    """conn省略時(自前で接続を開く場合)、成功時も接続を確実にcloseする。

    db.get_connection()をスパイし、新規接続にもテスト専用スキーマのsearch_pathを
    設定してから返すことで、publicスキーマへは一切触れずに自前接続の経路を検証する。
    """
    conn = tenant_schema
    with conn.cursor() as cur:
        cur.execute("SHOW search_path")
        schema_name = cur.fetchone()[0]
    _insert_legacy_date(conn, "2026-08-01")
    conn.commit()

    captured = []
    real_get_connection = db.get_connection

    def spy_get_connection():
        new_conn = real_get_connection()
        with new_conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema_name}")
        captured.append(new_conn)
        return new_conn

    monkeypatch.setattr(db, "get_connection", spy_get_connection)

    result = migrate_tenant_module.migrate_to_tenant_schema(uuid.uuid4())

    assert result["match"] is True
    assert captured[-1].closed


# --- restore_json_from_postgres() の新旧スキーマ互換性 ---


@requires_db
def test_restore_old_schema_without_tenant_id_restores_all_records(tmp_path, tenant_schema):
    """旧スキーマ(tenant_id列なし)＋tenant_id未指定：従来どおり全件をJSONへ復元できる。"""
    conn = tenant_schema
    _insert_legacy_date(conn, "2026-08-01")
    _insert_legacy_date(conn, "2026-08-02")
    conn.commit()

    data_file = tmp_path / "restored.json"
    dates = restore_module.restore_json_from_postgres(None, data_file=data_file, conn=conn)

    assert dates == {"2026-08-01", "2026-08-02"}


@requires_db
def test_restore_old_schema_with_tenant_id_raises_schema_mismatch(tmp_path, tenant_schema):
    """旧スキーマ(tenant_id列なし)＋tenant_id指定：まだテナントという概念がないためエラー停止する。"""
    conn = tenant_schema
    _insert_legacy_date(conn, "2026-08-01")
    conn.commit()

    data_file = tmp_path / "restored.json"
    with pytest.raises(restore_module.SchemaMismatchError):
        restore_module.restore_json_from_postgres(uuid.uuid4(), data_file=data_file, conn=conn)

    assert not data_file.exists()  # 失敗時はJSONへ一切書き込まない


@requires_db
def test_restore_new_schema_without_tenant_id_raises_schema_mismatch(tmp_path, tenant_schema):
    """新スキーマ(tenant_id列あり)＋tenant_id未指定：全テナントを暗黙に混ぜないためエラー停止する。"""
    conn = tenant_schema
    _insert_legacy_date(conn, "2026-08-01")
    conn.commit()
    migrate_tenant_module.migrate_to_tenant_schema(uuid.uuid4(), conn=conn)

    data_file = tmp_path / "restored.json"
    with pytest.raises(restore_module.SchemaMismatchError):
        restore_module.restore_json_from_postgres(None, data_file=data_file, conn=conn)

    assert not data_file.exists()


@requires_db
def test_restore_schema_mismatch_does_not_touch_existing_json_or_other_tenants(
    tmp_path, tenant_schema
):
    """失敗経路(スキーマ不一致)では、既存JSONも他テナントのDB内容も一切変更しない。"""
    conn = tenant_schema
    _insert_legacy_date(conn, "2026-08-01")
    conn.commit()
    tenant_a = uuid.uuid4()
    migrate_tenant_module.migrate_to_tenant_schema(tenant_a, conn=conn)

    from logic import save_dates as json_save_dates

    data_file = tmp_path / "records.json"
    json_save_dates({"2026-07-01"}, data_file=data_file)  # 既存JSON(復元対象と無関係な内容)
    existing_bytes = data_file.read_bytes()

    with pytest.raises(restore_module.SchemaMismatchError):
        # 新スキーマなのにtenant_id未指定 -> エラー停止するはず
        restore_module.restore_json_from_postgres(None, data_file=data_file, conn=conn)

    assert data_file.read_bytes() == existing_bytes  # 既存JSONは1バイトも変わっていない
    with conn.cursor() as cur:
        cur.execute("SELECT record_date FROM records WHERE tenant_id = %s", (tenant_a,))
        assert {row[0].isoformat() for row in cur.fetchall()} == {"2026-08-01"}  # 変化なし


@requires_db
def test_restore_closes_self_owned_connection_on_schema_mismatch(monkeypatch, tmp_path, tenant_schema):
    """失敗経路(スキーマ不一致)でも、自前で開いた接続は確実にcloseする。"""
    conn = tenant_schema
    with conn.cursor() as cur:
        cur.execute("SHOW search_path")
        schema_name = cur.fetchone()[0]
    _insert_legacy_date(conn, "2026-08-01")
    conn.commit()

    captured = []
    real_get_connection = db.get_connection

    def spy_get_connection():
        new_conn = real_get_connection()
        with new_conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema_name}")
        captured.append(new_conn)
        return new_conn

    monkeypatch.setattr(db, "get_connection", spy_get_connection)

    # 旧スキーマなのにtenant_idを指定 -> SchemaMismatchError
    with pytest.raises(restore_module.SchemaMismatchError):
        restore_module.restore_json_from_postgres(uuid.uuid4(), data_file=tmp_path / "r.json")

    assert captured[-1].closed


@requires_db
def test_restore_extracts_only_the_specified_tenant(tmp_path, tenant_schema):
    conn = tenant_schema
    _insert_legacy_date(conn, "2026-08-01")
    conn.commit()
    tenant_a = uuid.uuid4()
    migrate_tenant_module.migrate_to_tenant_schema(tenant_a, conn=conn)

    tenant_b = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant_b, "世帯B"))
        cur.execute(
            "INSERT INTO records (tenant_id, record_date) VALUES (%s, '2026-08-09')",
            (tenant_b,),
        )
    conn.commit()

    data_file = tmp_path / "restored.json"
    dates = restore_module.restore_json_from_postgres(tenant_a, data_file=data_file, conn=conn)

    assert dates == {"2026-08-01"}  # tenant_bの2026-08-09を含まない


@requires_db
def test_restore_backs_up_existing_json_before_overwriting(tmp_path, tenant_schema):
    conn = tenant_schema
    _insert_legacy_date(conn, "2026-08-05")
    conn.commit()
    tenant_id = uuid.uuid4()
    migrate_tenant_module.migrate_to_tenant_schema(tenant_id, conn=conn)

    from logic import list_backups, save_dates as json_save_dates

    data_file = tmp_path / "records.json"
    json_save_dates({"2026-07-01"}, data_file=data_file)

    restore_module.restore_json_from_postgres(tenant_id, data_file=data_file, conn=conn)

    assert len(list_backups(data_file=data_file)) >= 1


@requires_db
def test_restore_returns_empty_set_for_unknown_tenant(tmp_path, tenant_schema):
    conn = tenant_schema
    _insert_legacy_date(conn, "2026-08-01")
    conn.commit()
    migrate_tenant_module.migrate_to_tenant_schema(uuid.uuid4(), conn=conn)

    data_file = tmp_path / "restored.json"
    dates = restore_module.restore_json_from_postgres(uuid.uuid4(), data_file=data_file, conn=conn)

    assert dates == set()  # 存在しないtenant_idでは他テナントのデータを返さない


@requires_db
def test_restore_closes_self_owned_connection(monkeypatch, tmp_path, tenant_schema):
    """conn省略時(自前で接続を開く場合)、restore側が確実に接続をcloseする。

    db.get_connection()をスパイし、新規接続にもテスト専用スキーマのsearch_pathを
    設定してから返すことで、publicスキーマへは一切触れずに自前接続の経路を検証する。
    """
    conn = tenant_schema
    with conn.cursor() as cur:
        cur.execute("SHOW search_path")
        schema_name = cur.fetchone()[0]
    _insert_legacy_date(conn, "2026-08-01")
    conn.commit()
    tenant_id = uuid.uuid4()
    migrate_tenant_module.migrate_to_tenant_schema(tenant_id, conn=conn)

    captured = []
    real_get_connection = db.get_connection

    def spy_get_connection():
        new_conn = real_get_connection()
        with new_conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema_name}")
        captured.append(new_conn)
        return new_conn

    monkeypatch.setattr(db, "get_connection", spy_get_connection)

    dates = restore_module.restore_json_from_postgres(tenant_id, data_file=tmp_path / "r.json")

    assert dates == {"2026-08-01"}
    assert captured[-1].closed
