"""設計書17-0章(第13次改訂版)をそのまま実装。"""
import hashlib
import json
import os
from pathlib import Path

from psycopg import sql

CONSTRAINT_NAME = "tenant_subscriptions_stripe_subscription_id_key"
MIGRATION_NAME = "stripe_subscription_id_unique_schema"
ROLE_NAMES = ("app_data_owner", "app_runtime", "app_webhook")

EXPECTED_TABLE_NAMES = {
    "records", "tenants", "tenant_memberships", "users",
    "tenant_subscriptions", "tenant_usage", "processed_stripe_events",
}
EXPECTED_SEQUENCE_NAMES = {"records_id_seq"}

TEN_APP_DATA_OWNER_FUNCTION_SIGNATURES = [
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
]
ALL_TWELVE_FUNCTION_SIGNATURES = TEN_APP_DATA_OWNER_FUNCTION_SIGNATURES + [
    "resolve_login(text, text, boolean)",
    "find_tenant_id_by_subscription(text)",
]
EXECUTE_REVOKE_TARGETS = [
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
]


class RollbackPreconditionError(Exception):
    pass


class BaselineStateMismatchError(Exception):
    """第21回終了時点との厳密な不一致を表す。"""


def _existing_role_names(cur):
    cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(ROLE_NAMES),))
    return {r[0] for r in cur.fetchall()}


def _precondition_roles_exist(cur):
    missing = set(ROLE_NAMES) - _existing_role_names(cur)
    if missing:
        raise RollbackPreconditionError(
            f"このスクリプトは完全適用状態(3ロールすべて存在)から実行する"
            f"前提です。存在しないロール: {missing}。既にTier 2が実行済み"
            "であれば、17-4章の「復旧(resume)」(rollback_resume_to_full_"
            "restore.py)を使ってください。"
        )


def _precondition_ready_to_resume(cur):
    """「Tier 2完了状態(3ロールとも不在)」または「DEGRADED状態(一部
    ロールが安全に縮退済み: NOLOGIN化・カレントDB権限撤去済みだが、
    クロスDB依存で未DROPのまま残っている)」のいずれかから実行できる
    前提。存在するロールが危険な状態(LOGIN可能なまま、またはカレントDB
    の権限が残ったまま)であれば拒否する。
    """
    existing = _existing_role_names(cur)
    if not existing:
        return
    for role_name in existing:
        cur.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (role_name,))
        can_login = cur.fetchone()[0]
        if can_login:
            raise RollbackPreconditionError(
                f"ロール{role_name}が存在し、かつLOGIN可能なままです。安全に"
                "縮退済みの状態ではないため実行できません。完全適用状態から"
                "17-2章のTier 2または17-3章のTier 3を使うか、危険な状態を"
                "先に是正してください。"
            )
    if "app_data_owner" in existing:
        cur.execute(
            "SELECT has_table_privilege('app_data_owner', 'public.records', 'SELECT')"
        )
        if cur.fetchone()[0]:
            raise RollbackPreconditionError(
                "app_data_ownerが存在し、カレントDBのrecordsへの権限がまだ"
                "残っています。安全に縮退済みの状態ではないため実行できません。"
            )


def _finish_degraded_role_removal(cur):
    """前回の実行がDEGRADEDで終わり、一部ロールが「安全に縮退した状態」
    で残っている場合に、他データベースへの依存が解消されたかを再確認し、
    解消されていればDROP ROLEする。まだ解消されていないロールがあっても
    例外は送出せず、戻り値の`still_degraded`で呼び出し元へ伝える(呼び
    出し元がDEGRADEDとして扱い、それまでに解消できた分の進捗はcommit
    できるようにするため)。
    """
    remaining = _existing_role_names(cur)
    if not remaining:
        return {"newly_dropped": [], "still_degraded": {}}

    cross_db_problems = _check_cross_database_role_dependencies(cur, remaining)
    newly_dropped = []
    still_degraded = {}
    for role_name in sorted(remaining):
        if role_name in cross_db_problems:
            still_degraded[role_name] = cross_db_problems[role_name]
            continue
        cur.execute(f"DROP ROLE {role_name}")
        newly_dropped.append(role_name)

    return {"newly_dropped": newly_dropped, "still_degraded": still_degraded}


