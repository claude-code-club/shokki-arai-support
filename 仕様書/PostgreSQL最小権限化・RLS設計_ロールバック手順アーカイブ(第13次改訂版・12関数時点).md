# ⚠️実行禁止・歴史的記録 — ロールバック手順アーカイブ(第13次改訂版・12関数時点)

> **このドキュメントのコード例・SQL・Pythonスクリプトの記述内容は、
> 第13次改訂版(12関数、`record_with_memo_for_tenant`・
> `search_records_for_tenant`の2関数を追加する前)時点のものを、
> そのままの形で保存した歴史的記録です。**
>
> **このドキュメント内のコードを直接実行しないでください。** 第14次
> (14関数、検索できるDB=第22課題との統合後)以降、実際のロールバック
> 手順は必ず以下の実スクリプトファイルを正本として使用してください。
>
> - `scripts/rollback_tier2_remove_roles.py`
> - `scripts/rollback_tier3_full_restore.py`
> - `scripts/rollback_resume_to_full_restore.py`
> - `scripts/rollback_helpers.py`(`APP_DATA_OWNER_FUNCTION_SIGNATURES`・
>   `ALL_FUNCTION_SIGNATURES`・`EXECUTE_REVOKE_TARGETS`が14関数分すべて
>   更新済み — `record_with_memo_for_tenant`・`search_records_for_tenant`
>   の所有権復帰・EXECUTE取消も含む)
> - `scripts/export_and_hash_migration_log.py`
>
> 上記の実スクリプトは、以下に保存された第13次改訂版の設計・考え方
> (3スクリプト構成、Tier 2/Tier 3/resume の役割分担、DEGRADED状態の
> 扱い、監査ログの扱い分離など)を土台として、14関数分の対象を追加
> したものです。**設計思想・手順の骨格は現在も有効ですが、対象と
> なる関数の一覧・個数(12→14)、および対応するSQL文の一部が古い
> ままである点に注意してください。**
>
> 現行(14関数)の§5・§6・§16は、
> `仕様書/PostgreSQL最小権限化・RLS設計.md`の本文側で第14次改訂版
> として更新済みです。ロールバック手順の現行版の要約・参照先は、
> 同ドキュメントの「17. ロールバック手順」を参照してください。

---

以下、第13次改訂版時点の本文をそのまま保存します。

---

## 17. ロールバック手順（監査ログの扱いを分離、開始状態を明示した3スクリプト構成へ訂正）

> ⚠️**この章のロールバックスクリプトのコード例も第13次改訂版(12関数)
> 時点のものです。第14次(14関数)では、`record_with_memo_for_tenant`・
> `search_records_for_tenant`の所有権復帰・EXECUTE取消も対象に含める
> 必要があり、`scripts/rollback_helpers.py`の
> `APP_DATA_OWNER_FUNCTION_SIGNATURES`・`ALL_FUNCTION_SIGNATURES`・
> `EXECUTE_REVOKE_TARGETS`(いずれも更新済み)には既に反映されています。
> ロールバック作業は必ず実際のスクリプトファイルを正本として使用し、
> この章のコードを直接実行しないでください。**

**訂正の要点(第9次)**: 第8次改訂版は、12-2章で新設した
`public.schema_migration_log`(および付随する`bigserial`のシーケンス)を
Tier 3実行後も残す設計になっており、「1章①〜⑤と完全一致」「第21回
終了時点への全面復帰」という定義そのものと矛盾していた
(テーブル7件・シーケンス1件のはずが8件・2件になる)。また、Tier 3の
SQLが「①〜⑧はTier 2と同一」と省略されており単体で実行できず、
Tier 2実行後にTier 3を実行すると3ロールが既に無いため`DROP ROLE`等が
失敗する構造だった。監査ログの記録内容も固定値であり、`IF EXISTS`に
より実際には何も削除されなかった場合でも「削除した」という記録になり
うる不正確さがあった。

