"""設計書17-2章(第13次改訂版)をそのまま実装。

終了コード: 0=COMPLETE(3ロールすべて撤去) / 1=FAILED(例外・全体ROLLBACK)
/ 2=DEGRADED(クロスDB依存によりNOLOGIN化・カレントDB権限撤去のみでcommit)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
            verify_target_database_identity(cur)
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
