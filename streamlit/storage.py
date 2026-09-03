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

import authz
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


class InvalidInputError(StorageConfigError):
    """入力値が長さ・形式の検証に失敗した場合(第21回: SaaSのセキュリティ堅牢化)。

    StorageConfigErrorのサブクラスにすることで、既存のapp.py側の
    except (StorageConfigError, StorageUnavailableError)がそのまま
    安全なエラーメッセージ表示に使える(呼び出し元を変更しない)。
    """


TENANT_NAME_MAX_LENGTH = 100


def _validate_tenant_name(name):
    """世帯名の長さ・形式をサーバー側で検証する(第21回: SaaSのセキュリティ堅牢化)。

    画面を隠すだけでなく、DB書き込みの直前にも検証する。空文字・上限超過・
    制御文字はすべてInvalidInputErrorにまとめる。戻り値は前後空白を除いた文字列。
    """
    if not isinstance(name, str):
        raise InvalidInputError("世帯名は文字列で指定してください。")
    stripped = name.strip()
    if not stripped:
        raise InvalidInputError("世帯名を入力してください。")
    if len(stripped) > TENANT_NAME_MAX_LENGTH:
        raise InvalidInputError(f"世帯名は{TENANT_NAME_MAX_LENGTH}文字以内で入力してください。")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in stripped):
        raise InvalidInputError("世帯名に制御文字を含めることはできません。")
    return stripped


MEMO_MAX_LENGTH = 200
KEYWORD_MAX_LENGTH = 100


def _validate_memo(memo):
    """記録に添えるメモの長さ・形式をサーバー側で検証する(第22回: 検索できるDB)。

    _validate_tenant_name()と同じ方針(空・制御文字・上限超過を拒否)だが、メモは
    任意項目のため、Noneまたは空文字はエラーにせずNone(メモ無し)として扱う。
    """
    if memo is None:
        return None
    if not isinstance(memo, str):
        raise InvalidInputError("メモは文字列で指定してください。")
    stripped = memo.strip()
    if not stripped:
        return None
    if len(stripped) > MEMO_MAX_LENGTH:
        raise InvalidInputError(f"メモは{MEMO_MAX_LENGTH}文字以内で入力してください。")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in stripped):
        raise InvalidInputError("メモに制御文字を含めることはできません。")
    return stripped


def _validate_search_keyword(keyword):
    """検索キーワードの長さ・形式をサーバー側で検証する(第22回: 検索できるDB)。

    _validate_memo()と同じ方針。Noneまたは空文字は「絞り込まない」として扱う。
    """
    if keyword is None:
        return None
    if not isinstance(keyword, str):
        raise InvalidInputError("検索キーワードは文字列で指定してください。")
    stripped = keyword.strip()
    if not stripped:
        return None
    if len(stripped) > KEYWORD_MAX_LENGTH:
        raise InvalidInputError(f"検索キーワードは{KEYWORD_MAX_LENGTH}文字以内で入力してください。")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in stripped):
        raise InvalidInputError("検索キーワードに制御文字を含めることはできません。")
    return stripped


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


def rename_tenant(name, tenant_id=None, role=None):
    """世帯名を変更する(第17回: 認証基盤、admin専用操作)。

    表示側でボタンを隠すだけでなく、DB操作の直前にもrole検証を行う
    (仕様書/認証基盤設計.md⑨参照。サーバー側の最終防衛線)。
    """
    if get_backend_name() == "json":
        raise StorageConfigError("jsonバックエンドでは世帯の概念がないため利用できません。")
    _require_tenant_id(tenant_id)
    try:
        authz.require_admin(role)
    except authz.NotAdminError as e:
        raise StorageConfigError(str(e)) from e
    validated_name = _validate_tenant_name(name)
    conn = _get_postgres_connection()
    try:
        db.update_tenant_name(conn, tenant_id=tenant_id, name=validated_name)
        conn.commit()
    except psycopg.Error as e:
        conn.rollback()
        raise StorageUnavailableError("世帯名の変更に失敗しました。") from e
    finally:
        conn.close()


def add_date_with_memo(record_date, memo, tenant_id=None):
    """1件だけ原子的に記録を追加し、任意のメモを添える(第22回: 検索できるDB)。

    add_date()のメモ対応版。メモはpostgresバックエンド専用の機能(jsonバックエンドは
    dates(日付の集合)しか持たないため、メモの概念が無い。仕様書/検索できるDB設計.md
    参照)。jsonバックエンドで呼び出された場合はStorageConfigErrorを送出する。
    """
    if get_backend_name() == "json":
        raise StorageConfigError("jsonバックエンドではメモを保存できません(postgres限定機能)。")
    _require_tenant_id(tenant_id)
    validated_memo = _validate_memo(memo)
    conn = _get_postgres_connection()
    try:
        db.record_with_memo_for_tenant(record_date, validated_memo, conn, tenant_id=tenant_id)
        conn.commit()
    except psycopg.Error as e:
        conn.rollback()
        raise StorageUnavailableError("PostgreSQLへの書き込みに失敗しました。") from e
    finally:
        conn.close()


def search_records(tenant_id=None, keyword=None, order="desc"):
    """記録をキーワード検索・並び替えする(第22回: 検索できるDB、postgresバックエンド専用)。

    add_date_with_memo()と同じ理由で、jsonバックエンドではStorageConfigErrorを送出する。
    戻り値: db.search_records_for_tenant()と同じ形式のリスト。
    """
    if get_backend_name() == "json":
        raise StorageConfigError("jsonバックエンドでは検索できません(postgres限定機能)。")
    _require_tenant_id(tenant_id)
    validated_keyword = _validate_search_keyword(keyword)
    conn = _get_postgres_connection()
    try:
        result = db.search_records_for_tenant(
            conn, tenant_id=tenant_id, keyword=validated_keyword, order=order
        )
        conn.commit()
        return result
    except psycopg.Error as e:
        conn.rollback()
        raise StorageUnavailableError("PostgreSQLへの検索に失敗しました。") from e
    finally:
        conn.close()
