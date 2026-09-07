"""storage.apply_tenant_rename()の統合テスト(クロードコード部・第25課題:
「落ちない化」——いじわる入力でもエラーで落ちず、優しいメッセージを表示する)。

app.pyの「世帯名を変更する」ボタンが実際に呼び出す関数(apply_tenant_rename)を、
実際のPostgreSQLバックエンド(隔離スキーマ・実DB)に対して直接呼び出し、
以下を検証する:

1. 空欄・空白のみの入力は、具体的な案内とともに拒否され、DBは変更されない
2. 世帯名の文字数境界(100文字ちょうど=成功、101文字=拒否)
3. 改行・タブ・NUL文字などの制御文字を含む入力は拒否され、DBは変更されない
4. 日本語・英数字・記号を含む正常な世帯名は成功し、DBへ実際に反映される
5. 拒否時にDBの世帯名が一切変更されないこと(上記1〜3すべてで確認)
6. 実際のPostgreSQLバックエンド経由であること(json/モックではない)
7. 画面に渡す結果が、具体的で安全なメッセージのみであること
   (Python例外オブジェクトそのもの・スタックトレース・SQL文・接続情報が
   一切含まれないこと)
8. admin以外のroleで呼び出した場合は、入力エラーとは別に安全側の
   汎用メッセージ(内部のrole検証結果は見せない)で拒否されること

app.py自体はStreamlitのトップレベルスクリプトであり、importするだけで
st.set_page_config等が実行されてしまうため、直接テストしない
(このプロジェクトの既存テストは一貫してapp.pyを直接importしていない)。
apply_tenant_rename()をStorageモジュール側の純粋な関数として実装した
理由もこれと同じ——Streamlitを起動せずに実際のDB経路をテストするため。
"""
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))

import db  # noqa: E402
import storage  # noqa: E402
import scripts.migrate_to_auth_schema as migrate_auth_module  # noqa: E402
import scripts.migrate_to_tenant_schema as migrate_tenant_module  # noqa: E402

requires_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします",
)


@pytest.fixture
def auth_schema():
    """稼働中のpublic.recordsとは隔離した専用スキーマに、tenants/records(第16回)＋
    users/tenant_memberships(第17回)を用意し、テスト用の世帯(tenant_id)を1つ作る。

    tests/test_auth.pyのauth_schemaと同じ設計方針(このファイル単独でも
    実行できるよう、あえて重複定義する——他の既存テストファイルもそれぞれ
    自前でfixtureを持つのが、このプロジェクトの一貫したスタイル)。
    """
    if not db.is_configured():
        pytest.skip("DATABASE_URLが設定されていないため、PostgreSQL連携テストをスキップします")

    schema_name = f"test_hardening_{uuid.uuid4().hex}"
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema_name}")
        cur.execute(f"SET search_path TO {schema_name}")
    db.ensure_schema(conn)
    tenant_id = uuid.uuid4()
    migrate_tenant_module.migrate_to_tenant_schema(tenant_id, conn=conn)
    migrate_auth_module.migrate_to_auth_schema(conn=conn)
    conn.commit()

    try:
        yield conn, tenant_id
    finally:
        conn.rollback()
        conn.close()
        cleanup_conn = db.get_connection()
        try:
            with cleanup_conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()


def _use_postgres_backend_with_schema(monkeypatch, conn):
    """tests/test_auth.pyと同じヘルパー。db.get_connection()をスパイし、
    storage.pyが自前で開く接続にも、conn自身が使っている隔離スキーマの
    search_pathを設定してから返す。
    """
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "postgres")

    real_get_connection = db.get_connection
    with conn.cursor() as cur:
        cur.execute("SHOW search_path")
        schema_name = cur.fetchone()[0]

    def patched_get_connection():
        c = real_get_connection()
        with c.cursor() as cur:
            cur.execute(f"SET search_path TO {schema_name}")
        return c

    monkeypatch.setattr(db, "get_connection", patched_get_connection)


def _current_tenant_name(conn, tenant_id):
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM tenants WHERE id = %s", (tenant_id,))
        return cur.fetchone()[0]


