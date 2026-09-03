# PostgreSQL最小権限化とRLS導入の安全設計（第22回・第13次改訂版）

**状態: 設計・計画のみ。ロール作成・権限変更・RLS適用・DATABASE_URL変更・実装・
commit・push・PR作成・デプロイのいずれも未実施。岩瀬様・ChatGPTの明示承認待ちで停止。**

**この改訂の背景(第13次)**: 第12次改訂版の再監査で、次の5点の指摘が
あった。いずれもPostgreSQL 16・18の実機で修正・検証済み。

1. **[Critical・点A]** DEGRADED時に一部ロールだけ削除されると、Tier 2
   の再実行条件(全ロール存在)にも旧cleanupスクリプトの開始条件(全ロール
   不在)にも合致せず、正規の復旧経路が途切れる不具合があった →
   `_precondition_ready_to_resume`(「全部不在」または「一部が安全に
   縮退済み」を受理)と`_finish_degraded_role_removal`(残存ロールの
   依存解消を再確認しDROP)を新設し、`rollback_cleanup_after_tier2.py`
   を`rollback_resume_to_full_restore.py`へ一般化した
2. **[点B]** staging識別が、操作者が設定する2つの環境変数
   (`EXPECTED_TARGET_ENVIRONMENT_ID`/`ACTUAL_TARGET_ENVIRONMENT_ID`)
   同士の比較であり、独立した確認になっていなかった → Railwayが
   `railway run`実行時に自動注入する`RAILWAY_PROJECT_ID`・
   `RAILWAY_ENVIRONMENT_ID`(操作者が値を書く必要が無い)との比較へ
   訂正した
3. **[点C]** UNIQUE制約撤去時の同一性確認が`conkey`のみで、検証状態
   (`convalidated`)・遅延可能属性(`condeferrable`/`condeferred`)を
   含んでいなかった → 12-2章の作成時チェックと同じ4項目をすべて
   撤去直前にも再確認するよう訂正した
4. **[点D]** 監査ログ「保存済み」が真偽値のみで、保存先・行数・
   SHA-256を検証できなかった → 決定的な正規化(`_canonical_migration_
   log_export`)による行数・SHA-256の突き合わせを必須化し、専用の
   読み取り専用エクスポートツール(`scripts/export_and_hash_migration_
   log.py`)を新設した
5. **[点E]** 「DEGRADED→依存解消→正式な再開→第21回状態への完全復帰」
   の実機テストが不足していた → 点Aの新スクリプトを使った完全な復旧
   経路を実機(PG16・18)で再現・確認した

すべてPostgreSQL 16・18の実機で45項目を再検証し、全項目PASSした。
詳細は監査ZIP(第13次)を参照。

## ★統合追記(案A、2026-09-04): 第22課題「検索できるDB」(PR #30)との統合

**背景**: 第13次改訂版の設計承認後、実装フェーズでPR #29(この設計書)と並行して
PR #30(第22課題、記録へのメモ保存・キーワード検索・並び替え)が実装された。
PR #30の監査(ChatGPT、2026-09-03〜04)で、**PR #29の権限ロックダウンを適用すると
PR #30のメモ保存・検索機能が動かなくなる**という不整合が指摘された(監査ZIP
`PR30_audit_20260904_round2.zip`項目⑪)。理由: `app_runtime`ロールは
`records`テーブルへの直接GRANTを持たず、この設計書が定義するSECURITY DEFINER
関数のEXECUTE権限のみで動作する設計だが、PR #30が新設した2つのdb.py関数
(`record_with_memo_for_tenant`・`search_records_for_tenant`)は素のSQLで
`records`へ直接アクセスしており、この関数一覧に含まれていなかった。