これらを次の方針で解消する。

1. **監査ログは残さない**: Tier 3(および後述する残存物撤去手順)は、
   `schema_migration_log`の全行をDB外のローカル監査ファイルへ書き出して
   から、テーブルごと削除する(付随シーケンスも`DROP TABLE`で自動的に
   削除される)。これにより「完全復帰」を文字どおり達成する
2. **開始状態を明示した独立スクリプト**: Tier 2・Tier 3は、いずれも
   「完全適用状態(3ロール・GRANT・RLS・12関数・制約・監査ログすべて
   存在)」を開始状態とする、互いに独立した(連続実行を前提としない)
   スクリプトとする。加えて、Tier 2実行後(3ロールが既に無い状態)から
   完全復帰させるための専用スクリプト(17-4章)を新設し、開始状態が
   食い違うスクリプトを誤って連続実行した場合は、事前条件チェックで
   安全に停止する
3. **実測に基づく記録**: 固定値ではなく、実行前後のカタログ状態を
   実際にクエリして記録する
4. **削除直前の再検証**: 制約の撤去前に、12-2章と同じ`conkey`/`attnum`
   照合を同一トランザクション内で再実行し、不一致なら撤去せず安全停止
   する

### 17-0. 共通ヘルパー(第12次全面訂正、`scripts/rollback_helpers.py`)

**訂正の要点(第12次)**: 岩瀬様の実物監査により、異常時(クロスDB依存・
記録欠落・最終状態不一致)でも処理を続行し成功扱いにしてしまう系統的な
問題が判明した。次の訂正を行った。

- `_remove_roles_and_rls`の戻り値へ`status`(`"complete"`/`"degraded"`)
  を追加し、呼び出し元が`[OK]`と`[DEGRADED]`を確実に区別できるようにした
- `verify_round21_baseline_state()`を新設し、第21回終了時点との厳密な
  一致(テーブル集合・シーケンス集合の完全一致、NOT NULL復帰確認を含む)
  をassertする。不一致なら`BaselineStateMismatchError`を送出し、呼び
  出し元がROLLBACKする
- `_reverify_and_drop_constraint`を全面訂正し、`schema_migration_log`の
  履歴(このTaskが実際にNOT NULL・UNIQUEを追加したことがあるか)と
  ライブ状態を突き合わせたうえでのみ撤去するようにした。記録欠落・
  矛盾時は一切変更せず安全停止する
- `_export_and_drop_migration_log`を`_drop_migration_log_table`へ改名し、
  ファイル書き出しを安全性の根拠から外した(補助情報・ベストエフォート
  に格下げ)。テーブル削除の前提として、人間が事前に手動でエクスポート・
  保存・ハッシュ確認したことを示す`_precondition_migration_log_
  manually_archived`を新設し、Tier 3・17-4章の冒頭で必須の事前条件と
  した

