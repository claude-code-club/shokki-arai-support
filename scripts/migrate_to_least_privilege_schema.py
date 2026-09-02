"""設計書16章の main() を、訂正後の順序でそのまま実装したもの。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402

import db  # noqa: E402
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity  # noqa: E402
from least_privilege_lib import (  # noqa: E402
    MissingPasswordError,
    RoleAttributeMismatchError,
    UnexpectedGranteeError,
    _read_required_password,
    create_or_replace_functions,
    enable_rls_and_policies,
    grant_schema_usage,
    grant_table_privileges,
    reassign_function_owners,
    reset_and_grant_execute_permissions,
    verify_all_function_grants,
    verify_or_create_nologin_role,
    verify_or_set_login_role_password,
)


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)
            verify_or_create_nologin_role(cur, "app_data_owner")

            app_runtime_password = _read_required_password("LEAST_PRIVILEGE_APP_RUNTIME_PASSWORD")
            app_webhook_password = _read_required_password("LEAST_PRIVILEGE_APP_WEBHOOK_PASSWORD")
            verify_or_set_login_role_password(cur, "app_runtime", app_runtime_password)
            verify_or_set_login_role_password(cur, "app_webhook", app_webhook_password)

            grant_schema_usage(cur)  # 6-0章
            grant_table_privileges(cur)  # 6-1章
            create_or_replace_functions(cur)  # 5章
            reassign_function_owners(cur)  # 訂正: GRANTリセット・ACL検証より前に行う
            reset_and_grant_execute_permissions(cur)  # 6-2章段階1
            verify_all_function_grants(cur)  # 6-2章段階2
            enable_rls_and_policies(cur)  # 7章
        conn.commit()
        print("[OK] 最小権限化スキーマの適用が完了しました。")
        return 0
    except (psycopg.Error, RoleAttributeMismatchError, UnexpectedGranteeError,
            MissingPasswordError, TargetDatabaseMismatchError) as e:
        conn.rollback()
        print(f"[NG] 適用中にエラーが発生しました。変更はロールバックされました: {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