def _assert_message_is_safe(message):
    """画面に渡すメッセージに、例外オブジェクトの型名・スタックトレース・
    SQL文・接続情報等の内部情報が含まれていないことを確認する(要求仕様#7)。
    """
    assert isinstance(message, str)
    forbidden_fragments = [
        "Traceback",
        "psycopg",
        "Error(",
        "DATABASE_URL",
        "postgresql://",
        "SELECT ",
        "UPDATE ",
        "  File \"",
    ]
    for fragment in forbidden_fragments:
        assert fragment not in message, f"内部情報が漏れています: {fragment!r} in {message!r}"


# --- ①空欄・空白のみ ---


@requires_db
@pytest.mark.parametrize(
    "bad_name",
    ["", "   ", "　　"],  # 空文字・半角空白のみ・全角空白のみ
    ids=["empty", "half-width-spaces", "full-width-spaces"],
)
def test_empty_or_whitespace_only_is_rejected_with_specific_message(
    monkeypatch, auth_schema, bad_name
):
    conn, tenant_id = auth_schema
    _use_postgres_backend_with_schema(monkeypatch, conn)
    original_name = _current_tenant_name(conn, tenant_id)

    ok, message, should_stop = storage.apply_tenant_rename(
        bad_name, tenant_id=tenant_id, role="admin"
    )

    assert ok is False
    assert "入力してください" in message
    assert should_stop is False  # 入力エラーなのでst.stop()しない
    _assert_message_is_safe(message)
    assert _current_tenant_name(conn, tenant_id) == original_name  # DB不変


# --- ②文字数境界(100文字ちょうど・101文字) ---


@requires_db
def test_exactly_100_chars_succeeds(monkeypatch, auth_schema):
    conn, tenant_id = auth_schema
    _use_postgres_backend_with_schema(monkeypatch, conn)
    name_100 = "あ" * 100
    assert len(name_100) == 100

    ok, message, should_stop = storage.apply_tenant_rename(
        name_100, tenant_id=tenant_id, role="admin"
    )

    assert ok is True
    assert message == "世帯名を変更しました。"
    assert should_stop is False
    assert _current_tenant_name(conn, tenant_id) == name_100


@requires_db
def test_101_chars_is_rejected_with_specific_message(monkeypatch, auth_schema):
    conn, tenant_id = auth_schema
    _use_postgres_backend_with_schema(monkeypatch, conn)
    original_name = _current_tenant_name(conn, tenant_id)
    name_101 = "あ" * 101

    ok, message, should_stop = storage.apply_tenant_rename(
        name_101, tenant_id=tenant_id, role="admin"
    )

    assert ok is False
    assert "100文字以内" in message
    assert should_stop is False
    _assert_message_is_safe(message)
    assert _current_tenant_name(conn, tenant_id) == original_name  # DB不変


# --- ③改行・タブ・その他の制御文字 ---


@requires_db
@pytest.mark.parametrize(
    "bad_name",
    [
        "新しい\n世帯名",  # 改行(前後ではなく途中、strip()で消えないこと)
        "新しい\t世帯名",  # タブ
        "新しい世帯名\x00",  # NUL文字
        "新しい世帯名\x7f",  # DEL(制御文字)
        "新しい\r\n世帯名",  # CRLF
    ],
    ids=["embedded-newline", "embedded-tab", "null-byte", "del-char", "embedded-crlf"],
)
def test_control_characters_are_rejected_with_specific_message(
    monkeypatch, auth_schema, bad_name
):
    conn, tenant_id = auth_schema
    _use_postgres_backend_with_schema(monkeypatch, conn)
    original_name = _current_tenant_name(conn, tenant_id)

    ok, message, should_stop = storage.apply_tenant_rename(
        bad_name, tenant_id=tenant_id, role="admin"
    )

    assert ok is False
    assert "制御文字" in message
    assert should_stop is False
    _assert_message_is_safe(message)
    assert _current_tenant_name(conn, tenant_id) == original_name  # DB不変


# --- ④正常な日本語・英数字・記号を含む世帯名 ---