```python
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
    """[第12次新設] 第21回終了時点との厳密な不一致を表す。"""


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
    """[第13次新設・点A対応] 「Tier 2完了状態(3ロールとも不在)」または
    「DEGRADED状態(一部ロールが安全に縮退済み: NOLOGIN化・カレントDB
    権限撤去済みだが、クロスDB依存で未DROPのまま残っている)」の
    いずれかから実行できる前提。前版の`_precondition_roles_absent`は
    「全部不在」しか受理しなかったため、DEGRADED後に正規の復旧経路が
    途切れる不具合があった。存在するロールが危険な状態(LOGIN可能な
    まま、またはカレントDBの権限が残ったまま)であれば拒否する。
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
    """[第13次新設・点A対応] 前回の実行がDEGRADEDで終わり、一部ロールが
    「安全に縮退した状態」で残っている場合に、他データベースへの依存が
    解消されたかを再確認し、解消されていればDROP ROLEする。まだ解消
    されていないロールがあっても例外は送出せず、戻り値の
    `still_degraded`で呼び出し元へ伝える(呼び出し元がDEGRADEDとして
    扱い、それまでに解消できた分の進捗はcommitできるようにするため)。
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
    """[第13次新設・点D対応] schema_migration_logの全行を、決定的な
    (キー順・区切り記号が固定の)JSON文字列へ正規化し、SHA-256を計算する。
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
    """[第13次訂正・点D対応] schema_migration_logの自動ファイル書き出し
    は、実行環境の永続性・DBトランザクションとの整合性を保証できない
    ため、Tier 3・17-4章(resume)の安全性の根拠として使わない。単なる
    真偽値(SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED=true)だけでは保存先・
    行数・内容の一致を検証できないという指摘を受け、事前に
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

    [第12次訂正] 戻り値へ`status`(`"complete"`または`"degraded"`)を
    追加した。呼び出し元(Tier 2・Tier 3のmain())はこれを見て、
    `[OK]`(COMPLETE)と`[DEGRADED]`を明確に区別して報告すること
    ——`degraded_roles`が空でないのに`[OK]`と表示してはならない。
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

    for signature in TEN_APP_DATA_OWNER_FUNCTION_SIGNATURES:
        cur.execute(f"ALTER FUNCTION public.{signature} OWNER TO postgres")

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
            "依存関係を人手で確認・解消したうえで、17-4章「Tier 2完了後の"
            "残存物撤去」相当の再開手順を実行してください。"
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
    """[第12次新設] schema_migration_logから、このTaskがこれまでに
    実際にNOT NULL・UNIQUEを追加したことがあるかを集計する。
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
    """[第13次訂正・点C対応] schema_migration_logの記録(このTaskが実際に
    added_not_null/added_uniqueをtrueにしたことがあるか)とライブ状態を
    突き合わせ、本Taskが追加したと確認できたものだけを撤去する。記録が
    欠落・矛盾する場合は一切変更せず安全停止する(stagingで事前確認した
    固定の前提だけに依存しない)。制約の同一性確認は12-2章の作成時
    チェックと同じ4項目(contype・convalidated・condeferrable・
    condeferred・conkey)をすべて揃えて確認する(前版はconkeyのみ)。
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
    """[第13次訂正] テーブルの削除を実行する前に
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
    """[第12次新設] Tier 3・17-4章の完了直前に呼び、第21回終了時点と
    完全に一致することを厳密にassertする。printするだけで成功扱いに
    せず、1つでも不一致なら`BaselineStateMismatchError`を送出する——
    呼び出し元のmain()はこれをcommitせずROLLBACKすること。
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
```

### 17-1. Tier 1: 緊急切り戻し(接続だけを戻す)

問題発生直後、最短時間でサービスを復旧させるための手順。ロール・関数・
RLSはstagingに残ったままでよい。

```
web・stripe-webhookのDATABASE_URL変数の参照先を、
${{Postgres.DATABASE_URL}}(本Task中に一切変更していない、postgresロール
用の既存参照)へ戻し、両サービスを再デプロイする。
```

**完了条件**: `web`・`stripe-webhook`ともHTTP 200・ログにDB接続エラー
なし。

### 17-2. Tier 2: 新設ロールの撤去(開始状態: 完全適用状態)

`scripts/rollback_tier2_remove_roles.py`。開始状態は**完全適用状態**
(3ロール・GRANT・RLSが存在)。12関数・制約・`schema_migration_log`には
触れない。

**終了コード(第12次新設、点1対応)**: `0`=COMPLETE(3ロールすべて撤去)
/ `1`=FAILED(例外・全体ROLLBACK) / `2`=DEGRADED(クロスDB依存により
NOLOGIN化・カレントDB権限撤去のみでcommit)。**`degraded_roles`が空で
ないのに`[OK]`や終了コード0を返してはならない。**

