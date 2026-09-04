"""設計書12-3章の main() をそのまま実装。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402

import db  # noqa: E402
from constraint_lib import apply_stripe_subscription_id_constraint_if_needed  # noqa: E402
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity  # noqa: E402


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)
            result = apply_stripe_subscription_id_constraint_if_needed(cur)
        conn.commit()
        print(f"[OK] stripe_subscription_idのNOT NULL・UNIQUE制約の適用が完了しました。 detail={result}")
        return 0
    except (psycopg.Error, RuntimeError, TargetDatabaseMismatchError) as e:
        conn.rollback()
        print(f"[NG] 適用中にエラーが発生しました。変更はロールバックされました: {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