def _canonical_migration_log_export(cur):
    """schema_migration_logの全行を、決定的な(キー順・区切り記号が
    固定の)JSON文字列へ正規化し、SHA-256を計算する。
    scripts/export_and_hash_migration_log.py(削除は一切行わない事前
    エクスポート専用ツール)と`_precondition_migration_log_manually_
    archived`(削除直前の一致確認)の両方が、この関数を通じて同一の
    正規化ロジックを共有することで、「行数だけ・真偽値だけ」ではない
    内容そのものの一致確認を可能にする。
    """
    cur.execute(
        "SELECT id, migration_name, executed_at, before_state, applied_changes, result "
        "FROM public.schema_migration_log ORDER BY id"
    )
    columns = ["id", "migration_name", "executed_at", "before_state", "applied_changes", "result"]
    rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    canonical_text = json.dumps(
        rows, default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return rows, canonical_text, digest, len(rows)


def _precondition_migration_log_manually_archived(cur):
    """schema_migration_logの自動ファイル書き出しは、実行環境の永続性・
    DBトランザクションとの整合性を保証できないため、Tier 3・17-4章
    (resume)の安全性の根拠として使わない。単なる真偽値
    (SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED=true)だけでは保存先・行数・
    内容の一致を検証できないため、事前に
    scripts/export_and_hash_migration_log.pyで取得した行数・SHA-256を
    環境変数として要求し、現在のライブ状態と実際に一致するかを検証する。
    """
    cur.execute("SELECT to_regclass('public.schema_migration_log') IS NOT NULL")
    log_exists = cur.fetchone()[0]
    if not log_exists:
        return

    confirmed = os.environ.get("SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED", "").strip().lower()
    if confirmed != "true":
        raise RollbackPreconditionError(
            "public.schema_migration_logをこのTierの完了時に削除しますが、"
            "その前に人間による手動エクスポート・保存・ハッシュ確認が必要です"
            "(自動書き出しは安全性の根拠にしません)。事前に"
            "scripts/export_and_hash_migration_log.pyを実行し、確認済みで"
            "あれば環境変数SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED=trueを"
            "設定してから再実行してください。DDLは一切実行していません。"
        )

    expected_row_count_raw = os.environ.get("SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNT", "").strip()
    expected_sha256 = os.environ.get("SCHEMA_MIGRATION_LOG_ARCHIVE_SHA256", "").strip().lower()
    if not expected_row_count_raw or not expected_sha256:
        raise RollbackPreconditionError(
            "SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNT・SCHEMA_MIGRATION_LOG_"
            "ARCHIVE_SHA256が設定されていません。事前に"
            "scripts/export_and_hash_migration_log.pyを実行し、その出力の"
            "行数・SHA-256を設定してください。DDLは一切実行していません。"
        )
    try:
        expected_row_count = int(expected_row_count_raw)
    except ValueError:
        raise RollbackPreconditionError(
            "SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNTが整数として解釈できません: "
            f"{expected_row_count_raw!r}。DDLは一切実行していません。"
        )

    _, _, actual_sha256, actual_row_count = _canonical_migration_log_export(cur)
    if actual_row_count != expected_row_count or actual_sha256 != expected_sha256:
        raise RollbackPreconditionError(
            "現在のschema_migration_logの内容が、手動エクスポート時の記録と"
            f"一致しません(行数: 実際={actual_row_count} "
            f"期待={expected_row_count}、SHA-256: 実際={actual_sha256} "
            f"期待={expected_sha256})。エクスポート後に内容が変わった可能性が"
            "あるため、scripts/export_and_hash_migration_log.pyを再実行し、"
            "改めて確認してから再実行してください。DDLは一切実行していません。"
        )


def _capture_role_rls_state(cur):
    cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
    policy_count = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND rowsecurity")
    rls_enabled_count = cur.fetchone()[0]
    return {
        "existing_new_roles": sorted(_existing_role_names(cur)),
        "policy_count": policy_count,
        "rls_enabled_table_count": rls_enabled_count,
    }


def _check_cross_database_role_dependencies(cur, role_names):
    """`DROP ROLE`はPostgreSQLクラスタ全体で依存関係をチェックするため、
    「stagingにはアプリ用DBが1つだけ」と断定せず、対象ロールがカレント
    データベース以外にも依存オブジェクトを持たないかを事前に確認する。
    dbid=0(クラスタ共有オブジェクトへの依存)もカレントDB限定ではない
    ため「他所に依存あり」として扱う。
    戻り値: {role_name: [依存先データベース名, ...]}(依存が無いロール
    はキーに含まれない)。
    """
    cur.execute("SELECT oid FROM pg_database WHERE datname = current_database()")
    current_dbid = cur.fetchone()[0]

    problems = {}
    for role_name in role_names:
        cur.execute("SELECT oid FROM pg_roles WHERE rolname = %s", (role_name,))
        row = cur.fetchone()
        if row is None:
            continue
        role_oid = row[0]
        cur.execute(
            "SELECT DISTINCT COALESCE(d.datname, '(クラスタ共有オブジェクトへの依存)') "
            "FROM pg_shdepend sd "
            "LEFT JOIN pg_database d ON d.oid = sd.dbid "
            "WHERE sd.refclassid = 'pg_authid'::regclass AND sd.refobjid = %s "
            "AND sd.dbid <> %s",
            (role_oid, current_dbid),
        )
        others = [r[0] for r in cur.fetchall()]
        if others:
            problems[role_name] = others
    return problems


def _remove_roles_and_rls(cur):
    """①〜⑧を実行する。対象ロールがカレントDB以外にも依存を持つ場合、
    そのロールは`DROP ROLE`を行わず、代わりにNOLOGIN化して(LOGINロール
    の場合)、カレントDBの権限だけを撤去したうえで残す「安全な縮退」を
    行う。これは例外を送出して全体をロールバックするのではなく、確定的
    にコミットされる安全な終了状態である。

    戻り値の`status`(`"complete"`または`"degraded"`)を、呼び出し元
    (Tier 2・Tier 3のmain())は必ず確認し、`[OK]`(COMPLETE)と
    `[DEGRADED]`を明確に区別して報告すること——`degraded_roles`が
    空でないのに`[OK]`と表示してはならない。
    """
    before = _capture_role_rls_state(cur)
    cross_db_problems = _check_cross_database_role_dependencies(cur, ROLE_NAMES)

    for policy, table in [
        ("records_tenant_isolation", "records"),
        ("tenants_tenant_isolation", "tenants"),
        ("tenant_subscriptions_tenant_isolation", "tenant_subscriptions"),
        ("tenant_usage_tenant_isolation", "tenant_usage"),
    ]:
        cur.execute(
            sql.SQL("DROP POLICY IF EXISTS {policy} ON public.{table}").format(
                policy=sql.Identifier(policy), table=sql.Identifier(table)
            )
        )

    for table in ["records", "tenants", "tenant_subscriptions", "tenant_usage",
                  "tenant_memberships", "users"]:
        cur.execute(
            sql.SQL("ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY").format(
                table=sql.Identifier(table)
            )
        )

    # 実運用ではpostgres、CI等では別名のこともあるため、「migrationを
    # 実行した管理ロール」をcurrent_userから動的に取得して戻す
    # (least_privilege_lib.pyのADMIN_OWNER解決と同じ考え方)
    cur.execute("SELECT current_user")
    admin_owner = cur.fetchone()[0]
    for signature in TEN_APP_DATA_OWNER_FUNCTION_SIGNATURES:
        cur.execute(
            sql.SQL("ALTER FUNCTION public.{signature} OWNER TO {owner}").format(
                signature=sql.SQL(signature), owner=sql.Identifier(admin_owner)
            )
        )

    cur.execute("REVOKE SELECT, INSERT, DELETE ON public.records FROM app_data_owner")
    cur.execute("REVOKE USAGE ON public.records_id_seq FROM app_data_owner")
    cur.execute("REVOKE SELECT (id), UPDATE (name) ON public.tenants FROM app_data_owner")
    cur.execute("REVOKE SELECT, INSERT, UPDATE ON public.tenant_subscriptions FROM app_data_owner")
    cur.execute("REVOKE SELECT, INSERT, UPDATE ON public.tenant_usage FROM app_data_owner")
    cur.execute("REVOKE INSERT ON public.processed_stripe_events FROM app_data_owner")
    cur.execute("REVOKE USAGE ON SCHEMA public FROM app_data_owner")

    degraded_roles = {}
    if "app_data_owner" not in cross_db_problems:
        cur.execute("DROP ROLE app_data_owner")
    else:
        degraded_roles["app_data_owner"] = cross_db_problems["app_data_owner"]

    for signature, roles in EXECUTE_REVOKE_TARGETS:
        cur.execute(f"REVOKE EXECUTE ON FUNCTION public.{signature} FROM {', '.join(roles)}")

    for role_name in ("app_runtime", "app_webhook"):
        cur.execute(f"REVOKE USAGE ON SCHEMA public FROM {role_name}")
        if role_name not in cross_db_problems:
            cur.execute(f"DROP ROLE {role_name}")
        else:
            cur.execute(f"ALTER ROLE {role_name} NOLOGIN")
            degraded_roles[role_name] = cross_db_problems[role_name]

    after = _capture_role_rls_state(cur)
    status = "degraded" if degraded_roles else "complete"
    result = {"status": status, "before": before, "after": after, "degraded_roles": degraded_roles}
    if degraded_roles:
        print(
            "[警告] 次のロールは他データベースにも依存を持つため削除せず、"
            f"NOLOGIN化・カレントDB権限撤去のみ行いました: {degraded_roles}。"
            "Tier 1(接続ロールをpostgresへ戻す)を実施し、他データベースの"
            "依存関係を人手で確認・解消したうえで、17-4章「復旧(resume)」"
            "相当の再開手順を実行してください。"
        )
    return result


def _count_existing_functions(cur, signatures):
    count = 0
    for signature in signatures:
        cur.execute("SELECT to_regprocedure(%s) IS NOT NULL", (f"public.{signature}",))
        if cur.fetchone()[0]:
            count += 1
    return count


def _drop_all_functions(cur):
    before_count = _count_existing_functions(cur, ALL_TWELVE_FUNCTION_SIGNATURES)
    for signature in ALL_TWELVE_FUNCTION_SIGNATURES:
        cur.execute(f"DROP FUNCTION IF EXISTS public.{signature}")
    after_count = _count_existing_functions(cur, ALL_TWELVE_FUNCTION_SIGNATURES)
    return {"before_function_count": before_count, "after_function_count": after_count}


def _get_migration_log_history(cur):
    """schema_migration_logから、このTaskがこれまでに実際にNOT NULL・
    UNIQUEを追加したことがあるかを集計する。
    戻り値: (ever_added_not_null, ever_added_unique, record_count)
    """
    cur.execute(
        "SELECT bool_or((applied_changes->>'added_not_null')::boolean), "
        "       bool_or((applied_changes->>'added_unique')::boolean), "
        "       count(*) "
        "FROM public.schema_migration_log WHERE migration_name = %s",
        (MIGRATION_NAME,),
    )
    ever_added_not_null, ever_added_unique, record_count = cur.fetchone()
    return bool(ever_added_not_null), bool(ever_added_unique), record_count


def _reverify_and_drop_constraint(cur):
    """schema_migration_logの記録(このTaskが実際にadded_not_null/
    added_uniqueをtrueにしたことがあるか)とライブ状態を突き合わせ、
    本Taskが追加したと確認できたものだけを撤去する。記録が欠落・矛盾
    する場合は一切変更せず安全停止する(stagingで事前確認した固定の
    前提だけに依存しない)。制約の同一性確認は12-2章の作成時チェックと
    同じ4項目(contype・convalidated・condeferrable・condeferred・
    conkey)をすべて揃えて確認する。
    """
    cur.execute("SELECT to_regclass('public.schema_migration_log') IS NOT NULL")
    if not cur.fetchone()[0]:
        raise RuntimeError(
            "public.schema_migration_logが存在しないため、"
            "stripe_subscription_id制約の撤去可否を判断できません。"
            "安全のため撤去せず停止します。"
        )

    ever_added_not_null, ever_added_unique, record_count = _get_migration_log_history(cur)
    if record_count == 0:
        raise RuntimeError(
            f"schema_migration_logに{MIGRATION_NAME}の記録がありません。"
            "安全のため撤去せず停止します。"
        )

    cur.execute(
        "SELECT attnum FROM pg_attribute "
        "WHERE attrelid = 'public.tenant_subscriptions'::regclass "
        "AND attname = 'stripe_subscription_id'"
    )
    attnum_row = cur.fetchone()
    target_attnum = attnum_row[0] if attnum_row else None

    cur.execute(
        "SELECT contype, convalidated, condeferrable, condeferred, conkey "
        "FROM pg_constraint "
        "WHERE conrelid = 'public.tenant_subscriptions'::regclass AND conname = %s",
        (CONSTRAINT_NAME,),
    )
    row = cur.fetchone()
    before_state = {
        "constraint_exists": row is not None,
        "ever_added_not_null": ever_added_not_null,
        "ever_added_unique": ever_added_unique,
    }

    if row is not None:
        contype, convalidated, condeferrable, condeferred, conkey = row
        if (
            contype != "u"
            or not convalidated
            or condeferrable
            or condeferred
            or target_attnum is None
            or list(conkey) != [target_attnum]
        ):
            raise RuntimeError(
                f"撤去対象の制約{CONSTRAINT_NAME}が、本Taskが追加した"
                "stripe_subscription_id単独のUNIQUE制約と一致しません: "
                f"contype={contype} convalidated={convalidated} "
                f"condeferrable={condeferrable} condeferred={condeferred} "
                f"conkey={list(conkey)}(期待=[{target_attnum}])。"
                "本Task以外の経路で変更された可能性があるため、"
                "安全のため撤去せず停止します。"
            )
        if not ever_added_unique:
            raise RuntimeError(
                f"制約{CONSTRAINT_NAME}が存在しますが、schema_migration_log上"
                "本Taskがこのunique制約を追加した記録がありません。本Task"
                "以外の経路で追加された可能性があるため、安全のため撤去せず"
                "停止します。"
            )
        cur.execute(
            sql.SQL("ALTER TABLE public.tenant_subscriptions DROP CONSTRAINT {name}").format(
                name=sql.Identifier(CONSTRAINT_NAME)
            )
        )

    cur.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tenant_subscriptions' "
        "AND column_name = 'stripe_subscription_id'"
    )
    is_nullable_row = cur.fetchone()
    currently_not_null = is_nullable_row is not None and is_nullable_row[0] == "NO"
    if currently_not_null:
        if not ever_added_not_null:
            raise RuntimeError(
                "stripe_subscription_idはNOT NULLですが、schema_migration_log上"
                "本TaskがNOT NULLを追加した記録がありません。本Task以外の経路で"
                "追加された可能性があるため、安全のため撤去せず停止します。"
            )
        cur.execute(
            "ALTER TABLE public.tenant_subscriptions "
            "ALTER COLUMN stripe_subscription_id DROP NOT NULL"
        )

    cur.execute(
        "SELECT count(*) FROM pg_constraint "
        "WHERE conrelid = 'public.tenant_subscriptions'::regclass AND conname = %s",
        (CONSTRAINT_NAME,),
    )
    still_exists = cur.fetchone()[0] > 0
    return {"before_state": before_state, "constraint_removed": not still_exists}


