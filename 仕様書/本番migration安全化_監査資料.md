# 本番migration安全化 監査資料(第16〜20回)

対象ブランチ: `feature/production-migration-safety`(origin/staging から分岐、コミット `c85854b`)
作成日: 2026-09-07
作業範囲: 岩瀬様の正式指示「第16〜20回の本番migrationに対する安全化設計へ進んでください」に基づく、調査・設計・ローカルテスト・feature branchへのcommit/pushまで。**production・staging DBへの接続、実環境でのSQL/DDL/migration実行、秘密値の取得・表示・生成・登録、PRの作成・マージ、mainへのcommit/pushは一切行っていない。**

---

## 1. 安全化した設計書

### 1.1 背景・課題

第16〜20回(マルチテナント設計・認証基盤・Stripe課金・継続課金Webhook・プラン制限メータリング)の5本のmigrationスクリプトは、いずれも接続先データベースの識別確認を持たず、`DATABASE_URL`が指すDBに対して無条件にDDL(ALTER TABLE等)を実行する構造だった。誤って本番DBに接続した状態でテスト目的のmigrationを実行してしまう、あるいは逆にstaging用の設定のまま本番へ向けて実行してしまう、といった事故を機械的に防ぐ手段がなかった。

また、第16回(JSON相当のrecordsテーブル→tenant付きスキーマへの移行)は、移行前後のデータ一致検証が「日付集合のPython set比較」のみであり、件数や値の変化を第三者(ChatGPT監査等)が実行ログだけから独立に再検証する手段がなかった。

さらに、本番データに対して初めてDDLを実行する前に、バックアップと別環境での復元テストが完了していることを保証する仕組みもなかった。

### 1.2 設計方針

