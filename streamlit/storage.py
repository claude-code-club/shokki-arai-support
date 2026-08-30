"""保存方式(JSON / PostgreSQL)を、環境変数STORAGE_BACKENDで明示的に切り替える。

DATABASE_URLの有無ではなくSTORAGE_BACKENDの値だけで判断する。将来DATABASE_URLが
存在するだけで意図せず保存先が切り替わる事故を防ぐため。

PostgreSQL障害時にJSONへ自動フォールバックすることは行わない(二重管理・データ
不整合を避けるため)。障害時は例外を送出し、app.py側でエラー表示してst.stop()する、
既存のRecordsFileCorruptedError処理と同じ扱いにする。

commit/rollbackの責任はここ(storage.py)が持つ。db.pyはSQL操作のみを行い、
commit/rollbackを一切呼ばない(仕様書/保存方式切り替え設計.md ②-b参照)。
"""

import os

import psycopg

import db
import logic
from logic import RecordsFileCorruptedError  # noqa: F401 (app.pyから再利用)

STORAGE_BACKEND_ENV = "STORAGE_BACKEND"
VALID_BACKENDS = ("json", "postgres")


class StorageConfigError(Exception):
    """STORAGE_BACKENDの値が不正、またはpostgres指定なのにDATABASE_URL未設定の場合。"""


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


def load_dates():
    if get_backend_name() == "json":
        # logic.DATA_FILEを呼び出し時に明示的に読む(logic.load_dates()の既定引数に
        # 頼らない)。既定引数はモジュール読み込み時に固定されるため、テストで
        # logic.DATA_FILEを差し替えても反映されず、本番パスへ書き込む事故につながる。
        return logic.load_dates(data_file=logic.DATA_FILE)
    try:
        conn = db.get_connection()
    except db.DatabaseNotConfiguredError as e:
        raise StorageConfigError(str(e)) from e
    except psycopg.Error as e:
        # 接続自体の失敗(ホスト到達不可・タイムアウト等)。connがまだ無いのでcloseは不要。
        raise StorageUnavailableError("PostgreSQLへの接続に失敗しました。") from e
    try:
        db.ensure_schema(conn)
        result = db.load_dates(conn)
        conn.commit()  # ensure_schemaのDDLを確定させ、開いたトランザクションを綺麗に終える
        return result
    except psycopg.Error as e:
        conn.rollback()
        raise StorageUnavailableError("PostgreSQLへの接続・読み込みに失敗しました。") from e
    finally:
        conn.close()


def save_dates(dates):
    if get_backend_name() == "json":
        logic.save_dates(dates, data_file=logic.DATA_FILE)  # 理由はload_dates()と同じ
        return
    try:
        conn = db.get_connection()
    except db.DatabaseNotConfiguredError as e:
        raise StorageConfigError(str(e)) from e
    except psycopg.Error as e:
        # 接続自体の失敗(ホスト到達不可・タイムアウト等)。connがまだ無いのでcloseは不要。
        raise StorageUnavailableError("PostgreSQLへの接続に失敗しました。") from e
    try:
        db.ensure_schema(conn)
        db.save_dates(dates, conn=conn)  # db.pyはSQL操作のみ。commitはここで行う
        conn.commit()
    except psycopg.Error as e:
        conn.rollback()
        raise StorageUnavailableError("PostgreSQLへの書き込みに失敗しました。") from e
    finally:
        conn.close()
