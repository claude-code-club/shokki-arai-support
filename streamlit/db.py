"""PostgreSQL版の記録データ読み書き。

logic.py（JSON版）と並行して存在し、storage.py（保存方式の切り替え窓口）から使う。

このモジュールはSQL操作のみを行い、commit・rollbackは一切呼ばない。
トランザクションの確定・取消は必ず呼び出し元（storage.pyや
scripts/migrate_to_postgres.py）が行う（仕様書/保存方式切り替え設計.md ②-b参照）。
理由: 移行スクリプトで「書き込み→照合」を1つのトランザクションにまとめ、照合が
一致した場合だけ確定できるようにするため。db.py自身が書き込み直後にcommitして
しまうと、後続の照合で不一致が見つかっても書き込みが先に確定してしまう。

第16回（マルチテナント設計）以降、通常のアプリ操作（storage.py経由）は
_for_tenantが付いた関数（tenant_id必須のキーワード専用引数）を使う。
ensure_schema()・load_dates()・insert_dates()・save_dates()（tenant_id無し）は、
tenant_id列を追加する前の一度きりの移行（scripts/migrate_to_postgres.py・
scripts/migrate_to_tenant_schema.py）専用に残しており、テナント対応スキーマが
既に存在する環境の通常操作では使わない（仕様書/マルチテナント設計.md⑤参照）。
"""

import os
import uuid

import psycopg

DATABASE_URL_ENV = "DATABASE_URL"


class DatabaseNotConfiguredError(Exception):
    """DATABASE_URL環境変数が設定されていない場合に送出される。"""


def is_configured():
    return bool(os.environ.get(DATABASE_URL_ENV))


def get_connection():
    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        raise DatabaseNotConfiguredError(
            f"{DATABASE_URL_ENV}が設定されていません。PostgreSQLへは接続できません。"
        )
    return psycopg.connect(url)


def ensure_schema(conn):
    """recordsテーブルが無ければ作成する(SQL操作のみ、commitしない)。何度呼んでも安全(IF NOT EXISTS)。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                id          BIGSERIAL PRIMARY KEY,
                record_date DATE NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS records_record_date_unique
            ON records (record_date)
            """
        )


def load_dates(conn):
    """記録日の集合を返す(ISO日付文字列)。"""
    with conn.cursor() as cur:
        cur.execute("SELECT record_date FROM records")
        return {row[0].isoformat() for row in cur.fetchall()}


def insert_dates(dates, conn):
    """与えた日付集合を追加する(削除は行わない、追加専用。SQL操作のみ)。

    record_dateのUNIQUE制約とON CONFLICT DO NOTHINGにより、
    既に存在する日付を渡しても何度でも安全に再実行できる(冪等)。
    移行スクリプト(migrate_to_postgres.py)から使う。
    """
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO records (record_date) VALUES (%s) ON CONFLICT (record_date) DO NOTHING",
            [(d,) for d in sorted(dates)],
        )


def save_dates(dates, conn):
    """記録日の集合を、渡された内容と完全に一致するよう置き換える(SQL操作のみ)。

    logic.save_dates()と同じ「全体を置き換える」呼び出し方に合わせた関数。
    削除も行うため、移行スクリプトでは使わずinsert_dates()を使うこと。
    """
    dates = set(dates)
    existing = load_dates(conn)
    to_delete = sorted(existing - dates)
    to_add = sorted(dates - existing)

    with conn.cursor() as cur:
        if to_delete:
            cur.execute(
                "DELETE FROM records WHERE record_date = ANY(%s::date[])",
                (to_delete,),
            )
        if to_add:
            cur.executemany(
                "INSERT INTO records (record_date) VALUES (%s) ON CONFLICT (record_date) DO NOTHING",
                [(d,) for d in to_add],
            )


