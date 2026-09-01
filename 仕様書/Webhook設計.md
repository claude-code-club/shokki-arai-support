# Webhook設計（第19回: 継続課金・Webhook）

## ①目的・範囲

第18回は「初回契約・テスト決済・有料状態への反映」までだった。第19回では、Stripeの
Webhookを使い、継続運用に必要な次を完成させる。

- `checkout.session.completed`（成功URLに戻れなかった場合の取りこぼし防止・確実な反映）
- `customer.subscription.updated`（更新・状態変化の同期）
- `customer.subscription.deleted`（解約時にFreeへ降格）
- `invoice.payment_failed`（支払い失敗時の状態記録）
- Webhookの署名検証・重複イベント対策
- 解約フロー（Stripe Billing Portalを使った、admin専用の「サブスクを管理」導線）

Stripeテストモード限定・世帯単位という第18回の前提を引き継ぐ。production・mainには
一切触れない。

## ②なぜWebhookが必要か（第18回だけでは不十分な理由）

第18回の`confirm_checkout_session()`は、ブラウザが`success_url`へ戻ってきたタイミングで
Stripe APIへ問い合わせて反映する「その場限りの確認」である。次のケースで取りこぼす。

- 決済完了後、ブラウザが閉じられる／通信が切れて`success_url`へ戻れなかった場合
- 契約後の更新・解約・支払い失敗など、ブラウザ操作を伴わずStripe側だけで起きる変化

Stripe公式も、確実な状態同期にはWebhookを正とすることを推奨している。第19回からは
Webhookを「真実の同期経路」とし、第18回の`success_url`確認は「体感を早くするための
その場反映」という補助的な位置づけに変える（両方とも同じ`confirm`系ロジックへ収束させ、
二重に処理しても安全なようにする）。

## ③アーキテクチャ判断: WebhookをどこでHTTP受信するか

StreamlitはFlaskのような「好きなHTTPパスを追加する」公式手段を持たない
（Tornado内部への直接フック等は非公式・壊れやすいハックのため採用しない）。

比較した案:

| | Streamlit内部にハックで追加 | 別サービスとして新設（採用） |
|---|---|---|
| 公式サポート | 非公式・将来のStreamlitアップデートで壊れる可能性 | 標準的なWSGI/HTTPサーバーで安定 |
| 影響範囲 | 既存アプリの起動プロセスに直接手を入れる | 既存の食器洗いサポートアプリ(web)には触れない |
| Railway上の構成 | 追加サービス不要 | 新しいサービス1つ追加（同一プロジェクト・staging環境） |

**別サービス（軽量なPython製Webhook受信サーバー、`scripts/webhook_server.py`）を新設する案を
採用する。** 既存の`web`サービス（Streamlitアプリ本体）には一切変更を加えず、同じ
PostgreSQL（`DATABASE_URL`を共有）へ書き込む、独立したRailwayサービスとして動かす。

この新サービスの追加・Stripe側でのWebhookエンドポイント登録・`STRIPE_WEBHOOK_SECRET`の
設定は、いずれも外部設定（operatorの画面操作）が必要になるため、コード・DB・テストの
実装が完了した時点で一度停止し、1画面ずつ案内する（仕様書/Stripe課金設計.md⑥・第18回と
同じ方針）。

## ④DB設計

Webhookイベントの重複処理防止のため、`processed_stripe_events`を新設する。

```sql
CREATE TABLE IF NOT EXISTS processed_stripe_events (
    stripe_event_id  TEXT PRIMARY KEY,
    event_type       TEXT NOT NULL,
    processed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

- `stripe_event_id`をPRIMARY KEYとすることで、同じイベントIDの再処理を
  `INSERT ... ON CONFLICT (stripe_event_id) DO NOTHING`で防ぐ（挿入できなければ
  「既に処理済み」と判断し、何もせず200を返す）
- `tenant_subscriptions`（第18回で新設済み）は変更しない。既存の`plan`/`status`/
  `stripe_customer_id`/`stripe_subscription_id`/`current_period_end`列をそのまま使う

## ⑤イベントごとの処理方針

| イベント | 処理 |
|---|---|
| `checkout.session.completed` | 第18回の`confirm_checkout_session()`と同じ検証（mode・状態・metadataのtenant_id一致）を経て`tenant_subscriptions`をStandardへ反映。`success_url`確認と処理内容を共通化し、どちらが先に届いても二重反映しない |
| `customer.subscription.updated` | `subscription.metadata.tenant_id`（Checkout Session作成時にsubscriptionへも引き継がれる）を頼りに世帯を特定し、`plan`/`status`/`current_period_end`を最新化する |
| `customer.subscription.deleted` | 対象世帯を`plan='free'`, `status='canceled'`へ更新する（即時降格。猶予期間は設けない、最小構成） |
| `invoice.payment_failed` | 対象世帯の`status`を`'past_due'`に更新するのみ（`plan`は変更しない、自動的なFree降格は行わない。督促・猶予期間の設計は将来課題とする） |

いずれのイベントも、世帯の特定に失敗した場合（該当tenant_idが存在しない等）はDBへ
書き込まず、エラーとして記録するだけに留める（未知の世帯を誤って作成しない。
仕様書/認証基盤設計.md⑥・第17回と同じ「自動で何かを作らない」方針を踏襲）。

## ⑥署名検証

`stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)`を使い、
Stripeが送信したイベントであることを検証してから処理する。署名が不正な場合は
処理を行わず、400エラーを返す（DBへは一切触れない）。

`STRIPE_WEBHOOK_SECRET`はRailwayの新サービス（Webhook受信サーバー）側にのみ設定する
環境変数とし、Gitへは書かない。

## ⑦解約フロー（Stripe Billing Portal）

第19回でアプリ内に解約ボタンを自作せず、Stripeが提供する「Billing Portal」（顧客が
自分でプラン変更・解約・支払い方法変更ができるStripeホスト型の画面）を使う。

- admin専用の「サブスクを管理」ボタンを追加する（Standard契約中のみ表示）
- サーバー側で`stripe.billing_portal.Session.create(customer=customer_id, return_url=...)`を
  作成し、そのURLへ遷移させる（Checkout Sessionと同じ、サーバー側作成・admin限定の
  パターン）
- 実際の解約操作はStripe側の画面で行われ、結果は`customer.subscription.deleted`
  Webhookで反映される（アプリ側は解約処理そのものを実装しない）

## ⑧成功URL確認とWebhookの統合

`billing.py`の`confirm_checkout_session()`と、Webhookの`checkout.session.completed`
ハンドラは、同じ検証ロジック（mode・状態・metadataのtenant_id一致）を経由する。
どちらが先に届いても、`tenant_subscriptions`への反映は`stripe_checkout_session_id`の
UNIQUE制約とWHERE guardにより1回しか適用されない（第18回で実装済みの冪等設計を
そのまま再利用する）。

## ⑨テスト方針

第18回と同じく、Stripe通信は注入可能にし、実際のStripe接続なしでテストする。

- 署名検証：正しい署名は通過、不正な署名は拒否されDBに触れないことをテスト
- 重複イベント：同じ`stripe_event_id`を2回処理しても2回目は無反応（冪等）
- 各イベントタイプの処理内容（Standard反映・状態同期・解約時のFree降格・支払い失敗時の
  status更新）
- 未知のtenant_idを含むイベントはDBへ書き込まないこと

## ⑩第19回でやらないこと（将来課題）

- 支払い失敗時の自動Free降格タイミング・猶予期間の設計
- 複数プラン・年額プラン等への拡張
- production・mainへの反映（この回とは別に改めて承認を得てから着手する）
