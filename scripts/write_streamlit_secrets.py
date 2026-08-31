"""Railway環境変数から.streamlit/secrets.tomlを生成する(コンテナ起動時に実行)。

Streamlitの[auth]設定はsecrets.tomlファイルのみを読む仕様で、環境変数からの
直接読み込みには現時点で対応していない(仕様書/認証基盤設計.md①-6参照)。
Railwayはファイルではなく環境変数のみをサポートするため、Procfileの
streamlit起動前にこのスクリプトを実行し、環境変数からファイルを生成する。

AUTH_ENABLEDが未設定(第16回のDEFAULT_TENANT_ID方式のまま)の場合は何もせず
終了する。生成されたsecrets.tomlはコンテナのローカルディスクにのみ存在し、
Gitには一切コミットしない(.gitignore参照)。
"""

import os
import sys
from pathlib import Path

AUTH_ENABLED_ENV = "AUTH_ENABLED"
REQUIRED_ENV_VARS = (
    "AUTH0_CLIENT_ID",
    "AUTH0_CLIENT_SECRET",
    "AUTH0_DOMAIN",
    "COOKIE_SECRET",
    "APP_BASE_URL",
)

SECRETS_PATH = Path(__file__).resolve().parents[1] / "streamlit" / ".streamlit" / "secrets.toml"


def build_secrets_toml(env):
    """envの内容からsecrets.tomlの中身(文字列)を組み立てる(DB接続・外部通信なし)。"""
    domain = env["AUTH0_DOMAIN"].strip()
    base_url = env["APP_BASE_URL"].strip().rstrip("/")
    return (
        "[auth]\n"
        f'redirect_uri = "{base_url}/oauth2callback"\n'
        f'cookie_secret = "{env["COOKIE_SECRET"]}"\n'
        f'client_id = "{env["AUTH0_CLIENT_ID"]}"\n'
        f'client_secret = "{env["AUTH0_CLIENT_SECRET"]}"\n'
        f'server_metadata_url = "https://{domain}/.well-known/openid-configuration"\n'
    )


def main(argv):
    if os.environ.get(AUTH_ENABLED_ENV, "").strip().lower() != "true":
        print(f"[SKIP] {AUTH_ENABLED_ENV}が有効でないため、secrets.tomlは生成しません。")
        return 0

    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name, "").strip()]
    if missing:
        print(f"[NG] {AUTH_ENABLED_ENV}=trueですが、次の環境変数が未設定です: {', '.join(missing)}")
        return 1

    content = build_secrets_toml(os.environ)
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_PATH.write_text(content, encoding="utf-8")
    print(f"[OK] {SECRETS_PATH}を生成しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
