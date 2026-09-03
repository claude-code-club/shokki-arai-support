"""設計書4〜7章・16章の内容をそのまま実装したライブラリ。
scripts/migrate_to_least_privilege_schema.py から呼ばれる。

第22課題(検索できるDB、PR #30)との統合対応(案A、2026-09-04)により、
scripts/memo_search_functions.pyのrecord_with_memo_for_tenant()・
search_records_for_tenant()を関数一覧・GRANT・検証へ追加している
(12関数→14関数)。
"""
import os

from psycopg import sql

from memo_search_functions import (
    MEMO_SEARCH_FUNCTION_DEFINITIONS,
    MEMO_SEARCH_FUNCTION_SIGNATURES,
)

# ---- 4-2章 -----------------------------------------------------------

EXPECTED_ROLE_ATTRS = {
    "app_data_owner": {
        "rolsuper": False, "rolcreaterole": False, "rolcreatedb": False,
        "rolcanlogin": False, "rolbypassrls": False,
        "rolreplication": False, "rolinherit": False,
    },
    "app_runtime": {
        "rolsuper": False, "rolcreaterole": False, "rolcreatedb": False,
        "rolcanlogin": True, "rolbypassrls": False,
        "rolreplication": False, "rolinherit": False,
    },
    "app_webhook": {
        "rolsuper": False, "rolcreaterole": False, "rolcreatedb": False,
        "rolcanlogin": True, "rolbypassrls": False,
        "rolreplication": False, "rolinherit": False,
    },
}

ATTR_COLUMNS = [
    "rolsuper", "rolcreaterole", "rolcreatedb", "rolcanlogin",
    "rolbypassrls", "rolreplication", "rolinherit",
]


class RoleAttributeMismatchError(Exception):
    pass


class MissingPasswordError(Exception):
    pass


class UnexpectedGranteeError(Exception):
    pass


def _verify_role_attrs_and_membership(cur, role_name):
    cur.execute(
        f"SELECT {', '.join(ATTR_COLUMNS)} FROM pg_roles WHERE rolname = %s",
        (role_name,),
    )
    row = cur.fetchone()
    if row is None:
        return None

    actual = dict(zip(ATTR_COLUMNS, row))
    expected = EXPECTED_ROLE_ATTRS[role_name]
    if actual != expected:
        raise RoleAttributeMismatchError(
            f"ロール{role_name}の属性が想定と一致しません: "
            f"実際={actual} 期待={expected}"
        )

    cur.execute(
        "SELECT r.rolname FROM pg_auth_members m "
        "JOIN pg_roles r ON r.oid = m.roleid "
        "JOIN pg_roles member_role ON member_role.oid = m.member "
        "WHERE member_role.rolname = %s",
        (role_name,),
    )
    memberships = [r[0] for r in cur.fetchall()]
    if memberships:
        raise RoleAttributeMismatchError(
            f"ロール{role_name}が想定外のロールのメンバーです: {memberships}"
        )

    cur.execute(
        "SELECT member_role.rolname FROM pg_auth_members m "
        "JOIN pg_roles r ON r.oid = m.roleid "
        "JOIN pg_roles member_role ON member_role.oid = m.member "
        "WHERE r.rolname = %s",
        (role_name,),
    )
    members_of_this = [r[0] for r in cur.fetchall()]
    if members_of_this:
        raise RoleAttributeMismatchError(
            f"ロール{role_name}に想定外のメンバーが所属しています: {members_of_this}"
        )
    return actual


def verify_or_create_nologin_role(cur, role_name):
    existing = _verify_role_attrs_and_membership(cur, role_name)
    if existing is None:
        query = sql.SQL(
            "CREATE ROLE {role} WITH NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT"
        ).format(role=sql.Identifier(role_name))
        cur.execute(query)


# ---- 4-3章 -----------------------------------------------------------

def _read_required_password(env_var_name):
    value = os.environ.get(env_var_name, "").strip()
    if not value:
        raise MissingPasswordError(
            f"{env_var_name}が設定されていません。コマンドライン引数や"
            "対話入力は使わず、環境変数経由でのみ渡してください。"
        )
    return value


