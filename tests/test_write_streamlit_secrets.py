import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.write_streamlit_secrets as write_secrets_module  # noqa: E402


def test_build_secrets_toml_contains_expected_keys():
    env = {
        "AUTH0_CLIENT_ID": "client-id-123",
        "AUTH0_CLIENT_SECRET": "client-secret-456",
        "AUTH0_DOMAIN": "example.auth0.com",
        "COOKIE_SECRET": "cookie-secret-789",
        "APP_BASE_URL": "https://web-staging-beab.up.railway.app/",
    }

    content = write_secrets_module.build_secrets_toml(env)

    assert "[auth]" in content
    assert 'redirect_uri = "https://web-staging-beab.up.railway.app/oauth2callback"' in content
    assert 'cookie_secret = "cookie-secret-789"' in content
    assert 'client_id = "client-id-123"' in content
    assert 'client_secret = "client-secret-456"' in content
    assert (
        'server_metadata_url = "https://example.auth0.com/.well-known/openid-configuration"'
        in content
    )


def test_secrets_path_is_under_repository_root_not_streamlit_subdir():
    """streamlit runはリポジトリのルートから実行されるため、生成先もその直下の
    .streamlit/secrets.tomlであること(streamlit/.streamlit/配下だとStreamlitから
    読み込まれない)。"""
    assert write_secrets_module.SECRETS_PATH.parent.name == ".streamlit"
    assert write_secrets_module.SECRETS_PATH.parent.parent.name != "streamlit"


def test_main_skips_when_auth_disabled(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv(write_secrets_module.AUTH_ENABLED_ENV, raising=False)
    secrets_path = tmp_path / "secrets.toml"
    monkeypatch.setattr(write_secrets_module, "SECRETS_PATH", secrets_path)

    exit_code = write_secrets_module.main(["write_streamlit_secrets.py"])

    assert exit_code == 0
    assert "[SKIP]" in capsys.readouterr().out
    assert not secrets_path.exists()  # AUTH_ENABLEDが無効な間はファイルへ一切触れない


def test_main_reports_error_when_required_vars_missing(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(write_secrets_module.AUTH_ENABLED_ENV, "true")
    for name in write_secrets_module.REQUIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    secrets_path = tmp_path / "secrets.toml"
    monkeypatch.setattr(write_secrets_module, "SECRETS_PATH", secrets_path)

    exit_code = write_secrets_module.main(["write_streamlit_secrets.py"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "[NG]" in output
    # エラーメッセージには不足している環境変数名だけが含まれ、秘密値そのものは含まれない
    # (この時点ではすべて未設定のため値は存在しないが、名前のみの出力であることを確認する)
    for name in write_secrets_module.REQUIRED_ENV_VARS:
        assert name in output
    assert not secrets_path.exists()


def test_main_writes_secrets_file_when_enabled_and_configured(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(write_secrets_module.AUTH_ENABLED_ENV, "true")
    monkeypatch.setenv("AUTH0_CLIENT_ID", "cid")
    monkeypatch.setenv("AUTH0_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("AUTH0_DOMAIN", "example.auth0.com")
    monkeypatch.setenv("COOKIE_SECRET", "csecret2")
    monkeypatch.setenv("APP_BASE_URL", "https://example.test")

    secrets_path = tmp_path / "secrets.toml"
    monkeypatch.setattr(write_secrets_module, "SECRETS_PATH", secrets_path)

    exit_code = write_secrets_module.main(["write_streamlit_secrets.py"])

    assert exit_code == 0
    assert secrets_path.exists()
    assert "[auth]" in secrets_path.read_text(encoding="utf-8")

    # 標準出力にClient Secret・Cookie Secretの値そのものが出力されていないこと
    output = capsys.readouterr().out
    assert "csecret" not in output
    assert "csecret2" not in output

    if hasattr(os, "chmod") and os.name != "nt":
        # Windowsではchmodのビット意味が異なるため、POSIX環境でのみ0600を厳密確認する
        mode = stat.S_IMODE(secrets_path.stat().st_mode)
        assert mode == 0o600