def _drop_migration_log_table(cur, export_dir="audit/schema_migration_log_archive"):
    """テーブルの削除を実行する前に
    `_precondition_migration_log_manually_archived`が必ず先に呼ばれて
    いる前提(そちらが真の安全性の根拠)。ここでのローカルファイルへの
    書き出しは、Railwayの実行ファイル領域が永続保存先とは限らないため、
    あくまで補助的なベストエフォートであり、安全性の根拠にはしない。
    export_and_hash_migration_log.py・_precondition_migration_log_
    manually_archivedと同一の正規化ロジック(_canonical_migration_log_
    export)を使い、書き出す内容が事前確認済みの内容と形式的にも一致
    するようにする。
    """
    cur.execute("SELECT to_regclass('public.schema_migration_log') IS NOT NULL")
    table_exists = cur.fetchone()[0]
    if not table_exists:
        return {"exported_row_count": 0, "export_path": None, "table_existed": False}

    rows, canonical_text, digest, row_count = _canonical_migration_log_export(cur)

    export_path = None
    try:
        export_path_dir = Path(export_dir)
        export_path_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_path_dir / f"schema_migration_log_archive_{digest[:12]}.json"
        export_path.write_text(canonical_text, encoding="utf-8")
    except OSError as e:
        # ベストエフォートの補助書き出しであり、失敗してもテーブル削除自体は
        # 妨げない(真の安全性は事前の人手によるアーカイブ確認で担保済み)
        print(f"[警告] ローカルへの補助書き出しに失敗しました(処理は継続します): {e}")

    cur.execute("DROP TABLE IF EXISTS public.schema_migration_log")
    return {
        "exported_row_count": row_count,
        "export_sha256": digest,
        "export_path": str(export_path) if export_path else None,
        "table_existed": True,
    }