def load_dates_for_tenant(conn, *, tenant_id):
    """指定したtenant_idの記録日集合を返す(SQL操作のみ、commitしない)。

    tenant_idはデフォルト値なしのキーワード専用引数(呼び忘れるとTypeErrorになる。
    仕様書/マルチテナント設計.md⑤参照)。第16回以降、storage.py経由の通常操作は
    すべてこちらを使う。ensure_schema()と違い、テナント対応スキーマ(migrate_to_
    tenant_schema.py実行後)が既に存在する前提で、スキーマの作成・変更は行わない。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT record_date FROM records WHERE tenant_id = %s",
            (tenant_id,),
        )
        return {row[0].isoformat() for row in cur.fetchall()}


def insert_date_for_tenant(record_date, conn, *, tenant_id):
    """指定したtenant_idへ、1件の記録日だけを原子的に追加する(SQL操作のみ)。

    UNIQUE(tenant_id, record_date)とON CONFLICT DO NOTHINGにより、
    既に存在する場合は無視される(冪等)。日付集合全体を読み込んで置き換える
    save_dates_for_tenant()と違い、他の記録に一切触れないため、同じ世帯の
    複数端末からのほぼ同時操作でもロスト・アップデートが起きにくい
    (仕様書/マルチテナント設計.md⑩参照)。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO records (tenant_id, record_date) VALUES (%s, %s) "
            "ON CONFLICT (tenant_id, record_date) DO NOTHING",
            (tenant_id, record_date),
        )


def delete_date_for_tenant(record_date, conn, *, tenant_id):
    """指定したtenant_idから、1件の記録日だけを原子的に削除する(SQL操作のみ)。

    tenant_idとrecord_dateの両方をWHERE条件にするため、他テナントの同じ日付や、
    指定と異なるテナントの行には一切影響しない。
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM records WHERE tenant_id = %s AND record_date = %s",
            (tenant_id, record_date),
        )


def save_dates_for_tenant(dates, conn, *, tenant_id):
    """指定したtenant_id配下の記録日集合だけを、渡された内容と完全に一致するよう
    置き換える(SQL操作のみ)。他テナントの行には一切触れない。
    """
    dates = set(dates)
    existing = load_dates_for_tenant(conn, tenant_id=tenant_id)
    to_delete = sorted(existing - dates)
    to_add = sorted(dates - existing)

    with conn.cursor() as cur:
        if to_delete:
            cur.execute(
                "DELETE FROM records WHERE tenant_id = %s AND record_date = ANY(%s::date[])",
                (tenant_id, to_delete),
            )
        if to_add:
            cur.executemany(
                "INSERT INTO records (tenant_id, record_date) VALUES (%s, %s) "
                "ON CONFLICT (tenant_id, record_date) DO NOTHING",
                [(tenant_id, d) for d in to_add],
            )


def compare_date_sets(json_dates, db_dates):
    """2つの日付集合(set[str])を比較し、完全一致するかと差分を返す。

    DB接続を必要としない純粋関数(ダミーデータだけでテストできる)。
    移行前後の検証(件数だけでなく中身の完全一致)に使う。
    """
    json_dates = set(json_dates)
    db_dates = set(db_dates)
    return {
        "match": json_dates == db_dates,
        "only_in_json": sorted(json_dates - db_dates),
        "only_in_db": sorted(db_dates - json_dates),
    }


# --- 第17回(認証基盤): users・tenant_memberships(SQL操作のみ、commitしない) ---


def get_or_create_user(conn, *, auth_subject, email, email_verified):
    """auth_subjectで既存ユーザーを検索し、無ければ作成する。user_idを返す。

    emailとemail_verifiedは、既存ユーザーでも呼び出しのたびにAuth0側の最新値で
    同期する(アプリDB側は表示・監査用のキャッシュに過ぎず、真実の源はAuth0。
    仕様書/認証基盤設計.md②参照)。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE auth_subject = %s", (auth_subject,))
        row = cur.fetchone()
        if row is not None:
            user_id = row[0]
            cur.execute(
                "UPDATE users SET email = %s, email_verified = %s WHERE id = %s",
                (email, email_verified, user_id),
            )
            return user_id

        user_id = uuid.uuid4()
        cur.execute(
            "INSERT INTO users (id, auth_subject, email, email_verified) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, auth_subject, email, email_verified),
        )
        return user_id


def get_memberships_for_user(conn, *, user_id):
    """指定ユーザーの世帯所属一覧を[(tenant_id, role), ...]で返す。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, role FROM tenant_memberships WHERE user_id = %s",
            (user_id,),
        )
        return cur.fetchall()


def create_membership(conn, *, tenant_id, user_id, role):
    """世帯への所属を作成する(冪等: 既に存在すれば何もしない、SQL操作のみ)。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES (%s, %s, %s) "
            "ON CONFLICT (tenant_id, user_id) DO NOTHING",
            (tenant_id, user_id, role),
        )


def update_tenant_name(conn, *, tenant_id, name):
    """世帯名を変更する(SQL操作のみ)。呼び出し元がrole検証を行うこと(admin専用操作)。"""
    with conn.cursor() as cur:
        cur.execute("UPDATE tenants SET name = %s WHERE id = %s", (name, tenant_id))
