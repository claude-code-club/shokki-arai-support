"""岩瀬様のご指摘(2026-09-02、第11〜13次)を反映したモジュール。
migration/rollbackスクリプトの冒頭で必ず呼び出し、接続先を人間が確認
できる識別情報を出力したうえで、想定外の接続先ならDDL開始前に停止する。
接続文字列・パスワードはここでは一切扱わない(db.get_connection()の
内部にのみ存在し、このモジュールへは渡らない)。

[第13次訂正・点B] 環境識別を、操作者が両辺とも手入力する
EXPECTED_TARGET_ENVIRONMENT_ID / ACTUAL_TARGET_ENVIRONMENT_ID の比較
(独立性が無い)から、Railwayが`railway run`実行時に自動注入する
RAILWAY_PROJECT_ID・RAILWAY_ENVIRONMENT_ID(操作者が値を書く必要が
無い、Railway自身が供給する識別子)と、操作者が設定する期待値
(EXPECTED_RAILWAY_PROJECT_ID・EXPECTED_RAILWAY_ENVIRONMENT_ID)との
比較へ訂正した。実運用では、このスクリプト群は必ず`railway run`経由
(正しいプロジェクト/環境にリンクした状態)で実行すること。
"""
import os

EXPECTED_BASELINE_TABLES = {
    "records", "tenants", "tenant_memberships", "users",
    "tenant_subscriptions", "tenant_usage", "processed_stripe_events",
}


class TargetDatabaseMismatchError(Exception):
    pass


def verify_target_database_identity(cur):
    """接続先データベース名・接続ユーザー・PostgreSQLバージョン・想定7
    テーブルの存在を確認し、人間が目視確認できる形で標準出力へ表示する。

    次がすべて正しく設定・一致していない限り停止する(未設定を許容
    しない)。

    - EXPECTED_TARGET_DBNAME / 実際のdbnameと完全一致
    - EXPECTED_TARGET_USER / 実際のcurrent_userと完全一致
    - EXPECTED_RAILWAY_PROJECT_ID / RAILWAY_PROJECT_ID(Railway自動注入)と完全一致
    - EXPECTED_RAILWAY_ENVIRONMENT_ID / RAILWAY_ENVIRONMENT_ID(Railway自動注入)と完全一致
    - STAGING_DDL_EXPLICITLY_ALLOWED=true
    """
    cur.execute("SELECT current_database(), current_user, version()")
    dbname, user, version_string = cur.fetchone()

    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    actual_tables = {r[0] for r in cur.fetchall()}
    missing_tables = sorted(EXPECTED_BASELINE_TABLES - actual_tables)

    identity = {
        "current_database": dbname,
        "current_user": user,
        "server_version": version_string,
        "missing_expected_tables": missing_tables,
    }
    print(f"[接続先確認] {identity}")

    if missing_tables:
        raise TargetDatabaseMismatchError(
            f"接続先データベース({dbname})に想定するテーブルが見つかりません: "
            f"{missing_tables}。staging以外へ誤接続していないか、実行前に"
            "必ず人間が確認してください。DDLは一切実行していません。"
        )

    expected_dbname = os.environ.get("EXPECTED_TARGET_DBNAME", "").strip()
    if not expected_dbname:
        raise TargetDatabaseMismatchError(
            "EXPECTED_TARGET_DBNAMEが設定されていません。接続先データベース名を"
            "明示的に指定しない限り実行できません。DDLは一切実行していません。"
        )
    if dbname != expected_dbname:
        raise TargetDatabaseMismatchError(
            f"接続先データベース名が想定と異なります: 実際={dbname} "
            f"期待={expected_dbname}(EXPECTED_TARGET_DBNAME)。DDLは一切"
            "実行していません。"
        )

    expected_user = os.environ.get("EXPECTED_TARGET_USER", "").strip()
    if not expected_user:
        raise TargetDatabaseMismatchError(
            "EXPECTED_TARGET_USERが設定されていません。接続ユーザーを明示的に"
            "指定しない限り実行できません。DDLは一切実行していません。"
        )
    if user != expected_user:
        raise TargetDatabaseMismatchError(
            f"接続ユーザーが想定と異なります: 実際={user} "
            f"期待={expected_user}(EXPECTED_TARGET_USER)。DDLは一切"
            "実行していません。"
        )

    # [第13次訂正] Railway自動注入の識別子(railway run経由でのみ供給
    # される)と、操作者が設定した期待値を突き合わせる。独立性を確保する
    # ため、実測側(RAILWAY_PROJECT_ID等)は操作者が手入力するものでは
    # ない。
    expected_project_id = os.environ.get("EXPECTED_RAILWAY_PROJECT_ID", "").strip()
    if not expected_project_id:
        raise TargetDatabaseMismatchError(
            "EXPECTED_RAILWAY_PROJECT_IDが設定されていません。DDLは一切"
            "実行していません。"
        )
    actual_project_id = os.environ.get("RAILWAY_PROJECT_ID", "").strip()
    if not actual_project_id:
        raise TargetDatabaseMismatchError(
            "RAILWAY_PROJECT_IDが環境変数から取得できません。このスクリプトは"
            "必ず`railway run`経由(正しいプロジェクト/環境にリンクした状態)"
            "で実行してください。DDLは一切実行していません。"
        )
    if actual_project_id != expected_project_id:
        raise TargetDatabaseMismatchError(
            f"RAILWAY_PROJECT_IDが想定と異なります: 実際={actual_project_id} "
            f"期待={expected_project_id}。DDLは一切実行していません。"
        )

    expected_environment_id = os.environ.get("EXPECTED_RAILWAY_ENVIRONMENT_ID", "").strip()
    if not expected_environment_id:
        raise TargetDatabaseMismatchError(
            "EXPECTED_RAILWAY_ENVIRONMENT_IDが設定されていません。DDLは一切"
            "実行していません。"
        )
    actual_environment_id = os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip()
    if not actual_environment_id:
        raise TargetDatabaseMismatchError(
            "RAILWAY_ENVIRONMENT_IDが環境変数から取得できません。このスクリプト"
            "は必ず`railway run`経由で実行してください。DDLは一切実行して"
            "いません。"
        )
    if actual_environment_id != expected_environment_id:
        raise TargetDatabaseMismatchError(
            f"RAILWAY_ENVIRONMENT_IDが想定と異なります: "
            f"実際={actual_environment_id} 期待={expected_environment_id}。"
            "DDLは一切実行していません。"
        )

    staging_flag = os.environ.get("STAGING_DDL_EXPLICITLY_ALLOWED", "").strip().lower()
    if staging_flag != "true":
        raise TargetDatabaseMismatchError(
            "STAGING_DDL_EXPLICITLY_ALLOWED=trueが設定されていません"
            "(明示的なstaging DDL許可フラグが無いため停止します)。"
            "DDLは一切実行していません。"
        )

    return identity
