"""第16〜20回(マルチテナント・認証基盤・Stripe課金・継続課金Webhook・
プラン制限メータリング)の本番migrationスクリプト5本が共通で使う、
接続先安全確認モジュール。

scripts/target_identity.py(第22課題・PostgreSQL最小権限化PR #29向け、
既にChatGPT監査を通過し稼働実績のあるモジュール)とは意図的に独立させた
別ファイルとする。理由:
- PR #29はまだ監査サイクル継続中(round 4合格・staging Phase 2.5待ち)であり、
  無関係な本タスクのためにそのファイルへ手を入れると、監査対象の差分が
  混ざってしまう
- target_identity.pyのverify_target_database_identity()は、EXPECTED_
  BASELINE_TABLESとして7テーブル全部(第16〜21回・第22回分すべて)の
  存在を前提にしている。これは「最小権限化を適用する時点では全部
  揃っている」という第22課題側の前提であり、本タスク(tenants自体が
  まだ存在しない状態からの初回適用)にはそのまま使えない

役割は同種(誤った接続先でのDDL実行を防ぐ)だが、対象とする5本の
migrationスクリプトはそれぞれ異なる前提テーブル状態を要求するため、
テーブル存在確認は verify_expected_tables_exist() として分離し、
各migrationスクリプトが自分の前提を明示的に渡して呼び出す。

呼び出し方(各migrationスクリプトのmain()冒頭で):
    from production_target_identity import (
        verify_production_migration_target,
        verify_expected_tables_exist,
    )
    identity = verify_production_migration_target(cur)
    verify_expected_tables_exist(cur, {"tenants"}, "第17回(認証基盤)")

必須の環境変数(すべて未設定・不一致ならDDL実行前に例外で停止する。
どれか1つでも欠けていれば安全側に倒して停止し、DDLは一切実行しない):
    EXPECTED_TARGET_DBNAME
    EXPECTED_TARGET_USER
    EXPECTED_RAILWAY_PROJECT_ID     (実測側はRAILWAY_PROJECT_ID、Railwayが
                                      `railway run`実行時に自動注入する値。
                                      操作者が値を書く必要はない)
    EXPECTED_RAILWAY_ENVIRONMENT_ID (実測側はRAILWAY_ENVIRONMENT_ID、同上)
    PRODUCTION_DDL_EXPLICITLY_ALLOWED=true
        (第22課題側のSTAGING_DDL_EXPLICITLY_ALLOWEDとは別名のフラグ。
        本番向けであることを名前で明示し、staging用フラグの使い回しで
        誤って本番DDLが動いてしまう事故を防ぐ)
    BACKUP_RESTORE_TEST_CONFIRMED=true
        (scripts/backup_and_restore_test.pyによるバックアップ・使い捨て
        環境への復元テストを、この実行の直前に人間が実際に完了・確認
        したことを示す明示フラグ。バックアップを取らずに本番migrationが
        走ってしまう事故を防ぐための、機械的に強制できる事前条件)

実運用では、このモジュールを使うスクリプト群は必ず`railway run`経由
(正しいプロジェクト/環境にリンクした状態)で実行すること。接続文字列・
パスワードはこのモジュールでは一切扱わない(db.get_connection()の内部
にのみ存在し、このモジュールへは渡らない)。
"""
import os


class ProductionTargetMismatchError(Exception):
    """接続先の識別・前提テーブルの確認に失敗した場合に送出される。
    この例外が送出された時点で、呼び出し元はDDLを一切実行していない
    (このモジュール自体はSELECTしか行わない)。
    """