```python
"""scripts/rollback_tier2_remove_roles.py

終了コード: 0=COMPLETE(3ロールすべて撤去) / 1=FAILED(例外・全体ROLLBACK)
/ 2=DEGRADED(クロスDB依存によりNOLOGIN化・カレントDB権限撤去のみでcommit)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402
from rollback_helpers import (  # noqa: E402
    RollbackPreconditionError,
    _precondition_roles_exist,
    _remove_roles_and_rls,
)
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity  # noqa: E402


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)  # 0-1章、DDLより前に実行
            _precondition_roles_exist(cur)
            result = _remove_roles_and_rls(cur)
        conn.commit()
        if result["status"] == "complete":
            print(f"[OK] COMPLETE: Tier 2(新設ロールの撤去)が完了しました。 detail={result}")
            return 0
        print(f"[DEGRADED] Tier 2は完全には完了していません(クロスDB依存)。 detail={result}")
        return 2
    except (psycopg.Error, RollbackPreconditionError, RuntimeError, TargetDatabaseMismatchError) as e:
        conn.rollback()
        print(f"[FAILED] Tier 2の実行中にエラーが発生しました。変更はロールバックされました: {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

**完了条件**: 終了コード0(`[OK] COMPLETE`)まで到達し、
`result["after"]["existing_new_roles"]`が空リストであること。終了コード
2(`[DEGRADED]`)の場合は完了ではなく、Tier 1実施と他DB依存の解消が
必要(11章#5・#14)。

### 17-3. Tier 3: 第21回終了時点への全面復帰(開始状態: 完全適用状態)

`scripts/rollback_tier3_full_restore.py`。開始状態は**完全適用状態**
(Tier 2と同じ、Tier 2の後に続けて実行するものではない)。17-2章の
`_remove_roles_and_rls`に加え、12関数の削除・制約の再検証つき撤去・
監査ログテーブルの削除・`verify_round21_baseline_state`による厳密な
最終確認までを同一トランザクションで行う。

**終了コード(第12次新設、点2・3対応)**: `0`=COMPLETE(第21回終了時点と
完全一致・`verify_round21_baseline_state()`通過) / `1`=FAILED(例外・
全体ROLLBACK、baseline不一致を含む) / `2`=DEGRADED(クロスDB依存により
ロール撤去を完了できず、**関数・制約・ログの削除へは一切進まずに**
NOLOGIN化・カレントDB権限撤去のみでcommit)。

```python
"""scripts/rollback_tier3_full_restore.py

終了コード: 0=COMPLETE(第21回終了時点と完全一致・verify_round21_baseline_
state()通過) / 1=FAILED(例外・全体ROLLBACK、baseline不一致を含む) /
2=DEGRADED(クロスDB依存によりロール撤去を完了できず、関数・制約・ログの
削除へ進まずにNOLOGIN化・カレントDB権限撤去のみでcommit)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402
from rollback_helpers import (  # noqa: E402
    BaselineStateMismatchError,
    RollbackPreconditionError,
    _drop_all_functions,
    _drop_migration_log_table,
    _precondition_migration_log_manually_archived,
    _precondition_roles_exist,
    _remove_roles_and_rls,
    _reverify_and_drop_constraint,
    verify_round21_baseline_state,
)
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity  # noqa: E402


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)
            _precondition_roles_exist(cur)
            _precondition_migration_log_manually_archived(cur)

            role_rls_result = _remove_roles_and_rls(cur)
            if role_rls_result["degraded_roles"]:
                # [第12次訂正] クロスDB依存で完全なロール撤去ができな
                # かった場合、全面復帰処理(関数・制約・ログの削除)へは
                # 進まない。ここまでの安全な縮退(NOLOGIN化・カレントDB
                # 権限撤去)だけをcommitし、DEGRADEDとして停止する。
                conn.commit()
                print(
                    "[DEGRADED] Tier 3は第21回終了時点への全面復帰を完了できません"
                    "(クロスDB依存)。ロール・RLSの安全な縮退のみcommitしました。"
                    f" detail={role_rls_result}"
                )
                print(
                    "[案内] Tier 1(接続ロールをpostgresへ戻す)を実施し、他"
                    "データベースの依存関係を人手で解消したうえで、"
                    "17-4章「復旧(resume)」(rollback_resume_to_full_"
                    "restore.py)を再開手順として実行してください。"
                )
                return 2

            function_result = _drop_all_functions(cur)
            constraint_result = _reverify_and_drop_constraint(cur)
            log_result = _drop_migration_log_table(cur)
            final_state = verify_round21_baseline_state(cur)  # 不一致ならここで例外→ROLLBACK
        conn.commit()
        print("[OK] COMPLETE: Tier 3(第21回終了時点への全面復帰)が完了しました。")
        print(f"  role_rls={role_rls_result}")
        print(f"  functions={function_result}")
        print(f"  constraint={constraint_result}")
        print(f"  log_export={log_result}")
        print(f"  final_state={final_state}")
        return 0
    except (psycopg.Error, RollbackPreconditionError, RuntimeError,
            TargetDatabaseMismatchError, BaselineStateMismatchError) as e:
        conn.rollback()
        print(f"[FAILED] Tier 3の実行中にエラーが発生しました。変更はロールバックされました: {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

途中のいずれか(所有権移管・GRANT取消・`DROP ROLE`・関数削除・制約の
再検証・`verify_round21_baseline_state`による最終確認)が失敗した場合は
例外が送出され、`conn.rollback()`によりTier 3着手前の状態(ロール・
関数・制約・監査ログすべて)が維持される。DEGRADED(2)の場合はロール・
RLSの縮退のみが確定的にcommitされ、関数・制約・ログには一切触れて
いない。書き出し済みのローカルファイルが残ることはあるが、DB状態には
影響しない(17-0章の`_drop_migration_log_table`の注記を参照)。

**完了条件(1章①〜⑤との照合、テーブル・シーケンス名の完全一致・NOT NULL
復帰確認を含む)**: 終了コード0(`[OK] COMPLETE`)まで到達すること。
これは`verify_round21_baseline_state()`が例外を送出しなかったことと
同値であり、次のすべてを実測で満たす。

| 項目 | 第21回終了時点(1章) | Tier 3実行後の実測(`verify_round21_baseline_state`) |
|---|---|---|
| テーブル集合 | 7件(具体的な7テーブル名) | `table_names`が期待集合と完全一致 |
| シーケンス集合 | `records_id_seq`のみ | `sequence_names`が`{records_id_seq}`と完全一致 |
| カスタム関数の数 | 0件 | `custom_function_count: 0` |
| カスタムロール | `postgres`のみ | `existing_new_roles: []` |
| RLSポリシー数 | 0件 | `policy_count: 0` |
| RLS有効化状況 | 全テーブル`false` | `rls_enabled_table_count: 0` |
| `stripe_subscription_id`のUNIQUE制約 | 無し | `stripe_subscription_id_unique_constraint_names: []`(名前を問わない) |
| `stripe_subscription_id`のNOT NULL | 復帰済み(NULL許容) | `stripe_subscription_id_is_nullable: 'YES'` |
| `schema_migration_log` | 存在しない | `schema_migration_log_exists: False` |

この照合を、実際に15章のテストで実行する(11章#7・#21・#22で実測済み)。

### 17-3b. 監査ログの事前エクスポート(新設・点D対応、`scripts/export_and_hash_migration_log.py`)

**新設の要点**: `schema_migration_log`削除の安全性は、単なる真偽値
(`SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED=true`)ではなく、実際に
エクスポートした内容の行数・SHA-256との一致で担保する(17-0章
`_precondition_migration_log_manually_archived`)。この専用ツールは
読み取り専用(削除・変更は一切行わない)で、その行数・SHA-256を出力する。

```python
"""scripts/export_and_hash_migration_log.py

