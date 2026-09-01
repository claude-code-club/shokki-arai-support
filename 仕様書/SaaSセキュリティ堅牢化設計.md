# SaaSセキュリティ堅牢化設計（第21回: SaaSのセキュリティ堅牢化）

## ①目的・範囲

第16回〜第20回で、Auth0認証・世帯単位マルチテナント・Stripe Sandbox課金・
継続課金Webhook・プラン制限とメータリングを実装済み。今回は新機能を追加する回
ではなく、現在のSaaSに対して「攻撃・誤操作・情報漏えいが起きる前提」で守りを
固める回である。読み取り専用の監査（②〜④）→設計（⑤〜⑨）→
Critical/Highの実装、の順で進める。

Stripe Sandbox限定。production・mainには一切触れない。stagingの実データも
変更しない（読み取りのみ）。

## ②保護対象

| 保護対象 | 内容 |
|---|---|
| 世帯データ | 各世帯の食器洗い記録（`records`）、世帯名（`tenants`） |
| 課金状態 | `tenant_subscriptions`（plan・status・Stripe ID） |
| 利用量 | `tenant_usage`（今月の振り返り利用回数） |
| 個人情報 | `users`（email・auth_subject） |
| 秘密値 | `DATABASE_URL`、`STRIPE_SECRET_KEY`、`STRIPE_WEBHOOK_SECRET`、
  Auth0クライアントシークレット、`ANTHROPIC_API_KEY` |
| サービス可用性 | `web`・`stripe-webhook`の2 Railwayサービス、staging Postgres |

## ③想定する攻撃者

- **未認証の外部者**: アプリのURL・Webhookエンドポイントに到達できるが、
  Auth0アカウントもStripe資格情報も持たない
- **ログイン済みだが悪意のある一般ユーザー**: 自分の世帯のFree/member権限しか
  持たないが、ブラウザの開発者ツールやAPI直接呼び出しで制限を突破しようとする
- **同一世帯内の他ユーザーではなく、無関係な別世帯のユーザー**: 自分のTENANT_ID
  を使って他世帯のリソースへアクセスしようとする
- **Stripeを装う第三者**: 署名なし・偽署名のWebhookリクエストを送りつける
- 本調査は**他者のサービスへの侵入試験は行わない**（禁止事項）。上記はすべて
  「このアプリ自身に対して、このアプリのURLの範囲内で」という前提。

## ④攻撃経路と現在の防御・不足している防御

### 1. 世帯間のデータ越境

- **現在の防御**: `records`・`tenant_subscriptions`・`tenant_usage`・
  `tenant_memberships`はすべて`tenant_id`にFOREIGN KEY REFERENCES `tenants(id)`
  を持ち（[マルチテナント設計.md](マルチテナント設計.md)⑤、[プラン制限・メータリング設計.md](プラン制限・メータリング設計.md)④）、
  `db.py`の`_for_tenant`系関数はすべて`tenant_id`をデフォルト値なしの
  キーワード専用引数にしている（[db.py](../streamlit/db.py)）。`tenant_id`は
  ブラウザ入力・URLパラメータからは一切受け取らず、`auth.resolve_tenant_context()`
  がログインユーザーの`tenant_memberships`だけを根拠にサーバー側で解決する
  （[auth.py](../streamlit/auth.py)）
- **既存テストで確認済み**: `test_resolve_tenant_context_cross_tenant_isolation`・
  `test_storage_cross_tenant_isolation`・`test_increment_does_not_affect_other_tenant`・
  `test_stripe_checkout_session_id_cannot_be_reused_across_tenants`
- **不足**: DB接続ロールが`postgres`（スーパーユーザー、後述⑥）のため、
  アプリのSQLバグやコードレビュー漏れが将来発生した場合、DB側の防御（RLS）が
  一切効かない多層防御の欠如がある（Medium、⑥で設計）

### 2. admin/memberの権限突破

- **現在の防御**: 世帯名変更（`storage.rename_tenant`）・Checkout Session作成
  （`billing.create_checkout_session`）・確定（`confirm_checkout_session`）・
  Billing Portal（`create_billing_portal_session`）はすべて、呼び出された関数
  自身の内部で`role != "admin"`を検証し、`PermissionDeniedError`/
  `StorageConfigError`を送出する。UIの非表示（`if USER_ROLE == "admin":`）は
  あくまで見た目の制御であり、サーバー側の最終防衛線は各関数の内部にある
