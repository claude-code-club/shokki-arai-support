"""Railway環境変数から.streamlit/secrets.tomlを生成する(コンテナ起動時に実行)。

Streamlitの[auth]設定はsecrets.tomlファイルのみを読む仕様で、環境変数からの
直接読み込みには現時点で対応していない(仕様書/認証基盤設計.md①-6参照)。
Railwayはファイルではなく環境変数のみをサポートするため、Procfileの
streamlit起動前にこのスクリプトを実行し、環境変数からファイルを生成する。

Streamlitのプロジェクト単位secrets.tomlは、streamlit runを実行した時点の
カレントディレクトリ(CWD)を基準に"<CWD>/.streamlit/secrets.toml"を探す
(app.py自身の場所ではない。公式ドキュメントで確認済み)。Procfileは
リポジトリのルート(streamlit/app.pyへの相対パスでstreamlit runを実行)から
起動するため、生成先もリポジトリのルート直下の.streamlit/secrets.tomlにする。

AUTH_ENABLEDが未設定(第16回のDEFAULT_TENANT_ID方式のまま)の場合は何もせず
終了する(現在のstaging動作を一切変えない)。生成されたsecrets.tomlは
コンテナのローカルディスクにのみ存在し、Gitには一切コミットしない
(.gitignore参照)。ファイル権限は所有者のみ読み書き可能(0600)にし、
Client Secret・Cookie Secretの値そのものは標準出力・ログへ一切表示しない
(このスクリプトのprintはファイルパスと環境変数名のみを出力する)。
"""

import os
import stat
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

# リポジトリのルート直下(streamlit run実行時のCWD)を基準にする。
# streamlit/.streamlit/secrets.tomlではStreamlitから読み込まれない点に注意。
SECRETS_PATH = Path(__file__).resolve().parents[1] / ".streamlit" / "secrets.toml"


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
    try:
        os.chmod(SECRETS_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 所有者のみ読み書き可能(0600)
    except OSError:
        pass  # chmodが効かない環境(Windows等)では無視する。値そのものは出力しない。
    print(f"[OK] {SECRETS_PATH}を生成しました。")  # ファイルパスのみ。値は一切出力しない。
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
