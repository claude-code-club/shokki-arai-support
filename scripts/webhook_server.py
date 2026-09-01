"""Stripe Webhookを受信する軽量HTTPサーバー（第19回: 継続課金・Webhook）。

食器洗いサポート本体(Streamlitアプリ、webサービス)とは別の、独立したRailwayサービス
として動かす想定(仕様書/Webhook設計.md③参照)。Streamlitは任意のHTTPパスを追加する
公式手段を持たないため、Webhook専用のこの軽量サーバーを別途用意し、既存のweb
サービスには一切変更を加えない。

このスクリプトが行うのは、次の3つだけ。
1. StripeからのPOSTリクエストを受け取る
2. stripe.Webhook.construct_event()で署名を検証する(不正な署名は400で拒否し、DBには
   一切触れない)
3. 検証済みのeventをwebhook.process_event()へ渡す(重複防止・DB反映はそちらの責務)

必要な環境変数(このWebhookサーバー専用のRailwayサービスに設定する):
- DATABASE_URL: 食器洗いサポート本体と同じPostgreSQLへの接続文字列
- STRIPE_SECRET_KEY: Stripeのシークレットキー(本体と同じ値でよい)
- STRIPE_WEBHOOK_SECRET: このWebhookエンドポイント専用の署名検証シークレット
  (Stripeダッシュボードでこのエンドポイントを登録した際に発行される、whsec_で始まる値。
  本体のSTRIPE_SECRET_KEYとは別物)
- PORT: 待受ポート(Railwayが自動的に設定する)
"""

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import stripe  # noqa: E402

import db  # noqa: E402
import webhook  # noqa: E402

STRIPE_SECRET_KEY_ENV = "STRIPE_SECRET_KEY"
STRIPE_WEBHOOK_SECRET_ENV = "STRIPE_WEBHOOK_SECRET"

# Stripeの実際のWebhookペイロードは通常数KB程度。65536バイト(64KB)を大きく
# 超えることは無い前提で上限を設ける(第21回: SaaSのセキュリティ堅牢化)。
# Content-Lengthがこれを超えるリクエストは、本文を読み込む前に拒否する
# (巨大なContent-Lengthを送りつけるDoSへの対策)。
MAX_CONTENT_LENGTH = 65536

# 秘密値や個人情報を含みうるイベント本文・署名は一切print/logしない。
# ログに出すのは種別・成否・event_idなど、非機密の要約情報だけに限定する。


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        # BaseHTTPRequestHandlerの標準アクセスログ(リクエスト内容を含みうる)を無効化する。
        pass

    def do_GET(self):
        # Railwayのヘルスチェック用。DB・Stripeいずれにも触れない。
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            print("[NG] Content-Lengthヘッダの形式が不正です。リクエストを拒否しました。")
            self._respond(400)
            return
        if content_length < 0:
            print("[NG] Content-Lengthヘッダが負数です。リクエストを拒否しました。")
            self._respond(400)
            return
        if content_length > MAX_CONTENT_LENGTH:
            # 本文を読み込む前に拒否する(巨大なContent-Lengthによるメモリ圧迫を防ぐ)。
            print(f"[NG] リクエスト本文が上限({MAX_CONTENT_LENGTH}バイト)を超えています。拒否しました。")
            self._respond(413)
            return

        content_type = self.headers.get("Content-Type", "")
        if not content_type.split(";")[0].strip().lower() == "application/json":
            print("[NG] Content-Typeがapplication/jsonではありません。リクエストを拒否しました。")
            self._respond(400)
            return

        payload = self.rfile.read(content_length)
        sig_header = self.headers.get("Stripe-Signature", "")

        webhook_secret = os.environ.get(STRIPE_WEBHOOK_SECRET_ENV, "").strip()
        if not webhook_secret:
            print(f"[NG] {STRIPE_WEBHOOK_SECRET_ENV}が設定されていません。")
            self._respond(500)
            return

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        except (ValueError, stripe.SignatureVerificationError):
            print("[NG] Webhookの署名検証に失敗しました。リクエストを拒否しました。")
            self._respond(400)
            return

        stripe.api_key = os.environ.get(STRIPE_SECRET_KEY_ENV, "").strip()

        conn = None
        try:
            conn = db.get_connection()
            result = webhook.process_event(conn, event, stripe_client=stripe)
            print(f"[OK] event_type={getattr(event, 'type', '?')}, handled={result.get('handled')}")
        except Exception:
            # DB接続自体の失敗も含め、内部例外の詳細はクライアントへもログへも出さない。
            print("[NG] Webhookイベントの処理中にエラーが発生しました。")
            self._respond(500)
            return
        finally:
            if conn is not None:
                conn.close()

        self._respond(200)

    def _respond(self, status_code):
        self.send_response(status_code)
        self.end_headers()


def main():
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), WebhookHandler)
    print(f"[OK] Webhook受信サーバーを起動しました(port={port})")
    server.serve_forever()


if __name__ == "__main__":
    main()