既存の `scripts/target_identity.py`(第22課題PR #29向け、監査継続中)とは意図的に分離し、新規モジュール `scripts/production_target_identity.py` を正本とする独立ファイルとした。理由は2つ:

1. PR #29はround 4合格・staging Phase 2.5待ちの状態で監査サイクルが継続中であり、無関係な本タスクの変更を混ぜると監査対象の差分が汚染される。
2. `target_identity.py`の`verify_target_database_identity()`は「7テーブル全部(第16〜21回・第22回分すべて)が既に揃っている」ことを前提にしている(第22課題=最小権限化を全機能実装後に適用する想定)。これは本タスク(tenantsテーブル自体がまだ存在しない状態からの初回適用)の前提とは異なるため、そのまま流用できない。

役割は同種(誤った接続先でのDDL実行防止)だが、対象とする5本のmigrationはそれぞれ異なる前提テーブル状態を要求するため、テーブル存在確認は`verify_expected_tables_exist()`として分離し、各migrationスクリプトが自分の前提を明示的に渡して呼び出す設計にした。

### 1.3 `scripts/production_target_identity.py` の安全確認項目

`verify_production_migration_target(cur)` は、以下がすべて正しく設定・一致していない限り `ProductionTargetMismatchError` を送出して停止する(1つでも未設定なら安全側に倒して停止、DDLは一切実行しない)。

| # | 確認項目 | 突合先 |
|---|---|---|
| 1 | `EXPECTED_TARGET_DBNAME` | 実際の `current_database()` |
| 2 | `EXPECTED_TARGET_USER` | 実際の `current_user` |
| 3 | `EXPECTED_RAILWAY_PROJECT_ID` | `RAILWAY_PROJECT_ID`(Railwayが`railway run`実行時に自動注入。操作者が値を書く必要はない) |
| 4 | `EXPECTED_RAILWAY_ENVIRONMENT_ID` | `RAILWAY_ENVIRONMENT_ID`(同上) |
| 5 | `PRODUCTION_DDL_EXPLICITLY_ALLOWED=true` | 第22課題側の`STAGING_DDL_EXPLICITLY_ALLOWED`とは別名のフラグ。名前で本番向けを明示し、staging用フラグの使い回しによる誤爆を防ぐ |
| 6 | `BACKUP_RESTORE_TEST_CONFIRMED=true` | `scripts/backup_and_restore_test.py`によるバックアップ・復元テストを実行直前に人間が完了・確認したことを示す明示フラグ |

いずれの確認も`SELECT`のみで完結し、この関数自体はDDLを一切実行しない。例外送出時点で呼び出し元はDDLに到達していないことをテストで保証している(§3参照)。

`verify_expected_tables_exist(cur, expected_tables, migration_label)` は、各migrationが要求する前提テーブル集合の存在を確認する。不足があれば「正しい実行順序(第16回→17回→18回→19回→20回)を守っているか確認してください」というメッセージ付きで停止する。

### 1.4 5本のmigrationスクリプトへの統合

各スクリプトの`main()`が`db.get_connection()`で自前接続を開き、カーソルブロック内で`verify_production_migration_target(cur)` → `verify_expected_tables_exist(cur, {前提テーブル}, "回の名前")` の順に呼んでから、既存の移行本体関数(`conn`を渡す形に変更)を呼び出す。前提テーブル集合は以下の通り(第16→17→18→19→20回の依存順序を反映):

| スクリプト(回) | 前提テーブル |
|---|---|
| `migrate_to_tenant_schema.py`(第16回) | `{"records"}` |
| `migrate_to_auth_schema.py`(第17回) | `{"tenants"}` |
| `migrate_to_billing_schema.py`(第18回) | `{"tenants"}` |
| `migrate_to_webhook_schema.py`(第19回) | `{"tenant_subscriptions"}` |
| `migrate_to_usage_schema.py`(第20回) | `{"tenants", "tenant_subscriptions"}` |

各スクリプトのdocstringに「実行前条件・実行後状態・再実行時の挙動(冪等性)・途中失敗時の復旧方法(自動rollback)」を明記した(`migrate_to_tenant_schema.py`の該当ブロックを例として§1.5に引用)。

### 1.5 第16回のSHA-256検証強化

`migrate_to_tenant_schema.py`に`_canonical_date_set_hash(date_strings)`を追加した。移行前後の日付集合を、ソート済み・区切り記号固定のJSON文字列へ正規化し(`scripts/rollback_helpers.py`の`_canonical_migration_log_export`と同じ考え方)、SHA-256を計算する。`[OK]`メッセージへ`移行前後のSHA-256一致確認済み(前=..., 後=...)`として出力する。これにより、第三者が実行ログのハッシュ値だけを見て「移行前後で本当にデータが変わっていないか」を独立に検証できる。

該当箇所(`scripts/migrate_to_tenant_schema.py:19-46`、抜粋):

```
実行前条件:
    - recordsテーブルが既に存在すること
    - EXPECTED_TARGET_DBNAME等がすべて設定され、実測値と一致すること
実行後状態:
    - tenantsテーブルが存在し、指定したtenant_idの行が1件存在する
    - records.tenant_idがNOT NULLで、全行に同じtenant_idが設定されている
再実行時の挙動:
    - 完全に冪等。同じtenant_idで再実行しても、既に移行済みの行は変更されない
途中失敗時の復旧:
    - 検証(日付集合の完全一致)に失敗した場合は自動でrollbackされ、
      スキーマ変更・データ変更は一切確定しない
```

### 1.6 `scripts/backup_and_restore_test.py`(新規ツール)

対象DBを`pg_dump -Fc`でバックアップし、新規に作成する使い捨てDB(`restore_test_<uuid>`)へ`pg_restore`で復元、全publicテーブルについて行数と正規化データのSHA-256を比較する独立ツール。対象DBへの操作は読み取り(`pg_dump`)のみで、書き込みは常に新規作成した使い捨てDBに限定される(既存の他DBへは一切書き込まない)。

運用想定: 操作者がこのツールを実行し、`[OK]`(全テーブル一致)を目視確認したうえで`BACKUP_RESTORE_TEST_CONFIRMED=true`を設定してから本番migrationを実行する。`production_target_identity.py`がこのフラグを必須条件とするため、このツールを経ずに本番DDLを実行する経路は存在しない。

実機検証: ローカルの使い捨てPostgreSQLインスタンス(`postgresql://postgres:postgres@localhost:5433/backup_test_source`)に対して実際に`pg_dump`/`pg_restore`を実行し、バックアップ→復元→ハッシュ一致という一連の流れをエンドツーエンドで確認済み(終了コード0、全テーブル一致)。

---

## 2. 変更差分

```
$ git diff f5e6d86 c85854b --stat
 scripts/backup_and_restore_test.py        | 260 +++++++++++++++++++++++++++
 scripts/migrate_to_auth_schema.py         |  36 +++-
 scripts/migrate_to_billing_schema.py      |  32 +++-
 scripts/migrate_to_tenant_schema.py       |  85 ++++++++-
 scripts/migrate_to_usage_schema.py        |  36 +++-
 scripts/migrate_to_webhook_schema.py      |  34 +++-
 scripts/production_target_identity.py     | 202 +++++++++++++++++++++
 tests/test_backup_and_restore_test.py     | 184 +++++++++++++++++++
 tests/test_production_migration_safety.py | 285 ++++++++++++++++++++++++++++++
 9 files changed, 1145 insertions(+), 9 deletions(-)
```

- `f5e6d86`: origin/staging のHEAD(PR #29マージ後)
- `c85854b`: 本タスクのコミット(`feature/production-migration-safety`)

新規ファイル4本(`production_target_identity.py`・`backup_and_restore_test.py`・2つのテストファイル)、既存の5本のmigrationスクリプトへの追加変更(接続先確認の呼び出し追加、docstring拡充、`migrate_to_tenant_schema.py`のみSHA-256検証を追加)。**既存の移行ロジック本体(ALTER TABLE等のDDL文そのもの)は一切変更していない** — 変更は「実行前の安全確認を追加した」ことのみ。

コミット: `c85854b`(`feature/production-migration-safety`、`origin/staging`から分岐)
プッシュ済み: `origin/feature/production-migration-safety`(PR未作成)

---

## 3. テスト結果

### 3.1 新規テスト

| ファイル | テスト数 | 内容 |
|---|---|---|
| `tests/test_production_migration_safety.py` | 15件 | ①→⑤正常系(全成功・冪等再実行・SHA-256出力確認)、実行順序違反4パターン(依存先未適用でのDDL未実行確認)、接続先識別の不一致・欠落10パターン(いずれもDDL未実行のまま`[NG]`終了) |
| `tests/test_backup_and_restore_test.py` | 5件 | 実際の`pg_dump`/`pg_restore`によるバックアップ・復元・ハッシュ一致確認、復元先DBの既定削除・`--keep-restored-db`オプション、正規化ハッシュ関数の単体テスト(同一データ→同一ハッシュ、差分データ→異なるハッシュ) |

すべて disposable database(`CREATE DATABASE test_xxx_<uuid>` / `DROP DATABASE ... WITH (FORCE)`)を用いて隔離しており、既存の`tests/test_least_privilege_schema.py`と同じ設計方針。各migrationスクリプトはsubprocessとして実際に`main()`のCLIエントリポイントを経由して起動しており(モジュール内関数を直接呼ばない)、安全確認が実際にDDL実行より前に効いていることを、実行後のテーブル存在有無で保証している。

### 3.2 実行結果

```
既存237件 + 新規19件 = 256件
PostgreSQL 16 (ポータブル16.15): 全件PASS
PostgreSQL 18 (ポータブル18.6): 全件PASS
```

唯一の例外: `tests/test_webhook_server.py::test_non_get_post_methods_are_rejected[PATCH/PUT]` がフルスイート実行時にのみ稀に失敗(Windows環境依存の`ConnectionAbortedError`)。本変更とは無関係の既知のフレーキーテストであり、単体実行では再現性をもって成功することを確認済み。

### 3.3 実行環境

ローカルの使い捨てPostgreSQLサーバー(PG16/PG18、ポート5433/5434)に対してのみ実行。production・staging環境への接続は一切行っていない。

---

## 4. 残存リスク

1. **Auth0本番テナント・Stripe本番アカウントの準備状況が未確認** — 今回の作業範囲外(岩瀬様指示#9の通り)。第17回(認証基盤)・第18回(Stripe課金)の本番適用前に、別フェーズとして確認が必要。
2. **`requirements.txt`のバージョンとRailway本番環境の実際のインストール状態との一致は未検証** — ローカルの使い捨て環境でのテストのみ実施しており、Railway本番のPythonパッケージバージョンとの差異による予期しない挙動の可能性は排除できていない。
3. **`pg_dump`/`pg_restore`がRailway本番の実行シェル(`railway run`経由)で利用可能かは未確認** — `scripts/backup_and_restore_test.py`はローカルのポータブルPostgreSQLバイナリ(`PG_BIN_DIR`)またはPATH上のものを前提としており、Railway側の実行環境(コンテナ)に同バージョン・同バイナリが存在するかは今回検証していない。本番での初回実行時に、まずこのツール単体の動作確認が必要。
4. **`EXPECTED_TARGET_DBNAME`等の環境変数は操作者が手動設定する運用** — 値そのものの正しさ(実際の本番DB名・ユーザー名と一致しているか)は仕組みでは保証できず、設定時のヒューマンエラーの可能性は残る(ただし、誤って別のDB名を設定した場合は不一致として検出され、DDL実行前に停止するため、「気づかず誤爆する」リスクは大幅に低減されている)。
5. **バックアップ・復元テストは「実行直前」の1回のみを前提** — `BACKUP_RESTORE_TEST_CONFIRMED=true`はフラグのみで、実際にいつ・どのバックアップに対してテストが行われたかを機械的に記録・検証する仕組みはない。運用上、直前に実施したことを人間が確認する必要がある。
6. **第16〜20回を段階的に本番適用する間、各回の適用〜次の回の適用までの期間、アプリケーションコード側が新旧どちらのスキーマにも対応している必要がある** — 今回の安全化設計はDB migration側のみを対象としており、アプリケーションコード(Streamlit側)の後方互換性は別途確認が必要(岩瀬様指示の全体順序#9「第21回のコード変更」で対応予定)。

---

## 5. 推奨リリース順序

岩瀬様の指示で既に固定されている全体順序を、そのまま推奨する:

1. 第22課題をstagingで安全に完了(現在Phase 2.5まで完了、Phase 3以降が残タスク)
2. 第16〜20回の安全装置を実装・監査 ← **本資料が対象とする範囲。完了**
3. 本番バックアップと復元試験(`scripts/backup_and_restore_test.py`を実際の本番DBに対して初めて実行し、動作確認)
4. 第16回を単独適用・検証
5. 第17回を単独適用・検証
6. 第18回を単独適用・検証
7. 第19回を単独適用・検証
8. 第20回を単独適用・検証
9. 第21回のコード変更
10. 十分な安定確認後に第22課題を本番へ検討

各段階の適用(4〜8)は、`railway run`経由で対象migrationスクリプトを実行し、`[OK]`出力(SHA-256一致・接続先識別情報を含む)を岩瀬様がログとして保存する運用を推奨する。次の回へ進む前に、アプリケーションが新スキーマで問題なく稼働していることを確認してから進めることを推奨する。