def _capture_full_baseline_state(cur):
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    table_names = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")
    sequence_names = {r[0] for r in cur.fetchall()}
    cur.execute(
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.prokind = 'f'"
    )
    function_count = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
    policy_count = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND rowsecurity")
    rls_enabled_count = cur.fetchone()[0]

    cur.execute(
        "SELECT attnum FROM pg_attribute "
        "WHERE attrelid = 'public.tenant_subscriptions'::regclass "
        "AND attname = 'stripe_subscription_id'"
    )
    target_attnum_row = cur.fetchone()
    target_attnum = target_attnum_row[0] if target_attnum_row else None
    cur.execute(
        "SELECT conname, conkey FROM pg_constraint "
        "WHERE conrelid = 'public.tenant_subscriptions'::regclass AND contype = 'u'"
    )
    remaining_unique_constraint_names = [
        conname for conname, conkey in cur.fetchall()
        if target_attnum is not None and list(conkey) == [target_attnum]
    ]

    cur.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tenant_subscriptions' "
        "AND column_name = 'stripe_subscription_id'"
    )
    is_nullable_row = cur.fetchone()
    stripe_subscription_id_is_nullable = is_nullable_row[0] if is_nullable_row else None

    cur.execute("SELECT to_regclass('public.schema_migration_log') IS NOT NULL")
    migration_log_exists = cur.fetchone()[0]

    existing_roles = sorted(_existing_role_names(cur))
    existing_role_details = {}
    if existing_roles:
        cur.execute(
            "SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname = ANY(%s)",
            (existing_roles,),
        )
        existing_role_details = {name: {"rolcanlogin": can_login} for name, can_login in cur.fetchall()}

    return {
        "table_names": sorted(table_names),
        "sequence_names": sorted(sequence_names),
        "custom_function_count": function_count,
        "existing_new_roles": existing_roles,
        "existing_new_role_details": existing_role_details,
        "policy_count": policy_count,
        "rls_enabled_table_count": rls_enabled_count,
        "stripe_subscription_id_unique_constraint_names": remaining_unique_constraint_names,
        "stripe_subscription_id_is_nullable": stripe_subscription_id_is_nullable,
        "schema_migration_log_exists": migration_log_exists,
    }


