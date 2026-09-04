# 検索できるDB設計（第22課題: データを"本格的"に扱う）

## ①3行仕様

- 誰に: 食器洗いサポートを使う世帯のみんなに
- 何を: 記録するときに「ひとこと(任意)」を残せるようにし、あとから見返せるようにしたい
- どう動く: 記録ボタンを押す時に一言メモ(空欄可)を添えられる。一覧画面でキーワード検索・
  新しい順/古い順の並び替えができる

## ②目的・範囲

現在の`records`テーブルは`record_date`(日付)だけを保持しており、検索対象になる
自由記述の項目が無い。第22課題「検索できるDB」を実装するには、まず検索対象になる
項目(メモ)を新設する必要がある。

**postgresバックエンド専用の機能とする**（jsonバックエンドには追加しない）。理由:

- 課題タイトルが「検索できる**DB**」であり、postgresが実際のDB、jsonは単なる
  ファイル保存であるため、検索・並び替えという要件はpostgres側にこそふさわしい
- jsonバックエンド(`logic.py`)は`dates`(日付の集合)というシンプルな構造の上に
  第14回で構築したバックアップ・復元・schema_versionの仕組みが既にあり、ここへ
  メモ(日付ごとの付加情報)を持ち込むと構造を大きく変える必要があり、既存の
  202件のテストを広範囲に壊すリスクがある(第16回のtenant_id対応時と同じ理由で、
  jsonバックエンドは単一テナント前提のまま据え置く、という前例と同じ判断)
- 実際に稼働しているstaging/productionは`STORAGE_BACKEND=postgres`のため、
  実運用上の制約にはならない

## ★ChatGPT監査(2026-09-03〜04)の指摘と対応

PR #30作成後、監査ZIP(`PR30_audit_20260903.zip`)へのレビューで3件の指摘を受け、
いずれも対応済み(2026-09-04)。**PR #30はこの対応が完了するまでマージ・staging
migrationとも停止していた**。

| # | 深刻度 | 指摘内容 | 対応 |
|---|---|---|---|
| 1 | Critical | デプロイ順序が逆。`staging`マージ→自動デプロイ後、`memo`列が無いままだと記録・検索が失敗する | ⑥実行方法を**migration先行**の順序へ明文化(下記参照) |
| 2 | High | 検索欄に`max_chars`が無く、101文字以上で`InvalidInputError`(ただし実際には`StorageConfigError`のサブクラスのため`app.py`側の`except (StorageConfigError, StorageUnavailableError)`で捕捉済み、画面クラッシュはしない。実機・単体テストで確認済み) | `max_chars=storage.KEYWORD_MAX_LENGTH`を追加し、`except storage.InvalidInputError`を専用に分離して「◯文字以内で」という具体的な案内文を表示するよう改善。呼び出し層(`storage.search_records()`)でも101文字超が`InvalidInputError`を送出することをテストで確認 |
| 3 | High | migrationが接続先・既存列を検証せず、無条件で`ALTER TABLE records ADD COLUMN IF NOT EXISTS memo TEXT`を実行していた(誤接続・型不一致・想定外テーブルの変更を検知できない) | PR #29と同じ`scripts/target_identity.py`(接続先データベース名・ユーザー・Railwayプロジェクト/環境ID・明示許可フラグの一致確認)を導入し、`public.records`を完全修飾化。実行前後に`memo`列の定義(型=text・NULL許容・デフォルト値なし)を検証し、想定と異なる場合は`UnexpectedColumnDefinitionError`で停止する |

あわせて、監査資料作成の過程で見つかった実バグ(検索キーワードの`%`・`_`が
SQL LIKEワイルドカードとして漏れる)も別途修正済み(④参照)。

**★round 3(2026-09-04、案A統合後の監査)**: PR #29との統合(案A)後、
「現時点ではマージ・staging適用を承認できない」との判定でCritical 1件・
High 3件・Medium 1件の指摘を受け、すべて対応済み。

