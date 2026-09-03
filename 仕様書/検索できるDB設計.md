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

`streamlit/db.py`(SQL操作のみ、commitしない):

- `record_with_memo_for_tenant(record_date, memo, conn, *, tenant_id)` —
  記録日を追加し、任意のメモを添える。既に同じ日付の記録が存在する場合は
  memoだけを上書きする(`insert_date_for_tenant()`と同じ冪等な追加に、
  更新できる余地を持たせたもの)
- `search_records_for_tenant(conn, *, tenant_id, keyword=None, order="desc")` —
  記録を検索する。keyword指定時はmemoの部分一致(`ILIKE`、大文字小文字を区別しない)
  で絞り込む。orderは`"desc"`(新しい順、既定)または`"asc"`(古い順)。
  keywordに含まれる`%`・`_`・バックスラッシュは`_escape_like_pattern()`で
  エスケープしてからパターンに組み込むため、これらの文字はSQL LIKEワイルドカード
  としてではなく、常にkeywordそのものの文字として扱われる(修正前は
  `keyword="_"`がほぼ全件にマッチしてしまうバグがあった)

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

## ⑥実行方法・実行順序(staging) ★デプロイ順序に注意(監査指摘①)

**`staging`ブランチへのマージはRailway staging環境の自動デプロイを即座に
トリガーする(第7回・第13回で確立した仕組み)。マージ後にデプロイされる
`streamlit/app.py`は、`get_backend_name() == "postgres"`のとき無条件で
`memo`列を参照する(記録ボタンの手前のメモ入力欄・「🔍 記録をさがす」検索欄が
常時表示される)。したがって、`memo`列が存在しない状態でこのコードがデプロイ
されると、記録の保存・検索が全ユーザーに対して失敗する(`psycopg.errors.
UndefinedColumn`→`StorageUnavailableError`として捕捉されるため画面が
クラッシュすることはないが、機能自体が使えなくなる)。**

正しい順序:

```
① scripts/migrate_to_records_memo_schema.py を staging DB へ実行
   (接続先識別確認込み。実行コマンドは下記)
② memo列が text型・NULL許容・デフォルト値なしで追加されたことを確認
③ PR #30 を staging へマージ(Railwayが自動デプロイを開始)
④ 自動デプロイ完了後、実機でメモ保存・検索・並び替えを確認
```

①の時点ではまだ旧い`app.py`(memo列を参照しない版)が動いているため、
`memo`列がNULL許容で追加されるだけの本migrationは、旧アプリの動作に一切
影響しない(存在を知らない列が増えるだけ)。この非破壊性ゆえに、
「①migration→②確認→③デプロイ」の順序が安全に成立する。

### ①の実行コマンド

```bash
# 以下は必ずrailway run経由(正しいプロジェクト/環境にリンクした状態)で実行し、
# EXPECTED_TARGET_DBNAME・EXPECTED_TARGET_USER・EXPECTED_RAILWAY_PROJECT_ID・
# EXPECTED_RAILWAY_ENVIRONMENT_ID・STAGING_DDL_EXPLICITLY_ALLOWED=true を
# 事前に設定すること(scripts/target_identity.pyが全項目の一致を必須で確認する。
# 未設定・不一致の場合はDDLを一切実行せずに停止する)
DATABASE_URL=<staging接続文字列> python scripts/migrate_to_records_memo_schema.py
```

### ②の確認クエリ

```sql
SELECT data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'records' AND column_name = 'memo';
```

期待結果: `text | YES | (NULL)`。

### ロールバック

```sql
ALTER TABLE public.records DROP COLUMN IF EXISTS memo;
```

`record_date`・`tenant_id`・他のテーブル・関数・制約には一切影響しない。
列を削除すると保存済みのメモの内容は失われる(記録日自体は失われない)。
**PR #30のコード(memo列を無条件で参照する`app.py`)がまだデプロイされている
状態でDBだけ先にロールバックすると、記録ボタン自体が壊れる。DBロールバックは
必ずアプリのデプロイロールバックと同時に行うこと**(⑥の順序を逆再生する形、
すなわち先にアプリをロールバックしてからDBを戻す)。

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
