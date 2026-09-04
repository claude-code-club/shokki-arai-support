"""設計書17-3章(第13次改訂版)をそのまま実装。

終了コード: 0=COMPLETE(第21回終了時点と完全一致・verify_round21_baseline_
state()通過) / 1=FAILED(例外・全体ROLLBACK、baseline不一致を含む) /
2=DEGRADED(クロスDB依存によりロール撤去を完了できず、関数・制約・ログの
削除へ進まずにNOLOGIN化・カレントDB権限撤去のみでcommit)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