def verify_or_set_login_role_password(cur, role_name, password):
    existing = _verify_role_attrs_and_membership(cur, role_name)
    if existing is None:
        query = sql.SQL(
            "CREATE ROLE {role} WITH LOGIN PASSWORD {password} "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            "NOBYPASSRLS NOINHERIT"
        ).format(role=sql.Identifier(role_name), password=sql.Literal(password))
        cur.execute(query)
        return

    query = sql.SQL("ALTER ROLE {role} PASSWORD {password}").format(
        role=sql.Identifier(role_name), password=sql.Literal(password)
    )
    cur.execute(query)


# ---- 6-0章 -----------------------------------------------------------

def grant_schema_usage(cur):
    cur.execute("GRANT USAGE ON SCHEMA public TO app_data_owner")
    cur.execute("GRANT USAGE ON SCHEMA public TO app_runtime")
    cur.execute("GRANT USAGE ON SCHEMA public TO app_webhook")


# ---- 6-1章 -----------------------------------------------------------

def grant_table_privileges(cur):
    cur.execute("GRANT SELECT, INSERT, DELETE ON public.records TO app_data_owner")
    # record_with_memo_for_tenant()のON CONFLICT DO UPDATE SET memo = ...が
    # 必要とする(第22課題との統合対応、案A)。列単位でmemoのみに限定し、
    # tenant_id・record_dateの更新はDELETE+INSERTの原子性で行う既存方針を保つ。
    cur.execute("GRANT UPDATE (memo) ON public.records TO app_data_owner")
    cur.execute("GRANT USAGE ON public.records_id_seq TO app_data_owner")
    cur.execute("GRANT SELECT (id), UPDATE (name) ON public.tenants TO app_data_owner")
    cur.execute("GRANT SELECT, INSERT, UPDATE ON public.tenant_subscriptions TO app_data_owner")
    cur.execute("GRANT SELECT, INSERT, UPDATE ON public.tenant_usage TO app_data_owner")
    cur.execute("GRANT INSERT ON public.processed_stripe_events TO app_data_owner")


# ---- 5章 ---------------------------------------------------------------