- **既存テストで確認済み**: `test_rename_tenant_requires_admin_role`・
  `test_create_checkout_session_rejects_member`・
  `test_confirm_checkout_session_rejects_member`・
  `test_create_billing_portal_session_requires_admin`
- **不足**: 上記のadmin検証は`billing.py`と`storage.py`にそれぞれ個別に
  `if role != "admin": raise ...`という形で重複している。関数を1つ追加する
  たびに書き忘れるリスクがある（Medium、⑦で共通化）

### 3. IDやURLの差し替えによる不正操作

- **現在の防御**: `session_id`はURLクエリパラメータ由来で改ざん可能な値だが、
  `confirm_checkout_session()`はその値を**信用せず**、Stripe APIから
  Session/Subscriptionを取得し直し、mode・status・metadata内`tenant_id`が
  現在ログイン中の世帯と一致するかをサーバー側で再検証してから初めてDBへ反映する
  （[Stripe課金設計.md](Stripe課金設計.md)⑤）。一致しない場合は`TenantMismatchError`
- **既存テストで確認済み**: `test_confirm_checkout_session_rejects_tenant_id_mismatch`・
  `test_reaching_success_url_alone_does_not_upgrade_plan`
- **不足**: `session_id`自体の長さ・形式チェックがなく、任意の長さの文字列を
  そのままStripe APIへ渡している（Low〜Medium、⑦で軽量な長さ検証を追加）

### 4. Stripe Webhookの偽装・再送・巨大リクエスト

- **現在の防御**:
  - 署名検証（`stripe.Webhook.construct_event()`）を**DB接続より前**に行い、
    不正な署名は400を返してDBに一切触れない（[scripts/webhook_server.py](../scripts/webhook_server.py)）
  - 重複防止: `processed_stripe_events.stripe_event_id`をPRIMARY KEYとし、
    イベント記録とハンドラ処理を同一トランザクションでcommitする
    （[webhook.py](../streamlit/webhook.py) `process_event()`）
  - 未知の`tenant_id`・`subscription_id`は推測で世帯を作らず無視する
- **既存テストで確認済み**: `test_process_event_is_idempotent_for_same_event_id`・
  `test_checkout_session_completed_skips_missing_tenant_metadata`
- **不足（Critical）**: `do_POST()`が`Content-Length`ヘッダの値を**上限なしで
  そのまま**`self.rfile.read(content_length)`に渡しており、巨大な
  `Content-Length`を送るリクエスト1本でメモリを圧迫できる（DoS）。また
  `Content-Length`ヘッダが数値でない場合、`int()`が例外を送出し未処理のまま
  伝播する。`Content-Type`の検証も無い
- **不足（テスト欠如）**: `scripts/webhook_server.py`は`tests/`配下に
  対応するテストファイルが**存在しない**（署名不正・巨大本文・GET/PUT/DELETE
  拒否のいずれも自動テストされていない）

### 5. 秘密値・個人情報・Stripe識別子のログ漏えい

- **現在の防御**:
  - `webhook_server.py`は「種別・成否・event_idなどの非機密の要約情報だけ」
    を`print()`し、リクエスト本文・署名・例外の詳細は一切出力しない
  - `write_streamlit_secrets.py`は生成したファイルパスのみ出力し、値は出さない
  - `app.py`のすべての`st.error()`は固定の一般向け文言で、例外オブジェクト
    (`str(e)`)を画面へ出していない（`auth.AccessDeniedError`の
    `st.error(str(e))`のみ例外だが、このメッセージ自体が固定文言で秘密値を
    含まない設計になっている）
  - 各migrationスクリプトの`except psycopg.Error:`は生の例外を出力せず、
    固定の安全なメッセージのみ出力する
- **不足（Low）**: `encourage.py`が`except Exception as e:` を
  `sys.stderr`へそのまま出力している。Anthropic SDKの例外は通常APIキー自体を
  含まないが、明示的に「値を出さない」設計にはなっていない

### 6. DB接続権限が強すぎる問題（Critical）

- **staging Postgresで実施した読み取り専用確認結果**（2026-09-02、岩瀬様が
  Railway Data→Query画面で実行、パスワードローテーション後）:

  | 列 | 値 |
  |---|---|
  | `current_user` | `postgres` |
  | `rolsuper` | `true` |
  | `rolbypassrls` | `true` |
  | `rolcreaterole` | `true` |
  | `rolcreatedb` | `true` |

