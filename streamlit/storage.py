"""保存方式(JSON / PostgreSQL)を、環境変数STORAGE_BACKENDで明示的に切り替える。

DATABASE_URLの有無ではなくSTORAGE_BACKENDの値だけで判断する。将来DATABASE_URLが
存在するだけで意図せず保存先が切り替わる事故を防ぐため。

PostgreSQL障害時にJSONへ自動フォールバックすることは行わない(二重管理・データ
不整合を避けるため)。障害時は例外を送出し、app.py側でエラー表示してst.stop()する、
既存のRecordsFileCorruptedError処理と同じ扱いにする。

commit/rollbackの責任はここ(storage.py)が持つ。db.pyはSQL操作のみを行い、
commit/rollbackを一切呼ばない(仕様書/保存方式切り替え設計.md ②-b参照)。

第16回(マルチテナント設計)以降、postgresバックエンドの操作はすべてtenant_idを
必須にする。jsonバックエンドは単一テナント前提のまま据え置くため、tenant_idの
概念を持たない(常にNoneとして扱う。仕様書/マルチテナント設計.md⑨参照)。
tenant_id自体は環境変数DEFAULT_TENANT_ID(UUID)から取得する(get_tenant_id())。
第17回でログイン機能ができたら、ログインセッション由来の値に差し替える想定。
"""

import os
import uuid

import psycopg

import db
import logic
from logic import RecordsFileCorruptedError  # noqa: F401 (app.pyから再利用)

STORAGE_BACKEND_ENV = "STORAGE_BACKEND"
VALID_BACKENDS = ("json", "postgres")
DEFAULT_TENANT_ID_ENV = "DEFAULT_TENANT_ID"


class StorageConfigError(Exception):
    """STORAGE_BACKENDの値が不正、postgres指定なのにDATABASE_URL未設定、
    またはpostgresバックエンドでtenant_id(DEFAULT_TENANT_ID)が未設定・不正な場合。
    """


class StorageUnavailableError(Exception):
    """PostgreSQLへの接続・読み書きに失敗した場合。JSONへは自動フォールバックしない。"""


def get_backend_name():
    """STORAGE_BACKENDを検証して返す。未設定時のみ'json'を返し、それ以外の不正値は
    すべてStorageConfigErrorを送出する(jsonへのフォールバックは行わない)。
    """
    backend = os.environ.get(STORAGE_BACKEND_ENV, "json").strip().lower()
    if backend not in VALID_BACKENDS:
        raise StorageConfigError(
            f"{STORAGE_BACKEND_ENV}の値が不正です: {backend!r}（json または postgres）"
        )
    if backend == "postgres" and not db.is_configured():
        raise StorageConfigError(
            "STORAGE_BACKEND=postgres が指定されていますが、DATABASE_URLが設定されていません。"
        )
    return backend


def get_tenant_id():
    """postgresバックエンドの場合のみDEFAULT_TENANT_IDを検証して返す。

    jsonバックエンドは単一テナント前提のまま据え置くため、常にNoneを返す
    (JSON側にtenant_idという概念自体を持ち込まない)。postgresバックエンドで
    DEFAULT_TENANT_IDが未設定・空文字・不正なUUID形式の場合はすべて
    StorageConfigErrorを送出する。固定値へのフォールバックや自動生成、
    「既存の先頭テナントを自動選択」は一切行わない(仕様書/マルチテナント設計.md①参照)。
    """
    if get_backend_name() == "json":
        return None
    raw = os.environ.get(DEFAULT_TENANT_ID_ENV, "").strip()
    if not raw:
        raise StorageConfigError(f"{DEFAULT_TENANT_ID_ENV}が設定されていません。")
    try:
        return uuid.UUID(raw)
    except ValueError as e:
        raise StorageConfigError(
            f"{DEFAULT_TENANT_ID_ENV}がUUID形式ではありません: {raw!r}"
        ) from e