FUNCTION_DEFINITIONS = [
    """
    CREATE OR REPLACE FUNCTION public.load_dates_for_tenant(p_tenant_id uuid)
    RETURNS SETOF date
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    BEGIN
      PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
      RETURN QUERY
        SELECT record_date FROM public.records WHERE tenant_id = p_tenant_id;
    END;
    $$;
    """,
    """
    CREATE OR REPLACE FUNCTION public.insert_date_for_tenant(p_tenant_id uuid, p_record_date date)
    RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    BEGIN
      PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
      INSERT INTO public.records (tenant_id, record_date)
      VALUES (p_tenant_id, p_record_date)
      ON CONFLICT (tenant_id, record_date) DO NOTHING;
    END;
    $$;
    """,
    """
    CREATE OR REPLACE FUNCTION public.delete_date_for_tenant(p_tenant_id uuid, p_record_date date)
    RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    BEGIN
      PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
      DELETE FROM public.records
      WHERE tenant_id = p_tenant_id AND record_date = p_record_date;
    END;
    $$;
    """,
    """
    CREATE OR REPLACE FUNCTION public.update_tenant_name(p_tenant_id uuid, p_name text)
    RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    BEGIN
      PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
      UPDATE public.tenants SET name = p_name WHERE id = p_tenant_id;
    END;
    $$;
    """,
    """
    CREATE OR REPLACE FUNCTION public.resolve_login(
      p_auth_subject text, p_email text, p_email_verified boolean
    ) RETURNS TABLE(user_id uuid, tenant_id uuid, role text)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    DECLARE
      v_user_id uuid;
    BEGIN
      INSERT INTO public.users (id, auth_subject, email, email_verified)
      VALUES (gen_random_uuid(), p_auth_subject, p_email, p_email_verified)
      ON CONFLICT (auth_subject) DO UPDATE SET
        email = EXCLUDED.email,
        email_verified = EXCLUDED.email_verified
      RETURNING id INTO v_user_id;

      RETURN QUERY
        SELECT v_user_id, tm.tenant_id, tm.role
        FROM public.tenant_memberships tm
        WHERE tm.user_id = v_user_id;
    END;
    $$;
    """,
    """
    CREATE OR REPLACE FUNCTION public.get_subscription(p_tenant_id uuid)
    RETURNS TABLE(plan text, status text, current_period_end timestamptz, stripe_customer_id text)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    BEGIN
      PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
      RETURN QUERY
        SELECT ts.plan, ts.status, ts.current_period_end, ts.stripe_customer_id
        FROM public.tenant_subscriptions ts
        WHERE ts.tenant_id = p_tenant_id;
    END;
    $$;
    """,
    """
    CREATE OR REPLACE FUNCTION public.upsert_subscription_if_new_session(
      p_tenant_id uuid, p_plan text, p_status text, p_stripe_customer_id text,
      p_stripe_subscription_id text, p_stripe_checkout_session_id text,
      p_current_period_end timestamptz
    ) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    DECLARE
      v_applied boolean;
    BEGIN
      PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
      INSERT INTO public.tenant_subscriptions
        (tenant_id, plan, status, stripe_customer_id, stripe_subscription_id,
         stripe_checkout_session_id, current_period_end, updated_at)
      VALUES (p_tenant_id, p_plan, p_status, p_stripe_customer_id, p_stripe_subscription_id,
         p_stripe_checkout_session_id, p_current_period_end, now())
      ON CONFLICT (tenant_id) DO UPDATE SET
        plan = EXCLUDED.plan,
        status = EXCLUDED.status,
        stripe_customer_id = EXCLUDED.stripe_customer_id,
        stripe_subscription_id = EXCLUDED.stripe_subscription_id,
        stripe_checkout_session_id = EXCLUDED.stripe_checkout_session_id,
        current_period_end = EXCLUDED.current_period_end,
        updated_at = now()
      WHERE public.tenant_subscriptions.stripe_checkout_session_id
        IS DISTINCT FROM EXCLUDED.stripe_checkout_session_id
      RETURNING true INTO v_applied;
      RETURN COALESCE(v_applied, false);
    END;
    $$;
    """,
    """
    CREATE OR REPLACE FUNCTION public.find_tenant_id_by_subscription(p_stripe_subscription_id text)
    RETURNS uuid
    LANGUAGE sql SECURITY DEFINER SET search_path = ''
    AS $$
      SELECT tenant_id FROM public.tenant_subscriptions
      WHERE stripe_subscription_id = p_stripe_subscription_id;
    $$;
    """,
    """
    CREATE OR REPLACE FUNCTION public.update_subscription_status(
      p_tenant_id uuid, p_plan text, p_status text, p_current_period_end timestamptz
    ) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    DECLARE
      v_updated boolean;
    BEGIN
      PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
      UPDATE public.tenant_subscriptions
      SET plan = p_plan, status = p_status, current_period_end = p_current_period_end,
          updated_at = now()
      WHERE tenant_id = p_tenant_id
      RETURNING true INTO v_updated;
      RETURN COALESCE(v_updated, false);
    END;
    $$;
    """,
    """
    CREATE OR REPLACE FUNCTION public.get_tenant_usage_count(
      p_tenant_id uuid, p_metric_key text, p_period_start date
    ) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    DECLARE
      v_count integer;
    BEGIN
      PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
      SELECT usage_count INTO v_count
      FROM public.tenant_usage
      WHERE tenant_id = p_tenant_id AND metric_key = p_metric_key AND period_start = p_period_start;
      RETURN COALESCE(v_count, 0);
    END;
    $$;
    """,
    """
    CREATE OR REPLACE FUNCTION public.increment_tenant_usage_if_under_limit(
      p_tenant_id uuid, p_metric_key text, p_period_start date, p_limit integer
    ) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    DECLARE
      v_new_count integer;
    BEGIN
      PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
      INSERT INTO public.tenant_usage (tenant_id, metric_key, period_start, usage_count, updated_at)
      VALUES (p_tenant_id, p_metric_key, p_period_start, 1, now())
      ON CONFLICT (tenant_id, metric_key, period_start) DO UPDATE SET
        usage_count = public.tenant_usage.usage_count + 1,
        updated_at = now()
      WHERE p_limit IS NULL OR public.tenant_usage.usage_count < p_limit
      RETURNING usage_count INTO v_new_count;
      RETURN v_new_count;
    END;
    $$;
    """,
    """
    CREATE OR REPLACE FUNCTION public.mark_stripe_event_processed(p_event_id text, p_event_type text)
    RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    DECLARE
      v_inserted boolean;
    BEGIN
      INSERT INTO public.processed_stripe_events (stripe_event_id, event_type)
      VALUES (p_event_id, p_event_type)
      ON CONFLICT (stripe_event_id) DO NOTHING
      RETURNING true INTO v_inserted;
      RETURN COALESCE(v_inserted, false);
    END;
    $$;
    """,
] + MEMO_SEARCH_FUNCTION_DEFINITIONS


