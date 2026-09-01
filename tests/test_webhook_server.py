"""scripts/webhook_server.pyのHTTPレイヤーに対する統合テスト（第21回: SaaSのセキュリティ堅牢化）。

webhook.process_event()自体の単体テストはtests/test_webhook.pyが担う。ここでは
「署名検証・サイズ上限・Content-Type・HTTPメソッド制限」という、webhook_server.py
固有のHTTPハンドリングだけを、実際にThreadingHTTPServerを起動して検証する。
DATABASE_URLが未設定でも実行できる(署名検証を通過しない限りDBへは触れない設計の
ため。署名が有効な場合のテストのみ、DB接続失敗が安全に500になることを確認する)。
"""

import hashlib
import hmac
import http.client
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))

import scripts.webhook_server as webhook_server_module  # noqa: E402

WEBHOOK_SECRET = "whsec_test_secret_for_ci_only"


def _sign(payload_bytes, secret, timestamp=None):
    """Stripeの署名方式(t=<timestamp>,v1=<hmac_sha256>)を模した署名ヘッダを作る。"""
    timestamp = timestamp or int(time.time())
    signed_payload = f"{timestamp}.".encode() + payload_bytes
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


@pytest.fixture
def running_server(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webhook_server_module.WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield port
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _connection(port):
    return http.client.HTTPConnection("127.0.0.1", port, timeout=5)


def test_get_health_check_returns_ok(running_server):
    conn = _connection(running_server)
    conn.request("GET", "/")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.read() == b"ok"
    conn.close()


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_non_get_post_methods_are_rejected(running_server, method):
    conn = _connection(running_server)
    conn.request(method, "/", body=b"{}")
    resp = conn.getresponse()
    assert resp.status == 501
    resp.read()
    conn.close()


def test_post_without_signature_header_is_rejected(running_server):
    conn = _connection(running_server)
    body = b'{"id": "evt_test"}'
    conn.request("POST", "/", body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 400
    resp.read()
    conn.close()


def test_post_with_invalid_signature_is_rejected(running_server):
    conn = _connection(running_server)
    body = b'{"id": "evt_test"}'
    headers = {"Content-Type": "application/json", "Stripe-Signature": "t=1,v1=deadbeef"}
    conn.request("POST", "/", body=body, headers=headers)
    resp = conn.getresponse()
    assert resp.status == 400
    resp.read()
    conn.close()


def test_post_with_wrong_secret_signature_is_rejected(running_server):
    conn = _connection(running_server)
    body = b'{"id": "evt_test"}'
    headers = {
        "Content-Type": "application/json",
        "Stripe-Signature": _sign(body, "whsec_wrong_secret"),
    }
    conn.request("POST", "/", body=body, headers=headers)
    resp = conn.getresponse()
    assert resp.status == 400
    resp.read()
    conn.close()


def test_post_oversized_content_length_is_rejected_before_reading_body(running_server):
    """Content-Lengthが上限を超える場合、本文を実際には送らせずヘッダの時点で拒否する。"""
    conn = _connection(running_server)
    huge_length = webhook_server_module.MAX_CONTENT_LENGTH + 1
    conn.putrequest("POST", "/")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(huge_length))
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 413
    resp.read()
    conn.close()


def test_post_with_malformed_content_length_is_rejected(running_server):
    """Content-Lengthが数値でない場合、未処理の例外にならず安全に400を返す。"""
    sock = socket.create_connection(("127.0.0.1", running_server), timeout=5)
    try:
        sock.sendall(
            b"POST / HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: not-a-number\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
    finally:
        sock.close()
    status_line = response.split(b"\r\n", 1)[0]
    assert b"400" in status_line


def test_post_with_negative_content_length_is_rejected(running_server):
    sock = socket.create_connection(("127.0.0.1", running_server), timeout=5)
    try:
        sock.sendall(
            b"POST / HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: -1\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        response = sock.recv(4096)
    finally:
        sock.close()
    status_line = response.split(b"\r\n", 1)[0]
    assert b"400" in status_line


def test_post_with_wrong_content_type_is_rejected(running_server):
    conn = _connection(running_server)
    body = b'{"id": "evt_test"}'
    headers = {"Content-Type": "text/plain", "Stripe-Signature": _sign(body, WEBHOOK_SECRET)}
    conn.request("POST", "/", body=body, headers=headers)
    resp = conn.getresponse()
    assert resp.status == 400
    resp.read()
    conn.close()


def test_post_with_valid_signature_but_no_database_returns_500_not_crash(running_server):
    """署名検証を通過した後にDB接続が失敗しても、未処理の例外で落ちず500を返す。"""
    conn = _connection(running_server)
    body = b'{"id": "evt_test_valid_sig", "type": "unhandled.event.type"}'
    headers = {"Content-Type": "application/json", "Stripe-Signature": _sign(body, WEBHOOK_SECRET)}
    conn.request("POST", "/", body=body, headers=headers)
    resp = conn.getresponse()
    assert resp.status == 500
    resp.read()
    conn.close()


def test_missing_webhook_secret_env_returns_500_without_reading_signature(monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webhook_server_module.WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        conn = _connection(port)
        body = b'{"id": "evt_test"}'
        conn.request("POST", "/", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 500
        resp.read()
        conn.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
