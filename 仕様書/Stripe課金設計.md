# Stripe課金設計（第18回: 課金① — Stripeサブスク決済）

## ①目的・範囲

第16回（マルチテナント設計）・第17回（認証基盤）の上に、Stripeのテストモードを使った
サブスクリプション課金を追加する。今回（第18回）で完成させる範囲は次まで。

- 初回契約（Checkout Sessionの作成）
- テストモードでの決済
- サーバー側検証を経た「有料状態への反映」

次は第18回では扱わない（第19回で扱う）。

- 継続請求・更新
- 解約
- 支払い失敗時の扱い
- Webhookによる非同期イベント処理（署名検証・重複イベント対策を含む）

本番（production・main）へは一切反映しない。stagingのみ。実カード・実課金・liveキーは使用しない。

## ②課金単位・プラン

- 課金単位はユーザー個人ではなく「世帯（tenant）」。1世帯＝1サブスクリプション。
- プランは2つ。
  - `free`：0円（既定値。行が無いテナントもfree扱い）
  - `standard`：月額500円（税込想定の仮価格）
- プラン変更（Standardへの契約）を行えるのは、その世帯の`admin`のみ。`member`は不可
  （第17回のadmin/member区別をそのまま利用。仕様書/認証基盤設計.md⑨と同じ考え方）。

## ③責務分離（Stripe側 / アプリDB側）

第17回の「Auth0側 / アプリDB側」の分離と同じ考え方を踏襲する。

- Stripe側が真実の源: 顧客情報・カード情報・サブスクリプションの契約状態そのもの
- アプリDB側は表示・アクセス制御用のキャッシュ: 現在のプラン・状態・Stripe側IDへの参照のみ
- カード情報はアプリ・DBのどこにも一切保存しない（Stripe Checkoutのホスト型フォームに委ねる）

## ④DB設計

既存`tenants`へ課金列を直接追加する案と、`tenant_subscriptions`を分離する案を比較した。

| | tenantsへ直接追加 | tenant_subscriptions分離（採用） |
|---|---|---|
| 責務 | 世帯の基本情報と課金状態が混在 | 世帯の基本情報と課金状態を分離 |
| 将来の拡張（第19回のWebhookイベント履歴等） | tenantsが肥大化し続ける | 課金関連テーブルとして独立して拡張しやすい |
| 「world未契約＝行が無い」の表現 | tenantsは全世帯に必ず1行あるため不自然 | 行の有無で自然にfree/未契約を表現できる |

`tenant_subscriptions`を分離する案を採用する。世帯にサブスクリプションの行が無い場合は
free扱いとする（全世帯にfree行を作る必要がない、最小構成）。