- アプリ（`web`・`stripe-webhook`）は`DATABASE_URL`経由でPostgreSQLの
  スーパーユーザー`postgres`として接続している。`rolbypassrls=true`のため、
  **将来RLSを有効化しても、このロールにはRLSが一切効かない**。また
  `rolcreaterole`/`rolcreatedb`により、アプリのコードにバグがあった場合の
  被害範囲がデータベース全体・クラスタ全体に及びうる
- 低権限ロール・RLSの設計は⑥に記載。**ロール作成・DATABASE_URL変更・
  RLS有効化は、設計と隔離テスト（CIのpostgres:16サービス上）を先に行い、
  staging DBへ実際に適用する直前で一度停止して報告する**（第21回の指示の
  停止条件に該当するため、今回はここまでで止める）

### 7. 障害時に有料機能や他世帯データが誤開放される問題

- **現在の防御**: `billing.py`・`metering.py`はDB接続・クエリ失敗時に
  `BillingUnavailableError`/`MeteringUnavailableError`を送出し、`app.py`側は
  これを「Standardではない」「使用不可」として扱う（fail closed、
  [プラン制限・メータリング設計.md](プラン制限・メータリング設計.md)⑨）。
  `storage.py`もPostgreSQL障害時にJSONへ自動フォールバックしない
- **既存テストで確認済み**: `test_metering_unavailable_when_database_not_configured`・
  `test_storage_unavailable_does_not_touch_json`
- **不足**: 特になし（既存設計が既にfail closedを徹底している）。今回は
  ここに手を加えず、他の変更で退行させないことを確認する

### 8〜9. SQLインジェクション・入力検証

- **現在の防御**: `db.py`の全クエリがプレースホルダー（`%s`/`%(name)s`）を
  使用しており、文字列連結によるSQL構築は1箇所も無い（grep調査で確認済み）
- **不足（Medium）**: 世帯名変更（`st.text_input("世帯名")`）に長さ・形式の
  検証が無く、任意長の文字列・制御文字がそのまま`tenants.name`へ保存される
  （XSSではない。Streamlitの`st.write`はデフォルトでエスケープするため
  画面表示上の実害は無いが、無制限の長さはDB肥大化・表示崩れのリスクがある）

### 10. Python依存パッケージの既知脆弱性

- **不足（High）**: CIに依存関係の脆弱性検査が組み込まれていない
  （`.github/workflows/test.yml`は`pytest`実行のみ）

### 11. `.gitignore`

- **現在の防御**: `.streamlit/secrets.toml`・`streamlit/data/`（JSONバックアップ
  含む）・`.env`/`.env.*`は除外済み
- **不足（Low）**: `*.sql`（DBダンプ）・`*.log`の汎用パターンが無い。現状これらの
  ファイルを生成するスクリプトは無いが、将来の事故防止として追加する

### 12. requirements.txtの一致・production/main無変更

- `requirements.txt`と`streamlit/requirements.txt`は内容が完全一致（diffで確認済み）。
  問題なし
- `git status`は`staging`ブランチでクリーン、`main`ブランチには一切触れていない

## ⑤重大度分類まとめ

| # | 項目 | 重大度 | 対応方針 |
|---|---|---|---|
| 1 | Webhookの巨大本文によるDoS・Content-Length不正時の未処理例外 | **Critical** | 第21回で実装 |
| 2 | DB接続ロールがスーパーユーザー(RLS等が無意味) | **Critical** | 第21回で設計まで。適用は次回以降(停止条件) |
| 3 | `scripts/webhook_server.py`の自動テスト欠如 | High | 第21回で実装 |
| 4 | CIに依存関係脆弱性検査が無い | High | 第21回で実装 |
| 5 | admin検証ロジックの重複(共通化されていない) | Medium | 第21回で実装(共通認可関数) |
| 6 | 世帯名の長さ・形式検証が無い | Medium | 第21回で実装 |
| 7 | Webhookの`Content-Type`検証が無い | Medium | 第21回で実装 |
| 8 | `session_id`の長さ検証が無い | Low〜Medium | 第21回で実装 |
| 9 | `.gitignore`に`*.sql`/`*.log`が無い | Low | 第21回で実装 |
| 10 | `encourage.py`の例外を`{e}`のまま出力 | Low | 第21回で実装 |
| 11 | DB接続ロールの実際の権限剥奪・RLS有効化 | Critical(適用) | **将来対応**(設計のみ今回) |

