"""初回の管理者ユーザーを、指定した世帯へadminとして紐付ける、一回限りの管理スクリプト。

未登録ユーザーを自動的にどこかの世帯へ参加させることは一切行わない
(仕様書/認証基盤設計.md⑥参照)。実行にはauth_subject(Auth0にログイン済みの
本人のsub)とtenant_id(UUID)を明示的に渡すことを必須とする。

auth_subjectはAuth0ダッシュボードのUser Management、または対象者が一度
ログインした後にst.userの内容から確認する(operator本人が確認・入力すること。
このスクリプト自体はAuth0への接続を必要としない)。
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402


def bootstrap_admin_membership(
    auth_subject, tenant_id, conn=None, email=None, email_verified=False
):
    """指定したauth_subjectのユーザーを、指定したtenant_idへadminとして紐付ける。

    tenant_idが存在しない場合はValueErrorを送出する(誤ったtenant_idで新規に
    孤立したmembershipを作らないため)。戻り値はuser_id。
    """
    if not auth_subject or not isinstance(auth_subject, str):
        raise TypeError("auth_subjectは空でない文字列で渡してください。")
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_idはuuid.UUIDのインスタンスで渡してください。")

    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM tenants WHERE id = %s", (tenant_id,))
            if cur.fetchone() is None:
                raise ValueError(f"指定したtenant_idはtenantsに存在しません: {tenant_id}")

        user_id = db.get_or_create_user(
            conn, auth_subject=auth_subject, email=email, email_verified=email_verified
        )
        db.create_membership(conn, tenant_id=tenant_id, user_id=user_id, role="admin")
        conn.commit()
        return user_id
    except psycopg.Error:
        conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()


def main(argv):
    if len(argv) != 3:
        print(
            "使い方: python scripts/bootstrap_admin_membership.py <auth_subject> <tenant_idのUUID>"
        )
        return 1

    auth_subject = argv[1]
    try:
        tenant_id = uuid.UUID(argv[2])
    except ValueError:
        print(f"[NG] tenant_idがUUID形式ではありません: {argv[2]!r}")
        return 1

    try:
        user_id = bootstrap_admin_membership(auth_subject, tenant_id)
    except ValueError as e:
        print(f"[NG] {e}")
        return 1
    except db.DatabaseNotConfiguredError as e:
        print(f"[NG] {e}")
        return 1
    except psycopg.Error:
        print("[NG] PostgreSQLへの接続または操作に失敗しました。")
        return 1

    print(f"[OK] adminとして紐付けました。user_id={user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