def create_or_replace_functions(cur):
    for definition in FUNCTION_DEFINITIONS:
        cur.execute(definition)


APP_DATA_OWNER_FUNCTION_SIGNATURES = [
    "load_dates_for_tenant(uuid)",
    "insert_date_for_tenant(uuid, date)",
    "delete_date_for_tenant(uuid, date)",
    "update_tenant_name(uuid, text)",
    "get_subscription(uuid)",
    "upsert_subscription_if_new_session(uuid, text, text, text, text, text, timestamptz)",
    "update_subscription_status(uuid, text, text, timestamptz)",
    "get_tenant_usage_count(uuid, text, date)",
    "increment_tenant_usage_if_under_limit(uuid, text, date, integer)",
    "mark_stripe_event_processed(text, text)",
] + MEMO_SEARCH_FUNCTION_SIGNATURES

ALL_FUNCTION_SIGNATURES = APP_DATA_OWNER_FUNCTION_SIGNATURES + [
    "resolve_login(text, text, boolean)",
    "find_tenant_id_by_subscription(text)",
]


def reassign_function_owners(cur):
    for signature in APP_DATA_OWNER_FUNCTION_SIGNATURES:
        cur.execute(f"ALTER FUNCTION public.{signature} OWNER TO app_data_owner")


# ---- 6-2章段階1 --------------------------------------------------------

RESET_AND_GRANT_STATEMENTS = [
    ("load_dates_for_tenant(uuid)", ["app_runtime"]),
    ("insert_date_for_tenant(uuid, date)", ["app_runtime"]),
    ("delete_date_for_tenant(uuid, date)", ["app_runtime"]),
    ("update_tenant_name(uuid, text)", ["app_runtime"]),
    ("resolve_login(text, text, boolean)", ["app_runtime"]),
    ("get_subscription(uuid)", ["app_runtime", "app_webhook"]),
    ("upsert_subscription_if_new_session(uuid, text, text, text, text, text, timestamptz)",
     ["app_runtime", "app_webhook"]),
    ("find_tenant_id_by_subscription(text)", ["app_webhook"]),
    ("update_subscription_status(uuid, text, text, timestamptz)", ["app_webhook"]),
    ("get_tenant_usage_count(uuid, text, date)", ["app_runtime"]),
    ("increment_tenant_usage_if_under_limit(uuid, text, date, integer)", ["app_runtime"]),
    ("mark_stripe_event_processed(text, text)", ["app_webhook"]),
    ("record_with_memo_for_tenant(uuid, date, text)", ["app_runtime"]),
    ("search_records_for_tenant(uuid, text, text)", ["app_runtime"]),
]


def reset_and_grant_execute_permissions(cur):
    for signature, roles in RESET_AND_GRANT_STATEMENTS:
        cur.execute(
            f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC, app_runtime, app_webhook"
        )
        cur.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO {', '.join(roles)}")


# ---- 6-2章段階2 --------------------------------------------------------

# resolve_login・find_tenant_id_by_subscriptionは、migrationを実行した
# ロール(実運用ではpostgres、CI等では別名のこともある)の所有のまま
# 意図的な例外とする(3章)。「postgres」という特定の文字列ではなく
# 「migrationを実行した管理ロール」であることが本来の要件のため、
# ADMIN_OWNERは検証時にcurrent_userへ動的に解決するプレースホルダ。
ADMIN_OWNER = object()