def verify_production_migration_target(cur):
    """接続先データベース名・接続ユーザー・Railwayのプロジェクト/環境ID・
    明示的な本番DDL許可フラグを確認し、人間が目視確認できる形で標準出力へ
    表示する。次がすべて正しく設定・一致していない限り停止する
    (未設定を一切許容しない)。

    - EXPECTED_TARGET_DBNAME / 実際のcurrent_databaseと完全一致
    - EXPECTED_TARGET_USER / 実際のcurrent_userと完全一致
    - EXPECTED_RAILWAY_PROJECT_ID / RAILWAY_PROJECT_ID(Railway自動注入)と完全一致
    - EXPECTED_RAILWAY_ENVIRONMENT_ID / RAILWAY_ENVIRONMENT_ID(Railway自動注入)と完全一致
    - PRODUCTION_DDL_EXPLICITLY_ALLOWED=true

    戻り値: 接続先の識別情報を格納したdict(ログ・監査資料への転記用)。
    """
    cur.execute("SELECT current_database(), current_user, version()")
    dbname, user, version_string = cur.fetchone()

    identity = {
        "current_database": dbname,
        "current_user": user,
        "server_version": version_string,
    }
    print(f"[接続先確認(本番migration安全化)] {identity}")

    expected_dbname = os.environ.get("EXPECTED_TARGET_DBNAME", "").strip()
    if not expected_dbname:
        raise ProductionTargetMismatchError(
            "EXPECTED_TARGET_DBNAMEが設定されていません。接続先データベース名を"
            "明示的に指定しない限り実行できません。DDLは一切実行していません。"
        )
    if dbname != expected_dbname:
        raise ProductionTargetMismatchError(
            f"接続先データベース名が想定と異なります: 実際={dbname} "
            f"期待={expected_dbname}(EXPECTED_TARGET_DBNAME)。DDLは一切"
            "実行していません。"
        )

    expected_user = os.environ.get("EXPECTED_TARGET_USER", "").strip()
    if not expected_user:
        raise ProductionTargetMismatchError(
            "EXPECTED_TARGET_USERが設定されていません。接続ユーザーを明示的に"
            "指定しない限り実行できません。DDLは一切実行していません。"
        )
    if user != expected_user:
        raise ProductionTargetMismatchError(
            f"接続ユーザーが想定と異なります: 実際={user} "
            f"期待={expected_user}(EXPECTED_TARGET_USER)。DDLは一切"
            "実行していません。"
        )

    expected_project_id = os.environ.get("EXPECTED_RAILWAY_PROJECT_ID", "").strip()
    if not expected_project_id:
        raise ProductionTargetMismatchError(
            "EXPECTED_RAILWAY_PROJECT_IDが設定されていません。DDLは一切"
            "実行していません。"
        )
    actual_project_id = os.environ.get("RAILWAY_PROJECT_ID", "").strip()
    if not actual_project_id:
        raise ProductionTargetMismatchError(
            "RAILWAY_PROJECT_IDが環境変数から取得できません。このスクリプトは"
            "必ず`railway run`経由(正しいプロジェクト/環境にリンクした状態)"
            "で実行してください。DDLは一切実行していません。"
        )
    if actual_project_id != expected_project_id:
        raise ProductionTargetMismatchError(
            f"RAILWAY_PROJECT_IDが想定と異なります: 実際={actual_project_id} "
            f"期待={expected_project_id}。DDLは一切実行していません。"
        )

    expected_environment_id = os.environ.get("EXPECTED_RAILWAY_ENVIRONMENT_ID", "").strip()
    if not expected_environment_id:
        raise ProductionTargetMismatchError(
            "EXPECTED_RAILWAY_ENVIRONMENT_IDが設定されていません。DDLは一切"
            "実行していません。"
        )
    actual_environment_id = os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip()
    if not actual_environment_id:
        raise ProductionTargetMismatchError(
            "RAILWAY_ENVIRONMENT_IDが環境変数から取得できません。このスクリプト"
            "は必ず`railway run`経由で実行してください。DDLは一切実行して"
            "いません。"
        )
    if actual_environment_id != expected_environment_id:
        raise ProductionTargetMismatchError(
            f"RAILWAY_ENVIRONMENT_IDが想定と異なります: "
            f"実際={actual_environment_id} 期待={expected_environment_id}。"
            "DDLは一切実行していません。"
        )

    production_flag = os.environ.get("PRODUCTION_DDL_EXPLICITLY_ALLOWED", "").strip().lower()
    if production_flag != "true":
        raise ProductionTargetMismatchError(
            "PRODUCTION_DDL_EXPLICITLY_ALLOWED=trueが設定されていません"
            "(明示的な本番DDL許可フラグが無いため停止します)。"
            "DDLは一切実行していません。"
        )

    backup_restore_confirmed = (
        os.environ.get("BACKUP_RESTORE_TEST_CONFIRMED", "").strip().lower()
    )
    if backup_restore_confirmed != "true":
        raise ProductionTargetMismatchError(
            "BACKUP_RESTORE_TEST_CONFIRMED=trueが設定されていません。"
            "scripts/backup_and_restore_test.pyによるバックアップ・復元"
            "テストを実行前に完了・確認してください。DDLは一切実行して"
            "いません。"
        )

    return identity


def verify_expected_tables_exist(cur, expected_tables, migration_label):
    """呼び出し元(各migrationスクリプト)が、自分の実行前提として必要な
    テーブル集合を渡して、DDL実行前に存在確認する共通ヘルパー。

    expected_tables: 前提として存在すべきテーブル名のset。
    migration_label: エラーメッセージに表示する回の名前
        (例: "第17回(認証基盤)")。

    見つからないテーブルがあれば、正しい実行順序(第16回→17回→18回→
    19回→20回)を守っていない可能性が高いと判断し、DDL実行前に例外で
    停止する。
    """
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    actual_tables = {r[0] for r in cur.fetchall()}
    missing = sorted(set(expected_tables) - actual_tables)
    if missing:
        raise ProductionTargetMismatchError(
            f"{migration_label}の前提テーブルが見つかりません: {missing}。"
            "正しい実行順序(第16回→第17回→第18回→第19回→第20回)を"
            "守っているか確認してください。DDLは一切実行していません。"
        )


def snapshot_table_names(cur):
    """public スキーマの現在のテーブル名集合を返す(既に実行済みかどうかを
    呼び出し元が判断するための補助。例外は送出しない、単なる読み取り)。
    """
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {r[0] for r in cur.fetchall()}