```sql
CREATE TABLE IF NOT EXISTS tenant_subscriptions (
    tenant_id                   UUID PRIMARY KEY REFERENCES tenants(id),
    plan                        TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'standard')),
    status                      TEXT NOT NULL DEFAULT 'active',
    stripe_customer_id          TEXT,
    stripe_subscription_id      TEXT,
    stripe_checkout_session_id  TEXT UNIQUE,
    current_period_end          TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

- `tenant_id`をPRIMARY KEY兼外部キーとすることで「1世帯1行・UNIQUE」を保証する
- `stripe_checkout_session_id`にUNIQUE制約を付け、同じCheckout Sessionが別世帯の行へ
  適用されることをDBレベルでも防ぐ（メタデータ検証と合わせた二重の防御）
- 同じsession_idの再処理は、UPDATE文に`WHERE stripe_checkout_session_id IS DISTINCT FROM ...`
  という条件を付けたUPSERTで冪等にする（既に同じsession_idが記録済みなら何も更新しない）

## ⑤画面からStripeまでの流れ（サーバー側検証を必ず経由する）

1. admin がアプリ内の「Standardを購入」ボタンを押す
2. アプリのサーバー側（Streamlit実行プロセス）が、ログイン済みユーザーの`tenant_id`と
   `role`をサーバー側で確定した上で、Stripe Checkout Session を**サーバー側で**作成する
   （`mode="subscription"`、`metadata={"tenant_id": str(tenant_id)}`）
3. ユーザーをStripeのCheckoutページへ遷移させる（Stripeがカード情報を扱う）
4. 決済完了後、Stripeが`success_url`（`session_id`をクエリパラメータに含む）へ戻す
5. アプリは**success_urlの表示だけでは絶対にStandardへ変更しない**。戻ってきた`session_id`を
   使い、サーバー側でStripe APIからCheckout Sessionを取得し直し、次のすべてを検証する。
   - `mode`が`"subscription"`であること
   - `status`が`"complete"`であり、紐づくsubscriptionの状態が有効（`active`または`trialing`）
     であること
   - `metadata.tenant_id`が、現在ログイン中の世帯の`tenant_id`と完全一致すること
6. すべて検証を通過した場合のみ、`tenant_subscriptions`をStandardへUPSERTする
7. キャンセル時（`cancel_url`へ戻ってきた場合）は何もしない（Freeのまま）

## ⑥権限チェック（二重の防御）

第17回の`storage.rename_tenant`と同じ考え方で、UI側でボタンを隠すだけでなく、
サーバー側の関数呼び出しの直前でも必ずrole検証を行う。

- `create_checkout_session()`：呼び出し時点の`role`が`"admin"`でなければ拒否
- `confirm_checkout_session()`：同じく`role`が`"admin"`でなければ拒否（defense in depth）

## ⑦テストからの分離（Stripe呼び出しの注入）

`auth.py`が実際のAuth0接続なしでテストできるよう`get_conn`を注入可能にしたのと同じ考え方で、
`billing.py`の関数は`stripe_client`を引数として受け取れるようにする（省略時のみ、実際の
`stripe`パッケージを環境変数`STRIPE_SECRET_KEY`で初期化して使う）。テストでは、
`checkout.Session.create()` / `.retrieve()`を模した偽オブジェクトを渡すことで、実際の
Stripe通信を一切行わずに大半のロジック（権限チェック・tenant_id一致検証・状態検証・
冪等性・エラー時の非変更）を検証できる。

## ⑧設定（Gitに書かない値）

Railway側の環境変数として以下を設定する（値そのものはGit・チャットに一切書かない）。

- `STRIPE_SECRET_KEY`：Stripeテストモードのシークレットキー（`sk_test_...`）
- `STRIPE_PRICE_ID_STANDARD`：Standardプラン（月額500円）のPrice ID（`price_...`）
- `BILLING_ENABLED`：`true`の場合のみ課金UI・課金フローを有効にする（`AUTH_ENABLED`と
  同じ考え方のフラグ。既定は無効。第17回のAUTH_ENABLEDと同様、未設定の間は課金セクション
  自体を表示しない）

`BILLING_ENABLED`は`AUTH_ENABLED`が有効な場合のみ意味を持つ（課金は世帯・adminという
第17回の概念に依存するため）。

## ⑨エラー処理・表示

- Stripe API呼び出し自体が失敗した場合（ネットワークエラー・設定不備等）：DBには一切
  書き込まず、Freeのまま。画面には秘密情報を含まない一般向けメッセージのみ表示する
  （第16回・第17回の`StorageUnavailableError`と同じ方針）
- `STRIPE_SECRET_KEY`・`STRIPE_PRICE_ID_STANDARD`が未設定なのに`BILLING_ENABLED=true`の
  場合：`BillingConfigError`を送出し、安全に停止する（実際にStripeへ接続する前に検知する）
- DB書き込み中にエラーが起きた場合：rollbackし、Standardへは反映しない

## ⑩ログ・報告における秘密情報の扱い

第17回で確立した運用（auth_subject・メールアドレス・秘密値をチャット・ログに出さない）を
継続する。加えて、Stripeの`stripe_customer_id`・`stripe_subscription_id`の完全値も
チャット・報告には出さない（必要な場合は末尾数文字のみ、または「設定済み/未設定」のような
真偽値としてのみ言及する）。

## ⑪第19回へ引き継ぐ事項

- Webhook（`checkout.session.completed`・`customer.subscription.updated`・
  `customer.subscription.deleted`・`invoice.payment_failed`等）の受信・署名検証・
  重複イベント対策
- 解約フロー（Customer Portal、またはアプリ内解約ボタン）
- 支払い失敗時の扱い（猶予期間、Freeへの自動降格タイミング）
- production・mainへの反映（この回とは別に改めて承認を得てから着手する）