| # | 深刻度 | 指摘内容 | 対応 |
|---|---|---|---|
| 1 | Critical | 案A統合後、`streamlit/db.py`が呼ぶ2関数はPR #29のmigrationが作るため、round 1の「PR #30単独migration→マージ」の順序ではUndefinedFunctionで失敗する | ⑥を全面訂正。「①PR #29マージ→②③least_privilege_schema.py実行→④確認→⑤PR #30マージ→⑥確認→⑦app_runtime切替」の順序へ。`migrate_to_records_memo_schema.py`単独実行はローカル検証専用と明記 |
| 2 | High | SECURITY DEFINER関数自身にメモ・検索語の長さ/制御文字の検証が無く、Python層を経由しない直接呼び出しで素通りする | `scripts/memo_search_functions.py`に長さ(200/100文字)・制御文字の検証を追加(SQLSTATE 22023) |
| 3 | High | `search_records_for_tenant`の`p_order`検証が`NOT IN`のみで、`p_order=NULL`がSQLの3値論理により素通りし順序未保証の検索が成功する | `IS NULL OR`を追加して修正 |
| 4 | High | 設計書(PR #29側)に第13次改訂版(12関数)時点の実行可能コードが「過去版」と明示されず残っていた | PR #29側の設計書§5・§6・§16・§17に正本(`scripts/*.py`)を参照する警告を追加、§5-7・§6-1は14関数版へ更新 |
| 5 | Medium | `memo_search_functions.py`のdocstringに「memo列が無いとCREATE FUNCTION自体が失敗する」という誤った説明があった | 実機で「CREATE成功・呼び出し時にUndefinedColumn」であることを確認し、docstringを訂正 |

詳細は`仕様書/PostgreSQL最小権限化・RLS設計.md`の「★round 3統合監査対応」
セクションも参照(項目1・4はPR #29側が主担当)。

**★round 3後の追加3点対応(2026-09-04)**: round 3 ZIPへの監査は完了し、
以下3点の追加修正のみが残った。

| # | 指摘内容 | 対応 |
|---|---|---|
| 1 | ⑥に接続文字列を直接書く実行例`DATABASE_URL=<staging接続文字列> python ...`が残っていた | `railway run python scripts/migrate_to_least_privilege_schema.py`へ統一し、コマンド履歴・シェルログへ接続情報が平文で残る事故を防いだ |
| 2 | PR #29側の設計書§5・§6・§16・§17に旧12関数版の実行可能コードが残っていた | PR #29側で対応(§5・§6・§16は14関数版へ更新、§17は別文書へ実行禁止の歴史的記録として分離) |
| 3 | PG16・18の自動テストに`app_runtime`直接INSERT/UPDATE拒否・`app_webhook`による新2関数の実行拒否・バックスラッシュ文字どおり検索が不足していた | PR #29側の`tests/test_least_privilege_schema.py`へ追加、21/21 PASS |

項目2・3はPR #29側が主担当。詳細は`仕様書/PostgreSQL最小権限化・RLS
設計.md`の「★round 3後の追加3点対応」セクションを参照。

## ③DB設計

```sql
ALTER TABLE public.records ADD COLUMN IF NOT EXISTS memo TEXT;
```

第16回(マルチテナント設計)の`tenant_id`列が既に存在する前提
(`migrate_to_tenant_schema.py`実行後)。`memo`はNULL許容(メモ無しの記録も許可)。
`records`ではなく`public.records`と完全修飾する(search_path経由で想定外の
テーブルを変更しないため。監査指摘③)。

`scripts/migrate_to_records_memo_schema.py`は、このALTER文の前後で
`information_schema.columns`から`memo`列の定義(`data_type`・`is_nullable`・
`column_default`)を取得し、既に列が存在する場合は期待値
(`{"data_type": "text", "is_nullable": "YES", "column_default": None}`)と
一致するかを検証する。一致しない場合(例: 誰かが手動で`memo INTEGER`列を
追加していた)は`UnexpectedColumnDefinitionError`で停止し、`IF NOT EXISTS`が
サイレントに「変更不要」と誤認する事態を防ぐ。

## ④関数設計

**★round 3で更新(2026-09-04、案A統合)**: PostgreSQL最小権限化
(`仕様書/PostgreSQL最小権限化・RLS設計.md`)との統合により、以下の2関数は
素のSQLではなく、PostgreSQL側のSECURITY DEFINER関数
(`public.record_with_memo_for_tenant`・`public.search_records_for_tenant`、
定義は`scripts/memo_search_functions.py`、PR #29の関数一覧へ追加済み)を
呼ぶ形へ変更されている。この2関数は`app_runtime`ロールにEXECUTE権限のみで
呼び出せる(`records`テーブルへの直接GRANTを持たない)ため、将来アプリの
接続ロールが`app_runtime`へ切り替わっても動作する。keywordのLIKEエスケープ・
orderの検証・**メモ200文字/検索語100文字の長さ制限・制御文字の拒否**は
PG関数側で行う(呼び出し元のPython層を信頼しない設計。ChatGPT監査round 3
Highの指摘を反映し、長さ・制御文字の検証をDB関数側にも追加した)。

`streamlit/db.py`(SQL操作のみ、commitしない):

- `record_with_memo_for_tenant(record_date, memo, conn, *, tenant_id)` —
  `SELECT public.record_with_memo_for_tenant(%s, %s, %s)`を呼ぶ。既に
  同じ日付の記録が存在する場合はmemoだけを上書きする(PG関数側の
  `ON CONFLICT DO UPDATE`)
- `search_records_for_tenant(conn, *, tenant_id, keyword=None, order="desc")` —
  `SELECT ... FROM public.search_records_for_tenant(%s, %s, %s)`を呼ぶ。
  orderは`"desc"`(新しい順、既定)または`"asc"`(古い順)、それ以外は
  Python側で早期に`ValueError`(PG関数側でも独立に`NULL`を含め検証、
  round 3で`p_order=NULL`が素通りするバグを修正)。keywordに含まれる
  `%`・`_`・バックスラッシュはPG関数側でエスケープしてからパターンに
  組み込むため、これらの文字はSQL LIKEワイルドカードとしてではなく、
  常にkeywordそのものの文字として扱われる(round 1で発見・修正した
  `keyword="_"`がほぼ全件にマッチしてしまうバグの根本対応でもある)

`streamlit/storage.py`(commit/rollbackの責任を持つ):

- `add_date_with_memo(record_date, memo, tenant_id=None)` — メモの長さ(200文字以内)・
  制御文字を`_validate_memo()`でサーバー側検証してから保存する
  (`_validate_tenant_name()`と同じ方針。第21回参照)。jsonバックエンドでは
  `StorageConfigError`を送出する
- `search_records(tenant_id=None, keyword=None, order="desc")` — 検索キーワードも
  同様に`_validate_search_keyword()`で検証する(100文字以内)。jsonバックエンドでは
  `StorageConfigError`を送出する

## ⑤UI(`streamlit/app.py`)

- 「今日、洗いました！」ボタンの手前に「ひとこと(任意)」の入力欄を追加
  (postgresバックエンドのときのみ表示、`max_chars=storage.MEMO_MAX_LENGTH`)
- 「記録した日」の下に「🔍 記録をさがす」セクションを追加(postgresバックエンドの
  ときのみ表示)。キーワード入力欄(`max_chars=storage.KEYWORD_MAX_LENGTH`)・
  並び順(新しい順/古い順)セレクトボックス・該当する記録の一覧(日付とメモ)を表示する
- 検索時、`storage.InvalidInputError`(入力値そのものの問題)は
  `except (StorageConfigError, StorageUnavailableError)`より前に個別捕捉し、
  「◯文字以内で、制御文字を含めずに入力してください」という具体的な案内を表示する
  (システム障害と誤解させない。監査指摘②)

## ⑥実行方法・実行順序(staging) ★デプロイ順序に注意(監査round1指摘①、round3で全面訂正)

**★round 3で訂正(2026-09-04)**: 案A統合(PostgreSQL最小権限化との統合、
`仕様書/PostgreSQL最小権限化・RLS設計.md`「★統合追記」参照)により、
`streamlit/db.py`の`record_with_memo_for_tenant()`・
`search_records_for_tenant()`は素のSQLではなく、PostgreSQL側の関数
`public.record_with_memo_for_tenant`・`public.search_records_for_tenant`を
呼ぶように変更されている。**この2関数を作るのはPR #30自身の
`migrate_to_records_memo_schema.py`ではなく、PR #29の
`migrate_to_least_privilege_schema.py`(`scripts/memo_search_functions.py`を
関数一覧へ含める)である。** round 1で示していた「①PR #30単独の
migrate_to_records_memo_schema.py実行→②確認→③PR #30マージ」という順序
だけでは、この2関数が存在しないままデプロイすることになり、記録・検索が
`psycopg.errors.UndefinedFunction`で失敗する(`StorageUnavailableError`
として捕捉されクラッシュはしないが、機能自体が使えない点はround 1の
指摘と同じ)。

**正しい順序(PR #29の完了が前提)**:

```
① PR #29(PostgreSQL最小権限化)をstagingへマージ
   (コードのみ。migrate_to_least_privilege_schema.pyは自動実行されない
   ため、マージ自体はアプリの挙動を変えない)
② staging DBの接続先・パスワード方式・バックアップを確認
③ scripts/migrate_to_least_privilege_schema.py をstaging DBへ実行
   (ロール作成・records.memo列・14関数(record_with_memo_for_tenant・
   search_records_for_tenantを含む)・GRANT・RLSを一度に適用。
   接続先識別確認込み)
④ 既存アプリ(記録・課金・認証等、第16〜21回の機能)の正常性とDB状態を確認
⑤ PR #30(この設計書)をstagingへマージ(Railwayが自動デプロイを開始)
⑥ 自動デプロイ完了後、実機でメモ保存・検索・並び替え・世帯分離を確認
⑦ 別途承認のもとで、アプリの接続ロールをapp_runtimeへ切り替え
```

**`scripts/migrate_to_records_memo_schema.py`単独の位置づけ**: この
スクリプトは(a)PR #29の`migrate_to_least_privilege_schema.py`内部から
`ensure_records_memo_column()`として再利用される、(b)ローカル開発・
テストでmemo列だけを試したい場合の補助ツール、の2用途に限定する。
**このスクリプトを単独でstagingへ実行しても、2関数が無ければPR #30の
コードは動かないため、staging適用の手順としては使わないこと**
(`main()`によるCLI単独実行はローカル検証専用)。

### ③の実行コマンド(PR #29側)

```bash
# 必ずrailway run経由(正しいプロジェクト/環境にリンクした状態)で実行する。
# DATABASE_URLを含む接続情報はrailwayが自動注入するため、接続文字列を
# コマンドラインへ直書きしない(コマンド履歴・シェルログへ平文で残る事故を防ぐため)。
# EXPECTED_TARGET_DBNAME・EXPECTED_TARGET_USER・EXPECTED_RAILWAY_PROJECT_ID・
# EXPECTED_RAILWAY_ENVIRONMENT_ID・STAGING_DDL_EXPLICITLY_ALLOWED=true・
# LEAST_PRIVILEGE_APP_RUNTIME_PASSWORD・LEAST_PRIVILEGE_APP_WEBHOOK_PASSWORDを
# 事前に設定すること(scripts/target_identity.pyが全項目の一致を必須で確認する。
# 未設定・不一致の場合はDDLを一切実行せずに停止する)
railway run python scripts/migrate_to_least_privilege_schema.py
```

### ④の確認クエリ(抜粋)

```sql
-- memo列
SELECT data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'records' AND column_name = 'memo';
-- 期待結果: text | YES | (NULL)

-- 2関数の存在・所有者・EXECUTE権限
SELECT p.proname, p.proowner::regrole::text
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND p.proname IN ('record_with_memo_for_tenant', 'search_records_for_tenant');
-- 期待結果: 2行とも owner = app_data_owner
```

### ロールバック

PR #29側のTier 1〜3ロールバック手順(`仕様書/PostgreSQL最小権限化・
RLS設計.md`§17)に従う。`ALTER TABLE public.records DROP COLUMN IF EXISTS
memo;`単独でmemo列だけを戻すことも可能だが、その場合も
`record_with_memo_for_tenant`・`search_records_for_tenant`関数はmemo列を
参照したまま残るため、**列だけを先に戻すと関数呼び出しが
`UndefinedColumn`で失敗する。列のロールバックは関数の削除(PR #29の
ロールバック手順)と同時に行うか、先にアプリ(PR #30)のデプロイを
ロールバックしてから行うこと**。

## ⑦テスト

`tests/test_records_search.py`(37件)。

- スキーマ分離(`CREATE SCHEMA test_x/SET search_path`)で検証するもの:
  `db.record_with_memo_for_tenant()`・`search_records_for_tenant()`(新規追加・
  既存メモの上書き・メモ無し・新しい順/古い順・キーワードの部分一致・大文字小文字
  を区別しないこと・`%`/`_`/バックスラッシュを文字として扱うこと・該当無し・
  テナント越境しないこと・不正なorder値)、`storage`層の同等の検証(jsonバック
  エンドでの`StorageConfigError`・101文字超の`InvalidInputError`・tenant_id必須)
- 使い捨てデータベース(`tests/test_least_privilege_schema.py`の`lp_db`と同じ設計。
  `migrate_to_records_memo_schema.py`が`public.records`を完全修飾で扱うため
  スキーマ分離では検証できない)で検証するもの: 接続先識別envが無い/不一致の
  場合にDDLを実行せず停止すること、既存データ(migration前に作った記録)が
  変更されずmemo=NULLで追加されること、2回実行しても安全なこと、`memo`列が
  既に別の型で存在する場合に`UnexpectedColumnDefinitionError`で停止すること

ローカル実機確認: ポータブルPostgreSQL 16でアプリを起動し、メモ付き記録の保存・
「記録した日」への反映・キーワード検索(該当あり/無し)・並び順切替・記録取り消しを
ブラウザで確認済み。既存202件を含む全239件のテストがPASSすることを確認済み。
