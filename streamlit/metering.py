"""世帯単位の月間使用量メータリング窓口（第20回: プラン制限とメータリング）。

Standard契約(billing.has_standard_access()がTrue)の世帯は無制限、それ以外
(Free・past_due等)は月ごとの上限を強制する。上限判定と加算は1回の原子的なSQL
(db.increment_tenant_usage_if_under_limit())で行い、「読み込み→Python側で加算→保存」
という競合に弱い方式は使わない(仕様書/プラン制限・メータリング設計.md⑤参照)。

月の区切りは日本時間(Asia/Tokyo)を基準にする。period_startはその月の1日を表す
DATE値(タイムゾーン非依存の暦月キー)。created_at/updated_atはTIMESTAMPTZ(UTC)のまま。

DB接続・クエリが失敗した場合は例外を送出し、呼び出し元(app.py)が「Standardではない
・利用不可」として扱う(fail closed。仕様書/プラン制限・メータリング設計.md⑨参照)。
"""

import psycopg

import billing
import db
from logic import today_jst

MONTHLY_REFLECTION_METRIC = "monthly_reflection"
MONTHLY_REFLECTION_FREE_LIMIT = 3


class MeteringUnavailableError(Exception):
    """PostgreSQLへの接続・読み書きに失敗した場合。有料機能は誤って開放しない。"""


class UsageLimitExceededError(Exception):
    """Free(または非Standard状態)の上限に達しており、これ以上加算できない場合。"""


def current_period_start():
    """日本時間での「今月1日」を返す(datetime.date)。"""
    return today_jst().replace(day=1)


def get_usage_count(conn, *, tenant_id, metric_key, period_start):
    return db.get_tenant_usage_count(
        conn, tenant_id=tenant_id, metric_key=metric_key, period_start=period_start
    )


def check_and_increment_usage(conn, *, tenant_id, metric_key, period_start, limit):
    """使用回数を原子的に1件加算する(commitはしない、呼び出し元が行う)。

    上限に達していればUsageLimitExceededErrorを送出する(DBへは書き込まれない)。
    limit=Noneは無制限(Standard向け)。
    戻り値: 加算後の使用回数。
    """
    new_count = db.increment_tenant_usage_if_under_limit(
        conn,
        tenant_id=tenant_id,
        metric_key=metric_key,
        period_start=period_start,
        limit=limit,
    )
    if new_count is None:
        raise UsageLimitExceededError(f"{metric_key}の今月の利用回数が上限に達しています。")
    return new_count


def get_monthly_reflection_status(conn, *, tenant_id):
    """conn受け取り版のコアロジック(SQL操作のみ、commitはしない)。テストではこちらを
    直接呼ぶことで、隔離スキーマへ接続済みのconnをそのまま使い回せる。

    戻り値: {"has_standard_access", "usage_count", "limit", "period_start"}の辞書。
    limitはFreeなら3、Standardなら表示上None(無制限)。
    """
    plan_status = billing.get_plan_status(conn, tenant_id=tenant_id)
    standard = billing.has_standard_access(plan_status)
    period_start = current_period_start()
    usage_count = get_usage_count(
        conn,
        tenant_id=tenant_id,
        metric_key=MONTHLY_REFLECTION_METRIC,
        period_start=period_start,
    )
    return {
        "has_standard_access": standard,
        "usage_count": usage_count,
        "limit": None if standard else MONTHLY_REFLECTION_FREE_LIMIT,
        "period_start": period_start,
    }


def use_monthly_reflection_with_conn(conn, *, tenant_id):
    """conn受け取り版のコアロジック(SQL操作のみ、commitはしない)。

    Standardは無制限(上限チェックはしない)。Freeは月3回まで、上限に達していれば
    UsageLimitExceededErrorを送出しDBへは書き込まない。
    戻り値: 消費後の使用回数。
    """
    plan_status = billing.get_plan_status(conn, tenant_id=tenant_id)
    standard = billing.has_standard_access(plan_status)
    period_start = current_period_start()
    limit = None if standard else MONTHLY_REFLECTION_FREE_LIMIT
    return check_and_increment_usage(
        conn,
        tenant_id=tenant_id,
        metric_key=MONTHLY_REFLECTION_METRIC,
        period_start=period_start,
        limit=limit,
    )


# --- app.pyから使う、DB接続を自前で管理するラッパー(billing.pyと同じ方針) ---


def _get_postgres_connection():
    try:
        return db.get_connection()
    except db.DatabaseNotConfiguredError as e:
        raise MeteringUnavailableError(str(e)) from e
    except psycopg.Error as e:
        raise MeteringUnavailableError("PostgreSQLへの接続に失敗しました。") from e


def fetch_monthly_reflection_status(tenant_id):
    """app.pyから呼ぶ入口。接続は自前で開閉する。"""
    conn = _get_postgres_connection()
    try:
        result = get_monthly_reflection_status(conn, tenant_id=tenant_id)
        conn.commit()
        return result
    except psycopg.Error as e:
        conn.rollback()
        raise MeteringUnavailableError("利用状況の取得に失敗しました。") from e
    finally:
        conn.close()


def use_monthly_reflection(tenant_id):
    """app.pyから呼ぶ入口。接続は自前で開閉する。"""
    conn = _get_postgres_connection()
    try:
        new_count = use_monthly_reflection_with_conn(conn, tenant_id=tenant_id)
        conn.commit()
        return new_count
    except UsageLimitExceededError:
        conn.rollback()
        raise
    except psycopg.Error as e:
        conn.rollback()
        raise MeteringUnavailableError("利用状況の更新に失敗しました。") from e
    finally:
        conn.close()