EXPECTED_FUNCTION_GRANTS = [
    ("public.load_dates_for_tenant(uuid)", "app_data_owner", {"app_runtime"}),
    ("public.insert_date_for_tenant(uuid, date)", "app_data_owner", {"app_runtime"}),
    ("public.delete_date_for_tenant(uuid, date)", "app_data_owner", {"app_runtime"}),
    ("public.update_tenant_name(uuid, text)", "app_data_owner", {"app_runtime"}),
    ("public.resolve_login(text, text, boolean)", ADMIN_OWNER, {"app_runtime"}),
    ("public.get_subscription(uuid)", "app_data_owner", {"app_runtime", "app_webhook"}),
    (
        "public.upsert_subscription_if_new_session(uuid, text, text, text, text, text, timestamptz)",
        "app_data_owner", {"app_runtime", "app_webhook"},
    ),
    ("public.find_tenant_id_by_subscription(text)", ADMIN_OWNER, {"app_webhook"}),
    ("public.update_subscription_status(uuid, text, text, timestamptz)", "app_data_owner", {"app_webhook"}),
    ("public.get_tenant_usage_count(uuid, text, date)", "app_data_owner", {"app_runtime"}),
    (
        "public.increment_tenant_usage_if_under_limit(uuid, text, date, integer)",
        "app_data_owner", {"app_runtime"},
    ),
    ("public.mark_stripe_event_processed(text, text)", "app_data_owner", {"app_webhook"}),
    ("public.record_with_memo_for_tenant(uuid, date, text)", "app_data_owner", {"app_runtime"}),
    ("public.search_records_for_tenant(uuid, text, text)", "app_data_owner", {"app_runtime"}),
]


def verify_function_grant(cur, qualified_signature, expected_owner, expected_grantees):
    cur.execute(
        "SELECT p.proacl IS NULL AS acl_is_null, p.proowner::regrole::text AS owner "
        "FROM pg_proc p WHERE p.oid = %s::regprocedure",
        (qualified_signature,),
    )
    row = cur.fetchone()
    if row is None:
        raise UnexpectedGranteeError(f"{qualified_signature}が見つかりません。")
    acl_is_null, actual_owner = row

    if actual_owner != expected_owner:
        raise UnexpectedGranteeError(
            f"{qualified_signature}の所有者が想定と一致しません: "
            f"実際={actual_owner} 期待={expected_owner}"
        )

    if acl_is_null:
        raise UnexpectedGranteeError(
            f"{qualified_signature}にはACLが一度も設定されていません"
            "(proacl IS NULL)。REVOKE ALL FROM PUBLICが未適用のため、"
            "安全のため停止します。"
        )

    cur.execute(
        "SELECT CASE WHEN acl.grantee = 0 THEN 'PUBLIC' "
        "            ELSE acl.grantee::regrole::text END AS grantee "
        "FROM pg_proc p "
        "CROSS JOIN LATERAL aclexplode(p.proacl) "
        "  AS acl(grantor, grantee, privilege_type, is_grantable) "
        "WHERE p.oid = %s::regprocedure "
        "AND acl.privilege_type = 'EXECUTE' "
        "AND acl.grantee <> p.proowner",
        (qualified_signature,),
    )
    actual_grantees = {r[0] for r in cur.fetchall()}
    if actual_grantees != expected_grantees:
        raise UnexpectedGranteeError(
            f"{qualified_signature}のEXECUTE権限が想定と一致しません: "
            f"実際={actual_grantees} 期待={expected_grantees}。"
        )


def verify_all_function_grants(cur):
    cur.execute("SELECT current_user")
    admin_owner = cur.fetchone()[0]
    for signature, owner, grantees in EXPECTED_FUNCTION_GRANTS:
        resolved_owner = admin_owner if owner is ADMIN_OWNER else owner
        verify_function_grant(cur, signature, resolved_owner, grantees)


# ---- 7章 ----------------------------------------------------------------

RLS_STATEMENTS = [
    "ALTER TABLE public.records ENABLE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS records_tenant_isolation ON public.records",
    """CREATE POLICY records_tenant_isolation ON public.records
       TO app_data_owner
       USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
       WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""",

    "ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS tenants_tenant_isolation ON public.tenants",
    """CREATE POLICY tenants_tenant_isolation ON public.tenants
       TO app_data_owner
       USING (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
       WITH CHECK (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""",

    "ALTER TABLE public.tenant_subscriptions ENABLE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS tenant_subscriptions_tenant_isolation ON public.tenant_subscriptions",
    """CREATE POLICY tenant_subscriptions_tenant_isolation ON public.tenant_subscriptions
       TO app_data_owner
       USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
       WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""",

    "ALTER TABLE public.tenant_usage ENABLE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS tenant_usage_tenant_isolation ON public.tenant_usage",
    """CREATE POLICY tenant_usage_tenant_isolation ON public.tenant_usage
       TO app_data_owner
       USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
       WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""",

    "ALTER TABLE public.tenant_memberships ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE public.users ENABLE ROW LEVEL SECURITY",
]


def enable_rls_and_policies(cur):
    for statement in RLS_STATEMENTS:
        cur.execute(statement)
