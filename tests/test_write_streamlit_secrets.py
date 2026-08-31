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


def test_main_skips_when_auth_disabled(monkeypatch, capsys):
    monkeypatch.delenv(write_secrets_module.AUTH_ENABLED_ENV, raising=False)

    exit_code = write_secrets_module.main(["write_streamlit_secrets.py"])

    assert exit_code == 0
    assert "[SKIP]" in capsys.readouterr().out


def test_main_reports_error_when_required_vars_missing(monkeypatch, capsys):
    monkeypatch.setenv(write_secrets_module.AUTH_ENABLED_ENV, "true")
    for name in write_secrets_module.REQUIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    exit_code = write_secrets_module.main(["write_streamlit_secrets.py"])

    assert exit_code == 1
    assert "[NG]" in capsys.readouterr().out


def test_main_writes_secrets_file_when_enabled_and_configured(monkeypatch, tmp_path):
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
