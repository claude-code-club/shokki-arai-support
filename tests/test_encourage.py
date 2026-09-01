"""encourage.pyの例外処理に対するテスト（第21回: SaaSのセキュリティ堅牢化）。

Claude API呼び出し失敗時に、例外メッセージそのものではなく例外クラス名だけを
ログへ出すことを確認する(APIキー等が意図せず標準エラー出力へ混入しないため)。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "streamlit"))

import encourage  # noqa: E402


class _FailingMessages:
    def create(self, **kwargs):
        raise RuntimeError("secret-looking-detail sk-should-not-leak")


class _FailingClient:
    def __init__(self, api_key=None):
        self.messages = _FailingMessages()


def test_get_encouragement_returns_none_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert encourage.get_encouragement(3, 5) is None


def test_get_encouragement_logs_only_exception_type_not_message(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-key-for-test")
    monkeypatch.setattr(encourage.anthropic, "Anthropic", _FailingClient)

    result = encourage.get_encouragement(3, 5)

    assert result is None
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert "secret-looking-detail" not in captured.err
    assert "sk-should-not-leak" not in captured.err