## ⑥DB権限の設計（今回は設計のみ、適用しない）

### 現状

`web`・`stripe-webhook`とも、Postgresサービスの`DATABASE_URL`（＝スーパー
ユーザー`postgres`の接続文字列）をそのまま使っている。

### 設計方針

1. **アプリ専用ロール `app_runtime` を新設**（非スーパーユーザー）
   ```sql
   CREATE ROLE app_runtime WITH LOGIN PASSWORD '<secret>' NOSUPERUSER
     NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
   GRANT SELECT, INSERT, UPDATE, DELETE ON
     records, tenants, tenant_memberships, tenant_subscriptions,
     tenant_usage, users, processed_stripe_events
     TO app_runtime;
   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_runtime;
   -- 対象テーブル以外(将来のテーブル含む)へのデフォルト権限は与えない
   ```
2. **migration用接続と通常アプリ接続を分離**: `CREATE TABLE`/`ALTER TABLE`は
   引き続き所有者ロール（現行の`postgres`、または将来作る`app_owner`）で行う。
   `web`・`stripe-webhook`サービスの`DATABASE_URL`だけを`app_runtime`用の
   接続文字列に差し替え、`scripts/migrate_*.py`はこれまで通り所有者ロールで
   手動実行する運用を維持する
3. **RLSは`app_runtime`が非スーパーユーザー・`NOBYPASSRLS`になって初めて
   意味を持つ**。世帯単位のテーブル（records・tenant_subscriptions・
   tenant_usage・tenant_memberships）に`tenant_id`列を条件にした
   `USING (tenant_id = current_setting('app.tenant_id')::uuid)`のような
   ポリシーを設計する。ただし、アプリは1リクエストごとに`SET app.tenant_id`
   のような追加処理が必要になり、Streamlitのコネクション管理（`storage.py`の
   `conn = _get_postgres_connection() / conn.close()`という都度接続方式）
   に組み込む設計が必要なため、**具体的なポリシー文とアプリ側の実装は
   次回以降のTaskとして設計を継続する**
4. **isolated test**: 上記のロール作成・GRANT文は、CIのpostgres:16サービス上で
   隔離的に検証できる（本Task範囲では未実施。次回、CIに検証ジョブを追加する
   ことを推奨）

### 適用時の停止条件（今回はここで止める）

- `CREATE ROLE`の実行
- Railway `Postgres`/`web`/`stripe-webhook`の`DATABASE_URL`変更
- `ALTER TABLE ... ENABLE ROW LEVEL SECURITY`の実行

これらはすべて「影響の大きい変更」に該当するため、上記の設計をレビュー
いただいたうえで、改めて適用の可否をご判断いただく。

## ⑦第21回で実装する項目

1. `scripts/webhook_server.py`
   - `Content-Length`に上限（65536バイト）を設け、超過時は本文を読まずに413相当
     （413が無ければ400）で拒否する
   - `Content-Length`が数値でない場合は400で安全に拒否する（未処理例外を無くす）
   - `Content-Type`が`application/json`系でない場合は400で拒否する
   - 上記を追加しても、既存の「署名検証より前にDBへ触れない」順序は変えない
2. `streamlit/authz.py`（新設）: 共通認可関数`require_admin(role)`を追加し、
   `billing.py`・`storage.py`のadmin検証をこの1関数に集約する（重複排除）
3. `streamlit/storage.py`の`rename_tenant()`: 世帯名の長さ（1〜100文字）・
   制御文字を含まないことをサーバー側で検証する`InvalidInputError`を追加
4. `streamlit/billing.py`の`confirm_checkout_session()`: `session_id`の長さ上限
   （500文字）をサーバー側で検証してからStripe APIを呼ぶ
5. `streamlit/encourage.py`: 例外の値そのものではなく、例外クラス名だけを
   ログに出す（`type(e).__name__`）
6. `.gitignore`に`*.sql`・`*.log`を追加
7. `.github/workflows/test.yml`に`pip-audit`（依存関係の既知脆弱性検査）を追加
8. 上記すべてに対応する自動テストを追加（⑨参照）