def verify_round21_baseline_state(cur):
    """Tier 3・17-4章の完了直前に呼び、第21回終了時点と完全に一致する
    ことを厳密にassertする。printするだけで成功扱いにせず、1つでも
    不一致なら`BaselineStateMismatchError`を送出する——呼び出し元の
    main()はこれをcommitせずROLLBACKすること。
    """
    state = _capture_full_baseline_state(cur)
    problems = []

    actual_tables = set(state["table_names"])
    if actual_tables != EXPECTED_TABLE_NAMES:
        problems.append(
            f"テーブル集合が不一致: 実際={sorted(actual_tables)} "
            f"期待={sorted(EXPECTED_TABLE_NAMES)}"
        )

    actual_sequences = set(state["sequence_names"])
    if actual_sequences != EXPECTED_SEQUENCE_NAMES:
        problems.append(
            f"シーケンス集合が不一致: 実際={sorted(actual_sequences)} "
            f"期待={sorted(EXPECTED_SEQUENCE_NAMES)}"
        )

    if state["custom_function_count"] != 0:
        problems.append(f"カスタム関数が残存: {state['custom_function_count']}件")

    if state["existing_new_roles"]:
        problems.append(f"新設ロールが残存: {state['existing_new_roles']}")

    if state["rls_enabled_table_count"] != 0:
        problems.append(f"RLSが有効なテーブルが残存: {state['rls_enabled_table_count']}件")

    if state["policy_count"] != 0:
        problems.append(f"RLSポリシーが残存: {state['policy_count']}件")

    if state["stripe_subscription_id_unique_constraint_names"]:
        problems.append(
            "stripe_subscription_idのUNIQUE制約が残存: "
            f"{state['stripe_subscription_id_unique_constraint_names']}"
        )

    if state["stripe_subscription_id_is_nullable"] != "YES":
        problems.append(
            "stripe_subscription_idがNOT NULLのまま(NULL許容へ戻っていない): "
            f"is_nullable={state['stripe_subscription_id_is_nullable']}"
        )

    if state["schema_migration_log_exists"]:
        problems.append("schema_migration_logテーブルが残存")

    if problems:
        raise BaselineStateMismatchError(
            "第21回終了時点との不一致が検出されました: " + " / ".join(problems)
        )

    return state
