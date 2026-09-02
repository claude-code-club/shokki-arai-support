"""設計書12-2章の apply_stripe_subscription_id_constraint_if_needed() をそのまま実装。"""
import json

from psycopg import sql

CONSTRAINT_NAME = "tenant_subscriptions_stripe_subscription_id_key"
MIGRATION_NAME = "stripe_subscription_id_unique_schema"


def _ensure_migration_log_table(cur):
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.schema_migration_log ("
        "  id bigserial PRIMARY KEY,"
        "  migration_name text NOT NULL,"
        "  executed_at timestamptz NOT NULL DEFAULT now(),"
        "  before_state jsonb NOT NULL,"
        "  applied_changes jsonb NOT NULL,"
        "  result text NOT NULL"
        ")"
    )


def _record_migration_log(cur, before_state, applied_changes, result):
    cur.execute(
        "INSERT INTO public.schema_migration_log "
        "(migration_name, before_state, applied_changes, result) "
        "VALUES (%s, %s::jsonb, %s::jsonb, %s)",
        (MIGRATION_NAME, json.dumps(before_state), json.dumps(applied_changes), result),
    )


def apply_stripe_subscription_id_constraint_if_needed(cur):
    _ensure_migration_log_table(cur)

    cur.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tenant_subscriptions' "
        "AND column_name = 'stripe_subscription_id'"
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("stripe_subscription_id列が見つかりません。")
    is_nullable = row[0] == "YES"

    cur.execute(
        "SELECT attnum FROM pg_attribute "
        "WHERE attrelid = 'public.tenant_subscriptions'::regclass "
        "AND attname = 'stripe_subscription_id'"
    )
    target_attnum = cur.fetchone()[0]

    cur.execute(
        "SELECT contype, convalidated, condeferrable, condeferred, conkey "
        "FROM pg_constraint "
        "WHERE conrelid = 'public.tenant_subscriptions'::regclass "
        "AND conname = %s",
        (CONSTRAINT_NAME,),
    )
    named_row = cur.fetchone()
    has_named_unique = False
    if named_row is not None:
        contype, convalidated, condeferrable, condeferred, conkey = named_row
        is_single_column_on_target = (list(conkey) == [target_attnum])
        if (
            contype != "u"
            or not convalidated
            or condeferrable
            or condeferred
            or not is_single_column_on_target
        ):
            raise RuntimeError(
                f"制約名{CONSTRAINT_NAME}が既に想定と異なる定義で存在します: "
                f"contype={contype} convalidated={convalidated} "
                f"condeferrable={condeferrable} condeferred={condeferred} "
                f"conkey={list(conkey)}(期待=[{target_attnum}])。"
                "安全のため停止します。"
            )
        has_named_unique = True

    before_state = {
        "is_nullable": is_nullable,
        "has_named_unique_constraint": has_named_unique,
    }

    cur.execute(
        "SELECT conname, conkey FROM pg_constraint "
        "WHERE conrelid = 'public.tenant_subscriptions'::regclass "
        "AND contype = 'u'"
    )
    other_unique = [
        conname for conname, conkey in cur.fetchall()
        if conname != CONSTRAINT_NAME and list(conkey) == [target_attnum]
    ]
    if other_unique:
        raise RuntimeError(
            f"stripe_subscription_idに別名の同等UNIQUE制約が既に存在します: "
            f"{other_unique}。想定外の状態のため安全に停止します。"
        )

    if not is_nullable and has_named_unique:
        applied_changes = {"added_not_null": False, "added_unique": False}
        result = {"before_state": before_state, "applied_changes": applied_changes, "result": "skipped_already_applied"}
        _record_migration_log(cur, before_state, applied_changes, result["result"])
        print(f"[SKIP] NOT NULL・UNIQUE制約は既に適用済みです。 detail={result}")
        return result

    if is_nullable and has_named_unique:
        raise RuntimeError(
            "UNIQUE制約は存在するがNOT NULL制約が無い、想定外の状態です。"
            "自動処理の対象外のため、安全のため停止します。"
        )

    cur.execute(
        "SELECT COUNT(*) FILTER (WHERE stripe_subscription_id IS NULL) "
        "FROM public.tenant_subscriptions"
    )
    null_count = cur.fetchone()[0]
    if null_count > 0:
        raise RuntimeError(
            f"stripe_subscription_idがNULLの行が{null_count}件あります。"
            "NOT NULL制約を適用できません。"
        )

    applied_changes = {"added_not_null": False, "added_unique": False}

    if is_nullable:
        cur.execute(
            "ALTER TABLE public.tenant_subscriptions "
            "ALTER COLUMN stripe_subscription_id SET NOT NULL"
        )
        applied_changes["added_not_null"] = True

    if not has_named_unique:
        cur.execute(
            sql.SQL(
                "ALTER TABLE public.tenant_subscriptions "
                "ADD CONSTRAINT {name} UNIQUE (stripe_subscription_id)"
            ).format(name=sql.Identifier(CONSTRAINT_NAME))
        )
        applied_changes["added_unique"] = True

    result = {"before_state": before_state, "applied_changes": applied_changes, "result": "applied"}
    _record_migration_log(cur, before_state, applied_changes, result["result"])
    return result