@requires_db
@pytest.mark.parametrize(
    "good_name",
    [
        "山田家",
        "Smith Family #2",
        "田中家（実家）",
        "🍽️食器洗い隊",  # 絵文字を含む
        "  前後の空白は除去される  ",
    ],
    ids=["kanji", "ascii-with-symbol", "kanji-with-parens", "emoji", "surrounding-spaces"],
)
def test_valid_names_succeed_and_are_persisted(monkeypatch, auth_schema, good_name):
    conn, tenant_id = auth_schema
    _use_postgres_backend_with_schema(monkeypatch, conn)

    ok, message, should_stop = storage.apply_tenant_rename(
        good_name, tenant_id=tenant_id, role="admin"
    )

    assert ok is True
    assert message == "世帯名を変更しました。"
    assert should_stop is False
    assert _current_tenant_name(conn, tenant_id) == good_name.strip()


# --- ⑤拒否時にDBが変わらないこと(①③で個別確認済み)。ここでは連続実行での確認 ---


@requires_db
def test_rejected_attempts_never_affect_a_later_successful_rename(monkeypatch, auth_schema):
    """空欄→制御文字→長文字数超過、と続けて拒否された後でも、DBには一切
    書き込まれておらず、その後の正常な変更が正しく反映されることを確認する
    (「連打」を含む一連の操作でDBが意図せず変化しないことの確認)。
    """
    conn, tenant_id = auth_schema
    _use_postgres_backend_with_schema(monkeypatch, conn)
    original_name = _current_tenant_name(conn, tenant_id)

    for bad_name in ["", "新しい\n世帯名", "あ" * 101, "   "]:
        ok, _, _ = storage.apply_tenant_rename(bad_name, tenant_id=tenant_id, role="admin")
        assert ok is False
        assert _current_tenant_name(conn, tenant_id) == original_name

    ok, message, _ = storage.apply_tenant_rename(
        "最終的な世帯名", tenant_id=tenant_id, role="admin"
    )
    assert ok is True
    assert _current_tenant_name(conn, tenant_id) == "最終的な世帯名"


# --- ⑥PostgreSQLバックエンドの実経路であることの確認 ---


@requires_db
def test_uses_real_postgres_backend_not_json(monkeypatch, auth_schema):
    """STORAGE_BACKENDがjsonのままだと、apply_tenant_rename()もStorageConfigError
    経由の安全な汎用メッセージで拒否されることを確認する(postgres指定を
    忘れた場合に静かに成功したように見えることがない、という確認)。
    """
    conn, tenant_id = auth_schema
    monkeypatch.setenv(storage.STORAGE_BACKEND_ENV, "json")

    ok, message, should_stop = storage.apply_tenant_rename(
        "テスト世帯", tenant_id=tenant_id, role="admin"
    )

    assert ok is False
    assert should_stop is True
    _assert_message_is_safe(message)


# --- ⑦admin以外のroleは、入力エラーとは別の安全な汎用メッセージで拒否される ---


@requires_db
def test_non_admin_role_is_rejected_with_generic_safe_message(monkeypatch, auth_schema):
    """role="member"の場合、storage.rename_tenant()内部のadmin検証で
    StorageConfigErrorが送出されるが、これはInvalidInputErrorではないため、
    具体的な入力検証メッセージではなく安全な汎用メッセージになることを確認する
    (「なぜadminではないと判定されたか」の内部詳細を画面に出さないため)。
    """
    conn, tenant_id = auth_schema
    _use_postgres_backend_with_schema(monkeypatch, conn)
    original_name = _current_tenant_name(conn, tenant_id)

    ok, message, should_stop = storage.apply_tenant_rename(
        "新しい世帯名", tenant_id=tenant_id, role="member"
    )

    assert ok is False
    assert should_stop is True
    assert message == (
        "世帯名の変更中に問題が発生しました。安全のため処理を停止しました。"
        "しばらくしてから再度お試しいただくか、管理者に連絡してください。"
    )
    _assert_message_is_safe(message)
    assert _current_tenant_name(conn, tenant_id) == original_name  # DB不変
