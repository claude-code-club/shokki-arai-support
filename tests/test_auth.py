import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))

import auth  # noqa: E402
import db  # noqa: E402
import psycopg  # noqa: E402
import scripts.bootstrap_admin_membership as bootstrap_module  # noqa: E402
import scripts.migrate_to_auth_schema as migrate_auth_module  # noqa: E402
import scripts.migrate_to_tenant_schema as migrate_tenant_module  # noqa: E402
import storage  # noqa: E402

requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします",
)


@pytest.fixture
def auth_schema():
    """稼働中のpublic.recordsとは隔離した専用スキーマに、tenants/records(第16回)＋
    users/tenant_memberships(第17回)を用意し、テスト用の世帯(tenant_id)を1つ作る。
    """
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    schema_name = f"test_auth_{uuid.uuid4().hex}"
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    db.ensure_schema(conn)
    tenant_id = uuid.uuid4()
    migrate_tenant_module.migrate_to_tenant_schema(tenant_id, conn=conn)
    migrate_auth_module.migrate_to_auth_schema(conn=conn)
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


# --- AUTH_ENABLEDの判定(DB接続不要) ---


def test_is_auth_enabled_defaults_to_false(monkeypatch):
    monkeypatch.delenv(auth.AUTH_ENABLED_ENV, raising=False)
    assert auth.is_auth_enabled() is False


def test_is_auth_enabled_true(monkeypatch):
    monkeypatch.setenv(auth.AUTH_ENABLED_ENV, "true")
    assert auth.is_auth_enabled() is True


def test_is_auth_enabled_other_values_are_false(monkeypatch):
    monkeypatch.setenv(auth.AUTH_ENABLED_ENV, "1")
    assert auth.is_auth_enabled() is False


# --- migrate_to_auth_schema() ---


@requires_db
def test_migrate_to_auth_schema_is_idempotent(auth_schema):
    conn, _tenant_id = auth_schema
    migrate_auth_module.migrate_to_auth_schema(conn=conn)  # 2回目(fixtureで既に1回実行済み)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
            "AND tablename IN ('users', 'tenant_memberships')"
        )
        assert {row[0] for row in cur.fetchall()} == {"users", "tenant_memberships"}


# --- resolve_tenant_context() ---


@requires_db
def test_resolve_tenant_context_creates_user_and_returns_membership(auth_schema):
    conn, tenant_id = auth_schema
    user_id = db.get_or_create_user(
        conn, auth_subject="auth0|alice", email="alice@example.com", email_verified=True
    )
    db.create_membership(conn, tenant_id=tenant_id, user_id=user_id, role="member")
    conn.commit()

    resolved_tenant_id, role = auth.resolve_tenant_context(
        auth_subject="auth0|alice", email="alice@example.com", email_verified=True, conn=conn
    )

    assert resolved_tenant_id == tenant_id
    assert role == "member"


@requires_db
def test_resolve_tenant_context_syncs_email_on_existing_user(auth_schema):
    conn, tenant_id = auth_schema
    user_id = db.get_or_create_user(
        conn, auth_subject="auth0|bob", email="old@example.com", email_verified=False
    )
    db.create_membership(conn, tenant_id=tenant_id, user_id=user_id, role="admin")
    conn.commit()

    auth.resolve_tenant_context(
        auth_subject="auth0|bob", email="new@example.com", email_verified=True, conn=conn
    )

    with conn.cursor() as cur:
        cur.execute("SELECT email, email_verified FROM users WHERE id = %s", (user_id,))
        assert cur.fetchone() == ("new@example.com", True)


@requires_db
def test_resolve_tenant_context_no_membership_raises_access_denied(auth_schema):
    conn, _tenant_id = auth_schema

    with pytest.raises(auth.AccessDeniedError):
        auth.resolve_tenant_context(
            auth_subject="auth0|nobody", email=None, email_verified=False, conn=conn
        )

    # ユーザー自体はupsertされているが、membershipが無いため利用拒否される
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE auth_subject = %s", ("auth0|nobody",))
        assert cur.fetchone() is not None