## ⑧今回は直さず将来対応にする項目

- DB専用ロール(`app_runtime`)の実際の作成・`DATABASE_URL`切り替え・RLS有効化
  （⑥参照、影響大のため停止・要判断）
- Row Level Securityの具体的なポリシー文とアプリ側の`SET app.tenant_id`実装
- `past_due`状態からの自動Free降格・猶予期間（第19回から継続する将来課題）
- 30日間の詳細分析の高度化（第20回から継続する将来課題）
- レート制限（Webhookエンドポイントへの単位時間あたりのリクエスト数制限）。
  Railway側のインフラ的な保護に依存する部分が大きく、アプリ側の実装は
  次回以降のTaskとする

## ⑨切り戻し方法

- すべての変更はfeatureブランチ上で行い、`staging`へのmerge前にPRレビューと
  CI通過を確認する
- `staging`への反映後に問題が見つかった場合、Railway上で当該デプロイの1つ前の
  デプロイへロールバックする（Railwayの「Redeploy」機能で過去のデプロイを
  再指定）
- DB側の変更（テーブル追加等）は今回発生しない（⑥は設計のみで未適用のため）
- `.github/workflows/test.yml`の変更（pip-audit追加）は、CIが赤くなった場合は
  当該ステップだけをrevertすれば良く、既存のpytestジョブには影響しない

## ⑩テスト項目

第21回の指示にある【自動テスト必須項目】に対応する形で実装する。

| 項目 | 状態 |
|---|---|
| 世帯Aから世帯Bの記録を読めない/追加・削除できない | 既存テストで確認済み(維持) |
| 世帯Aから世帯Bの使用量を操作できない | 既存テストで確認済み(維持) |
| 世帯Aから世帯Bの課金状態を変更できない | 既存テストで確認済み(維持) |
| memberがadmin操作を直接呼んでも拒否される | 既存+`authz.require_admin`の新規テスト |
| FreeがStandard限定関数を直接呼んでも拒否される | 既存テストで確認済み(維持) |
| 不正UUIDを拒否する | 既存テストで確認済み(維持) |
| 空文字・巨大文字列・制御文字を拒否する | 新規(世帯名検証) |
| SQLインジェクション形式の入力を安全に処理する | 新規(プレースホルダー経由で無害化されることを回帰確認) |
| Webhook署名不正を拒否する | 新規(`webhook_server`統合テスト) |
| Webhookの巨大本文を拒否する | 新規(`webhook_server`統合テスト) |
| WebhookのGET／PUT／DELETEを拒否する | 新規(`webhook_server`統合テスト) |
| Webhook再送で二重反映されない | 既存テストで確認済み(維持) |
| ログへ秘密値や個人情報が出ない | 新規(`encourage.py`) + 既存設計の確認 |
| DB障害時に権限を誤って開放しない | 既存テストで確認済み(維持) |
| 日々の記録、認証、課金、プラン制限に回帰がない | 既存テスト全件の継続PASSで確認 |
| PostgreSQL連携を含むCI全件PASS | CI実行で確認 |

## ⑪完了条件

- Critical/Highの未解決脆弱性0件（DB権限の実適用は設計完了をもって
  「今回の完了条件」を満たすものとし、適用自体は別途停止・承認事項とする）
- 世帯間越境・admin/member権限突破・Free/Standard制限突破のいずれも無し
- Webhookの偽装・再送・巨大入力を安全に拒否
- 秘密値・個人情報のログ漏えい無し
- 依存関係検査PASS・CI全件PASS
- staging実機正常・production/main無変更・Stripe Sandboxのみ

## ⑫残存リスク

- **DB接続ロールが依然スーパーユーザーのまま**（⑥は設計のみ、次回以降に
  実際のロール切り替え・RLS適用が必要）。アプリコード側の対策（tenant_id
  必須引数・FK制約）が唯一の防御層であり、多層防御にはなっていない
- Webhookのレート制限は未実装（Railwayインフラ側の保護に依存）
- `past_due`状態が続いた場合の自動Free降格が無いため、支払いが恒久的に
  失敗した世帯がStandard機能を使えない状態のまま残り続ける（意図的な仕様、
  第19回から継続）
- RLSポリシーの具体的な設計（`SET app.tenant_id`をどこで発行するか）が
  未確定のため、⑥のロール切り替えだけでは世帯分離の多層防御として不完全
