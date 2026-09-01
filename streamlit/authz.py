"""世帯ロールの認可チェックを1箇所に集約する窓口（第21回: SaaSのセキュリティ堅牢化）。

billing.py・storage.pyがそれぞれ個別に持っていた「role != "admin"なら拒否する」
という判定を、この1関数へ統一する。呼び出し元は自分のモジュールの例外型で
ラップしてよい（PermissionDeniedError(str(e))のように再送出する）ため、
既存の例外型・エラーメッセージの互換性は変えない。
"""


class NotAdminError(Exception):
    """roleがadminでない場合に送出される、共通の認可エラー。"""


def require_admin(role):
    """roleが"admin"でなければNotAdminErrorを送出する。admin専用操作の呼び出し
    直前に必ず呼ぶこと(UIの非表示だけに頼らない、サーバー側の最終防衛線)。
    """
    if role != "admin":
        raise NotAdminError("この操作にはadmin権限が必要です。")