@requires_db
def test_resolve_tenant_context_multiple_memberships_raises_access_denied(auth_schema):
    conn, tenant_a = auth_schema
    user_id = db.get_or_create_user(
        conn, auth_subject="auth0|multi", email=None, email_verified=False
    )
    tenant_b = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant_b, "世帯B"))
    db.create_membership(conn, tenant_id=tenant_a, user_id=user_id, role="member")
    db.create_membership(conn, tenant_id=tenant_b, user_id=user_id, role="member")
    conn.commit()

    with pytest.raises(auth.AccessDeniedError):
        auth.resolve_tenant_context(
            auth_subject="auth0|multi", email=None, email_verified=False, conn=conn
        )


@requires_db
def test_resolve_tenant_context_cross_tenant_isolation(auth_schema):
    """世帯A・Bの越境防止: 別々のユーザーはそれぞれ自分の世帯だけに解決される。"""
    conn, tenant_a = auth_schema
    tenant_b = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant_b, "世帯B"))
    conn.commit()

    user_a = db.get_or_create_user(conn, auth_subject="auth0|user-a", email=None, email_verified=False)
    user_b = db.get_or_create_user(conn, auth_subject="auth0|user-b", email=None, email_verified=False)
    db.create_membership(conn, tenant_id=tenant_a, user_id=user_a, role="member")
    db.create_membership(conn, tenant_id=tenant_b, user_id=user_b, role="member")
    conn.commit()

    tenant_for_a, _ = auth.resolve_tenant_context(
        auth_subject="auth0|user-a", email=None, email_verified=False, conn=conn
    )
    tenant_for_b, _ = auth.resolve_tenant_context(
        auth_subject="auth0|user-b", email=None, email_verified=False, conn=conn
    )

    assert tenant_for_a == tenant_a
    assert tenant_for_b == tenant_b
    assert tenant_for_a != tenant_for_b


# --- storage.rename_tenant()(admin専用操作、サーバー側role検証) ---


@requires_db
def test_rename_tenant_requires_admin_role(monkeypatch, auth_schema):
    conn, tenant_id = auth_schema
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")

    real_get_connection = db.get_connection
    with conn.cursor() as cur:
        cur.execute("SHOW search_path")
        schema_name = cur.fetchone()[0]

    def patched_get_connection():
        c = real_get_connection()
        with c.cursor() as cur:
            cur.execute(f"SET search_path TO {schema_name}")
        return c

    monkeypatch.setattr(db, "get_connection", patched_get_connection)

    with pytest.raises(storage.StorageConfigError):
        storage.rename_tenant("新しい世帯名", tenant_id=tenant_id, role="member")

    storage.rename_tenant("新しい世帯名", tenant_id=tenant_id, role="admin")

    with conn.cursor() as cur:
        cur.execute("SELECT name FROM tenants WHERE id = %s", (tenant_id,))
        assert cur.fetchone()[0] == "新しい世帯名"


# --- bootstrap_admin_membership.py ---


@requires_db
def test_bootstrap_admin_membership_creates_admin(auth_schema):
    conn, tenant_id = auth_schema

    user_id = bootstrap_module.bootstrap_admin_membership(
        "auth0|first-admin", tenant_id, conn=conn
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT role FROM tenant_memberships WHERE tenant_id = %s AND user_id = %s",
            (tenant_id, user_id),
        )
        assert cur.fetchone()[0] == "admin"


@requires_db
def test_bootstrap_admin_membership_rejects_unknown_tenant(auth_schema):
    conn, _tenant_id = auth_schema
    unknown_tenant = uuid.uuid4()

    with pytest.raises(ValueError):
        bootstrap_module.bootstrap_admin_membership("auth0|x", unknown_tenant, conn=conn)


def test_bootstrap_admin_membership_rejects_non_uuid_tenant_id():
    with pytest.raises(TypeError):
        bootstrap_module.bootstrap_admin_membership("auth0|x", "not-a-uuid")


def test_bootstrap_admin_membership_rejects_empty_auth_subject():
    with pytest.raises(TypeError):
        bootstrap_module.bootstrap_admin_membership("", uuid.uuid4())


@requires_db
def test_bootstrap_admin_membership_is_idempotent(auth_schema):
    conn, tenant_id = auth_schema

    bootstrap_module.bootstrap_admin_membership("auth0|dup", tenant_id, conn=conn)
    bootstrap_module.bootstrap_admin_membership("auth0|dup", tenant_id, conn=conn)  # 再実行

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM users WHERE auth_subject = %s", ("auth0|dup",))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT COUNT(*) FROM tenant_memberships")
        assert cur.fetchone()[0] == 1