def _require_tenant_id(tenant_id):
    """postgresバックエンドの操作はtenant_idを必須にする(呼び忘れを確実に検知する)。"""
    if tenant_id is None:
        raise StorageConfigError("postgresバックエンドではtenant_idの指定が必須です。")
    if not isinstance(tenant_id, uuid.UUID):
        raise TypeError("tenant_idはuuid.UUIDのインスタンスを渡してください。")


def _get_postgres_connection():
    try:
        return db.get_connection()
    except db.DatabaseNotConfiguredError as e:
        raise StorageConfigError(str(e)) from e
    except psycopg.Error as e:
        # 接続自体の失敗(ホスト到達不可・タイムアウト等)。connがまだ無いのでcloseは不要。
        raise StorageUnavailableError("PostgreSQLへの接続に失敗しました。") from e


def load_dates(tenant_id=None):
    if get_backend_name() == "json":
        # logic.DATA_FILEを呼び出し時に明示的に読む(logic.load_dates()の既定引数に
        # 頼らない)。既定引数はモジュール読み込み時に固定されるため、テストで
        # logic.DATA_FILEを差し替えても反映されず、本番パスへ書き込む事故につながる。
        return logic.load_dates(data_file=logic.DATA_FILE)
    _require_tenant_id(tenant_id)
    conn = _get_postgres_connection()
    try:
        result = db.load_dates_for_tenant(conn, tenant_id=tenant_id)
        conn.commit()
        return result
    except psycopg.Error as e:
        conn.rollback()
        raise StorageUnavailableError("PostgreSQLへの接続・読み込みに失敗しました。") from e
    finally:
        conn.close()


def add_date(record_date, tenant_id=None):
    """1件だけ原子的に記録を追加する(postgresバックエンドではtenant_id必須)。

    日付集合全体を読み込んで置き換えるsave_dates()と違い、他の記録に一切触れない
    ため、同じ世帯の複数端末からのほぼ同時操作でもロスト・アップデートが起きにくい
    (仕様書/マルチテナント設計.md⑩参照)。
    """
    if get_backend_name() == "json":
        dates = logic.load_dates(data_file=logic.DATA_FILE)
        dates.add(record_date)
        logic.save_dates(dates, data_file=logic.DATA_FILE)
        return
    _require_tenant_id(tenant_id)
    conn = _get_postgres_connection()
    try:
        db.insert_date_for_tenant(record_date, conn, tenant_id=tenant_id)
        conn.commit()
    except psycopg.Error as e:
        conn.rollback()
        raise StorageUnavailableError("PostgreSQLへの書き込みに失敗しました。") from e
    finally:
        conn.close()


def cancel_date(record_date, tenant_id=None):
    """1件だけ原子的に記録を削除する(postgresバックエンドではtenant_id必須)。"""
    if get_backend_name() == "json":
        dates = logic.load_dates(data_file=logic.DATA_FILE)
        dates.discard(record_date)
        logic.save_dates(dates, data_file=logic.DATA_FILE)
        return
    _require_tenant_id(tenant_id)
    conn = _get_postgres_connection()
    try:
        db.delete_date_for_tenant(record_date, conn, tenant_id=tenant_id)
        conn.commit()
    except psycopg.Error as e:
        conn.rollback()
        raise StorageUnavailableError("PostgreSQLへの削除に失敗しました。") from e
    finally:
        conn.close()


def save_dates(dates, tenant_id=None):
    """記録日集合全体を置き換える(postgresバックエンドではtenant_id必須)。

    app.pyの記録追加・取り消しは第16回以降add_date()/cancel_date()を使う。
    この関数はテスト・将来の一括同期用途のために残す。
    """
    if get_backend_name() == "json":
        logic.save_dates(dates, data_file=logic.DATA_FILE)  # 理由はload_dates()と同じ
        return
    _require_tenant_id(tenant_id)
    conn = _get_postgres_connection()
    try:
        db.save_dates_for_tenant(dates, conn, tenant_id=tenant_id)  # db.pyはSQL操作のみ
        conn.commit()
    except psycopg.Error as e:
        conn.rollback()
        raise StorageUnavailableError("PostgreSQLへの書き込みに失敗しました。") from e
    finally:
        conn.close()
