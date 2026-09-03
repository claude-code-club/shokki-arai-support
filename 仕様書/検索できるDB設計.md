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

## ③DB設計

```sql
ALTER TABLE records ADD COLUMN IF NOT EXISTS memo TEXT;
```

第16回(マルチテナント設計)の`tenant_id`列が既に存在する前提
(`migrate_to_tenant_schema.py`実行後)。`memo`はNULL許容(メモ無しの記録も許可)。

## ④関数設計

`streamlit/db.py`(SQL操作のみ、commitしない):

- `record_with_memo_for_tenant(record_date, memo, conn, *, tenant_id)` —
  記録日を追加し、任意のメモを添える。既に同じ日付の記録が存在する場合は
  memoだけを上書きする(`insert_date_for_tenant()`と同じ冪等な追加に、
  更新できる余地を持たせたもの)
- `search_records_for_tenant(conn, *, tenant_id, keyword=None, order="desc")` —
  記録を検索する。keyword指定時はmemoの部分一致(`ILIKE`、大文字小文字を区別しない)
  で絞り込む。orderは`"desc"`(新しい順、既定)または`"asc"`(古い順)

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
  (postgresバックエンドのときのみ表示)
- 「記録した日」の下に「🔍 記録をさがす」セクションを追加(postgresバックエンドの
  ときのみ表示)。キーワード入力欄・並び順(新しい順/古い順)セレクトボックス・
  該当する記録の一覧(日付とメモ)を表示する

## ⑥実行方法(staging)

```bash
DATABASE_URL=<stagingの接続文字列> python scripts/migrate_to_records_memo_schema.py
```

`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`による冪等な追加のみで、既存行は
`memo=NULL`のまま変更されない。ロールバックは列の削除
(`ALTER TABLE records DROP COLUMN IF EXISTS memo`)で戻せる(既存の`record_date`・
`tenant_id`・他のテーブルには一切影響しない)。

## ⑦テスト

`tests/test_records_search.py`(26件)。稼働中の`public.records`とは隔離した
専用スキーマ(`migrate_to_tenant_schema.py`→`migrate_to_records_memo_schema.py`の順で
適用)で検証する。

- `db.record_with_memo_for_tenant()`: 新規追加・既存メモの上書き・メモ無し(NULL)
- `db.search_records_for_tenant()`: 新しい順/古い順、キーワードの部分一致・
  大文字小文字を区別しないこと・該当無し、テナント越境しないこと、不正なorder値
- `storage._validate_memo()` / `_validate_search_keyword()`: 上限超過・制御文字・
  空文字の扱い
- `storage.add_date_with_memo()` / `search_records()`: jsonバックエンドでの
  `StorageConfigError`、postgresバックエンドでの実機ラウンドトリップ、
  tenant_id必須の検証

ローカル実機確認: ポータブルPostgreSQL 16でアプリを起動し、メモ付き記録の保存・
「記録した日」への反映・キーワード検索(該当あり/無し)・並び順切替・記録取り消しを
ブラウザで確認済み。既存202件を含む全228件のテストがPASSすることを確認済み。