**採用した対応方針(案A)**: 新設対応の責務をこの設計書(PR #29)側に統合する。
PR #30側は素のSQLをやめ、この設計書が新設する2つのSECURITY DEFINER関数を
呼ぶ形へ変更した(`streamlit/db.py`)。

**追加した2関数**(既存12関数と同じ設計方針: `LANGUAGE plpgsql SECURITY
DEFINER SET search_path = ''`、`pg_catalog.set_config('app.tenant_id', ...)`
でRLSと連動、`app_data_owner`所有・`app_runtime`のみへEXECUTE付与)。
定義の実体は`scripts/memo_search_functions.py`(PR #29・PR #30の両ブランチへ
バイト単位で同一の内容を配置、この設計書の§5関数一覧とは別ファイルとして
一元管理する。理由: PR #30側のテストからも同じ定義を再利用するため)。

| 関数 | 所有者 | EXECUTE付与先 | 概要 |
|---|---|---|---|
| `record_with_memo_for_tenant(uuid, date, text)` | app_data_owner | app_runtime | 記録日を追加し任意のメモを添える(`ON CONFLICT DO UPDATE SET memo = ...`) |
| `search_records_for_tenant(uuid, text, text)` | app_data_owner | app_runtime | 記録を検索(キーワードの部分一致・`%`/`_`/バックスラッシュのLIKEエスケープを関数内で実施・大文字小文字を区別しない)、新しい順/古い順で返す |

**関数総数: 12関数→14関数**(以降、この設計書の他セクションにある「12関数」・
「全12関数」という記述は、この統合追記より前(第13次改訂版まで)の状態を
指す歴史的記録であり、現在の正しい総数は14関数)。`APP_DATA_OWNER_FUNCTION_
SIGNATURES`(旧`TEN_APP_DATA_OWNER_FUNCTION_SIGNATURES`)・`ALL_FUNCTION_
SIGNATURES`(旧`ALL_TWELVE_FUNCTION_SIGNATURES`)へ改名した(件数が10・12
固定でなくなったため)。

**実装中に発見した追加のバグ(設計未検出)**: `record_with_memo_for_tenant`の
`ON CONFLICT DO UPDATE SET memo = ...`は、INSERTだけでなく**UPDATE権限**を
必要とする。既存の`grant_table_privileges()`は`records`へ`SELECT, INSERT,
DELETE`しか付与しておらず(既存11関数はいずれも`records`へのUPDATE操作を
行わないため、この設計の第1〜13次改訂でも一度も必要にならなかった)、
実機で`app_runtime`として関数を呼び出すテストを追加して初めて
`InsufficientPrivilege`(`permission denied for table records`)を検出した。
`GRANT UPDATE (memo) ON public.records TO app_data_owner`(列単位、
`tenant_id`・`record_date`は含めない)を追加して解決。ロールバック
(`rollback_helpers.py`)にも対応する`REVOKE UPDATE (memo) ...`を追加した。
**教訓**: ACL検証(`verify_all_function_grants`)は「関数のEXECUTE権限」しか
見ておらず、「関数の中身が実際にどのテーブル操作を必要とし、それに見合う
テーブル権限が揃っているか」は静的検証の対象外だった。実際に対象ロールで
関数を呼び出す実機テスト(`test_memo_search_functions_work_as_app_runtime_
and_isolate_tenants`)を追加して初めて発覚した——ACL検証と実行時テストは
どちらか一方では不十分で、両方が必要という教訓が再確認された。

**main()の実行順序への影響**: `ensure_records_memo_column()`
(`scripts/migrate_to_records_memo_schema.py`、PR #30由来、PR #29へも
バイト単位で同一の内容を配置)を、`grant_table_privileges()`(UPDATE (memo)
のGRANT文がmemo列を参照する)・`create_or_replace_functions()`(関数定義が
memo列を参照する)のどちらよりも前に実行するよう`main()`を変更した。

**検証**(PR #29・PR #30はまだgit上マージされておらず、それぞれ独立した
ブランチのため合算した件数は存在しない。各ブランチ単独での確認結果):

- PR #29(この設計書、`feature/least-privilege-postgres-schema`): 既存202件+
  この設計書のテスト16件(既存14件+今回追加2件)=218件
- PR #30(検索できるDB、`feature/records-memo-search`): 既存202件+新規37件=239件

いずれもPostgreSQL 16・18双方の実機(ローカルのポータブル環境)で全PASSを
確認済み。stagingへは一切接続していない。

### 両ブランチ統合後の実機確認(2026-09-04実施)

ローカルで`feature/least-privilege-postgres-schema`から`integration/pr29-
pr30-test`という一時ブランチを作成し、`feature/records-memo-search`を
実際に`git merge`した(このブランチはローカル検証専用で、origin へは
一切pushしていない。検証後に削除済み)。

- **マージ結果**: `scripts/target_identity.py`(round 2でPR #30側にも
  バイト単位で同一の内容を配置済み)を含め、**無編集で自動マージ成功
  (`Merge made by the 'ort' strategy`、コンフリクト0件)**。
  「diffで内容が同一と確認」だけでなく、実際の`git merge`コマンドの
  結果で確認した
- **テストスイート**: 既存202件+PR #29の16件+PR #30の37件=**255件、
  全PASS**(マージ後の統合状態で、両PRのテストが互いに干渉しないことを確認)
- **app_runtimeとしての実測**(このセクションの核心): 使い捨てデータベースに
  `migrate_to_least_privilege_schema.py`を実行(ロール作成・GRANT・RLS・
  14関数すべて適用)したうえで、`DATABASE_URL`を`app_runtime`ロール
  (パスワード認証、スーパーユーザーではない)へ切り替え、`streamlit/db.py`の
  実関数`record_with_memo_for_tenant()`・`search_records_for_tenant()`
  (PR #30がPostgreSQL関数呼び出しへ切り替えた後のコード)をそのまま
  Pythonから呼び出した。結果:
  - `SELECT current_user`が`app_runtime`であることを確認したうえで実行
  - 世帯A・世帯Bそれぞれにメモ付きで記録を保存 → 成功
  - 世帯Aの検索結果に世帯Bのメモが含まれない(逆も同様)ことを確認 →
    世帯分離(RLS)が最小権限下で機能
  - `素のSQLで直接public.recordsへアクセスしようとした場合は拒否される`
    (`tests/test_least_privilege_schema.py::
    test_app_runtime_cannot_query_records_table_directly`で別途確認済み)ことと
    合わせ、「app_runtimeはEXECUTE権限だけでmemo保存・検索ができ、
    直接のテーブルアクセスはできない」という設計どおりの状態を実測で確認した

この実測により、監査項目⑪(PR #29適用後にPR #30のメモ機能が動かなくなる
不整合)は**解消されたことを実機で確認済み**。

### ★round 3統合監査対応(2026-09-04): Critical 1件・High 3件・Medium 1件

上記の統合監査ZIP提出後、ChatGPTより「現時点ではマージ・staging適用を
承認できない」との判定を受けた。指摘5点はいずれも妥当であり、すべて
対応済み。

**1. Critical: staging適用順序が成立しない(全面訂正)**——`streamlit/db.py`は
`public.record_with_memo_for_tenant`・`public.search_records_for_tenant`を
直接呼ぶが、この2関数を作るのはPR #29の`migrate_to_least_privilege_
schema.py`であり、**PR #30自身の`migrate_to_records_memo_schema.py`は
memo列だけを作り、関数は作らない**。旧版(round 2)で示していた
「PR #30単独のmigration→PR #30マージ」という順序では、関数が存在せず
`UndefinedFunction`で保存・検索が失敗する。**正しい順序へ全面訂正**:

```
① PR #29(この設計書)をstagingへマージ(コードのみ、DDLは自動実行されない)
② staging DBの接続先・パスワード方式・バックアップを確認
③ scripts/migrate_to_least_privilege_schema.pyをstagingで実行
   (ロール作成・memo列・14関数・GRANT・RLSすべてを一度に適用)
④ 既存アプリの正常性(記録・課金・認証等、第16〜21回の機能)とDB状態を確認
⑤ PR #30をstagingへマージ(Railway自動デプロイ)
⑥ 保存・検索・並び替え・世帯分離を実機で確認
⑦ 別途承認のもとで、アプリの接続ロールをapp_runtimeへ切り替え
```

`scripts/migrate_to_records_memo_schema.py`単独の位置づけを明記する:
**stagingへは単独実行しない**。用途は(a)PR #29の`migrate_to_least_
privilege_schema.py`内部から`ensure_records_memo_column()`として再利用
される、(b)ローカル開発・テストで最小権限化を伴わずmemo列だけを試したい
場合の補助ツール、の2点に限定する。`main()`(CLIとしての単独実行)は
ローカル検証専用とし、staging・productionへは実行しないことを明記する
(仕様書/検索できるDB設計.md側にも同様の訂正を反映済み)。

**2. High: DB関数自身に入力検証が無かった**——`record_with_memo_for_tenant`・
`search_records_for_tenant`は、keywordのLIKEエスケープ・orderの検証は
関数内で行っていたが、メモ・検索キーワードの**長さ・制御文字**の検証は
Python側(`streamlit/storage.py`)にしか無く、`app_runtime`資格情報を
使って関数を直接呼べば素通りしてしまっていた。「呼び出し元を信頼せず
関数自身で安全性を保証する」という設計思想と矛盾していたため、
`scripts/memo_search_functions.py`に以下を追加した。

- `p_memo`: 200文字超・制御文字(`[[:cntrl:]]`)を拒否
- `p_keyword`: 100文字超・制御文字を拒否
- いずれもSQLSTATE `22023`(`invalid_parameter_value`)で送出し、呼び出し側
  (`psycopg.errors.InvalidParameterValue`)が判別できるようにした
- 拒否時に既存行が変更されないことを実機テストで確認

**3. High: `p_order=NULL`が拒否されなかった**——`IF p_order NOT IN ('asc',
'desc') THEN`は、SQLの3値論理により`p_order`が`NULL`の場合`NULL`(FALSEでは
ない)と評価され、例外が発生しないまま両方の`ORDER BY`条件がNULLとなり
順序未保証の検索が成功してしまうバグがあった(実機で再現・修正確認済み)。
`IF p_order IS NULL OR p_order NOT IN ('asc', 'desc') THEN`へ修正した。

**4. High: 設計書内に旧12関数版の実行可能コードが残っていた**——本文
§5(関数一覧)・§6(GRANT一覧)・§16(main())・§17(ロールバック手順)は
いずれも第13次改訂版(12関数)時点のコード例のままで、「過去版」と
明示されずに実行可能な形で残っていた。各章の冒頭に、現在の正本
(`scripts/*.py`)を参照するよう促す警告を追加し、§5-7(関数一覧まとめ)・
§6-1(テーブルGRANT)は表・コードを14関数版へ更新した(本文の完全な
書き換えは行っていない。理由: 第1〜13次の経緯そのものに監査上の価値が
あるため、実行禁止を明示したうえで経緯記録として残す方針とした)。

**5. Medium: PL/pgSQL事前検証の説明が不正確だった**——
`scripts/memo_search_functions.py`のdocstringに「memo列が無い状態では
CREATE FUNCTION自体が失敗する」という趣旨の記述があったが、これは誤り。
PL/pgSQL本体は基本的な構文チェックのみでCREATEでき、埋め込まれたSQLが
参照する列の存在は実行時まで検証されない(実機で確認: memo列が無い状態
でも`CREATE FUNCTION`は成功し、実際に呼び出した時点で初めて
`UndefinedColumn`になる)。docstringを訂正し、安全性の根拠は
`ensure_records_memo_column()`の明示的な列定義検証であることを明記した。

**追加した自動テスト**(`tests/test_least_privilege_schema.py`、
`app_runtime`として完全な最小権限適用後に直接呼び出す実機テスト、
PostgreSQL 16・18双方で確認):

- メモ201文字を拒否し既存行を変更しない
  (`test_memo_validation_rejects_invalid_input_and_preserves_existing_data`)
- 検索語101文字・メモ/検索語の制御文字・`order=NULL`/不正値を拒否
  (`test_search_validation_rejects_invalid_input`)
- 同日再保存時のmemo更新(`test_memo_resave_same_date_updates_memo`)
- `%`・`_`・`\`を文字どおり検索、asc/desc両方が機能
  (`test_search_keyword_escapes_wildcards_and_both_orders_work`)

既存202件+この設計書の20件(既存14件+第14次の2件+round 3の4件)=222件、
PostgreSQL 16・18双方で全PASS。詳細は次の統合監査round 3 ZIPを参照。

**この改訂の背景(第12次)**: 第11次改訂版の監査資料ZIPを岩瀬様が実物監査
した結果、**「異常時(クロスDB依存・記録欠落・最終状態不一致等)でも
処理を続行し、`[OK]`や終了コード0で成功扱いにしてしまう」という重大な
系統的問題**が判明した。8点の指摘を受け、すべて修正した。

1. **[Critical]** クロスDB依存でTier 2が縮退(ロール未削除)しても
   `[OK]`・終了コード0を返していた → `COMPLETE`(0)/`DEGRADED`(2)/
   `FAILED`(1)を明確に分離した
2. **[Critical]** Tier 3はロール縮退後も関数・制約・ログの削除へ処理を
   続行し、成功扱いできた → `degraded_roles`が空でなければ以降の削除
   処理へ進まず`DEGRADED`(2)で停止するよう訂正した
3. 最終状態がprintされるだけで合否判定していなかった →
   `verify_round21_baseline_state()`を新設し、不一致なら
   `BaselineStateMismatchError`で例外を送出、呼び出し元がROLLBACKする
   ようにした
4. NOT NULLの復帰確認が抜けていた → 3の関数へ
   `information_schema.columns.is_nullable`の確認を追加した
5. 制約撤去がmigration記録を参照せず、NOT NULLを無条件に外していた →
   `schema_migration_log`の履歴(このTaskが実際にadded_not_null/
   added_uniqueをtrueにしたことがあるか)と照合するよう全面訂正した
6. 監査ログの外部退避がDBトランザクションと分離しており、耐久性も
   未保証だった → 自動書き出しを安全性の根拠から外し、テーブル削除の
   前提として「人間が事前に手動でエクスポート・保存・ハッシュ確認した」
   ことを示す環境変数を必須の事前条件とした
7. staging/production識別の環境変数が未設定でも処理が続いた →
   4項目(接続先DB名・接続ユーザー・環境固有識別子・明示的staging DDL
   許可フラグ)すべてを未設定/不一致なら停止する必須項目へ変更した
8. 追加テストスクリプトが検証失敗時も終了コード0になり得た →
   全チェックをDBへの直接問い合わせによる判定へ統一し、1件でも失敗
   したら終了コード1になるテストランナー(`run_all_checks.py`)へ刷新
   した

すべてPostgreSQL 16・18の実機で33項目を再検証し、全項目PASSした。
詳細は監査ZIP(第12次)を参照。

**この改訂はChatGPTが一時的に利用できない間の、Claudeによる自己監査である**
(ChatGPTによる第三者チェックの代わりにはならない。岩瀬様がChatGPTの代役を
明示的に引き受けるとおっしゃった場合を除き、最終承認はChatGPT復帰後を
基本とする)。第9次改訂版に対し、ChatGPTがこれまで指摘してきたのと同じ観点
(トランザクション境界・冪等性・事前条件・「完全復帰」の実測整合性)で
17章の共有ヘルパー関数を1行ずつ点検し、次の3件(いずれも実行時エラーや
安全停止を起こす重大な不具合ではなく、検証・監査の正確さに関する軽微な
穴)を発見・修正した。

1. `_capture_full_baseline_state`(Tier 3・17-4章の最終確認)の
   `stripe_subscription_id`制約チェックが、本Taskが使う制約名だけの
   存在確認になっており、本Task以外の経路で**別名の**同等UNIQUE制約が
   作られていた場合、それを見逃したまま「完全復帰」と誤って確認して
   しまう非対称性があった(12-2章の作成時重複検出は列単位で正確なのに、
   17章の最終確認は名前単位だった)
2. `_export_and_drop_migration_log`が、`schema_migration_log`テーブル
   自体が存在しない場合(12-3章の制約migrationが一度も実行されないまま
   Tier 3等が呼ばれた場合)に、素の`SELECT`が「テーブルが存在しません」
   でエラーになる想定漏れがあった
3. `_drop_all_functions`の実行前後の件数確認が、関数名だけの一致判定
   (`proname = ANY(...)`)であり、引数型まで含めた完全なシグネチャでの
   照合になっていなかった(実際の`DROP FUNCTION`文自体は完全シグネチャ
   指定で正確だが、件数確認の精度が甘かった)

いずれも17-0章の共有ヘルパーへ反映済み。実装・DDL・ロール作成・commit・
push・PR作成は引き続き行っていない。

**この改訂の背景**: 第8次改訂版の監査で、次の5件が判明した。うち①は
**Critical**で、第8次改訂版で新設した`schema_migration_log`が
「第21回終了時点への全面復帰」という定義そのものと矛盾する内容だった。

1. **[Critical]** 12-2章で新設した`public.schema_migration_log`テーブル
   (および付随する`bigserial`のシーケンス)がTier 3実行後も残る設計に
   なっており、「1章①〜⑤と完全一致」「第21回終了時点への全面復帰」と
   いう記載と矛盾していた(テーブル7件・シーケンス1件のはずが8件・
   2件になる)
2. Tier 3のSQLが「①〜⑧はTier 2と同一の内容をこの中で実行する」という
   省略表現であり、単体で実行可能な全文になっていなかった
3. Tier 2実行後にTier 3を実行すると、3ロールが既に存在しないため
   `DROP ROLE`等が失敗する構造であり、両者を順番に実行するのか排他的に
   選ぶのかが不明確だった
4. Tier 3の監査ログが固定値(`{"dropped_roles": 3, "dropped_functions":
   12, "dropped_constraints": true}`)で記録されており、`IF EXISTS`に
   より実際には何も削除されなかった場合でも「削除した」という誤った
   記録になりうった
5. Tier 3の制約撤去前確認が「12-4章の事前確認手順を先に済ませたうえで」
   という人手依存の記載のみで、確認から実行までの間に状態が変わる
   可能性に対応していなかった

これらはChatGPTによる第8次改訂版監査で判明し、第9次改訂版で修正済みで
ある。第6〜7次改訂版からの訂正も前版までに反映済みのため、以下の本文
には統合済みの姿のみを記す(第10次改訂版としての差分は本ページ冒頭の
自己監査3件のみ)。

---

## 0. 前提・スコープ

- 対象: `claude-code-club/shokki-arai-support`、`staging`環境のみ
- production・`main`: 変更しない、Stripe: Sandboxのみ、実課金なし
- 秘密値・接続文字列は本書に一切記載しない
- 本書のSQL・Pythonコードはすべて「未実行、承認後に実行」の計画

### 0-1. 接続先識別確認(第13次訂正: Railway自動注入識別子で独立検証、全migration/rollbackスクリプト共通)

**訂正の要点(第13次・点B)**: 第12次改訂版は環境識別を
`EXPECTED_TARGET_ENVIRONMENT_ID`/`ACTUAL_TARGET_ENVIRONMENT_ID`という
**操作者が両辺とも手入力する2値の比較**で行っており、独立した確認に
なっていない(操作者が同じ誤った値を両方へ書けば通ってしまう)という
指摘を受けた。Railwayは`railway run`実行時に、リンクされたプロジェクト/
環境の識別子(`RAILWAY_PROJECT_ID`・`RAILWAY_ENVIRONMENT_ID`等)を
**操作者が値を書く必要なく自動的に環境変数へ注入する**。これを実測側
として使うことで、期待値(操作者/ドキュメントが定める)と実測値
(Railway自身が供給する)という真に独立した2系統の比較へ訂正した。
**実運用では、これらのスクリプトは必ず`railway run`経由(正しい
プロジェクト/環境にリンクした状態)で実行すること。**

```python
"""scripts/target_identity.py

migration/rollbackスクリプトの冒頭で必ず呼び出し、接続先を人間が確認
できる識別情報を出力したうえで、想定外の接続先ならDDL開始前に停止する。
接続文字列・パスワードはここでは一切扱わない(db.get_connection()の
内部にのみ存在し、このモジュールへは渡らない)。
"""
import os

EXPECTED_BASELINE_TABLES = {
    "records", "tenants", "tenant_memberships", "users",
    "tenant_subscriptions", "tenant_usage", "processed_stripe_events",
}


class TargetDatabaseMismatchError(Exception):
    pass


def verify_target_database_identity(cur):
    """接続先データベース名・接続ユーザー・PostgreSQLバージョン・想定7
    テーブルの存在を確認し、人間が目視確認できる形で標準出力へ表示する。

    次がすべて正しく設定・一致していない限り停止する(未設定を許容
    しない)。

    - EXPECTED_TARGET_DBNAME / 実際のdbnameと完全一致
    - EXPECTED_TARGET_USER / 実際のcurrent_userと完全一致
    - EXPECTED_RAILWAY_PROJECT_ID / RAILWAY_PROJECT_ID(Railway自動注入)と完全一致
    - EXPECTED_RAILWAY_ENVIRONMENT_ID / RAILWAY_ENVIRONMENT_ID(Railway自動注入)と完全一致
    - STAGING_DDL_EXPLICITLY_ALLOWED=true
    """
    cur.execute("SELECT current_database(), current_user, version()")
    dbname, user, version_string = cur.fetchone()

    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    actual_tables = {r[0] for r in cur.fetchall()}
    missing_tables = sorted(EXPECTED_BASELINE_TABLES - actual_tables)

    identity = {
        "current_database": dbname,
        "current_user": user,
        "server_version": version_string,
        "missing_expected_tables": missing_tables,
    }
    print(f"[接続先確認] {identity}")

    if missing_tables:
        raise TargetDatabaseMismatchError(
            f"接続先データベース({dbname})に想定するテーブルが見つかりません: "
            f"{missing_tables}。staging以外へ誤接続していないか、実行前に"
            "必ず人間が確認してください。DDLは一切実行していません。"
        )

    expected_dbname = os.environ.get("EXPECTED_TARGET_DBNAME", "").strip()
    if not expected_dbname:
        raise TargetDatabaseMismatchError(
            "EXPECTED_TARGET_DBNAMEが設定されていません。接続先データベース名を"
            "明示的に指定しない限り実行できません。DDLは一切実行していません。"
        )
    if dbname != expected_dbname:
        raise TargetDatabaseMismatchError(
            f"接続先データベース名が想定と異なります: 実際={dbname} "
            f"期待={expected_dbname}(EXPECTED_TARGET_DBNAME)。DDLは一切"
            "実行していません。"
        )

    expected_user = os.environ.get("EXPECTED_TARGET_USER", "").strip()
    if not expected_user:
        raise TargetDatabaseMismatchError(
            "EXPECTED_TARGET_USERが設定されていません。接続ユーザーを明示的に"
            "指定しない限り実行できません。DDLは一切実行していません。"
        )
    if user != expected_user:
        raise TargetDatabaseMismatchError(
            f"接続ユーザーが想定と異なります: 実際={user} "
            f"期待={expected_user}(EXPECTED_TARGET_USER)。DDLは一切"
            "実行していません。"
        )

    # [第13次訂正] Railway自動注入の識別子(railway run経由でのみ供給
    # される)と、操作者が設定した期待値を突き合わせる。独立性を確保する
    # ため、実測側(RAILWAY_PROJECT_ID等)は操作者が手入力するものでは
    # ない。
    expected_project_id = os.environ.get("EXPECTED_RAILWAY_PROJECT_ID", "").strip()
    if not expected_project_id:
        raise TargetDatabaseMismatchError(
            "EXPECTED_RAILWAY_PROJECT_IDが設定されていません。DDLは一切"
            "実行していません。"
        )
    actual_project_id = os.environ.get("RAILWAY_PROJECT_ID", "").strip()
    if not actual_project_id:
        raise TargetDatabaseMismatchError(
            "RAILWAY_PROJECT_IDが環境変数から取得できません。このスクリプトは"
            "必ず`railway run`経由(正しいプロジェクト/環境にリンクした状態)"
            "で実行してください。DDLは一切実行していません。"
        )
    if actual_project_id != expected_project_id:
        raise TargetDatabaseMismatchError(
            f"RAILWAY_PROJECT_IDが想定と異なります: 実際={actual_project_id} "
            f"期待={expected_project_id}。DDLは一切実行していません。"
        )

    expected_environment_id = os.environ.get("EXPECTED_RAILWAY_ENVIRONMENT_ID", "").strip()
    if not expected_environment_id:
        raise TargetDatabaseMismatchError(
            "EXPECTED_RAILWAY_ENVIRONMENT_IDが設定されていません。DDLは一切"
            "実行していません。"
        )
    actual_environment_id = os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip()
    if not actual_environment_id:
        raise TargetDatabaseMismatchError(
            "RAILWAY_ENVIRONMENT_IDが環境変数から取得できません。このスクリプト"
            "は必ず`railway run`経由で実行してください。DDLは一切実行して"
            "いません。"
        )
    if actual_environment_id != expected_environment_id:
        raise TargetDatabaseMismatchError(
            f"RAILWAY_ENVIRONMENT_IDが想定と異なります: "
            f"実際={actual_environment_id} 期待={expected_environment_id}。"
            "DDLは一切実行していません。"
        )

    staging_flag = os.environ.get("STAGING_DDL_EXPLICITLY_ALLOWED", "").strip().lower()
    if staging_flag != "true":
        raise TargetDatabaseMismatchError(
            "STAGING_DDL_EXPLICITLY_ALLOWED=trueが設定されていません"
            "(明示的なstaging DDL許可フラグが無いため停止します)。"
            "DDLは一切実行していません。"
        )

    return identity
```

各`main()`の先頭(`with conn.cursor() as cur:`の直後、最初のDDLより前)で
`verify_target_database_identity(cur)`を呼び、`TargetDatabaseMismatchError`
を例外捕捉リストへ追加する(12章・16章・17章の該当箇所に反映済み)。
**実運用では`EXPECTED_TARGET_DBNAME`・`EXPECTED_TARGET_USER`・
`EXPECTED_RAILWAY_PROJECT_ID`・`EXPECTED_RAILWAY_ENVIRONMENT_ID`・
`STAGING_DDL_EXPLICITLY_ALLOWED`をすべて設定することが必須であり、
未設定では一切のDDLが実行できない。加えて、`RAILWAY_PROJECT_ID`・
`RAILWAY_ENVIRONMENT_ID`はRailwayが`railway run`実行時に自動注入する
ため、必ず`railway run`経由(正しいプロジェクト/環境にリンクした状態)
で実行すること。**

PostgreSQL 16・18の実機で、①想定7テーブルが無いデータベースへの接続、
②`EXPECTED_TARGET_DBNAME`未設定、③`EXPECTED_TARGET_USER`未設定、
④`EXPECTED_RAILWAY_PROJECT_ID`未設定、⑤`RAILWAY_PROJECT_ID`未注入
(`railway run`未経由を模擬)、⑥`RAILWAY_PROJECT_ID`不一致、
⑦`EXPECTED_RAILWAY_ENVIRONMENT_ID`未設定、⑧`RAILWAY_ENVIRONMENT_ID`
不一致、⑨`STAGING_DDL_EXPLICITLY_ALLOWED`未設定の9パターンすべてで
`TargetDatabaseMismatchError`により即座に停止し、ロールを含む一切の
DDLが実行されないことを確認済み(11章#16〜#20群)。

---

## 1. 現状監査結果（①〜⑤）

- テーブル7件・シーケンス1件(`records_id_seq`)・カスタム関数0件
- `users.auth_subject`列に`UNIQUE NOT NULL`制約が既に存在する
- `tenant_id`は現状すべてSQLプレースホルダーで直接バインド
- コネクションプール未使用。PgBouncer未導入。admin/member判定はアプリ層のみ

| # | 確認内容 | 結果 |
|---|---|---|
| ① | テーブル・シーケンスの所有者(全8件) | すべて`postgres` |
| ② | RLS有効化状況(全7テーブル) | すべて`false` |
| ③ | RLSポリシー | 0件 |
| ④ | 既存ロール一覧(全17件) | PostgreSQL標準16件＋`postgres`のみ |
| ⑤ | カスタム関数 | 0件 |

**この5項目(特に③⑤=0件)は、20章の「第21回終了時点への完全復帰」テスト
の照合基準として、17章で再度参照する。**

---

## 2. 追加確認SQL（⑥〜⑪、実行結果）

- ⑥ `PostgreSQL 18.6`。`public_role_can_create = false`
- ⑦ `log_statement = none`、`log_min_duration_statement = -1`
- ⑧ `stripe_subscription_id`に一意制約無し
- ⑨ `ssl = on`(サーバー側の可否のみ)
- ⑩ `total_rows=1`・`nonnull_rows=1`・`null_rows=0`・`distinct_nonnull_values=1`
- ⑪ `duplicate_value_groups=0`・`extra_duplicate_rows=0`

`NOT NULL`＋`UNIQUE`を採用する。

---

## 3. RLSと関数ファサードの役割の切り分け

### 3-1〜3-5(変更なし)

`postgres`所有の関数はRLSを迂回する。RLSが実際に効果を持つのは将来の
誤GRANTに対する保険のみ。`app_data_owner`(NOLOGIN・NOBYPASSRLS)へ関数
所有者を分離することでRLSが機能するようにするが、`FORCE`は不要。各関数
冒頭の`pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true)`
がRLSの絞り込み基準になるが、これは「関数のSQL文がWHERE句を書き忘れた」
バグのみを防ぎ、引数そのものの偽装は防げない。`resolve_login`・
`find_tenant_id_by_subscription`は`postgres`所有のまま意図的な例外とする。

---

## 4. ロール構成表・CREATE ROLE文・パスワード管理（既存LOGINロールの扱いを訂正）

### 4-1. ロール構成表

| ロール | 用途 | LOGIN | SUPERUSER | BYPASSRLS | CREATEROLE | CREATEDB | REPLICATION | INHERIT |
|---|---|---|---|---|---|---|---|---|
| `postgres`(既存) | 所有・migration・保守 | true | true | true | true | true | true(既定) | true(既定) |
| `app_data_owner`(新設) | 10関数の所有者 | false | false | false | false | false | false | false |
| `app_runtime`(新設) | `web`サービス | true | false | false | false | false | false | false |
| `app_webhook`(新設) | `stripe-webhook`サービス | true | false | false | false | false | false | false |

### 4-2. 既存ロールの属性検証（変更なし、NOLOGIN専用）

```python
EXPECTED_ROLE_ATTRS = {
    "app_data_owner": {
        "rolsuper": False, "rolcreaterole": False, "rolcreatedb": False,
        "rolcanlogin": False, "rolbypassrls": False,
        "rolreplication": False, "rolinherit": False,
    },
    "app_runtime": {
        "rolsuper": False, "rolcreaterole": False, "rolcreatedb": False,
        "rolcanlogin": True, "rolbypassrls": False,
        "rolreplication": False, "rolinherit": False,
    },
    "app_webhook": {
        "rolsuper": False, "rolcreaterole": False, "rolcreatedb": False,
        "rolcanlogin": True, "rolbypassrls": False,
        "rolreplication": False, "rolinherit": False,
    },
}

ATTR_COLUMNS = [
    "rolsuper", "rolcreaterole", "rolcreatedb", "rolcanlogin",
    "rolbypassrls", "rolreplication", "rolinherit",
]


class RoleAttributeMismatchError(Exception):
    """既存ロールの属性またはメンバーシップが想定と一致しない場合。"""


def _verify_role_attrs_and_membership(cur, role_name):
    """属性・メンバーシップだけを検証する(作成やパスワード設定は行わない、
    NOLOGIN・LOGIN両方で共通に使う下請け関数)。ロールが存在しなければ
    Noneを返す。
    """
    cur.execute(
        f"SELECT {', '.join(ATTR_COLUMNS)} FROM pg_roles WHERE rolname = %s",
        (role_name,),
    )
    row = cur.fetchone()
    if row is None:
        return None

    actual = dict(zip(ATTR_COLUMNS, row))
    expected = EXPECTED_ROLE_ATTRS[role_name]
    if actual != expected:
        raise RoleAttributeMismatchError(
            f"ロール{role_name}の属性が想定と一致しません: "
            f"実際={actual} 期待={expected}"
        )

    cur.execute(
        "SELECT r.rolname FROM pg_auth_members m "
        "JOIN pg_roles r ON r.oid = m.roleid "
        "JOIN pg_roles member_role ON member_role.oid = m.member "
        "WHERE member_role.rolname = %s",
        (role_name,),
    )
    memberships = [r[0] for r in cur.fetchall()]
    if memberships:
        raise RoleAttributeMismatchError(
            f"ロール{role_name}が想定外のロールのメンバーです: {memberships}"
        )

    cur.execute(
        "SELECT member_role.rolname FROM pg_auth_members m "
        "JOIN pg_roles r ON r.oid = m.roleid "
        "JOIN pg_roles member_role ON member_role.oid = m.member "
        "WHERE r.rolname = %s",
        (role_name,),
    )
    members_of_this = [r[0] for r in cur.fetchall()]
    if members_of_this:
        raise RoleAttributeMismatchError(
            f"ロール{role_name}に想定外のメンバーが所属しています: {members_of_this}"
        )
    return actual


def verify_or_create_nologin_role(cur, role_name):
    """app_data_owner専用。パスワードの概念が無いため、既存なら属性検証
    のみで完了する。
    """
    existing = _verify_role_attrs_and_membership(cur, role_name)
    if existing is None:
        query = sql.SQL(
            "CREATE ROLE {role} WITH NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT"
        ).format(role=sql.Identifier(role_name))
        cur.execute(query)
```

### 4-3. LOGINロール(`app_runtime`・`app_webhook`)のパスワード管理（訂正: 一致確認ではなく明示的な上書きで保証する）

**訂正の要点**: 前版の`verify_or_create_role()`は、ロールが既存の場合は
属性検証のみを行い、CREATE処理(パスワード設定)を呼ばなかった。しかし
PostgreSQLは、既存ロールの平文パスワードをカタログから読み出す手段を
一切提供しない(`pg_authid.rolpassword`にはハッシュしか保存されず、
逆算はできない)。**「環境変数の値と既存ロールのパスワードが一致して
いるはずだ」という前提を、確認せずに置くことはできない**。この前提を
本書からは一切排除し、次の方針へ統一する。

> **既存のLOGINロールであっても、migration実行のたびに
> `ALTER ROLE ... PASSWORD ...`で明示的にパスワードを上書きする。**
> これにより「一致しているかを確認する」のではなく、「migration実行後は
> 必ず環境変数の値と一致している」という状態を、確認ではなく上書きに
> よって保証する。**訂正**: 同じ平文パスワードで`ALTER ROLE`を再実行
> しても、PostgreSQLのSCRAM認証はソルトを毎回新しく生成するため、
> `pg_authid.rolpassword`に保存される内部ハッシュ値そのものは実行の
> たびに変わりうる。「副作用が無く完全に冪等」ではなく、**「その平文
> パスワードで接続できるという、接続資格情報としての結果は変わらない」**
> という意味で、この操作を毎回行うこと自体に問題は無い。

```python
def verify_or_set_login_role_password(cur, role_name, password):
    """app_runtime・app_webhook専用。

    既存ロールの場合: 属性・メンバーシップを検証したうえで、
    ALTER ROLE ... PASSWORD ...により、常に環境変数の値へ上書きする
    (既存パスワードとの一致確認はカタログから行えないため、確認ではなく
    上書きで事後状態を保証する)。
    新規の場合: CREATE ROLE ... PASSWORD ...で作成する。

    いずれの場合も、この関数の実行後にロールのパスワードが本当に
    渡された値と一致しているかは、このSQL操作自体からは検証できない。
    **必ず、この後の別接続試験(14章手順)で実際に接続できることを
    確認すること。**「パスワードの一致をカタログから確認できる」とは
    どこにも記載しない。
    """
    existing = _verify_role_attrs_and_membership(cur, role_name)
    if existing is None:
        query = sql.SQL(
            "CREATE ROLE {role} WITH LOGIN PASSWORD {password} "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            "NOBYPASSRLS NOINHERIT"
        ).format(role=sql.Identifier(role_name), password=sql.Literal(password))
        cur.execute(query)
        return

    query = sql.SQL("ALTER ROLE {role} PASSWORD {password}").format(
        role=sql.Identifier(role_name), password=sql.Literal(password)
    )
    cur.execute(query)
```

- パスワードは環境変数(`LEAST_PRIVILEGE_APP_RUNTIME_PASSWORD`・
  `LEAST_PRIVILEGE_APP_WEBHOOK_PASSWORD`)経由でのみ取得し、未設定なら
  即座に停止する(前版どおり)
- `sql.Literal(password)`によりパスワードは安全にエスケープされ、
  文字列連結・f-stringは使わない
- **本書はどこにも「既存ロールのパスワードが環境変数の値と一致している
  ことをカタログから確認できる」とは記載しない。migration実行(上書き)
  のあとに、実際に新しい資格情報で接続できることを、14章の別接続試験で
  確認することが唯一の保証手段である**

```python
import os
from psycopg import sql


class MissingPasswordError(Exception):
    """パスワードが環境変数から取得できない場合。"""


def _read_required_password(env_var_name):
    value = os.environ.get(env_var_name, "").strip()
    if not value:
        raise MissingPasswordError(
            f"{env_var_name}が設定されていません。コマンドライン引数や"
            "対話入力は使わず、環境変数経由でのみ渡してください。"
        )
    return value
```

---

## 5. 関数一覧（全12関数、完全修飾・原子的resolve_login、変更なし）

> ⚠️**この章のコード例は第13次改訂版(12関数)時点のものであり、
> 第14次(案A統合、2026-09-04、14関数)の内容を反映していません**。
> `record_with_memo_for_tenant`・`search_records_for_tenant`の2関数が
> `scripts/memo_search_functions.py`に追加されています。**実装・運用時は
> この章のSQLを一切コピー&ペーストせず、必ず実際のソースファイル
> (`scripts/least_privilege_lib.py`・`scripts/memo_search_functions.py`)を
> 正本として参照してください。** 現在の正しい14関数の一覧は5-7章
> (更新済み)、および冒頭の「★統合追記(案A、2026-09-04)」を参照。
> この章自体は「なぜこの設計になったか」を理解するための経緯記録として
> 残しています。

### 5-1. `records`関連(3関数、`web`専用)

```sql
CREATE OR REPLACE FUNCTION public.load_dates_for_tenant(p_tenant_id uuid)
RETURNS SETOF date
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
BEGIN
  PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
  RETURN QUERY
    SELECT record_date FROM public.records WHERE tenant_id = p_tenant_id;
END;
$$;


CREATE OR REPLACE FUNCTION public.insert_date_for_tenant(p_tenant_id uuid, p_record_date date)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
BEGIN
  PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
  INSERT INTO public.records (tenant_id, record_date)
  VALUES (p_tenant_id, p_record_date)
  ON CONFLICT (tenant_id, record_date) DO NOTHING;
END;
$$;


CREATE OR REPLACE FUNCTION public.delete_date_for_tenant(p_tenant_id uuid, p_record_date date)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
BEGIN
  PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
  DELETE FROM public.records
  WHERE tenant_id = p_tenant_id AND record_date = p_record_date;
END;
$$;
```

### 5-2. `tenants`関連(1関数)

```sql
CREATE OR REPLACE FUNCTION public.update_tenant_name(p_tenant_id uuid, p_name text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
BEGIN
  PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
  UPDATE public.tenants SET name = p_name WHERE id = p_tenant_id;
END;
$$;
```

### 5-3. ログイン・所属世帯の特定(1関数、`postgres`所有)

```sql
CREATE OR REPLACE FUNCTION public.resolve_login(
  p_auth_subject text, p_email text, p_email_verified boolean
) RETURNS TABLE(user_id uuid, tenant_id uuid, role text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE
  v_user_id uuid;
BEGIN
  INSERT INTO public.users (id, auth_subject, email, email_verified)
  VALUES (gen_random_uuid(), p_auth_subject, p_email, p_email_verified)
  ON CONFLICT (auth_subject) DO UPDATE SET
    email = EXCLUDED.email,
    email_verified = EXCLUDED.email_verified
  RETURNING id INTO v_user_id;

  RETURN QUERY
    SELECT v_user_id, tm.tenant_id, tm.role
    FROM public.tenant_memberships tm
    WHERE tm.user_id = v_user_id;
END;
$$;
```

### 5-4. Stripe課金状態(4関数)

```sql
CREATE OR REPLACE FUNCTION public.get_subscription(p_tenant_id uuid)
RETURNS TABLE(plan text, status text, current_period_end timestamptz, stripe_customer_id text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
BEGIN
  PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
  RETURN QUERY
    SELECT ts.plan, ts.status, ts.current_period_end, ts.stripe_customer_id
    FROM public.tenant_subscriptions ts
    WHERE ts.tenant_id = p_tenant_id;
END;
$$;


CREATE OR REPLACE FUNCTION public.upsert_subscription_if_new_session(
  p_tenant_id uuid, p_plan text, p_status text, p_stripe_customer_id text,
  p_stripe_subscription_id text, p_stripe_checkout_session_id text,
  p_current_period_end timestamptz
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE
  v_applied boolean;
BEGIN
  PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
  INSERT INTO public.tenant_subscriptions
    (tenant_id, plan, status, stripe_customer_id, stripe_subscription_id,
     stripe_checkout_session_id, current_period_end, updated_at)
  VALUES (p_tenant_id, p_plan, p_status, p_stripe_customer_id, p_stripe_subscription_id,
     p_stripe_checkout_session_id, p_current_period_end, now())
  ON CONFLICT (tenant_id) DO UPDATE SET
    plan = EXCLUDED.plan,
    status = EXCLUDED.status,
    stripe_customer_id = EXCLUDED.stripe_customer_id,
    stripe_subscription_id = EXCLUDED.stripe_subscription_id,
    stripe_checkout_session_id = EXCLUDED.stripe_checkout_session_id,
    current_period_end = EXCLUDED.current_period_end,
    updated_at = now()
  WHERE public.tenant_subscriptions.stripe_checkout_session_id
    IS DISTINCT FROM EXCLUDED.stripe_checkout_session_id
  RETURNING true INTO v_applied;
  RETURN COALESCE(v_applied, false);
END;
$$;


CREATE OR REPLACE FUNCTION public.find_tenant_id_by_subscription(p_stripe_subscription_id text)
RETURNS uuid
LANGUAGE sql SECURITY DEFINER SET search_path = ''
AS $$
  SELECT tenant_id FROM public.tenant_subscriptions
  WHERE stripe_subscription_id = p_stripe_subscription_id;
$$;


CREATE OR REPLACE FUNCTION public.update_subscription_status(
  p_tenant_id uuid, p_plan text, p_status text, p_current_period_end timestamptz
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE
  v_updated boolean;
BEGIN
  PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
  UPDATE public.tenant_subscriptions
  SET plan = p_plan, status = p_status, current_period_end = p_current_period_end,
      updated_at = now()
  WHERE tenant_id = p_tenant_id
  RETURNING true INTO v_updated;
  RETURN COALESCE(v_updated, false);
END;
$$;
```

### 5-5. プラン制限・メータリング(2関数)

```sql
CREATE OR REPLACE FUNCTION public.get_tenant_usage_count(
  p_tenant_id uuid, p_metric_key text, p_period_start date
) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE
  v_count integer;
BEGIN
  PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
  SELECT usage_count INTO v_count
  FROM public.tenant_usage
  WHERE tenant_id = p_tenant_id AND metric_key = p_metric_key AND period_start = p_period_start;
  RETURN COALESCE(v_count, 0);
END;
$$;


CREATE OR REPLACE FUNCTION public.increment_tenant_usage_if_under_limit(
  p_tenant_id uuid, p_metric_key text, p_period_start date, p_limit integer
) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE
  v_new_count integer;
BEGIN
  PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
  INSERT INTO public.tenant_usage (tenant_id, metric_key, period_start, usage_count, updated_at)
  VALUES (p_tenant_id, p_metric_key, p_period_start, 1, now())
  ON CONFLICT (tenant_id, metric_key, period_start) DO UPDATE SET
    usage_count = public.tenant_usage.usage_count + 1,
    updated_at = now()
  WHERE p_limit IS NULL OR public.tenant_usage.usage_count < p_limit
  RETURNING usage_count INTO v_new_count;
  RETURN v_new_count;
END;
$$;
```

### 5-6. Webhookイベント重複防止(1関数)

```sql
CREATE OR REPLACE FUNCTION public.mark_stripe_event_processed(p_event_id text, p_event_type text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE
  v_inserted boolean;
BEGIN
  INSERT INTO public.processed_stripe_events (stripe_event_id, event_type)
  VALUES (p_event_id, p_event_type)
  ON CONFLICT (stripe_event_id) DO NOTHING
  RETURNING true INTO v_inserted;
  RETURN COALESCE(v_inserted, false);
END;
$$;
```

### 5-7. 関数一覧まとめ(★第14次改訂で更新: 全14関数、所有者を明記)

**この表は現在の正しい状態(第14次、2026-09-04、14関数)を反映しています**
(§5本文のSQL自体は第13次<12関数>のまま、上記の警告を参照)。

| # | 関数名(完全修飾) | 所有者 | 呼び出し可能ロール |
|---|---|---|---|
| 1 | `public.load_dates_for_tenant(uuid)` | app_data_owner | app_runtime |
| 2 | `public.insert_date_for_tenant(uuid, date)` | app_data_owner | app_runtime |
| 3 | `public.delete_date_for_tenant(uuid, date)` | app_data_owner | app_runtime |
| 4 | `public.update_tenant_name(uuid, text)` | app_data_owner | app_runtime |
| 5 | `public.resolve_login(text, text, boolean)` | **postgres** | app_runtime |
| 6 | `public.get_subscription(uuid)` | app_data_owner | app_runtime, app_webhook |
| 7 | `public.upsert_subscription_if_new_session(uuid, text, text, text, text, text, timestamptz)` | app_data_owner | app_runtime, app_webhook |
| 8 | `public.find_tenant_id_by_subscription(text)` | **postgres** | app_webhook |
| 9 | `public.update_subscription_status(uuid, text, text, timestamptz)` | app_data_owner | app_webhook |
| 10 | `public.get_tenant_usage_count(uuid, text, date)` | app_data_owner | app_runtime |
| 11 | `public.increment_tenant_usage_if_under_limit(uuid, text, date, integer)` | app_data_owner | app_runtime |
| 12 | `public.mark_stripe_event_processed(text, text)` | app_data_owner | app_webhook |
| 13 | `public.record_with_memo_for_tenant(uuid, date, text)`(★第14次追加) | app_data_owner | app_runtime |
| 14 | `public.search_records_for_tenant(uuid, text, text)`(★第14次追加) | app_data_owner | app_runtime |

`APP_DATA_OWNER_FUNCTION_SIGNATURES`(旧`TEN_APP_DATA_OWNER_FUNCTION_
SIGNATURES`)は#1-4・#6-7・#9-14の12件、`ALL_FUNCTION_SIGNATURES`(旧
`ALL_TWELVE_FUNCTION_SIGNATURES`)は全14件(`scripts/least_privilege_lib.py`)。

---

## 6. GRANT一覧・スキーマUSAGE・EXECUTE権限のリセットと検証（`aclexplode`構文を訂正）

### 6-0. `public`スキーマのUSAGE権限を明示GRANTする（新設）

**訂正の要点**: 前版は`public`スキーマの`PUBLIC`のCREATE権限が`false`
であることのみ確認しており(2章⑥)、**USAGE権限(オブジェクトを参照する
ために必要)を未確認・未固定のままにしていた**。PostgreSQLの既定では
`PUBLIC`が`public`スキーマへのUSAGEを持つことが多いが、これはインストール
やバージョンによって変わりうる既定値に依存する状態であり、最小権限の
設計として明確さを欠く。**必要なロールへ明示的にGRANTし、既定値に
依存しない**方針を採用する。

```sql
GRANT USAGE ON SCHEMA public TO app_data_owner;
GRANT USAGE ON SCHEMA public TO app_runtime;
GRANT USAGE ON SCHEMA public TO app_webhook;
```

ロールバック時は個別に`REVOKE USAGE ON SCHEMA public FROM <role>;`で
取り消す(17章)。

### 6-1. `app_data_owner`への直接テーブルGRANT(列単位、★第14次改訂で更新)

```sql
GRANT SELECT, INSERT, DELETE ON public.records TO app_data_owner;
GRANT UPDATE (memo) ON public.records TO app_data_owner;  -- ★第14次追加(下記参照)
GRANT USAGE ON public.records_id_seq TO app_data_owner;
GRANT SELECT (id), UPDATE (name) ON public.tenants TO app_data_owner;
GRANT SELECT, INSERT, UPDATE ON public.tenant_subscriptions TO app_data_owner;
GRANT SELECT, INSERT, UPDATE ON public.tenant_usage TO app_data_owner;
GRANT INSERT ON public.processed_stripe_events TO app_data_owner;
```

**★第14次追加の背景**: `record_with_memo_for_tenant`の
`ON CONFLICT DO UPDATE SET memo = ...`はUPDATE権限を必要とするが、
既存11関数はいずれも`records`へのUPDATE操作を行わなかったため、この
GRANTは第1〜13次改訂のどの回でも必要にならず見逃されていた。
`app_runtime`として実際に関数を呼び出す実機テストで発見・修正した
(詳細は冒頭の「★統合追記」参照)。`tenant_id`・`record_date`列は含めず、
`memo`列のみに限定している(列単位GRANTによる最小権限)。

### 6-2. 関数EXECUTE権限のリセットと検証（`aclexplode`のSQLを全面訂正）

> ⚠️**この章のコード例(段階1・段階2とも)も第13次改訂版(12関数)時点の
> ものです。第14次(14関数)の正しいGRANT/検証対象は`scripts/
> least_privilege_lib.py`の`RESET_AND_GRANT_STATEMENTS`・
> `EXPECTED_FUNCTION_GRANTS`(いずれも14件)を参照してください。**
> `record_with_memo_for_tenant`・`search_records_for_tenant`とも、
> 所有者`app_data_owner`・EXECUTE付与先`app_runtime`のみ(段階1の
> パターンでいう`load_dates_for_tenant`と同じ扱い)です。

**訂正の要点(3点)**: ①`aclexplode(proacl)`は複数行を返す集合関数であり、
WHERE句で直接呼び出す構文(前版)は誤り。`CROSS JOIN LATERAL`で展開する
必要がある。②`grantee`が`PUBLIC`(GRANT対象がPUBLIC全体)の場合、内部的な
OIDは`0`であり、実在するロールが存在しないため`0::regrole::text`は
**キャストエラーになる**。`CASE`で明示的に`'PUBLIC'`という文字列へ変換
する必要がある。③`proacl`列自体が`NULL`の場合(=このオブジェクトに
一度もGRANT/REVOKEが行われていない、PostgreSQLの既定状態)、
`aclexplode(NULL)`は0行を返すため、一見「権限が何も無い」ように見えるが、
**実際にはPostgreSQLの関数の既定ACLにより、所有者は全権限・PUBLICは
EXECUTEを暗黙に持つ**。この既定状態を安全側(=危険な状態)として明示的に
検出する必要がある。

**段階1: 既知ロールに対するリセット(前版から変更なし)**

```sql
-- 12関数それぞれについて、CREATE OR REPLACE FUNCTIONの直後に実行する
REVOKE ALL ON FUNCTION public.load_dates_for_tenant(uuid) FROM PUBLIC, app_runtime, app_webhook;
GRANT EXECUTE ON FUNCTION public.load_dates_for_tenant(uuid) TO app_runtime;

REVOKE ALL ON FUNCTION public.insert_date_for_tenant(uuid, date) FROM PUBLIC, app_runtime, app_webhook;
GRANT EXECUTE ON FUNCTION public.insert_date_for_tenant(uuid, date) TO app_runtime;

REVOKE ALL ON FUNCTION public.delete_date_for_tenant(uuid, date) FROM PUBLIC, app_runtime, app_webhook;
GRANT EXECUTE ON FUNCTION public.delete_date_for_tenant(uuid, date) TO app_runtime;

REVOKE ALL ON FUNCTION public.update_tenant_name(uuid, text) FROM PUBLIC, app_runtime, app_webhook;
GRANT EXECUTE ON FUNCTION public.update_tenant_name(uuid, text) TO app_runtime;

REVOKE ALL ON FUNCTION public.resolve_login(text, text, boolean) FROM PUBLIC, app_runtime, app_webhook;
GRANT EXECUTE ON FUNCTION public.resolve_login(text, text, boolean) TO app_runtime;

REVOKE ALL ON FUNCTION public.get_subscription(uuid) FROM PUBLIC, app_runtime, app_webhook;
GRANT EXECUTE ON FUNCTION public.get_subscription(uuid) TO app_runtime, app_webhook;

REVOKE ALL ON FUNCTION public.upsert_subscription_if_new_session(uuid, text, text, text, text, text, timestamptz)
  FROM PUBLIC, app_runtime, app_webhook;
GRANT EXECUTE ON FUNCTION public.upsert_subscription_if_new_session(uuid, text, text, text, text, text, timestamptz)
  TO app_runtime, app_webhook;

REVOKE ALL ON FUNCTION public.find_tenant_id_by_subscription(text) FROM PUBLIC, app_runtime, app_webhook;
GRANT EXECUTE ON FUNCTION public.find_tenant_id_by_subscription(text) TO app_webhook;

REVOKE ALL ON FUNCTION public.update_subscription_status(uuid, text, text, timestamptz)
  FROM PUBLIC, app_runtime, app_webhook;
GRANT EXECUTE ON FUNCTION public.update_subscription_status(uuid, text, text, timestamptz) TO app_webhook;

REVOKE ALL ON FUNCTION public.get_tenant_usage_count(uuid, text, date) FROM PUBLIC, app_runtime, app_webhook;
GRANT EXECUTE ON FUNCTION public.get_tenant_usage_count(uuid, text, date) TO app_runtime;

REVOKE ALL ON FUNCTION public.increment_tenant_usage_if_under_limit(uuid, text, date, integer)
  FROM PUBLIC, app_runtime, app_webhook;
GRANT EXECUTE ON FUNCTION public.increment_tenant_usage_if_under_limit(uuid, text, date, integer) TO app_runtime;

REVOKE ALL ON FUNCTION public.mark_stripe_event_processed(text, text) FROM PUBLIC, app_runtime, app_webhook;
GRANT EXECUTE ON FUNCTION public.mark_stripe_event_processed(text, text) TO app_webhook;
```

**段階2: `aclexplode`による実際のACL検証(構文・PUBLIC判定・NULL ACL・所有者を訂正)**

```python
# (signature, expected_owner, expected_grantees)のタプルで管理する
EXPECTED_FUNCTION_GRANTS = [
    ("public.load_dates_for_tenant(uuid)", "app_data_owner", {"app_runtime"}),
    ("public.insert_date_for_tenant(uuid, date)", "app_data_owner", {"app_runtime"}),
    ("public.delete_date_for_tenant(uuid, date)", "app_data_owner", {"app_runtime"}),
    ("public.update_tenant_name(uuid, text)", "app_data_owner", {"app_runtime"}),
    ("public.resolve_login(text, text, boolean)", "postgres", {"app_runtime"}),
    ("public.get_subscription(uuid)", "app_data_owner", {"app_runtime", "app_webhook"}),
    (
        "public.upsert_subscription_if_new_session(uuid, text, text, text, text, text, timestamptz)",
        "app_data_owner", {"app_runtime", "app_webhook"},
    ),
    ("public.find_tenant_id_by_subscription(text)", "postgres", {"app_webhook"}),
    ("public.update_subscription_status(uuid, text, text, timestamptz)", "app_data_owner", {"app_webhook"}),
    ("public.get_tenant_usage_count(uuid, text, date)", "app_data_owner", {"app_runtime"}),
    (
        "public.increment_tenant_usage_if_under_limit(uuid, text, date, integer)",
        "app_data_owner", {"app_runtime"},
    ),
    ("public.mark_stripe_event_processed(text, text)", "app_data_owner", {"app_webhook"}),
]


class UnexpectedGranteeError(Exception):
    """関数のEXECUTE権限・所有者が想定と一致しない場合。"""


def verify_function_grant(cur, qualified_signature, expected_owner, expected_grantees):
    # ①所有者の確認、②proaclがNULL(=既定ACL、危険な状態)でないことの確認
    cur.execute(
        "SELECT p.proacl IS NULL AS acl_is_null, p.proowner::regrole::text AS owner "
        "FROM pg_proc p WHERE p.oid = %s::regprocedure",
        (qualified_signature,),
    )
    row = cur.fetchone()
    if row is None:
        raise UnexpectedGranteeError(f"{qualified_signature}が見つかりません。")
    acl_is_null, actual_owner = row

    if actual_owner != expected_owner:
        raise UnexpectedGranteeError(
            f"{qualified_signature}の所有者が想定と一致しません: "
            f"実際={actual_owner} 期待={expected_owner}"
        )

    if acl_is_null:
        # proacl未設定は、REVOKE ALL FROM PUBLICが一度も適用されていない
        # ことを意味し、PostgreSQLの既定によりPUBLICへEXECUTEが暗黙付与
        # されている危険な状態である。aclexplode(NULL)は0行を返すため、
        # このチェックを飛ばすと「権限なし」と誤認してしまう
        raise UnexpectedGranteeError(
            f"{qualified_signature}にはACLが一度も設定されていません"
            "(proacl IS NULL)。PostgreSQLの既定によりPUBLICへEXECUTEが"
            "暗黙付与されている可能性があります。REVOKE ALL FROM PUBLICが"
            "未適用のため、安全のため停止します。"
        )

    # ③CROSS JOIN LATERALで正しく展開し、④PUBLIC(OID 0)を安全に判定し、
    # ⑤所有者自身のACLエントリ(存在する場合)を比較対象から除外する
    cur.execute(
        "SELECT CASE WHEN acl.grantee = 0 THEN 'PUBLIC' "
        "            ELSE acl.grantee::regrole::text END AS grantee "
        "FROM pg_proc p "
        "CROSS JOIN LATERAL aclexplode(p.proacl) "
        "  AS acl(grantor, grantee, privilege_type, is_grantable) "
        "WHERE p.oid = %s::regprocedure "
        "AND acl.privilege_type = 'EXECUTE' "
        "AND acl.grantee <> p.proowner",
        (qualified_signature,),
    )
    actual_grantees = {r[0] for r in cur.fetchall()}
    if actual_grantees != expected_grantees:
        raise UnexpectedGranteeError(
            f"{qualified_signature}のEXECUTE権限が想定と一致しません: "
            f"実際={actual_grantees} 期待={expected_grantees}。"
            "段階1のREVOKEが対象としていない未知のロールへ過去に付与された"
            "権限が残っている可能性があります。安全のためmigrationを停止します。"
        )


def verify_all_function_grants(cur):
    for signature, owner, grantees in EXPECTED_FUNCTION_GRANTS:
        verify_function_grant(cur, signature, owner, grantees)
```

**実PostgreSQLでの検証(15章に反映)**: PostgreSQL 16・18の両方で、次の
3ケースを確認する。

1. 正常ACL(段階1のとおりREVOKE→GRANTした直後): 検証が成功する
2. 未知ロールへのGRANT(テスト用に想定外のロールへ手動でEXECUTEを付与し、
   検証関数を呼ぶ): `UnexpectedGranteeError`が送出される
3. PUBLICへの再付与(`GRANT EXECUTE ON FUNCTION ... TO PUBLIC`を実行後に
   検証関数を呼ぶ): PUBLICが`'PUBLIC'`という文字列として正しく検出され、
   `UnexpectedGranteeError`が送出される(想定grantee集合に含まれないため)

---

## 7. RLSポリシー全文（変更なし）

```sql
ALTER TABLE public.records ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS records_tenant_isolation ON public.records;
CREATE POLICY records_tenant_isolation ON public.records
  TO app_data_owner
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenants_tenant_isolation ON public.tenants;
CREATE POLICY tenants_tenant_isolation ON public.tenants
  TO app_data_owner
  USING (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE public.tenant_subscriptions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_subscriptions_tenant_isolation ON public.tenant_subscriptions;
CREATE POLICY tenant_subscriptions_tenant_isolation ON public.tenant_subscriptions
  TO app_data_owner
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE public.tenant_usage ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_usage_tenant_isolation ON public.tenant_usage;
CREATE POLICY tenant_usage_tenant_isolation ON public.tenant_usage
  TO app_data_owner
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE public.tenant_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
-- processed_stripe_eventsはtenant非依存のためRLSは有効化しない
```

---

## 8〜10. Webhook矛盾解消・SECURITY DEFINER安全化・想定リスク（変更なし）

`tenant_subscriptions`の全操作を関数化しWebhookのGRANT矛盾を解消(8章)。
完全修飾名・`search_path=''`・`public`のCREATE権限確認済み(9章)。防げる
脅威(バグ・書き忘れ・設定ミス)と防げない脅威(DB資格情報漏えい・公開
関数の悪用・`resolve_login`によるemail/email_verified書き換え)を明確に
区別(10章)。

---

## 11. 悪用テスト一覧（ACL検証の3ケースを追加）

| # | シナリオ | 期待される結果 |
|---|---|---|
| 1〜11 | (前版から変更なし。世帯越境・横断検索・直接SQL拒否・SET ROLE拒否・並行resolve_login・processed_stripe_eventsのSELECT省略確認 等) | 前版と同一 |
| 12 | `resolve_login`に他ユーザーの`auth_subject`と、任意の`email`・`email_verified=true`を渡す | 成功し、そのユーザーのemail・email_verifiedが書き換わる(10章の残存リスクの実地確認) |
| 13 | 完全ロールバック(17章)を実行し3ロールを`DROP ROLE`できることを確認 | 成功する |
| 14 | ロールバック後、全7テーブルのRLS無効・12関数削除(または所有権復帰)を確認 | 成功する |
| 15 | **(新規)6-2章検証を、正常ACL・未知ロールへのGRANT・PUBLICへの再付与の3パターンで実行する** | **正常ACLは成功、他2パターンは`UnexpectedGranteeError`で停止する** |
| 16 | **(新規)既存の`app_runtime`ロールに対し、異なる環境変数値でmigrationを再実行する** | **`ALTER ROLE`により新しい値へ上書きされ、新しい値でのみ接続できることを確認する(古い値では接続できなくなることも確認)** |
| 17 | **(新規・第8次)16章訂正後の`main()`を、何も無い状態から初回実行する** | **`[OK]`まで到達する(前版は所有者不一致で必ず停止しており、この項目は前版では失敗していた)** |
| 18 | **(新規・第8次)同じ`main()`を、17がcommit済みの状態のまま2回目実行する** | **すべての`verify_or_*`系が既存一致で通過し、`[OK]`まで到達する** |
| 19 | **(新規・第8次)Tier 2のトランザクション内、⑤`DROP ROLE app_data_owner`の直後に人為的な失敗(例: ⑥の対象関数名を一時的に存在しないものへ差し替える等)を起こす** | **`ROLLBACK`され、Tier 2着手前の状態(3ロール・GRANT・RLS・関数所有権すべて)が維持される** |
| 20 | **(新規・第8次)Tier 3のトランザクション内、⑨の関数削除の途中で同様に人為的な失敗を起こす** | **`ROLLBACK`され、Tier 3着手前の状態が維持される(関数だけ削除済み、制約だけ撤去済みという中間状態にならない)** |
| 21 | **(新規・第9次)完全適用状態から`rollback_tier2_remove_roles.py`(Tier 2)を実行する** | **`[OK]`まで到達し、3ロール・GRANT・RLSが撤去される。12関数・制約・`schema_migration_log`は変化しない** |
| 22 | **(新規・第9次)完全適用状態から`rollback_tier3_full_restore.py`(Tier 3)を実行する** | **`[OK]`まで到達し、17-3章の完了条件表(テーブル7・シーケンス1・関数0・ロールなし・ポリシー0・RLS無効・制約なし)と一致する** |
| 23 | **(新規・第9次)Tier 2完了状態から`rollback_cleanup_after_tier2.py`を実行する** | **`[OK]`まで到達し、22と同じ完了条件表と一致する(残存していた12関数・制約・`schema_migration_log`が撤去される)** |
| 24 | **(新規・第9次)Tier 2完了状態のまま誤って`rollback_tier3_full_restore.py`を実行する** | **`_precondition_roles_exist`が`RollbackPreconditionError`を送出し、`[NG]`で即座に停止する(`DROP ROLE`等は一切実行されない)** |
| 25 | **(新規・第9次)完全適用状態のまま誤って`rollback_cleanup_after_tier2.py`を実行する** | **`_precondition_roles_absent`が`RollbackPreconditionError`を送出し、`[NG]`で即座に停止する(関数・制約・ログは一切削除されない)** |
| 26 | **(新規・第9次)Tier 3・17-4スクリプトの制約再検証ステップの直前に、`tenant_subscriptions_stripe_subscription_id_key`とは別に複合列のUNIQUE制約を人為的に追加してから実行する** | **`_reverify_and_drop_constraint`が不一致を検出して`RuntimeError`を送出し、`ROLLBACK`される(制約は撤去されない)** |
| 27 | **(新規・第9次)Tier 3実行後、`public.schema_migration_log`が存在しないこと、書き出し先のローカルJSONファイルに実行前の全行が含まれていることを確認する** | **テーブルは存在せず、JSONファイルには12-2章で記録された行(制約migrationの実行履歴)が保持されている** |
| 28〜29 | (第10次自己監査分、17-3章完了条件表の`stripe_subscription_id_unique_constraint_names`・欠落テーブル分岐に統合済み) | — |
| 30 | **(新規・第11次・実機PASS済み)完全適用状態で、`app_data_owner`が別データベースの何らかのオブジェクトに依存する状況を人為的に作ってからTier2を実行する** | **`app_data_owner`は`DROP ROLE`されずNOLOGIN化のみ行われ、カレントDBの権限は失われる。`result["degraded_roles"]`に対象ロールと依存先DB名が記録され、`[警告]`メッセージが出力される** |
| 31 | **(新規・第11次・実機PASS済み)想定7テーブルが存在しないデータベースへ16章のmigrationを実行する** | **`TargetDatabaseMismatchError`で即座に停止し、ロールを含む一切のDDLが実行されない(`missing_expected_tables`に不足分が列挙される)** |
| 32 | **(新規・第11次・実機PASS済み)#31と同じ状況を17章の3スクリプトそれぞれでも確認する** | **いずれも`verify_target_database_identity`が最初に呼ばれ、`TargetDatabaseMismatchError`で停止する** |
| 33 | **(新規・第12次・点1・実機PASS済み)#30と同じクロスDB依存状況でTier2を実行し、終了コードを確認する** | **終了コード=2(DEGRADED)。標準出力に`[OK]`は一切含まれず`[DEGRADED]`と表示される** |
| 34 | **(新規・第12次・点2・実機PASS済み)#30と同じクロスDB依存状況でTier3を実行し、関数・制約・`schema_migration_log`に触れていないことをDBへ直接問い合わせて確認する** | **終了コード=2(DEGRADED)。実行後も関数12件・制約・ログテーブルがすべて実行前のまま残っている(削除処理へ一切進んでいない)** |
| 35 | **(新規・第12次・点3・実機PASS済み)完全適用状態のTier3実行直前に、想定外の8番目のテーブルを人為的に作成する** | **`verify_round21_baseline_state`が`BaselineStateMismatchError`を送出し、終了コード=1、`ROLLBACK`によりロール・関数を含む全体が実行前の状態のまま維持される(想定外テーブルも削除されず残る)** |
| 36 | **(新規・第12次・点4・実機PASS済み)#35のテーブル集合不一致テストが、NOT NULL復帰確認(`stripe_subscription_id_is_nullable: 'YES'`)も含めて`verify_round21_baseline_state`で判定されることを確認する** | **完全適用状態からの正常なTier3実行後、実際に`information_schema.columns.is_nullable`が`'YES'`であることをDBへ直接問い合わせて確認する** |
| 37 | **(新規・第12次・点5・実機PASS済み)完全適用状態で`schema_migration_log`の全行を削除してからTier3を実行する** | **`_reverify_and_drop_constraint`が記録欠落を検知して`RuntimeError`を送出し、終了コード=1、`ROLLBACK`によりロール・関数件数が実行前後で不変** |
| 38 | **(新規・第12次・点6・実機PASS済み)`SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED`を設定せずにTier3を実行する** | **`_precondition_migration_log_manually_archived`が`RollbackPreconditionError`を送出し、終了コード=1で、関数削除等の処理に一切入らず事前に停止する(関数はまだ12件のまま)** |
| 39〜42 | **(新規・第12次・点7・実機PASS済み)`EXPECTED_TARGET_DBNAME`/`EXPECTED_TARGET_USER`/`ACTUAL_TARGET_ENVIRONMENT_ID`(不一致)/`STAGING_DDL_EXPLICITLY_ALLOWED`をそれぞれ個別に未設定・不一致にして16章のmigrationを実行する** | **4パターンすべてで`TargetDatabaseMismatchError`により終了コード=1、DDLは一切実行されない** |
| 43 | **(新規・第12次・点8・実機PASS済み)テストランナー(`run_all_checks.py`)自体に意図的な失敗を1件注入する** | **別プロセスでの実行結果が終了コード=1になることを確認する(メタテスト)** |
| 44 | **(新規・第13次・点C・実機PASS済み)完全適用状態で、`stripe_subscription_id_key`を一度DROPしDEFERRABLE化して同名・同一列で再ADDしてからTier3を実行する** | **`_reverify_and_drop_constraint`がconvalidated/condeferrable/condeferredの不一致を検出して`RuntimeError`を送出し、ROLLBACKされる(制約は撤去されない)** |
| 45 | **(新規・第13次・点D・実機PASS済み)`SCHEMA_MIGRATION_LOG_ARCHIVE_SHA256`または`SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNT`を、実際の値と異なる値へ差し替えてTier3を実行する** | **`_precondition_migration_log_manually_archived`が不一致を検出し`RollbackPreconditionError`で事前停止する(関数は12件のまま変化しない)** |
| 14b | **(新規・第13次・点A・E・実機PASS済み)クロスDB依存→Tier2 DEGRADED後、依存解消してから旧不具合どおりTier2を再実行する** | **`_precondition_roles_exist`が「全部存在」を要求するため`RollbackPreconditionError`で弾かれる(=前版の不具合の再現)。続けて`rollback_resume_to_full_restore.py`を実行すると`COMPLETE`(0)まで到達し、round-21基準と完全一致する** |
| 15b | **(新規・第13次・点A・E・実機PASS済み)クロスDB依存→Tier3 DEGRADED後、依存解消してから`rollback_resume_to_full_restore.py`を実行する** | **`COMPLETE`(0)まで到達し、round-21基準(テーブル7・シーケンス1・関数0・ロールなし・ポリシー0・RLS無効・制約なし・監査ログなし)と完全一致する** |

---

## 12. `stripe_subscription_id`のUNIQUE制約: 列単位の正確な判定・4状態表現の訂正・main()全文

### 12-1. 「4状態」の表現を訂正(3状態の自動処理＋1状態の安全停止)

**訂正の要点**: 前版は「4状態を個別に正しく処理する」と説明していたが、
実際のコードは「UNIQUE制約はあるがNOT NULL制約が無い」という状態を
**自動処理せず安全停止する**設計になっており、説明と食い違っていた。
説明を実装に合わせて訂正する。

> **自動的に適用するのは次の3状態のみである**: ①両方未適用(NOT NULL・
> UNIQUEの両方を追加する)、②NOT NULLのみ適用済み(UNIQUEだけ追加する)、
> ③両方適用済み(何もしない)。**「UNIQUE制約はあるがNOT NULLが無い」
> という状態は、通常のアプリ運用では起こり得ない組み合わせであり、
> 手動での事前作業の可能性を示唆するため、自動処理せず例外で安全停止
> する(この状態は4番目の「処理対象」ではなく、想定外の状態として扱う)。**

### 12-2. 同名制約の列単位の正確な検証(部分一致から`conkey`/`attnum`照合へ訂正)

**訂正の要点**: 前版は`"stripe_subscription_id" not in definition`という
文字列の部分一致で同名制約の妥当性を判定していたが、これでは
`UNIQUE (stripe_subscription_id, other_column)`のような**複合列
UNIQUE制約**が存在する場合も、文字列に列名が含まれているという理由だけで
「正しい」と誤判定してしまう。`pg_constraint.conkey`(制約が対象とする
列のattnum配列)と`pg_attribute.attnum`を突き合わせ、**対象列が
`stripe_subscription_id`1列だけであること**を正確に検証する。あわせて
`contype='u'`・`convalidated=true`(未検証状態でないこと)・
`condeferrable=false`(遅延可能でないこと、通常のUNIQUE制約と一致する
ことの確認)も検証する。

**[新設] 変更状態の構造化・永続化**: 12-4章で述べる「実行前状態・今回
実際に追加したもの・実行結果」を、秘密値を含まない構造化データとして
関数の戻り値にし、かつ`public.schema_migration_log`テーブル(初回のみ
`CREATE TABLE IF NOT EXISTS`)へ、**制約変更と同じトランザクション内で**
挿入する。同一トランザクションで挿入するため、制約変更が
成功・commitされた場合のみログ行も残り(原子性)、`RuntimeError`による
安全停止(制約は一切変更していない)の場合はログ行も残らない
— この場合はDB側の状態自体が変化していないため、記録すべき「変更」が
そもそも存在しない。

**[第9次訂正] このテーブルは恒久的な存在ではない**: `schema_migration_
log`は「第21回終了時点への完全復帰」(17章Tier 3、または17-4章)を実行
する際、全行をローカルの監査ファイルへ書き出したうえでテーブルごと
削除される(付随する`bigserial`のシーケンスも合わせて削除される)。
Tier 3実行後もこのテーブルが残ることを前提にした記載は本書のどこにも
無い。詳細は17章を参照。

```python
import json

from psycopg import sql

CONSTRAINT_NAME = "tenant_subscriptions_stripe_subscription_id_key"
MIGRATION_NAME = "stripe_subscription_id_unique_schema"


def _ensure_migration_log_table(cur):
    cur.execute(
        "CREATE TABLE IF NOT EXISTS public.schema_migration_log ("
        "  id bigserial PRIMARY KEY,"
        "  migration_name text NOT NULL,"
        "  executed_at timestamptz NOT NULL DEFAULT now(),"
        "  before_state jsonb NOT NULL,"
        "  applied_changes jsonb NOT NULL,"
        "  result text NOT NULL"
        ")"
    )


def _record_migration_log(cur, before_state, applied_changes, result):
    cur.execute(
        "INSERT INTO public.schema_migration_log "
        "(migration_name, before_state, applied_changes, result) "
        "VALUES (%s, %s::jsonb, %s::jsonb, %s)",
        (MIGRATION_NAME, json.dumps(before_state), json.dumps(applied_changes), result),
    )


def apply_stripe_subscription_id_constraint_if_needed(cur):
    """秘密値を一切含まない構造化された結果 (dict) を返す。
    キー: before_state, applied_changes, result
    """
    _ensure_migration_log_table(cur)

    cur.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tenant_subscriptions' "
        "AND column_name = 'stripe_subscription_id'"
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("stripe_subscription_id列が見つかりません。")
    is_nullable = row[0] == "YES"

    cur.execute(
        "SELECT attnum FROM pg_attribute "
        "WHERE attrelid = 'public.tenant_subscriptions'::regclass "
        "AND attname = 'stripe_subscription_id'"
    )
    target_attnum = cur.fetchone()[0]

    # 同名制約が既に存在するか、存在するなら列単位で正確に内容を確認する
    cur.execute(
        "SELECT contype, convalidated, condeferrable, condeferred, conkey "
        "FROM pg_constraint "
        "WHERE conrelid = 'public.tenant_subscriptions'::regclass "
        "AND conname = %s",
        (CONSTRAINT_NAME,),
    )
    named_row = cur.fetchone()
    has_named_unique = False
    if named_row is not None:
        contype, convalidated, condeferrable, condeferred, conkey = named_row
        is_single_column_on_target = (list(conkey) == [target_attnum])
        if (
            contype != "u"
            or not convalidated
            or condeferrable
            or condeferred
            or not is_single_column_on_target
        ):
            raise RuntimeError(
                f"制約名{CONSTRAINT_NAME}が既に想定と異なる定義で存在します: "
                f"contype={contype} convalidated={convalidated} "
                f"condeferrable={condeferrable} condeferred={condeferred} "
                f"conkey={list(conkey)}(期待=[{target_attnum}])。"
                "安全のため停止します。"
            )
        has_named_unique = True

    before_state = {
        "is_nullable": is_nullable,
        "has_named_unique_constraint": has_named_unique,
    }

    # 別名で同等のUNIQUE制約(stripe_subscription_id列単独)が既に存在
    # しないか確認する(同じくconkeyで正確に判定、重複作成の防止)
    cur.execute(
        "SELECT conname, conkey FROM pg_constraint "
        "WHERE conrelid = 'public.tenant_subscriptions'::regclass "
        "AND contype = 'u'"
    )
    other_unique = [
        conname for conname, conkey in cur.fetchall()
        if conname != CONSTRAINT_NAME and list(conkey) == [target_attnum]
    ]
    if other_unique:
        raise RuntimeError(
            f"stripe_subscription_idに別名の同等UNIQUE制約が既に存在します: "
            f"{other_unique}。想定外の状態のため安全に停止します。"
        )

    if not is_nullable and has_named_unique:
        applied_changes = {"added_not_null": False, "added_unique": False}
        result = {"before_state": before_state, "applied_changes": applied_changes, "result": "skipped_already_applied"}
        _record_migration_log(cur, before_state, applied_changes, result["result"])
        print(f"[SKIP] NOT NULL・UNIQUE制約は既に適用済みです。 detail={result}")
        return result

    if is_nullable and has_named_unique:
        # 「UNIQUE制約はあるがNOT NULLが無い」状態は自動処理の対象外
        # (12-1章参照)。手動での事前作業の可能性があるため停止する。
        # この時点では一切のDDLを実行していないため、schema_migration_log
        # への記録は行わない(記録すべき「変更」が存在しない)。この状態
        # 自体は12-2章の確認SQL(conkey/attnum照合・is_nullable)でいつでも
        # 再現・確認できる。
        raise RuntimeError(
            "UNIQUE制約は存在するがNOT NULL制約が無い、想定外の状態です。"
            "自動処理の対象外のため、安全のため停止します。"
        )

    cur.execute(
        "SELECT COUNT(*) FILTER (WHERE stripe_subscription_id IS NULL) "
        "FROM public.tenant_subscriptions"
    )
    null_count = cur.fetchone()[0]
    if null_count > 0:
        raise RuntimeError(
            f"stripe_subscription_idがNULLの行が{null_count}件あります。"
            "NOT NULL制約を適用できません。"
        )

    applied_changes = {"added_not_null": False, "added_unique": False}

    if is_nullable:
        cur.execute(
            "ALTER TABLE public.tenant_subscriptions "
            "ALTER COLUMN stripe_subscription_id SET NOT NULL"
        )
        applied_changes["added_not_null"] = True

    if not has_named_unique:
        cur.execute(
            sql.SQL(
                "ALTER TABLE public.tenant_subscriptions "
                "ADD CONSTRAINT {name} UNIQUE (stripe_subscription_id)"
            ).format(name=sql.Identifier(CONSTRAINT_NAME))
        )
        applied_changes["added_unique"] = True

    result = {"before_state": before_state, "applied_changes": applied_changes, "result": "applied"}
    _record_migration_log(cur, before_state, applied_changes, result["result"])
    return result
```

### 12-3. 専用migrationスクリプトの`main()`全文（新設）

```python
"""scripts/migrate_to_stripe_subscription_id_unique_schema.py

tenant_subscriptions.stripe_subscription_idへNOT NULL・UNIQUE制約を
追加する専用migration。ロール・関数・RLSには一切触れない。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity  # noqa: E402


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)  # 0-1章、DDLより前に実行
            result = apply_stripe_subscription_id_constraint_if_needed(cur)
        conn.commit()
        # resultは秘密値を含まない構造化データ(before_state・applied_changes・
        # result)。schema_migration_logテーブルへも同一トランザクションで
        # 既に記録済み(commit済み)であり、この標準出力はあくまで実行時の
        # 即時確認用の補助情報である。
        print(f"[OK] stripe_subscription_idのNOT NULL・UNIQUE制約の適用が完了しました。 detail={result}")
        return 0
    except (psycopg.Error, RuntimeError, TargetDatabaseMismatchError) as e:
        conn.rollback()
        # 秘密値・SQL全文はログへ出さない。例外の型名と、あらかじめ非機密の
        # 情報(テーブル名・列名・件数・真偽値)だけで組み立てたメッセージのみ
        # を出力する(apply_stripe_subscription_id_constraint_if_needed()の
        # RuntimeErrorメッセージ自体が既にこの方針に従っている)。
        print(f"[NG] 適用中にエラーが発生しました。変更はロールバックされました: {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

**再実行テスト**: このスクリプトを2回連続で実行し、1回目は実際に
`ALTER TABLE`が2文実行されて成功、2回目は`[SKIP]`で即座に成功終了する
ことを確認する(15章のテスト項目へ追加)。

### 12-4. 正確な切り戻しSQL（新設）

```sql
BEGIN;

ALTER TABLE public.tenant_subscriptions
  DROP CONSTRAINT IF EXISTS tenant_subscriptions_stripe_subscription_id_key;

ALTER TABLE public.tenant_subscriptions
  ALTER COLUMN stripe_subscription_id DROP NOT NULL;

COMMIT;
```

`DROP CONSTRAINT IF EXISTS`は制約が存在しなくてもエラーにならない。
`DROP NOT NULL`は、その列がそもそもNOT NULLでなくてもエラーにならない
(PostgreSQLの仕様上、両方とも冪等に安全実行できる)。

**状態記録の注意(訂正: 永続化された記録を根拠にする)**: この切り戻しは
「本Taskが追加したNOT NULL・UNIQUEを除去する」ものであり、2章⑧⑩⑪の
確認時点で**両方とも未適用だった(stagingの現状)**ことを前提にしている。
将来、他の目的で別途これらの制約が追加されていた場合、この切り戻しSQLは
意図せずそれも削除してしまう。前版は「ログへ記録し照合する」としていたが
記録の実装が無かった。12-2章の更新により、実際に変更した内容
(`before_state`・`applied_changes`)は`public.schema_migration_log`
テーブルへ、制約変更と同一トランザクションで永続化されるようになった。
切り戻しの判断は次の手順で行う。

1. `SELECT * FROM public.schema_migration_log WHERE migration_name =
   'stripe_subscription_id_unique_schema' ORDER BY executed_at DESC
   LIMIT 1;`で直近の適用記録を確認し、`applied_changes`が
   `{"added_not_null": true, "added_unique": true}`(本Taskが両方とも
   追加した)であることを確認する
2. **ただし、このログはあくまで「migration実行時点で何を変更したか」の
   監査記録であり、それだけを切り戻しの唯一の根拠にはしない。** ログ
   確認後、必ず12-2章と同じ確認SQL(`conkey`/`attnum`照合・
   `information_schema.columns.is_nullable`)で**切り戻し実行直前の
   実際のライブ状態**を再確認し、ログの記録内容と食い違いが無いことを
   確かめてから実行する(ログ記録後に、本Task以外の経路で制約が変更
   された可能性を排除するため)
3. `DROP CONSTRAINT IF EXISTS`・`DROP NOT NULL`は、対象が既に存在しない
   場合でもエラーにならない(冪等)ため、上記の事前確認が一致していれば
   安全に実行できる

---

## 13. パスワードの安全な生成・設定手順（前版から変更なし）

未確定のまま、実装フェーズ着手前に3案(13-3章)のいずれかを確定させる。
確定するまで14章手順9以降には進まない。

---

## 14. 実施順序（`public`スキーマUSAGE・パスワード上書き方式・別接続試験を反映）

1. **設計承認**(本書、現在地)
2. **featureブランチ作成**
3. **`stripe_subscription_id`のNOT NULL＋UNIQUE制約用migrationスクリプト
   (12章)を実装する(stagingへは実行しない)**
4. **アプリ側の実装**: `streamlit/db.py`を5章の関数呼び出しへ置き換え、
   `auth.py`を`resolve_login`1回へ統合する
5. **PostgreSQLを使う専用の統合テストを作成**(15章)。CIジョブごとの
   専用データベースで、PostgreSQL 16・18の両方を検証する
6. **既存202件のCI＋新規テストを全てPASS**させる
7. **6-1章のGRANT絞り込みを実測で確定させる**
8. **Railwayのバックアップが実際に取得済みで、復元可能であることを
   確認する**(18章)
9. **【ブロッカー】13章のパスワード設定方法を確定させ、岩瀬様の承認を
   得る**
10. **staging上で`stripe_subscription_id`のNOT NULL＋UNIQUE制約を適用
    する**(12章、ロック時間・失敗の可能性を考慮し利用が少ない時間帯に)
11. **staging用のロール・関数・ポリシーを準備**: `postgres`ロールで
    4章のロール(既存LOGINロールへのパスワード上書きを含む)・6章の
    GRANT(`public`スキーマUSAGE含む)・5章の関数を、
    `scripts/migrate_to_least_privilege_schema.py`経由で作成する。
    **16章の訂正後の順序**(関数作成→所有権移管→EXECUTE権限リセット→
    ACL検証→RLS)のとおりに実行され、最後まで到達して`[OK]`で終わる
    ことを確認する
12. **【新設・訂正: 失敗時は手順13以降へ進まない】別接続試験**:
    `app_runtime`・`app_webhook`の新しい資格情報(環境変数の値)を使い、
    `web`・`stripe-webhook`本体とは別の一時的な接続で、実際にログイン
    でき、意図した関数が呼び出せることを確認する。**これが、4-3章で
    「カタログからは確認できない」としたパスワードの正しさを、実地で
    確認する唯一の手段である。**

    - **1件でも接続・呼び出しに失敗したら、手順13(メンテナンス開始)
      以降へは進まない**
    - この時点では`web`・`stripe-webhook`の`DATABASE_URL`はまだ
      変更していない(既存の`postgres`接続のまま稼働中)ため、**別接続
      試験の失敗そのものはサービス障害を引き起こさない**。既存の
      `postgres`接続で稼働中のサービスには、この段階で一切変更を
      加えない
    - 試験結果は成否・エラーの型名などの非機密情報のみを記録し、
      **秘密値(パスワード)や接続文字列は一切ログへ出さない**
      (実出力例のcatや貼り付けを含む。[[shokki-arai-support-status]]の
      Railway秘密値インシデントと同じ経路のため特に注意する)
    - 失敗時は、原因(4章のロール属性・6章のGRANT・パスワード設定など)を
      特定して修正したうえで、11章のmigrationを再実行し、別接続試験を
      再試行する。原因不明・修正見込みが立たない場合は、17章Tier 2で
      新設ロール・関数・RLSを一旦撤去し、設計を見直す
    - すべての別接続試験が成功したことを確認したうえで、**その場合に
      限り**手順13(メンテナンス開始)・手順14(接続切替)へ進む
13. **メンテナンス開始**
14. **staging接続ロール切替**: `web`・`stripe-webhook`の`DATABASE_URL`
    をRailway変数参照で切り替え、再デプロイ
15. **HTTP正常性確認・ログ確認**
16. **世帯間越境・認証・Webhook・既存機能の非破壊確認**
17. **問題発生時は緊急切り戻し(17章Tier 1)**
18. **安定確認後にメンテナンス終了**

---

## 15. テスト手順（ACL検証3ケース・再実行テスト・3段階ロールバック実測を追加）

### 15-1. テスト隔離方式(変更なし)

`CREATE DATABASE test_least_privilege`によるCIジョブごとの専用データ
ベースを使う(関数が`public.*`へ固定で完全修飾されているため)。

### 15-2. PostgreSQL 16・18マトリクス(変更なし)

### 15-3. テスト項目(追加分)

前版の項目に加え、次を追加する。

- **(新規)6-2章のACL検証を、正常ACL・未知ロールへのGRANT・PUBLICへの
  再付与の3パターンで実行し、後者2つが`UnexpectedGranteeError`で
  停止することを確認する**(11章#15)
- **(新規)`app_runtime`が既に存在する状態で異なるパスワードでmigration
  を再実行し、`ALTER ROLE`による上書きが行われ、新しい資格情報でのみ
  接続できることを確認する**(11章#16)
- **(新規)12-3章の専用スクリプトを2回連続実行し、1回目は適用・2回目は
  `[SKIP]`で終わることを確認する**
- **(新規)17章の3段階(Tier 1緊急切り戻し・Tier 2ロール撤去・Tier 3
  全面復帰)それぞれを実PostgreSQL上で実行し、Tier 3実行後は1章①〜⑤と
  同じ結果(所有者は全てpostgres、RLS全false、ポリシー0件、カスタム
  ロールなし、カスタム関数0件)が再現されることを1章の確認SQLで照合する**
- **(新規・第8次)16章訂正後の`main()`の初回実行・2回目実行(再適用)を
  PostgreSQL 16・18の両方で行い、両方とも`[OK]`で終わることを確認する
  (11章#17・#18)**
- **(新規・第8次)Tier 2・Tier 3のトランザクション内で人為的に失敗を
  発生させ、`ROLLBACK`により適用前の状態が維持されることをPostgreSQL
  16・18の両方で実測する(11章#19・#20)**
- **(新規・第8次)12-3章の専用スクリプトの実行後、
  `public.schema_migration_log`に`before_state`・`applied_changes`・
  `result`が正しく記録されていることをSELECTで確認する。あわせて、
  12-1章の「UNIQUE制約はあるがNOT NULLが無い」安全停止パスでは
  ログ行が増えないことも確認する**
- **(新規・第9次)完全適用状態から`rollback_tier2_remove_roles.py`
  (Tier 2)を実行し、`[OK]`まで到達すること・12関数と制約と
  `schema_migration_log`が変化しないことを確認する(11章#21)**
- **(新規・第9次)完全適用状態から`rollback_tier3_full_restore.py`
  (Tier 3)を実行し、17-3章の完了条件表(テーブル7・シーケンス1・関数0・
  ロールなし・ポリシー0・RLS無効・制約なし)と一致することを確認する
  (11章#22)**
- **(新規・第9次)Tier 2完了状態から`rollback_cleanup_after_tier2.py`
  を実行し、同じ完了条件表と一致することを確認する(11章#23)**
- **(新規・第9次)開始状態の前提を誤ったスクリプト実行(Tier 2完了状態で
  Tier 3、完全適用状態で残存物撤去)が、それぞれ`RollbackPreconditionError`
  で即座に停止し、一切のDDLが実行されないことを確認する(11章#24・#25)**
- **(新規・第9次)制約撤去直前に想定外のUNIQUE制約が存在する状態を作り、
  `_reverify_and_drop_constraint`が検出して安全停止することを確認する
  (11章#26)**
- **(新規・第9次)Tier 3・残存物撤去の実行後、`schema_migration_log`
  テーブルが存在しないこと、書き出し先ローカルJSONファイルに実行前の
  全行が保持されていることを確認する(11章#27)**
- **(新規・第13次・点A・E)クロスDB依存でTier2/Tier3がDEGRADEDに終わった
  後、依存を解消して`rollback_resume_to_full_restore.py`を実行すると
  `COMPLETE`(0)まで到達し、round-21基準と完全一致することを確認する。
  あわせて、DEGRADED後に旧`_precondition_roles_exist`(全部存在が前提)
  でTier2を再実行すると弾かれること(=前版の不具合の再現)も確認する
  (11章#14b・#15b)**
- **(新規・第13次・点B)`EXPECTED_RAILWAY_PROJECT_ID`/`RAILWAY_PROJECT_ID`・
  `EXPECTED_RAILWAY_ENVIRONMENT_ID`/`RAILWAY_ENVIRONMENT_ID`の
  それぞれについて、未設定・未注入(`railway run`未経由を模擬)・
  不一致の各パターンで`TargetDatabaseMismatchError`により停止すること
  を確認する(11章#16〜#20群)**
- **(新規・第13次・点C)検証状態・遅延可能属性が異なる同名同一列の
  UNIQUE制約に対し、`_reverify_and_drop_constraint`が撤去せず安全停止
  することを確認する(11章#44)**
- **(新規・第13次・点D)`export_and_hash_migration_log.py`が出力する
  行数・SHA-256が、実際に改ざん・誤記された場合に
  `_precondition_migration_log_manually_archived`が検知して停止する
  ことを確認する。正しい値(同ツールの実出力)を使った場合は成功する
  ことも確認する(11章#45)**
- 以上をすべてPostgreSQL 16・18の両方で実行する(第13次改訂版時点で
  計45項目、全項目PASS)

---

## 16. migrationスクリプトの冪等性とトランザクション制御（関数所有権の検証順序を訂正）

> ⚠️**この章の`main()`コード例は第13次改訂版(12関数)時点のものです。
> 第14次(14関数)では、`grant_table_privileges()`より前に
> `ensure_records_memo_column()`(records.memo列の用意。
> `grant_table_privileges()`自体が`memo`列を参照するGRANT文を含むため)を
> 追加で呼ぶ必要があります。実際の実行順序は
> `scripts/migrate_to_least_privilege_schema.py`の`main()`を正本として
> 参照してください(冒頭の「★統合追記」にも記載)。**

**[Critical・訂正の要点]**: 前版の`main()`は
`reset_and_grant_execute_permissions → verify_all_function_grants →
reassign_function_owners`の順で呼んでいた。ところが6-2章の
`EXPECTED_FUNCTION_GRANTS`が定める期待所有者は、10関数について
`app_data_owner`である。`create_or_replace_functions()`
(`CREATE OR REPLACE FUNCTION`)直後の実際の所有者は、それを実行した
ロール(このmigrationを実行する`postgres`)のままであり、
`reassign_function_owners()`を呼ぶまでは`app_data_owner`に変わらない。
そのため前版の順序では、`verify_all_function_grants()`が1関数目の時点で
必ず「所有者が想定と一致しません: 実際=postgres 期待=app_data_owner」で
例外を送出し、後続の`reassign_function_owners()`に一度も到達できず、
migrationは**安全に停止するが、一度も完了しない**。

**訂正後の順序**: 所有権移管を、GRANTリセット・ACL検証より前に行う。

```python
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)  # 0-1章、DDLより前に実行
            verify_or_create_nologin_role(cur, "app_data_owner")

            app_runtime_password = _read_required_password("LEAST_PRIVILEGE_APP_RUNTIME_PASSWORD")
            app_webhook_password = _read_required_password("LEAST_PRIVILEGE_APP_WEBHOOK_PASSWORD")
            verify_or_set_login_role_password(cur, "app_runtime", app_runtime_password)
            verify_or_set_login_role_password(cur, "app_webhook", app_webhook_password)

            grant_schema_usage(cur)  # 6-0章
            grant_table_privileges(cur)  # 6-1章
            create_or_replace_functions(cur)  # 5章
            reassign_function_owners(cur)  # 訂正: GRANTリセット・ACL検証より前に行う
            reset_and_grant_execute_permissions(cur)  # 6-2章段階1
            verify_all_function_grants(cur)  # 6-2章段階2、この時点で所有者は既にapp_data_owner/postgres想定どおり
            enable_rls_and_policies(cur)  # 7章
        conn.commit()
        print("[OK] 最小権限化スキーマの適用が完了しました。")
        return 0
    except (psycopg.Error, RoleAttributeMismatchError, UnexpectedGranteeError,
            MissingPasswordError, TargetDatabaseMismatchError) as e:
        conn.rollback()
        print(f"[NG] 適用中にエラーが発生しました。変更はロールバックされました: {type(e).__name__}")
        return 1
    finally:
        conn.close()
```

**実PostgreSQLでの検証(15章に反映)**: PostgreSQL 16・18の両方で、この
訂正後の`main()`を使い、①初回適用(ロール・関数・GRANT・RLSが何も無い
状態から実行し、最後まで到達して`[OK]`で終わることを確認)、②2回目の
再適用(全てが既に適用済みの状態から再実行し、`verify_or_*`系がすべて
既存一致で通過し、同じく`[OK]`で終わることを確認)の両方を行う。

---

## 17. ロールバック手順（監査ログの扱いを分離、開始状態を明示した3スクリプト構成へ訂正）

> ⚠️**この章のロールバックスクリプトのコード例も第13次改訂版(12関数)
> 時点のものです。第14次(14関数)では、`record_with_memo_for_tenant`・
> `search_records_for_tenant`の所有権復帰・EXECUTE取消も対象に含める
> 必要があり、`scripts/rollback_helpers.py`の
> `APP_DATA_OWNER_FUNCTION_SIGNATURES`・`ALL_FUNCTION_SIGNATURES`・
> `EXECUTE_REVOKE_TARGETS`(いずれも更新済み)には既に反映されています。
> ロールバック作業は必ず実際のスクリプトファイルを正本として使用し、
> この章のコードを直接実行しないでください。**

**訂正の要点(第9次)**: 第8次改訂版は、12-2章で新設した
`public.schema_migration_log`(および付随する`bigserial`のシーケンス)を
Tier 3実行後も残す設計になっており、「1章①〜⑤と完全一致」「第21回
終了時点への全面復帰」という定義そのものと矛盾していた
(テーブル7件・シーケンス1件のはずが8件・2件になる)。また、Tier 3の
SQLが「①〜⑧はTier 2と同一」と省略されており単体で実行できず、
Tier 2実行後にTier 3を実行すると3ロールが既に無いため`DROP ROLE`等が
失敗する構造だった。監査ログの記録内容も固定値であり、`IF EXISTS`に
より実際には何も削除されなかった場合でも「削除した」という記録になり
うる不正確さがあった。

これらを次の方針で解消する。

1. **監査ログは残さない**: Tier 3(および後述する残存物撤去手順)は、
   `schema_migration_log`の全行をDB外のローカル監査ファイルへ書き出して
   から、テーブルごと削除する(付随シーケンスも`DROP TABLE`で自動的に
   削除される)。これにより「完全復帰」を文字どおり達成する
2. **開始状態を明示した独立スクリプト**: Tier 2・Tier 3は、いずれも
   「完全適用状態(3ロール・GRANT・RLS・12関数・制約・監査ログすべて
   存在)」を開始状態とする、互いに独立した(連続実行を前提としない)
   スクリプトとする。加えて、Tier 2実行後(3ロールが既に無い状態)から
   完全復帰させるための専用スクリプト(17-4章)を新設し、開始状態が
   食い違うスクリプトを誤って連続実行した場合は、事前条件チェックで
   安全に停止する
3. **実測に基づく記録**: 固定値ではなく、実行前後のカタログ状態を
   実際にクエリして記録する
4. **削除直前の再検証**: 制約の撤去前に、12-2章と同じ`conkey`/`attnum`
   照合を同一トランザクション内で再実行し、不一致なら撤去せず安全停止
   する

### 17-0. 共通ヘルパー(第12次全面訂正、`scripts/rollback_helpers.py`)

**訂正の要点(第12次)**: 岩瀬様の実物監査により、異常時(クロスDB依存・
記録欠落・最終状態不一致)でも処理を続行し成功扱いにしてしまう系統的な
問題が判明した。次の訂正を行った。

- `_remove_roles_and_rls`の戻り値へ`status`(`"complete"`/`"degraded"`)
  を追加し、呼び出し元が`[OK]`と`[DEGRADED]`を確実に区別できるようにした
- `verify_round21_baseline_state()`を新設し、第21回終了時点との厳密な
  一致(テーブル集合・シーケンス集合の完全一致、NOT NULL復帰確認を含む)
  をassertする。不一致なら`BaselineStateMismatchError`を送出し、呼び
  出し元がROLLBACKする
- `_reverify_and_drop_constraint`を全面訂正し、`schema_migration_log`の
  履歴(このTaskが実際にNOT NULL・UNIQUEを追加したことがあるか)と
  ライブ状態を突き合わせたうえでのみ撤去するようにした。記録欠落・
  矛盾時は一切変更せず安全停止する
- `_export_and_drop_migration_log`を`_drop_migration_log_table`へ改名し、
  ファイル書き出しを安全性の根拠から外した(補助情報・ベストエフォート
  に格下げ)。テーブル削除の前提として、人間が事前に手動でエクスポート・
  保存・ハッシュ確認したことを示す`_precondition_migration_log_
  manually_archived`を新設し、Tier 3・17-4章の冒頭で必須の事前条件と
  した

```python
"""設計書17-0章(第13次改訂版)をそのまま実装。"""
import hashlib
import json
import os
from pathlib import Path

from psycopg import sql

CONSTRAINT_NAME = "tenant_subscriptions_stripe_subscription_id_key"
MIGRATION_NAME = "stripe_subscription_id_unique_schema"
ROLE_NAMES = ("app_data_owner", "app_runtime", "app_webhook")

EXPECTED_TABLE_NAMES = {
    "records", "tenants", "tenant_memberships", "users",
    "tenant_subscriptions", "tenant_usage", "processed_stripe_events",
}
EXPECTED_SEQUENCE_NAMES = {"records_id_seq"}

TEN_APP_DATA_OWNER_FUNCTION_SIGNATURES = [
    "load_dates_for_tenant(uuid)",
    "insert_date_for_tenant(uuid, date)",
    "delete_date_for_tenant(uuid, date)",
    "update_tenant_name(uuid, text)",
    "get_subscription(uuid)",
    "upsert_subscription_if_new_session(uuid, text, text, text, text, text, timestamptz)",
    "update_subscription_status(uuid, text, text, timestamptz)",
    "get_tenant_usage_count(uuid, text, date)",
    "increment_tenant_usage_if_under_limit(uuid, text, date, integer)",
    "mark_stripe_event_processed(text, text)",
]
ALL_TWELVE_FUNCTION_SIGNATURES = TEN_APP_DATA_OWNER_FUNCTION_SIGNATURES + [
    "resolve_login(text, text, boolean)",
    "find_tenant_id_by_subscription(text)",
]
EXECUTE_REVOKE_TARGETS = [
    ("load_dates_for_tenant(uuid)", ["app_runtime"]),
    ("insert_date_for_tenant(uuid, date)", ["app_runtime"]),
    ("delete_date_for_tenant(uuid, date)", ["app_runtime"]),
    ("update_tenant_name(uuid, text)", ["app_runtime"]),
    ("resolve_login(text, text, boolean)", ["app_runtime"]),
    ("get_subscription(uuid)", ["app_runtime", "app_webhook"]),
    ("upsert_subscription_if_new_session(uuid, text, text, text, text, text, timestamptz)",
     ["app_runtime", "app_webhook"]),
    ("find_tenant_id_by_subscription(text)", ["app_webhook"]),
    ("update_subscription_status(uuid, text, text, timestamptz)", ["app_webhook"]),
    ("get_tenant_usage_count(uuid, text, date)", ["app_runtime"]),
    ("increment_tenant_usage_if_under_limit(uuid, text, date, integer)", ["app_runtime"]),
    ("mark_stripe_event_processed(text, text)", ["app_webhook"]),
]


class RollbackPreconditionError(Exception):
    pass


class BaselineStateMismatchError(Exception):
    """[第12次新設] 第21回終了時点との厳密な不一致を表す。"""


def _existing_role_names(cur):
    cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(ROLE_NAMES),))
    return {r[0] for r in cur.fetchall()}


def _precondition_roles_exist(cur):
    missing = set(ROLE_NAMES) - _existing_role_names(cur)
    if missing:
        raise RollbackPreconditionError(
            f"このスクリプトは完全適用状態(3ロールすべて存在)から実行する"
            f"前提です。存在しないロール: {missing}。既にTier 2が実行済み"
            "であれば、17-4章の「復旧(resume)」(rollback_resume_to_full_"
            "restore.py)を使ってください。"
        )


def _precondition_ready_to_resume(cur):
    """[第13次新設・点A対応] 「Tier 2完了状態(3ロールとも不在)」または
    「DEGRADED状態(一部ロールが安全に縮退済み: NOLOGIN化・カレントDB
    権限撤去済みだが、クロスDB依存で未DROPのまま残っている)」の
    いずれかから実行できる前提。前版の`_precondition_roles_absent`は
    「全部不在」しか受理しなかったため、DEGRADED後に正規の復旧経路が
    途切れる不具合があった。存在するロールが危険な状態(LOGIN可能な
    まま、またはカレントDBの権限が残ったまま)であれば拒否する。
    """
    existing = _existing_role_names(cur)
    if not existing:
        return
    for role_name in existing:
        cur.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (role_name,))
        can_login = cur.fetchone()[0]
        if can_login:
            raise RollbackPreconditionError(
                f"ロール{role_name}が存在し、かつLOGIN可能なままです。安全に"
                "縮退済みの状態ではないため実行できません。完全適用状態から"
                "17-2章のTier 2または17-3章のTier 3を使うか、危険な状態を"
                "先に是正してください。"
            )
    if "app_data_owner" in existing:
        cur.execute(
            "SELECT has_table_privilege('app_data_owner', 'public.records', 'SELECT')"
        )
        if cur.fetchone()[0]:
            raise RollbackPreconditionError(
                "app_data_ownerが存在し、カレントDBのrecordsへの権限がまだ"
                "残っています。安全に縮退済みの状態ではないため実行できません。"
            )


def _finish_degraded_role_removal(cur):
    """[第13次新設・点A対応] 前回の実行がDEGRADEDで終わり、一部ロールが
    「安全に縮退した状態」で残っている場合に、他データベースへの依存が
    解消されたかを再確認し、解消されていればDROP ROLEする。まだ解消
    されていないロールがあっても例外は送出せず、戻り値の
    `still_degraded`で呼び出し元へ伝える(呼び出し元がDEGRADEDとして
    扱い、それまでに解消できた分の進捗はcommitできるようにするため)。
    """
    remaining = _existing_role_names(cur)
    if not remaining:
        return {"newly_dropped": [], "still_degraded": {}}

    cross_db_problems = _check_cross_database_role_dependencies(cur, remaining)
    newly_dropped = []
    still_degraded = {}
    for role_name in sorted(remaining):
        if role_name in cross_db_problems:
            still_degraded[role_name] = cross_db_problems[role_name]
            continue
        cur.execute(f"DROP ROLE {role_name}")
        newly_dropped.append(role_name)

    return {"newly_dropped": newly_dropped, "still_degraded": still_degraded}


def _canonical_migration_log_export(cur):
    """[第13次新設・点D対応] schema_migration_logの全行を、決定的な
    (キー順・区切り記号が固定の)JSON文字列へ正規化し、SHA-256を計算する。
    scripts/export_and_hash_migration_log.py(削除は一切行わない事前
    エクスポート専用ツール)と`_precondition_migration_log_manually_
    archived`(削除直前の一致確認)の両方が、この関数を通じて同一の
    正規化ロジックを共有することで、「行数だけ・真偽値だけ」ではない
    内容そのものの一致確認を可能にする。
    """
    cur.execute(
        "SELECT id, migration_name, executed_at, before_state, applied_changes, result "
        "FROM public.schema_migration_log ORDER BY id"
    )
    columns = ["id", "migration_name", "executed_at", "before_state", "applied_changes", "result"]
    rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    canonical_text = json.dumps(
        rows, default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return rows, canonical_text, digest, len(rows)


def _precondition_migration_log_manually_archived(cur):
    """[第13次訂正・点D対応] schema_migration_logの自動ファイル書き出し
    は、実行環境の永続性・DBトランザクションとの整合性を保証できない
    ため、Tier 3・17-4章(resume)の安全性の根拠として使わない。単なる
    真偽値(SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED=true)だけでは保存先・
    行数・内容の一致を検証できないという指摘を受け、事前に
    scripts/export_and_hash_migration_log.pyで取得した行数・SHA-256を
    環境変数として要求し、現在のライブ状態と実際に一致するかを検証する。
    """
    cur.execute("SELECT to_regclass('public.schema_migration_log') IS NOT NULL")
    log_exists = cur.fetchone()[0]
    if not log_exists:
        return

    confirmed = os.environ.get("SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED", "").strip().lower()
    if confirmed != "true":
        raise RollbackPreconditionError(
            "public.schema_migration_logをこのTierの完了時に削除しますが、"
            "その前に人間による手動エクスポート・保存・ハッシュ確認が必要です"
            "(自動書き出しは安全性の根拠にしません)。事前に"
            "scripts/export_and_hash_migration_log.pyを実行し、確認済みで"
            "あれば環境変数SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED=trueを"
            "設定してから再実行してください。DDLは一切実行していません。"
        )

    expected_row_count_raw = os.environ.get("SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNT", "").strip()
    expected_sha256 = os.environ.get("SCHEMA_MIGRATION_LOG_ARCHIVE_SHA256", "").strip().lower()
    if not expected_row_count_raw or not expected_sha256:
        raise RollbackPreconditionError(
            "SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNT・SCHEMA_MIGRATION_LOG_"
            "ARCHIVE_SHA256が設定されていません。事前に"
            "scripts/export_and_hash_migration_log.pyを実行し、その出力の"
            "行数・SHA-256を設定してください。DDLは一切実行していません。"
        )
    try:
        expected_row_count = int(expected_row_count_raw)
    except ValueError:
        raise RollbackPreconditionError(
            "SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNTが整数として解釈できません: "
            f"{expected_row_count_raw!r}。DDLは一切実行していません。"
        )

    _, _, actual_sha256, actual_row_count = _canonical_migration_log_export(cur)
    if actual_row_count != expected_row_count or actual_sha256 != expected_sha256:
        raise RollbackPreconditionError(
            "現在のschema_migration_logの内容が、手動エクスポート時の記録と"
            f"一致しません(行数: 実際={actual_row_count} "
            f"期待={expected_row_count}、SHA-256: 実際={actual_sha256} "
            f"期待={expected_sha256})。エクスポート後に内容が変わった可能性が"
            "あるため、scripts/export_and_hash_migration_log.pyを再実行し、"
            "改めて確認してから再実行してください。DDLは一切実行していません。"
        )


def _capture_role_rls_state(cur):
    cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
    policy_count = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND rowsecurity")
    rls_enabled_count = cur.fetchone()[0]
    return {
        "existing_new_roles": sorted(_existing_role_names(cur)),
        "policy_count": policy_count,
        "rls_enabled_table_count": rls_enabled_count,
    }


def _check_cross_database_role_dependencies(cur, role_names):
    """`DROP ROLE`はPostgreSQLクラスタ全体で依存関係をチェックするため、
    「stagingにはアプリ用DBが1つだけ」と断定せず、対象ロールがカレント
    データベース以外にも依存オブジェクトを持たないかを事前に確認する。
    dbid=0(クラスタ共有オブジェクトへの依存)もカレントDB限定ではない
    ため「他所に依存あり」として扱う。
    戻り値: {role_name: [依存先データベース名, ...]}(依存が無いロール
    はキーに含まれない)。
    """
    cur.execute("SELECT oid FROM pg_database WHERE datname = current_database()")
    current_dbid = cur.fetchone()[0]

    problems = {}
    for role_name in role_names:
        cur.execute("SELECT oid FROM pg_roles WHERE rolname = %s", (role_name,))
        row = cur.fetchone()
        if row is None:
            continue
        role_oid = row[0]
        cur.execute(
            "SELECT DISTINCT COALESCE(d.datname, '(クラスタ共有オブジェクトへの依存)') "
            "FROM pg_shdepend sd "
            "LEFT JOIN pg_database d ON d.oid = sd.dbid "
            "WHERE sd.refclassid = 'pg_authid'::regclass AND sd.refobjid = %s "
            "AND sd.dbid <> %s",
            (role_oid, current_dbid),
        )
        others = [r[0] for r in cur.fetchall()]
        if others:
            problems[role_name] = others
    return problems


def _remove_roles_and_rls(cur):
    """①〜⑧を実行する。対象ロールがカレントDB以外にも依存を持つ場合、
    そのロールは`DROP ROLE`を行わず、代わりにNOLOGIN化して(LOGINロール
    の場合)、カレントDBの権限だけを撤去したうえで残す「安全な縮退」を
    行う。これは例外を送出して全体をロールバックするのではなく、確定的
    にコミットされる安全な終了状態である。

    [第12次訂正] 戻り値へ`status`(`"complete"`または`"degraded"`)を
    追加した。呼び出し元(Tier 2・Tier 3のmain())はこれを見て、
    `[OK]`(COMPLETE)と`[DEGRADED]`を明確に区別して報告すること
    ——`degraded_roles`が空でないのに`[OK]`と表示してはならない。
    """
    before = _capture_role_rls_state(cur)
    cross_db_problems = _check_cross_database_role_dependencies(cur, ROLE_NAMES)

    for policy, table in [
        ("records_tenant_isolation", "records"),
        ("tenants_tenant_isolation", "tenants"),
        ("tenant_subscriptions_tenant_isolation", "tenant_subscriptions"),
        ("tenant_usage_tenant_isolation", "tenant_usage"),
    ]:
        cur.execute(
            sql.SQL("DROP POLICY IF EXISTS {policy} ON public.{table}").format(
                policy=sql.Identifier(policy), table=sql.Identifier(table)
            )
        )

    for table in ["records", "tenants", "tenant_subscriptions", "tenant_usage",
                  "tenant_memberships", "users"]:
        cur.execute(
            sql.SQL("ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY").format(
                table=sql.Identifier(table)
            )
        )

    for signature in TEN_APP_DATA_OWNER_FUNCTION_SIGNATURES:
        cur.execute(f"ALTER FUNCTION public.{signature} OWNER TO postgres")

    cur.execute("REVOKE SELECT, INSERT, DELETE ON public.records FROM app_data_owner")
    cur.execute("REVOKE USAGE ON public.records_id_seq FROM app_data_owner")
    cur.execute("REVOKE SELECT (id), UPDATE (name) ON public.tenants FROM app_data_owner")
    cur.execute("REVOKE SELECT, INSERT, UPDATE ON public.tenant_subscriptions FROM app_data_owner")
    cur.execute("REVOKE SELECT, INSERT, UPDATE ON public.tenant_usage FROM app_data_owner")
    cur.execute("REVOKE INSERT ON public.processed_stripe_events FROM app_data_owner")
    cur.execute("REVOKE USAGE ON SCHEMA public FROM app_data_owner")

    degraded_roles = {}
    if "app_data_owner" not in cross_db_problems:
        cur.execute("DROP ROLE app_data_owner")
    else:
        degraded_roles["app_data_owner"] = cross_db_problems["app_data_owner"]

    for signature, roles in EXECUTE_REVOKE_TARGETS:
        cur.execute(f"REVOKE EXECUTE ON FUNCTION public.{signature} FROM {', '.join(roles)}")

    for role_name in ("app_runtime", "app_webhook"):
        cur.execute(f"REVOKE USAGE ON SCHEMA public FROM {role_name}")
        if role_name not in cross_db_problems:
            cur.execute(f"DROP ROLE {role_name}")
        else:
            cur.execute(f"ALTER ROLE {role_name} NOLOGIN")
            degraded_roles[role_name] = cross_db_problems[role_name]

    after = _capture_role_rls_state(cur)
    status = "degraded" if degraded_roles else "complete"
    result = {"status": status, "before": before, "after": after, "degraded_roles": degraded_roles}
    if degraded_roles:
        print(
            "[警告] 次のロールは他データベースにも依存を持つため削除せず、"
            f"NOLOGIN化・カレントDB権限撤去のみ行いました: {degraded_roles}。"
            "Tier 1(接続ロールをpostgresへ戻す)を実施し、他データベースの"
            "依存関係を人手で確認・解消したうえで、17-4章「Tier 2完了後の"
            "残存物撤去」相当の再開手順を実行してください。"
        )
    return result


def _count_existing_functions(cur, signatures):
    count = 0
    for signature in signatures:
        cur.execute("SELECT to_regprocedure(%s) IS NOT NULL", (f"public.{signature}",))
        if cur.fetchone()[0]:
            count += 1
    return count


def _drop_all_functions(cur):
    before_count = _count_existing_functions(cur, ALL_TWELVE_FUNCTION_SIGNATURES)
    for signature in ALL_TWELVE_FUNCTION_SIGNATURES:
        cur.execute(f"DROP FUNCTION IF EXISTS public.{signature}")
    after_count = _count_existing_functions(cur, ALL_TWELVE_FUNCTION_SIGNATURES)
    return {"before_function_count": before_count, "after_function_count": after_count}


def _get_migration_log_history(cur):
    """[第12次新設] schema_migration_logから、このTaskがこれまでに
    実際にNOT NULL・UNIQUEを追加したことがあるかを集計する。
    戻り値: (ever_added_not_null, ever_added_unique, record_count)
    """
    cur.execute(
        "SELECT bool_or((applied_changes->>'added_not_null')::boolean), "
        "       bool_or((applied_changes->>'added_unique')::boolean), "
        "       count(*) "
        "FROM public.schema_migration_log WHERE migration_name = %s",
        (MIGRATION_NAME,),
    )
    ever_added_not_null, ever_added_unique, record_count = cur.fetchone()
    return bool(ever_added_not_null), bool(ever_added_unique), record_count


def _reverify_and_drop_constraint(cur):
    """[第13次訂正・点C対応] schema_migration_logの記録(このTaskが実際に
    added_not_null/added_uniqueをtrueにしたことがあるか)とライブ状態を
    突き合わせ、本Taskが追加したと確認できたものだけを撤去する。記録が
    欠落・矛盾する場合は一切変更せず安全停止する(stagingで事前確認した
    固定の前提だけに依存しない)。制約の同一性確認は12-2章の作成時
    チェックと同じ4項目(contype・convalidated・condeferrable・
    condeferred・conkey)をすべて揃えて確認する(前版はconkeyのみ)。
    """
    cur.execute("SELECT to_regclass('public.schema_migration_log') IS NOT NULL")
    if not cur.fetchone()[0]:
        raise RuntimeError(
            "public.schema_migration_logが存在しないため、"
            "stripe_subscription_id制約の撤去可否を判断できません。"
            "安全のため撤去せず停止します。"
        )

    ever_added_not_null, ever_added_unique, record_count = _get_migration_log_history(cur)
    if record_count == 0:
        raise RuntimeError(
            f"schema_migration_logに{MIGRATION_NAME}の記録がありません。"
            "安全のため撤去せず停止します。"
        )

    cur.execute(
        "SELECT attnum FROM pg_attribute "
        "WHERE attrelid = 'public.tenant_subscriptions'::regclass "
        "AND attname = 'stripe_subscription_id'"
    )
    attnum_row = cur.fetchone()
    target_attnum = attnum_row[0] if attnum_row else None

    cur.execute(
        "SELECT contype, convalidated, condeferrable, condeferred, conkey "
        "FROM pg_constraint "
        "WHERE conrelid = 'public.tenant_subscriptions'::regclass AND conname = %s",
        (CONSTRAINT_NAME,),
    )
    row = cur.fetchone()
    before_state = {
        "constraint_exists": row is not None,
        "ever_added_not_null": ever_added_not_null,
        "ever_added_unique": ever_added_unique,
    }

    if row is not None:
        contype, convalidated, condeferrable, condeferred, conkey = row
        if (
            contype != "u"
            or not convalidated
            or condeferrable
            or condeferred
            or target_attnum is None
            or list(conkey) != [target_attnum]
        ):
            raise RuntimeError(
                f"撤去対象の制約{CONSTRAINT_NAME}が、本Taskが追加した"
                "stripe_subscription_id単独のUNIQUE制約と一致しません: "
                f"contype={contype} convalidated={convalidated} "
                f"condeferrable={condeferrable} condeferred={condeferred} "
                f"conkey={list(conkey)}(期待=[{target_attnum}])。"
                "本Task以外の経路で変更された可能性があるため、"
                "安全のため撤去せず停止します。"
            )
        if not ever_added_unique:
            raise RuntimeError(
                f"制約{CONSTRAINT_NAME}が存在しますが、schema_migration_log上"
                "本Taskがこのunique制約を追加した記録がありません。本Task"
                "以外の経路で追加された可能性があるため、安全のため撤去せず"
                "停止します。"
            )
        cur.execute(
            sql.SQL("ALTER TABLE public.tenant_subscriptions DROP CONSTRAINT {name}").format(
                name=sql.Identifier(CONSTRAINT_NAME)
            )
        )

    cur.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tenant_subscriptions' "
        "AND column_name = 'stripe_subscription_id'"
    )
    is_nullable_row = cur.fetchone()
    currently_not_null = is_nullable_row is not None and is_nullable_row[0] == "NO"
    if currently_not_null:
        if not ever_added_not_null:
            raise RuntimeError(
                "stripe_subscription_idはNOT NULLですが、schema_migration_log上"
                "本TaskがNOT NULLを追加した記録がありません。本Task以外の経路で"
                "追加された可能性があるため、安全のため撤去せず停止します。"
            )
        cur.execute(
            "ALTER TABLE public.tenant_subscriptions "
            "ALTER COLUMN stripe_subscription_id DROP NOT NULL"
        )

    cur.execute(
        "SELECT count(*) FROM pg_constraint "
        "WHERE conrelid = 'public.tenant_subscriptions'::regclass AND conname = %s",
        (CONSTRAINT_NAME,),
    )
    still_exists = cur.fetchone()[0] > 0
    return {"before_state": before_state, "constraint_removed": not still_exists}


def _drop_migration_log_table(cur, export_dir="audit/schema_migration_log_archive"):
    """[第13次訂正] テーブルの削除を実行する前に
    `_precondition_migration_log_manually_archived`が必ず先に呼ばれて
    いる前提(そちらが真の安全性の根拠)。ここでのローカルファイルへの
    書き出しは、Railwayの実行ファイル領域が永続保存先とは限らないため、
    あくまで補助的なベストエフォートであり、安全性の根拠にはしない。
    export_and_hash_migration_log.py・_precondition_migration_log_
    manually_archivedと同一の正規化ロジック(_canonical_migration_log_
    export)を使い、書き出す内容が事前確認済みの内容と形式的にも一致
    するようにする。
    """
    cur.execute("SELECT to_regclass('public.schema_migration_log') IS NOT NULL")
    table_exists = cur.fetchone()[0]
    if not table_exists:
        return {"exported_row_count": 0, "export_path": None, "table_existed": False}

    rows, canonical_text, digest, row_count = _canonical_migration_log_export(cur)

    export_path = None
    try:
        export_path_dir = Path(export_dir)
        export_path_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_path_dir / f"schema_migration_log_archive_{digest[:12]}.json"
        export_path.write_text(canonical_text, encoding="utf-8")
    except OSError as e:
        # ベストエフォートの補助書き出しであり、失敗してもテーブル削除自体は
        # 妨げない(真の安全性は事前の人手によるアーカイブ確認で担保済み)
        print(f"[警告] ローカルへの補助書き出しに失敗しました(処理は継続します): {e}")

    cur.execute("DROP TABLE IF EXISTS public.schema_migration_log")
    return {
        "exported_row_count": row_count,
        "export_sha256": digest,
        "export_path": str(export_path) if export_path else None,
        "table_existed": True,
    }


def _capture_full_baseline_state(cur):
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    table_names = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")
    sequence_names = {r[0] for r in cur.fetchall()}
    cur.execute(
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.prokind = 'f'"
    )
    function_count = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
    policy_count = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND rowsecurity")
    rls_enabled_count = cur.fetchone()[0]

    cur.execute(
        "SELECT attnum FROM pg_attribute "
        "WHERE attrelid = 'public.tenant_subscriptions'::regclass "
        "AND attname = 'stripe_subscription_id'"
    )
    target_attnum_row = cur.fetchone()
    target_attnum = target_attnum_row[0] if target_attnum_row else None
    cur.execute(
        "SELECT conname, conkey FROM pg_constraint "
        "WHERE conrelid = 'public.tenant_subscriptions'::regclass AND contype = 'u'"
    )
    remaining_unique_constraint_names = [
        conname for conname, conkey in cur.fetchall()
        if target_attnum is not None and list(conkey) == [target_attnum]
    ]

    cur.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tenant_subscriptions' "
        "AND column_name = 'stripe_subscription_id'"
    )
    is_nullable_row = cur.fetchone()
    stripe_subscription_id_is_nullable = is_nullable_row[0] if is_nullable_row else None

    cur.execute("SELECT to_regclass('public.schema_migration_log') IS NOT NULL")
    migration_log_exists = cur.fetchone()[0]

    existing_roles = sorted(_existing_role_names(cur))
    existing_role_details = {}
    if existing_roles:
        cur.execute(
            "SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname = ANY(%s)",
            (existing_roles,),
        )
        existing_role_details = {name: {"rolcanlogin": can_login} for name, can_login in cur.fetchall()}

    return {
        "table_names": sorted(table_names),
        "sequence_names": sorted(sequence_names),
        "custom_function_count": function_count,
        "existing_new_roles": existing_roles,
        "existing_new_role_details": existing_role_details,
        "policy_count": policy_count,
        "rls_enabled_table_count": rls_enabled_count,
        "stripe_subscription_id_unique_constraint_names": remaining_unique_constraint_names,
        "stripe_subscription_id_is_nullable": stripe_subscription_id_is_nullable,
        "schema_migration_log_exists": migration_log_exists,
    }


def verify_round21_baseline_state(cur):
    """[第12次新設] Tier 3・17-4章の完了直前に呼び、第21回終了時点と
    完全に一致することを厳密にassertする。printするだけで成功扱いに
    せず、1つでも不一致なら`BaselineStateMismatchError`を送出する——
    呼び出し元のmain()はこれをcommitせずROLLBACKすること。
    """
    state = _capture_full_baseline_state(cur)
    problems = []

    actual_tables = set(state["table_names"])
    if actual_tables != EXPECTED_TABLE_NAMES:
        problems.append(
            f"テーブル集合が不一致: 実際={sorted(actual_tables)} "
            f"期待={sorted(EXPECTED_TABLE_NAMES)}"
        )

    actual_sequences = set(state["sequence_names"])
    if actual_sequences != EXPECTED_SEQUENCE_NAMES:
        problems.append(
            f"シーケンス集合が不一致: 実際={sorted(actual_sequences)} "
            f"期待={sorted(EXPECTED_SEQUENCE_NAMES)}"
        )

    if state["custom_function_count"] != 0:
        problems.append(f"カスタム関数が残存: {state['custom_function_count']}件")

    if state["existing_new_roles"]:
        problems.append(f"新設ロールが残存: {state['existing_new_roles']}")

    if state["rls_enabled_table_count"] != 0:
        problems.append(f"RLSが有効なテーブルが残存: {state['rls_enabled_table_count']}件")

    if state["policy_count"] != 0:
        problems.append(f"RLSポリシーが残存: {state['policy_count']}件")

    if state["stripe_subscription_id_unique_constraint_names"]:
        problems.append(
            "stripe_subscription_idのUNIQUE制約が残存: "
            f"{state['stripe_subscription_id_unique_constraint_names']}"
        )

    if state["stripe_subscription_id_is_nullable"] != "YES":
        problems.append(
            "stripe_subscription_idがNOT NULLのまま(NULL許容へ戻っていない): "
            f"is_nullable={state['stripe_subscription_id_is_nullable']}"
        )

    if state["schema_migration_log_exists"]:
        problems.append("schema_migration_logテーブルが残存")

    if problems:
        raise BaselineStateMismatchError(
            "第21回終了時点との不一致が検出されました: " + " / ".join(problems)
        )

    return state
```

### 17-1. Tier 1: 緊急切り戻し(接続だけを戻す)

問題発生直後、最短時間でサービスを復旧させるための手順。ロール・関数・
RLSはstagingに残ったままでよい。

```
web・stripe-webhookのDATABASE_URL変数の参照先を、
${{Postgres.DATABASE_URL}}(本Task中に一切変更していない、postgresロール
用の既存参照)へ戻し、両サービスを再デプロイする。
```

**完了条件**: `web`・`stripe-webhook`ともHTTP 200・ログにDB接続エラー
なし。

### 17-2. Tier 2: 新設ロールの撤去(開始状態: 完全適用状態)

`scripts/rollback_tier2_remove_roles.py`。開始状態は**完全適用状態**
(3ロール・GRANT・RLSが存在)。12関数・制約・`schema_migration_log`には
触れない。

**終了コード(第12次新設、点1対応)**: `0`=COMPLETE(3ロールすべて撤去)
/ `1`=FAILED(例外・全体ROLLBACK) / `2`=DEGRADED(クロスDB依存により
NOLOGIN化・カレントDB権限撤去のみでcommit)。**`degraded_roles`が空で
ないのに`[OK]`や終了コード0を返してはならない。**

```python
"""scripts/rollback_tier2_remove_roles.py

終了コード: 0=COMPLETE(3ロールすべて撤去) / 1=FAILED(例外・全体ROLLBACK)
/ 2=DEGRADED(クロスDB依存によりNOLOGIN化・カレントDB権限撤去のみでcommit)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402
from rollback_helpers import (  # noqa: E402
    RollbackPreconditionError,
    _precondition_roles_exist,
    _remove_roles_and_rls,
)
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity  # noqa: E402


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)  # 0-1章、DDLより前に実行
            _precondition_roles_exist(cur)
            result = _remove_roles_and_rls(cur)
        conn.commit()
        if result["status"] == "complete":
            print(f"[OK] COMPLETE: Tier 2(新設ロールの撤去)が完了しました。 detail={result}")
            return 0
        print(f"[DEGRADED] Tier 2は完全には完了していません(クロスDB依存)。 detail={result}")
        return 2
    except (psycopg.Error, RollbackPreconditionError, RuntimeError, TargetDatabaseMismatchError) as e:
        conn.rollback()
        print(f"[FAILED] Tier 2の実行中にエラーが発生しました。変更はロールバックされました: {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

**完了条件**: 終了コード0(`[OK] COMPLETE`)まで到達し、
`result["after"]["existing_new_roles"]`が空リストであること。終了コード
2(`[DEGRADED]`)の場合は完了ではなく、Tier 1実施と他DB依存の解消が
必要(11章#5・#14)。

### 17-3. Tier 3: 第21回終了時点への全面復帰(開始状態: 完全適用状態)

`scripts/rollback_tier3_full_restore.py`。開始状態は**完全適用状態**
(Tier 2と同じ、Tier 2の後に続けて実行するものではない)。17-2章の
`_remove_roles_and_rls`に加え、12関数の削除・制約の再検証つき撤去・
監査ログテーブルの削除・`verify_round21_baseline_state`による厳密な
最終確認までを同一トランザクションで行う。

**終了コード(第12次新設、点2・3対応)**: `0`=COMPLETE(第21回終了時点と
完全一致・`verify_round21_baseline_state()`通過) / `1`=FAILED(例外・
全体ROLLBACK、baseline不一致を含む) / `2`=DEGRADED(クロスDB依存により
ロール撤去を完了できず、**関数・制約・ログの削除へは一切進まずに**
NOLOGIN化・カレントDB権限撤去のみでcommit)。

```python
"""scripts/rollback_tier3_full_restore.py

終了コード: 0=COMPLETE(第21回終了時点と完全一致・verify_round21_baseline_
state()通過) / 1=FAILED(例外・全体ROLLBACK、baseline不一致を含む) /
2=DEGRADED(クロスDB依存によりロール撤去を完了できず、関数・制約・ログの
削除へ進まずにNOLOGIN化・カレントDB権限撤去のみでcommit)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402
from rollback_helpers import (  # noqa: E402
    BaselineStateMismatchError,
    RollbackPreconditionError,
    _drop_all_functions,
    _drop_migration_log_table,
    _precondition_migration_log_manually_archived,
    _precondition_roles_exist,
    _remove_roles_and_rls,
    _reverify_and_drop_constraint,
    verify_round21_baseline_state,
)
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity  # noqa: E402


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)
            _precondition_roles_exist(cur)
            _precondition_migration_log_manually_archived(cur)

            role_rls_result = _remove_roles_and_rls(cur)
            if role_rls_result["degraded_roles"]:
                # [第12次訂正] クロスDB依存で完全なロール撤去ができな
                # かった場合、全面復帰処理(関数・制約・ログの削除)へは
                # 進まない。ここまでの安全な縮退(NOLOGIN化・カレントDB
                # 権限撤去)だけをcommitし、DEGRADEDとして停止する。
                conn.commit()
                print(
                    "[DEGRADED] Tier 3は第21回終了時点への全面復帰を完了できません"
                    "(クロスDB依存)。ロール・RLSの安全な縮退のみcommitしました。"
                    f" detail={role_rls_result}"
                )
                print(
                    "[案内] Tier 1(接続ロールをpostgresへ戻す)を実施し、他"
                    "データベースの依存関係を人手で解消したうえで、"
                    "17-4章「復旧(resume)」(rollback_resume_to_full_"
                    "restore.py)を再開手順として実行してください。"
                )
                return 2

            function_result = _drop_all_functions(cur)
            constraint_result = _reverify_and_drop_constraint(cur)
            log_result = _drop_migration_log_table(cur)
            final_state = verify_round21_baseline_state(cur)  # 不一致ならここで例外→ROLLBACK
        conn.commit()
        print("[OK] COMPLETE: Tier 3(第21回終了時点への全面復帰)が完了しました。")
        print(f"  role_rls={role_rls_result}")
        print(f"  functions={function_result}")
        print(f"  constraint={constraint_result}")
        print(f"  log_export={log_result}")
        print(f"  final_state={final_state}")
        return 0
    except (psycopg.Error, RollbackPreconditionError, RuntimeError,
            TargetDatabaseMismatchError, BaselineStateMismatchError) as e:
        conn.rollback()
        print(f"[FAILED] Tier 3の実行中にエラーが発生しました。変更はロールバックされました: {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

途中のいずれか(所有権移管・GRANT取消・`DROP ROLE`・関数削除・制約の
再検証・`verify_round21_baseline_state`による最終確認)が失敗した場合は
例外が送出され、`conn.rollback()`によりTier 3着手前の状態(ロール・
関数・制約・監査ログすべて)が維持される。DEGRADED(2)の場合はロール・
RLSの縮退のみが確定的にcommitされ、関数・制約・ログには一切触れて
いない。書き出し済みのローカルファイルが残ることはあるが、DB状態には
影響しない(17-0章の`_drop_migration_log_table`の注記を参照)。

**完了条件(1章①〜⑤との照合、テーブル・シーケンス名の完全一致・NOT NULL
復帰確認を含む)**: 終了コード0(`[OK] COMPLETE`)まで到達すること。
これは`verify_round21_baseline_state()`が例外を送出しなかったことと
同値であり、次のすべてを実測で満たす。

| 項目 | 第21回終了時点(1章) | Tier 3実行後の実測(`verify_round21_baseline_state`) |
|---|---|---|
| テーブル集合 | 7件(具体的な7テーブル名) | `table_names`が期待集合と完全一致 |
| シーケンス集合 | `records_id_seq`のみ | `sequence_names`が`{records_id_seq}`と完全一致 |
| カスタム関数の数 | 0件 | `custom_function_count: 0` |
| カスタムロール | `postgres`のみ | `existing_new_roles: []` |
| RLSポリシー数 | 0件 | `policy_count: 0` |
| RLS有効化状況 | 全テーブル`false` | `rls_enabled_table_count: 0` |
| `stripe_subscription_id`のUNIQUE制約 | 無し | `stripe_subscription_id_unique_constraint_names: []`(名前を問わない) |
| `stripe_subscription_id`のNOT NULL | 復帰済み(NULL許容) | `stripe_subscription_id_is_nullable: 'YES'` |
| `schema_migration_log` | 存在しない | `schema_migration_log_exists: False` |

この照合を、実際に15章のテストで実行する(11章#7・#21・#22で実測済み)。

### 17-3b. 監査ログの事前エクスポート(新設・点D対応、`scripts/export_and_hash_migration_log.py`)

**新設の要点**: `schema_migration_log`削除の安全性は、単なる真偽値
(`SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED=true`)ではなく、実際に
エクスポートした内容の行数・SHA-256との一致で担保する(17-0章
`_precondition_migration_log_manually_archived`)。この専用ツールは
読み取り専用(削除・変更は一切行わない)で、その行数・SHA-256を出力する。

```python
"""scripts/export_and_hash_migration_log.py

事前に手動実行し、schema_migration_logの内容を確定させ、行数・
SHA-256を出力する。削除・変更は一切行わない(読み取り専用)。

操作者はこの出力を保存し、Tier 3・17-4(resume)実行時に
SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNT・SCHEMA_MIGRATION_LOG_ARCHIVE_
SHA256として設定する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402
from rollback_helpers import _canonical_migration_log_export  # noqa: E402
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity  # noqa: E402


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)
            cur.execute("SELECT to_regclass('public.schema_migration_log') IS NOT NULL")
            if not cur.fetchone()[0]:
                print("[OK] schema_migration_logは存在しません(エクスポート不要)。")
                return 0
            _, canonical_text, digest, row_count = _canonical_migration_log_export(cur)
        conn.rollback()  # 読み取りのみ、DBへの変更は一切無い

        export_dir = Path("audit/schema_migration_log_archive")
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / f"schema_migration_log_export_{digest[:12]}.json"
        export_path.write_text(canonical_text, encoding="utf-8")

        print(f"[OK] エクスポート完了。 row_count={row_count} sha256={digest} path={export_path}")
        print("この内容を永続的な保存先(git・S3等)へ保存したうえで、")
        print("Tier 3・17-4(resume)実行時に次の環境変数を設定してください:")
        print(f"  SCHEMA_MIGRATION_LOG_ARCHIVE_ROW_COUNT={row_count}")
        print(f"  SCHEMA_MIGRATION_LOG_ARCHIVE_SHA256={digest}")
        return 0
    except (psycopg.Error, TargetDatabaseMismatchError) as e:
        conn.rollback()
        print(f"[FAILED] エクスポート中にエラーが発生しました: {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

### 17-4. 復旧(resume): Tier 2完了後、またはDEGRADED後の第21回終了時点への復帰(第13次全面訂正)

**訂正の要点(第13次・点A対応)**: 前版の`rollback_cleanup_after_tier2.py`
は「Tier 2完了後(3ロールとも不在)」しか受理せず、DEGRADED状態(一部
ロールだけ削除された状態)はTier 2の再実行条件(全ロール存在)にも
このスクリプトの開始条件(全ロール不在)にも合致せず、**正規の復旧経路が
途切れる**という指摘を受けた。`rollback_resume_to_full_restore.py`へ
一般化し、次の2つの開始状態のいずれも正しく受理するよう訂正した。

- Tier 2完了状態(3ロールとも不在)
- DEGRADED状態(一部ロールが「安全に縮退済み」: NOLOGIN化・カレントDB
  権限撤去済みだが、クロスDB依存で未DROPのまま残っている)

**終了コード**: `0`=COMPLETE(第21回終了時点と完全一致) / `1`=FAILED
(例外・全体ROLLBACK、baseline不一致を含む) / `2`=DEGRADED(他データ
ベースへの依存がまだ解消されていないロールが残っている。それまでに
解消できた分の進捗はcommitされる)。

```python
"""scripts/rollback_resume_to_full_restore.py

[第13次改訂・点A対応] 旧rollback_cleanup_after_tier2.pyを一般化した
「Tier 2完了状態」または「DEGRADED状態(一部ロールが安全に縮退済み)」
のいずれからでも実行できる、第21回終了時点への復旧スクリプト。

終了コード: 0=COMPLETE(第21回終了時点と完全一致) / 1=FAILED(例外・
全体ROLLBACK、baseline不一致を含む) / 2=DEGRADED(他データベースへの
依存がまだ解消されていないロールが残っている。それまでに解消できた
分の進捗はcommitされる)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import psycopg  # noqa: E402

import db  # noqa: E402
from rollback_helpers import (  # noqa: E402
    BaselineStateMismatchError,
    RollbackPreconditionError,
    _drop_all_functions,
    _drop_migration_log_table,
    _finish_degraded_role_removal,
    _precondition_migration_log_manually_archived,
    _precondition_ready_to_resume,
    _reverify_and_drop_constraint,
    verify_round21_baseline_state,
)
from target_identity import TargetDatabaseMismatchError, verify_target_database_identity  # noqa: E402


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            verify_target_database_identity(cur)
            _precondition_ready_to_resume(cur)

            role_result = _finish_degraded_role_removal(cur)
            if role_result["still_degraded"]:
                # まだ他DB依存が解消されていないロールがある。それまでに
                # 解消できた分(newly_dropped)の進捗はcommitし、関数・
                # 制約・ログには一切触れずDEGRADEDとして停止する。
                conn.commit()
                print(
                    "[DEGRADED] まだ他データベースへの依存が解消されていない"
                    f"ロールがあります。 detail={role_result}"
                )
                print(
                    "[案内] 依存先データベースを人手で確認・解消したうえで、"
                    "このスクリプトを再実行してください。"
                )
                return 2

            _precondition_migration_log_manually_archived(cur)
            function_result = _drop_all_functions(cur)
            constraint_result = _reverify_and_drop_constraint(cur)
            log_result = _drop_migration_log_table(cur)
            final_state = verify_round21_baseline_state(cur)  # 不一致ならここで例外→ROLLBACK
        conn.commit()
        print("[OK] COMPLETE: 第21回終了時点への復旧が完了しました。")
        print(f"  role_removal={role_result}")
        print(f"  functions={function_result}")
        print(f"  constraint={constraint_result}")
        print(f"  log_export={log_result}")
        print(f"  final_state={final_state}")
        return 0
    except (psycopg.Error, RollbackPreconditionError, RuntimeError,
            TargetDatabaseMismatchError, BaselineStateMismatchError) as e:
        conn.rollback()
        print(f"[FAILED] 復旧処理中にエラーが発生しました。変更はロールバックされました: {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

**完了条件**: 終了コード0(`[OK] COMPLETE`)まで到達し、17-3章と同じ
`verify_round21_baseline_state`の表と一致することを確認する。終了
コード2(`[DEGRADED]`)の場合は、他データベースの依存関係を人手で解消
したうえで、このスクリプトを再実行する(11章#14b・#15bで実機確認済み)。

### 17-5. 適用範囲のまとめ

| スクリプト | 開始状態(事前条件) | 終了状態 |
|---|---|---|
| Tier 1(緊急切り戻し) | 接続切替直後に問題発生 | 接続先のみ変更。ロール・関数・RLS・制約・監査ログは残る |
| Tier 2(`rollback_tier2_remove_roles.py`) | **完全適用状態**(3ロール存在) | COMPLETE: 3ロール・関数所有権・GRANT・RLSが撤去。12関数定義・UNIQUE制約・監査ログは残る。DEGRADED: 一部ロールが安全に縮退したまま残る |
| Tier 3(`rollback_tier3_full_restore.py`) | **完全適用状態**(3ロール存在、Tier 2の後に続けて実行しない) | COMPLETE: 1章①〜⑤と一致する第21回終了時点。DEGRADED: ロール・RLSの縮退のみcommit、関数・制約・ログは無変更 |
| 復旧・resume(`rollback_resume_to_full_restore.py`) | **Tier 2完了状態**または**DEGRADED状態**のいずれか(3ロール不在、または一部が安全に縮退済み) | COMPLETE: Tier 3と同じ、1章①〜⑤と一致する状態。DEGRADED: 未解消の依存が残るロールがあれば、解消できた分だけ進めてcommitし再度DEGRADED |
| 監査ログエクスポート(`export_and_hash_migration_log.py`) | 任意(読み取り専用) | DBへの変更なし。行数・SHA-256を出力するのみ |

**選択の目安**: 設計自体を見直したいだけならTier 2のみでよい(関数・
制約は残したまま、ロールだけ元に戻す)。第21回終了時点まで完全に戻す
必要がある場合、完全適用状態からであれば直接Tier 3、既にTier 2・
Tier 3を実行済み(COMPLETEでもDEGRADEDでも)であれば「復旧(resume)」を
使う——**開始状態を問わず、これ1本が正規の復旧経路である**。

---

## 18. バックアップ確認（変更なし）

Railwayの`Backups`タブで直近のバックアップ取得時刻・復元手順を確認する
ことを、14章手順10・11(staging DDL実行)の前提条件とする。

---

## 19. 岩瀬様が承認すべき判断事項

1〜15. (前版から変更なし。脱superuser化見送り・Webhook関数化・
`resolve_login`関数化・監査ロール見送り・メンテナンス時間設定・パスワード
方式未確定・脅威モデル・`resolve_login`残存リスク・`app_data_owner`分離・
RLS位置づけ・UNIQUE制約追加・CI 16/18マトリクス・列単位GRANT実測検証・
ロール属性不一致時の安全停止・staging DDL前のバックアップ確認)

16. **(新規)`aclexplode`検証SQLの訂正(`CROSS JOIN LATERAL`・PUBLIC(OID 0)
    判定・`proacl IS NULL`検出、6-2章)への同意**
17. **(新規)UNIQUE制約の同名検証を`conkey`/`attnum`による列単位の正確な
    照合へ訂正したこと(12-2章)への同意**
18. **(新規)「4状態処理」の説明を「3状態自動処理＋1状態安全停止」へ
    訂正したこと(12-1章)への同意**
19. **(新規)既存LOGINロールのパスワードは「一致確認」ではなく「常に
    明示的に上書き」で保証する方針(4-3章)と、それに伴う別接続試験
    (14章手順12)を必須手順とすることへの同意**
20. **(新規)`public`スキーマのUSAGEを3ロールへ明示GRANTする方針(6-0章)
    への同意**
21. **(新規)UNIQUE・NOT NULL制約の正確な切り戻しSQL(12-4章)と、実際に
    適用した内容をログへ記録してから切り戻す運用上の注意への同意**
22. **(新規)ロールバックをTier 1(緊急切り戻し)・Tier 2(ロール撤去)・
    Tier 3(第21回終了時点への全面復帰)へ分離したこと(17章)への同意**
23. 本書5〜7章の関数・GRANT・RLS定義全文をもって実装承認とするか、
    それとも別途コードレビューの機会を設けるか

24. **(新規・第8次・Critical)16章`main()`の関数所有権移管の順序訂正
    (`reassign_function_owners`をGRANTリセット・ACL検証より前に移動)
    への同意**
25. **(新規・第8次→第9次で構成変更)手順12(別接続試験)が1件でも失敗した
    場合、手順13以降へ進まず、原因修正後の再試行またはTier 2撤去の
    いずれかへ進むという停止・復旧手順(14章手順12)への同意**
26. **(新規・第8次)`ALTER ROLE ... PASSWORD`の冪等性に関する説明を
    「副作用が無く完全に冪等」から「接続資格情報としての結果は同じ」へ
    訂正したこと(4-3章)への同意**
27. **(新規・第9次・Critical)`schema_migration_log`を、Tier 3(または
    17-4章「Tier 2完了後の残存物撤去」)の実行時に全行をローカル監査
    ファイルへ書き出したうえでテーブルごと削除する方針(12-2章、17-0章
    `_export_and_drop_migration_log`)への同意。**「第21回終了時点への
    完全復帰」はこのテーブル・付随シーケンスの削除まで含む**ことへの
    同意**
28. **(新規・第9次)Tier 2・Tier 3を、いずれも「完全適用状態」を開始
    状態とする独立スクリプトとし、Tier 2実行後から完全復帰させるための
    専用スクリプト(17-4章「Tier 2完了後の残存物撤去」)を別途新設した
    こと、および開始状態の前提が誤っている場合は`RollbackPrecondition
    Error`で安全停止する設計(17-0章)への同意**
29. **(新規・第9次)Tier 3・17-4章の完了条件を、1章①〜⑤に加えて
    テーブル数(7件)・シーケンス数(1件)の実測一致まで含めることへの
    同意(17-3章完了条件表)**
30. **(新規・第9次)Tier 3・17-4章の監査ログ・実行結果を、固定値ではなく
    実行前後のカタログ状態を実測した`dict`(`before`/`after`・
    `before_function_count`/`after_function_count`等)として記録・出力
    する方式(17-0章)への同意**
31. **(新規・第9次)制約撤去の直前に、12-2章と同じ`conkey`/`attnum`照合を
    同一トランザクション内で再実行し、不一致なら撤去せず安全停止する
    設計(17-0章`_reverify_and_drop_constraint`)への同意**
32. **(新規・第11次)`DROP ROLE`前にカレントDB以外への依存
    (`pg_shdepend`)を確認し、依存がある場合はDROPを見送ってNOLOGIN化・
    カレントDB権限撤去のみ行う「安全な縮退」設計(0-1章・17-0章
    `_check_cross_database_role_dependencies`)への同意。実機
    (PG16・18)で検知・縮退・完了条件表への反映を確認済み(11章#30)**
33. **(新規・第11次→第12次で必須化)全migration/rollbackスクリプトの
    冒頭で、接続先DB名・ユーザー・PostgreSQLバージョン・想定7テーブルの
    存在を確認し、不一致ならDDL開始前に安全停止する`target_identity`
    モジュール(0-1章)への同意**
34. **(新規・第12次・Critical)Tier 2・Tier 3の終了コードを
    `COMPLETE`(0)/`DEGRADED`(2)/`FAILED`(1)へ分離し、クロスDB依存等で
    完全に完了していない場合に`[OK]`・終了コード0を返さないよう訂正
    したこと(17-2・17-3章)への同意**
35. **(新規・第12次・Critical)Tier 3は`degraded_roles`が空でない場合、
    関数・制約・ログの削除処理へ進まずDEGRADEDで停止するよう訂正した
    こと(17-3章)への同意**
36. **(新規・第12次)`verify_round21_baseline_state()`により、第21回
    終了時点との厳密な一致(テーブル・シーケンス集合の完全一致、NOT NULL
    復帰確認を含む)をassertし、不一致ならROLLBACKする設計(17-0章)への
    同意**
37. **(新規・第12次)制約撤去を`schema_migration_log`の履歴(このTaskが
    実際にNOT NULL・UNIQUEを追加したことがあるか)と照合したうえでのみ
    行うよう、`_reverify_and_drop_constraint`を全面訂正したこと(17-0章)
    への同意**
38. **(新規・第12次)`schema_migration_log`の自動ファイル書き出しを
    安全性の根拠から外し、テーブル削除の前提として人間による手動
    エクスポート・保存・ハッシュ確認を示す環境変数
    (`SCHEMA_MIGRATION_LOG_MANUALLY_ARCHIVED=true`)を必須の事前条件と
    したこと(17-0章)への同意**
39. **(新規・第12次→第13次で環境識別部分を訂正)接続先識別の必須項目
    (`EXPECTED_TARGET_DBNAME`・`EXPECTED_TARGET_USER`・
    `EXPECTED_RAILWAY_PROJECT_ID`/`RAILWAY_PROJECT_ID`・
    `EXPECTED_RAILWAY_ENVIRONMENT_ID`/`RAILWAY_ENVIRONMENT_ID`・
    `STAGING_DDL_EXPLICITLY_ALLOWED`)を、未設定/不一致なら停止する
    必須項目としたこと(0-1章)への同意**
40. **(新規・第12次)テストランナーを`run_all_checks.py`へ刷新し、
    全チェックをDBへの直接問い合わせによる判定へ統一、1件でも失敗
    したら終了コード1になるようにしたこと(11章#33〜#43)への同意**
41. **(新規・第13次・点A・Critical)DEGRADED状態が正規の復旧経路を持たない
    不具合を修正し、`_precondition_ready_to_resume`・
    `_finish_degraded_role_removal`を新設、
    `rollback_cleanup_after_tier2.py`を`rollback_resume_to_full_
    restore.py`へ一般化して「Tier 2完了状態」「DEGRADED状態」の
    いずれからも復旧できるようにしたこと(17-4章)への同意**
42. **(新規・第13次・点B)staging識別のRailway環境確認を、操作者が両辺を
    手入力する比較から、`railway run`が自動注入する`RAILWAY_PROJECT_ID`・
    `RAILWAY_ENVIRONMENT_ID`との比較へ訂正したこと(0-1章)への同意。
    実運用では必ず`railway run`経由で実行することへの同意**
43. **(新規・第13次・点C)UNIQUE制約撤去時の同一性確認へ、
    `convalidated`・`condeferrable`・`condeferred`の確認を追加した
    こと(17-0章`_reverify_and_drop_constraint`)への同意**
44. **(新規・第13次・点D)監査ログ「保存済み」の確認を、真偽値のみから
    行数・SHA-256の突き合わせへ強化し、専用の読み取り専用エクスポート
    ツール(`scripts/export_and_hash_migration_log.py`)を新設したこと
    への同意**
45. **(新規・第13次・点E)「DEGRADED→依存解消→resume→第21回状態への
    完全復帰」の完全な復旧経路を、PostgreSQL 16・18の実機で再現・確認
    したこと(11章#14b・#15b)への同意**

---

## 20. 完了条件

### 第9次改訂版(ChatGPT監査対応)

- [x] **[Critical]** `schema_migration_log`(および付随シーケンス)を
      Tier 3・17-4章の実行時にローカル監査ファイルへ書き出したうえで
      テーブルごと削除するようにし、「第21回終了時点への完全復帰」が
      文字どおり成立するようにした(12-2章、17-0章)
- [x] Tier 3のSQLを「Tier 2と同一」という省略無しに、共有ヘルパー関数
      (17-0章)を経由した完全なPythonスクリプトとして提示した(17-3章)
- [x] Tier 2・Tier 3を、いずれも「完全適用状態」を開始状態とする独立
      スクリプトへ整理し、Tier 2実行後から完全復帰するための専用
      スクリプト(17-4章)を新設した。開始状態の前提が誤っている場合は
      `RollbackPreconditionError`で安全停止するようにした(17-0章)
- [x] Tier 3・17-4章の監査記録を固定値から、実行前後のカタログ状態を
      実測した構造化データへ訂正した(17-0章の各ヘルパー関数の戻り値)
- [x] 制約撤去の直前に`conkey`/`attnum`による再検証を同一トランザクション
      内で行い、不一致なら撤去せず安全停止するようにした
      (17-0章`_reverify_and_drop_constraint`)
- [x] Tier 3・17-4章の完了条件表へ、テーブル数(7件)・シーケンス数(1件)
      の実測一致を追加した(17-3章)

### 第10次改訂版(Claude自己監査、ChatGPT不在時の暫定対応)

- [x] `_capture_full_baseline_state`の制約確認を、制約名だけの一致から
      `conkey`/`attnum`による列単位の照合(別名の同等制約も検出)へ
      訂正した(17-0章)
- [x] `_export_and_drop_migration_log`に、対象テーブルが存在しない場合の
      分岐を追加した(17-0章)
- [x] `_drop_all_functions`の前後件数確認を、関数名一致から
      `to_regprocedure`によるシグネチャ単位の照合へ訂正した(17-0章)
- [x] **この自己監査は独立した第三者チェックの代替ではない**。実装着手の
      最終承認は、ChatGPT復帰後の確認、または岩瀬様が明示的にその代役を
      引き受けるとおっしゃった場合にのみ進める

### 第11次改訂版(岩瀬様のローカル実機検証結果への指摘対応)

- [x] `DROP ROLE`前にカレントDB以外への依存(`pg_shdepend`)を確認する
      `_check_cross_database_role_dependencies`を新設し、依存がある場合
      はDROPを見送ってNOLOGIN化・カレントDB権限撤去のみ行う「安全な
      縮退」へ`_remove_roles_and_rls`を訂正した(0-1章・17-0章)。
      「stagingにはアプリ用DBが1つだけ」とは断定しない設計とした
- [x] 全migration/rollbackスクリプトの冒頭で接続先DB名・ユーザー・
      PostgreSQLバージョン・想定7テーブルの存在を確認し、不一致なら
      DDL開始前に安全停止する`target_identity`モジュールを新設し、
      12章・16章・17章の全`main()`へ組み込んだ(0-1章)
- [x] 上記2件をPostgreSQL 16・18の実機(scratchpad上の使い捨て環境)で
      追加検証し、いずれもPASSした(11章#30〜#32)。手続き事項(監査ZIP
      共有・PostgreSQL配布物の真正性確認)への対応は、本改訂と併せて
      別途チャットで報告する
- [x] 実変更は一切行われていない(検証はすべてscratchpad上の使い捨て
      PostgreSQLインスタンスで実施。stagingへは一切接続していない)

### 第12次改訂版(岩瀬様の監査ZIP実物監査への指摘対応)

- [x] **[Critical]** クロスDB依存でTier 2が縮退しても`[OK]`・終了コード
      0を返していた不具合を修正し、`COMPLETE`(0)/`DEGRADED`(2)/
      `FAILED`(1)を明確に分離した(17-2章)
- [x] **[Critical]** Tier 3が縮退後も関数・制約・ログの削除処理を続行し
      成功扱いできた不具合を修正し、`degraded_roles`が空でなければ以降
      の削除処理へ進まずDEGRADEDで停止するようにした(17-3章)
- [x] `verify_round21_baseline_state()`を新設し、第21回終了時点との
      厳密な一致(テーブル・シーケンス集合の完全一致を含む)を
      assertするようにした。不一致なら`BaselineStateMismatchError`を
      送出し、呼び出し元がROLLBACKする(17-0章)
- [x] `stripe_subscription_id`のNOT NULL復帰確認
      (`information_schema.columns.is_nullable`)を
      `verify_round21_baseline_state()`へ追加した(17-0章)
- [x] `_reverify_and_drop_constraint`を全面訂正し、`schema_migration_log`
      の履歴(このTaskが実際にNOT NULL・UNIQUEを追加したことがあるか)と
      ライブ状態を突き合わせたうえでのみ撤去するようにした。記録欠落・
      矛盾時は一切変更せず安全停止する(17-0章)
- [x] `schema_migration_log`の自動ファイル書き出しを安全性の根拠から
      外し(`_drop_migration_log_table`)、テーブル削除の前提として人間
      による手動エクスポート・保存・ハッシュ確認を示す環境変数を必須の
      事前条件とした(`_precondition_migration_log_manually_archived`、
      17-0章)
- [x] 接続先識別の4環境変数を、未設定/不一致なら停止する必須項目へ
      訂正した(0-1章)
- [x] テストランナーを`run_all_checks.py`(統合版)へ刷新し、全チェック
      をDBへの直接問い合わせによる判定へ統一、1件でも失敗したら終了
      コード1になるようにした
- [x] 上記すべてをPostgreSQL 16・18の実機(scratchpad上の使い捨て環境)
      で計33項目再検証し、全項目PASSした(11章#1〜#43)
- [x] 実変更は一切行われていない(検証はすべてscratchpad上の使い捨て
      PostgreSQLインスタンスで実施。stagingへは一切接続していない)

### 第13次改訂版(岩瀬様の再監査への指摘対応)

- [x] **[Critical・点A]** DEGRADED状態が正規の復旧経路を持たない不具合
      (Tier 2の再実行条件にも旧cleanupスクリプトの開始条件にも合致
      しない)を修正した。`_precondition_ready_to_resume`・
      `_finish_degraded_role_removal`を新設し、
      `rollback_cleanup_after_tier2.py`を`rollback_resume_to_full_
      restore.py`へ一般化して、「Tier 2完了状態」「DEGRADED状態」の
      いずれからも復旧できるようにした(17-4章)
- [x] **点B** staging識別のRailway環境確認を、操作者が両辺を手入力する
      比較(`EXPECTED_TARGET_ENVIRONMENT_ID`/`ACTUAL_TARGET_ENVIRONMENT_
      ID`)から、`railway run`実行時にRailwayが自動注入する
      `RAILWAY_PROJECT_ID`・`RAILWAY_ENVIRONMENT_ID`との比較へ訂正し、
      独立した確認とした(0-1章)
- [x] **点C** UNIQUE制約撤去時の同一性確認へ、12-2章の作成時チェックと
      同じ`convalidated`・`condeferrable`・`condeferred`の確認を追加
      した(17-0章`_reverify_and_drop_constraint`)
- [x] **点D** 監査ログ「保存済み」の確認を、真偽値のみから決定的な
      正規化(`_canonical_migration_log_export`)による行数・SHA-256の
      突き合わせへ強化し、専用の読み取り専用エクスポートツール
      (`scripts/export_and_hash_migration_log.py`)を新設した(17-0章・
      17-3b章)
- [x] **点E** 「DEGRADED→依存解消→resume→第21回状態への完全復帰」の
      完全な復旧経路を、PostgreSQL 16・18の実機で再現・確認した
      (11章#14b・#15b)
- [x] 上記すべてをPostgreSQL 16・18の実機(scratchpad上の使い捨て環境)
      で計45項目再検証し、全項目PASSした(11章#1〜#45)
- [x] 実変更は一切行われていない(検証はすべてscratchpad上の使い捨て
      PostgreSQLインスタンスで実施。stagingへは一切接続していない)
- [x] 岩瀬様の承認待ちで停止している(この提出をもって再度停止)
