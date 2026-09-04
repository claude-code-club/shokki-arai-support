"""[第13次改訂・点A対応] 旧rollback_cleanup_after_tier2.pyを一般化した
「Tier 2完了状態」または「DEGRADED状態(一部ロールが安全に縮退済み)」
のいずれからでも実行できる、第21回終了時点への復旧スクリプト。

前版は「Tier 2完了後(3ロールとも不在)」しか受理せず、DEGRADED後
(一部ロールだけ削除された状態)がTier 2の再実行条件にもこのスクリプト
の開始条件にも合致しないという指摘を受け、両方の開始状態を正しく受理
するよう訂正した。

終了コード: 0=COMPLETE(第21回終了時点と完全一致) / 1=FAILED(例外・
全体ROLLBACK、baseline不一致を含む) / 2=DEGRADED(他データベースへの
依存がまだ解消されていないロールが残っている。それまでに解消できた
分の進捗はcommitされる)。
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