事前に手動実行し、schema_migration_logの内容を確定させ、行数・
SHA-256を出力する。削除・変更は一切行わない(読み取り専用)。

操作者はこの出力を保存し、Tier 3・17-4(resume)実行時に
SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNT・SCHEMA_MIGRATION_LOG_ARCHIVE_
SHA256として設定する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402
from rollback_helpers import _canonical_migration_log_export  # noqa: E402
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity  # noqa: E402


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)
            cur.execute("SELECT to_regclass('public.schema_migration_log') IS NOT NULL")
            if not cur.fetchone()[0]:
                print("[OK] schema_migration_logは存在しません(エクスポート不要)。")
                return 0
            _, canonical_text, digest, row_count = _canonical_migration_log_export(cur)
        conn.rollback()  # 読み取りのみ、DBへの変更は一切無い

        export_dir = Path("audit/schema_migration_log_archive")
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / f"schema_migration_log_export_{digest[:12]}.json"
        export_path.write_text(canonical_text, encoding="utf-8")

        print(f"[OK] エクスポート完了。 row_count={row_count} sha256={digest} path={export_path}")
        print("この内容を永続的な保存先(git・S3等)へ保存したうえで、")
        print("Tier 3・17-4(resume)実行時に次の環境変数を設定してください:")
        print(f"  SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNT={row_count}")
        print(f"  SCHEMA_MIGRATION_LOG_ARCHIVE_SHA256={digest}")
        return 0
    except (psycopg.Error, TargetDatabaseMismatchError) as e:
        conn.rollback()
        print(f"[FAILED] エクスポート中にエラーが発生しました: {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

### 17-4. 復旧(resume): Tier 2完了後、またはDEGRADED後の第21回終了時点への復帰(第13次全面訂正)

**訂正の要点(第13次・点A対応)**: 前版の`rollback_cleanup_after_tier2.py`
は「Tier 2完了後(3ロールとも不在)」しか受理せず、DEGRADED状態(一部
ロールだけ削除された状態)はTier 2の再実行条件(全ロール存在)にも
このスクリプトの開始条件(全ロール不在)にも合致せず、**正規の復旧経路が
途切れる**という指摘を受けた。`rollback_resume_to_full_restore.py`へ
一般化し、次の2つの開始状態のいずれも正しく受理するよう訂正した。

- Tier 2完了状態(3ロールとも不在)
- DEGRADED状態(一部ロールが「安全に縮退済み」: NOLOGIN化・カレントDB
  権限撤去済みだが、クロスDB依存で未DROPのまま残っている)

**終了コード**: `0`=COMPLETE(第21回終了時点と完全一致) / `1`=FAILED
(例外・全体ROLLBACK、baseline不一致を含む) / `2`=DEGRADED(他データ
ベースへの依存がまだ解消されていないロールが残っている。それまでに
解消できた分の進捗はcommitされる)。

```python
"""scripts/rollback_resume_to_full_restore.py

[第13次改訂・点A対応] 旧rollback_cleanup_after_tier2.pyを一般化した
「Tier 2完了状態」または「DEGRADED状態(一部ロールが安全に縮退済み)」
のいずれからでも実行できる、第21回終了時点への復旧スクリプト。

終了コード: 0=COMPLETE(第21回終了時点と完全一致) / 1=FAILED(例外・
全体ROLLBACK、baseline不一致を含む) / 2=DEGRADED(他データベースへの
依存がまだ解消されていないロールが残っている。それまでに解消できた
分の進捗はcommitされる)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402
from rollback_helpers import (  # noqa: E402
    BaselineStateMismatchError,
    RollbackPreconditionError,
    _drop_all_functions,
    _drop_migration_log_table,
    _finish_degraded_role_removal,
    _precondition_migration_log_manually_archived,
    _precondition_ready_to_resume,
    _reverify_and_drop_constraint,
    verify_round21_baseline_state,
)
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity  # noqa: E402


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)
            _precondition_ready_to_resume(cur)

            role_result = _finish_degraded_role_removal(cur)
            if role_result["still_degraded"]:
                # まだ他DB依存が解消されていないロールがある。それまでに
                # 解消できた分(newly_dropped)の進捗はcommitし、関数・
                # 制約・ログには一切触れずDEGRADEDとして停止する。
                conn.commit()
                print(
                    "[DEGRADED] まだ他データベースへの依存が解消されていない"
                    f"ロールがあります。 detail={role_result}"
                )
                print(
                    "[案内] 依存先データベースを人手で確認・解消したうえで、"
                    "このスクリプトを再実行してください。"
                )
                return 2

            _precondition_migration_log_manually_archived(cur)
            function_result = _drop_all_functions(cur)
            constraint_result = _reverify_and_drop_constraint(cur)
            log_result = _drop_migration_log_table(cur)
            final_state = verify_round21_baseline_state(cur)  # 不一致ならここで例外→ROLLBACK
        conn.commit()
        print("[OK] COMPLETE: 第21回終了時点への復旧が完了しました。")
        print(f"  role_removal={role_result}")
        print(f"  functions={function_result}")
        print(f"  constraint={constraint_result}")
        print(f"  log_export={log_result}")
        print(f"  final_state={final_state}")
        return 0
    except (psycopg.Error, RollbackPreconditionError, RuntimeError,
            TargetDatabaseMismatchError, BaselineStateMismatchError) as e:
        conn.rollback()
        print(f"[FAILED] 復旧処理中にエラーが発生しました。変更はロールバックされました: {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

**完了条件**: 終了コード0(`[OK] COMPLETE`)まで到達し、17-3章と同じ
`verify_round21_baseline_state`の表と一致することを確認する。終了
コード2(`[DEGRADED]`)の場合は、他データベースの依存関係を人手で解消
したうえで、このスクリプトを再実行する(11章#14b・#15bで実機確認済み)。

### 17-5. 適用範囲のまとめ

| スクリプト | 開始状態(事前条件) | 終了状態 |
|---|---|---|
| Tier 1(緊急切り戻し) | 接続切替直後に問題発生 | 接続先のみ変更。ロール・関数・RLS・制約・監査ログは残る |
| Tier 2(`rollback_tier2_remove_roles.py`) | **完全適用状態**(3ロール存在) | COMPLETE: 3ロール・関数所有権・GRANT・RLSが撤去。12関数定義・UNIQUE制約・監査ログは残る。DEGRADED: 一部ロールが安全に縮退したまま残る |
| Tier 3(`rollback_tier3_full_restore.py`) | **完全適用状態**(3ロール存在、Tier 2の後に続けて実行しない) | COMPLETE: 1章①〜⑤と一致する第21回終了時点。DEGRADED: ロール・RLSの縮退のみcommit、関数・制約・ログは無変更 |
| 復旧・resume(`rollback_resume_to_full_restore.py`) | **Tier 2完了状態**または**DEGRADED状態**のいずれか(3ロール不在、または一部が安全に縮退済み) | COMPLETE: Tier 3と同じ、1章①〜⑤と一致する状態。DEGRADED: 未解消の依存が残るロールがあれば、解消できた分だけ進めてcommitし再度DEGRADED |
| 監査ログエクスポート(`export_and_hash_migration_log.py`) | 任意(読み取り専用) | DBへの変更なし。行数・SHA-256を出力するのみ |

**選択の目安**: 設計自体を見直したいだけならTier 2のみでよい(関数・
制約は残したまま、ロールだけ元に戻す)。第21回終了時点まで完全に戻す
必要がある場合、完全適用状態からであれば直接Tier 3、既にTier 2・
Tier 3を実行済み(COMPLETEでもDEGRADEDでも)であれば「復旧(resume)」を
使う——**開始状態を問わず、これ1本が正規の復旧経路である**。
