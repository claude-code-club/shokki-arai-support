"""Streamlit標準OIDC(st.login/st.user/st.logout)とAuth0を使った認証窓口。

第17回(認証基盤)。Auth0がアカウント作成・メール確認・パスワード保存・
パスワード再設定・ログアウトを担う。アプリDBはパスワードを一切保存せず、
Auth0のOIDC subject(sub)をユーザー識別子として保存し、「どのユーザーが
どの世帯にどのroleで所属しているか」(tenant_memberships)だけを管理する
(仕様書/認証基盤設計.md②参照)。

AUTH_ENABLED(既定は未設定=無効)がtrueの場合のみこのモジュールの認証フローが
有効になる。未設定の間は第16回のDEFAULT_TENANT_ID方式のまま動作する
(仕様書/認証基盤設計.md⑪参照)。

tenant_idはブラウザ入力・URLパラメータからは一切決めない。ログインユーザーの
subから、resolve_tenant_context()がDBに保存されたmembershipだけを根拠に
サーバー側で解決する。
"""

import os

import psycopg

import db

AUTH_ENABLED_ENV = "AUTH_ENABLED"


class AccessDeniedError(Exception):
    """認証済みだが、世帯への所属(membership)が無い、または複数あって現状未対応の場合。"""


def is_auth_enabled():
    return os.environ.get(AUTH_ENABLED_ENV, "").strip().lower() == "true"


def resolve_tenant_context(*, auth_subject, email, email_verified, conn):
    """auth_subjectのユーザーを作成・更新し、所属する世帯とroleを1件だけ確定して返す。

    tenant_idはこの関数の中だけで、DBに保存されたmembershipから決定する
    (仕様書/認証基盤設計.md⑦参照)。Streamlitの実行時コンテキストに依存しない
    純粋なDB操作関数のため、実際のAuth0接続が無くてもテストできる。

    戻り値: (tenant_id, role)のタプル。

    - membershipが0件: AccessDeniedError(どの世帯にも所属していない)
    - membershipが2件以上: AccessDeniedError(第17回では複数世帯対応は行わない)
    """
    try:
        user_id = db.get_or_create_user(
            conn, auth_subject=auth_subject, email=email, email_verified=email_verified
        )
        memberships = db.get_memberships_for_user(conn, user_id=user_id)
        conn.commit()
    except psycopg.Error:
        conn.rollback()
        raise

    if len(memberships) == 0:
        raise AccessDeniedError(
            "どの世帯にも所属していません。管理者にお問い合わせください。"
        )
    if len(memberships) > 1:
        raise AccessDeniedError(
            "複数世帯への所属には現在対応していません。管理者にお問い合わせください。"
        )

    tenant_id, role = memberships[0]
    return tenant_id, role


def require_login_and_resolve_tenant():
    """app.pyから呼ぶ入口。

    未ログインならst.login()導線を表示し、(None, None)を返す
    (呼び出し側でst.stop()すること)。ログイン済みならtenant_id・roleを
    resolve_tenant_context()で解決して返す(AccessDeniedErrorはそのまま伝播する)。
    """
    import streamlit as st  # Streamlit実行時コンテキストが必要な部分だけここに閉じ込める

    if not st.user.is_logged_in:
        st.button("ログイン", on_click=st.login)
        return None, None

    conn = db.get_connection()
    try:
        tenant_id, role = resolve_tenant_context(
            auth_subject=st.user["sub"],
            email=st.user.get("email"),
            email_verified=bool(st.user.get("email_verified", False)),
            conn=conn,
        )
    finally:
        conn.close()
    return tenant_id, role
