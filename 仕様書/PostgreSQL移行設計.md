# PostgreSQL移行 設計（調査・設計フェーズ）

Git×Railway上級マスター応用編 修了後、SaaS本体編 第16回「マルチテナント設計」の前提として、`records.json`（JSONファイル保存）からPostgreSQLへ移行するための設計。**この文書は設計のみで、まだ実装・移行は行っていない。**

---

## ① 移行する理由

現在のJSON保存（`streamlit/data/records.json`）は、[`本番DB運用.md`](./本番DB運用.md)⑥に明記の通り「1人利用を前提とした暫定措置」。

- JSONファイルの読み書きには**排他制御がない**。複数人（複数テナント）が同時に読み書きすると、後から保存した方が他方の変更を消してしまう競合が起こり得る
- 第16回のマルチテナント化は「顧客ごとにデータを分ける」設計であり、ファイル1つに全員分を混在させるJSON方式のままでは安全に実現できない
- 現状は「あきら本人」専用の単一ユーザーMVP（[`仕様書/仕様書.md`](./仕様書.md)）。テナントという概念自体がまだ存在しない

→ **まずPostgreSQLへ移行し、その後に第16回でテナント分離を設計する**、という2段階に分ける（本番DB運用.mdで確立した「一発変換ではなく、なだらかな移行」の方針を踏襲）。

---

## ② Railway PostgreSQLの導入

RailwayにはPostgreSQLのマネージドDBアドオン（「+ Add Database」→「PostgreSQL」）があり、追加すると`DATABASE_URL`が自動的にサービスの環境変数へ注入される。既存の`ANTHROPIC_API_KEY`と同様、コードへ接続情報を直書きしない。

- production・staging、それぞれの環境に**別々のPostgreSQLインスタンス**を追加する（環境分離の原則を維持、[`リリース手順.md`](./リリース手順.md)）
- **料金は専用定額ではなく、CPU・メモリ・ストレージ等の使用量に応じた従量制**。Hobbyプランは月額最低5ドルで、その5ドル分は利用料に充当され、超過分が追加請求される。production・staging両方に常時PostgreSQLを置けば、両方の利用量が発生する（[Railway料金](https://railway.com/pricing)）
- **料金への影響はRailwayの現在のプランとUsage画面を確認するまで未確定**として記録する。確認が済むまで、PostgreSQLサービスはまだ追加しない

---

## ③ テーブル設計（第1段階：現行の単一ユーザーモデルをそのまま移す）

第16回のテナント分離より前に、まず「今のJSONと同じ意味のデータ」をPostgreSQLへ1:1で移す。

```sql
CREATE TABLE records (
    id          BIGSERIAL PRIMARY KEY,
    record_date DATE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX records_record_date_unique ON records (record_date);
```

- `record_date`：`records.json`の`dates`配列の要素（ISO日付文字列）に対応。UNIQUE制約で重複日を防ぐ（現状もPythonの`set`で重複排除されているのと同じ意味）
- `id`・`created_at`は将来の監査・デバッグ用に持たせる（現行JSONには存在しないが、実運用上あると安全性が上がる）
- **テナント列（`tenant_id`等）は、この段階ではまだ追加しない**。第16回で改めて設計し、後方互換な形で追加する（本番DB運用.mdの`schema_version`と同じ「なだらかな移行」の考え方）

---

## ④ アプリ側の設計（db.pyの新設）

`logic.py`（JSON版の読み書き）はそのまま残し、並行して`streamlit/db.py`を新設する。

- `db.load_dates(conn=...) -> set[str]`
- `db.save_date(record_date: str, conn=...) -> None`（1件追加）
- `db.delete_date(record_date: str, conn=...) -> None`（記録取り消し用）
- 接続は`DATABASE_URL`環境変数から取得し、ドライバーは**Psycopg 3（`psycopg[binary]`）を第一候補とする**。新規開発向けとして公式が現行世代として案内しており、Python 3.13にも対応している（[Psycopg公式](https://www.psycopg.org/)）。psycopg2-binaryは採用しない
- `app.py`の切り替えは、`load_dates`/`save_dates`の呼び出し元を`logic`から`db`へ差し替える1箇所のみで完結するように、関数シグネチャ（`set[str]`を受け渡す）を揃える

**JSON側は削除しない。** 移行後もしばらくは`records.json`と自動バックアップの仕組みをそのまま残し、PostgreSQLに問題が起きた場合の参照・切り戻し用に維持する。

### DB→JSON復元手順（切り戻し用）

JSONを「移行時点のまま」残すだけでは、PostgreSQL移行後に追加された記録（切り替え後にユーザーが新たに記録した日）を含めて切り戻すことができない。そのため、PostgreSQLの最新データをJSONへ書き戻してから切り替える「DB→JSON復元手順」を用意する。

- `scripts/restore_json_from_postgres.py`（新規作成予定）：PostgreSQLの`records`テーブルから全`record_date`を取得し、`logic.py`の`save_dates()`と同じ形式（`{"schema_version": 2, "dates": [...]}`）で、`logic._atomic_write()`を使って`records.json`へ原子的に書き戻す
- 実行前に、書き戻し対象の`records.json`を`backup_data()`で退避してから上書きする（既存の`restore_data()`と同じ安全設計を踏襲）
- この手順は、`app.py`をPostgreSQL読み書きからJSON読み書きへ戻す（`db`→`logic`の呼び出し元差し替え）**前に**必ず実行する

---

## ⑤ 移行スクリプトの実行方式（Pre-deploy Commandは使えない、`railway run`も不採用）

RailwayのPre-deploy Commandは、デプロイ完了前に一度だけ実行される仕組みだが、**永続ボリュームへアクセスできない**（[Railway公式ガイド](https://docs.railway.com/guides/nextjs)）。`records.json`はVolumeマウント先（`/app/streamlit/data`）にあるため、Pre-deploy Commandからは読めない。したがって、Pre-deploy Commandを使った自動移行は採用しない。

また、当初検討していた「ローカルPCから`railway run`で`DATABASE_URL`をプロキシしてPostgreSQLへ書き込む」方式は**不採用**とする。RailwayのPostgreSQLは初期状態でRailway内部ネットワーク専用（内部ホスト名、例: `postgres.railway.internal`）であり、`railway run`はローカルへ環境変数を渡すだけでネットワークまでは中継しないため、ローカルPCから内部用`DATABASE_URL`へ接続できない可能性がある。これを回避するために**PostgreSQLのPublic Access（外部公開）を有効化する対応は採用しない**（不要な外部公開を避けるため、[Railway PostgreSQL公式](https://docs.railway.com/databases/postgresql)）。

### 検討した実行方式

| 方式 | Volumeアクセス | 内部DATABASE_URL接続 | 公開Web露出 | Public Access要否 | 採用可否 |
|---|---|---|---|---|---|
| Pre-deploy Command | 不可（Railway仕様） | - | なし | - | 不可（Volume未アクセスのため） |
| アプリ内に移行用エンドポイント/ボタンを追加 | 可（実行中コンテナ内） | 可 | **あり**（公開Webに露出してしまう） | 不要 | 不採用（公開Web画面に移行ボタンは作らない方針） |
| ローカルPCから`railway run`でDATABASE_URLをプロキシ | Volumeは`volume files download`で別途取得 | **不可の可能性**（内部ホスト名は外部から未解決） | なし | **必要**（外部公開したくない） | 不採用 |
| Railway内部の稼働コンテナで`railway ssh`を使い一度だけ実行 | 可（コンテナに元々マウントされているVolumeをそのまま使用） | 可（コンテナは元々内部ネットワーク内） | なし | 不要（Public Access有効化なし） | **採用** |

### 採用する実行方式

- **JSONバックアップ**：`railway volume files --volume <対象環境のVolume名> download /records.json <Gitリポジトリ外のPC側保存先>/records.json`（第14回で実証済みの経路。**読み取り専用の安全確認・バックアップ目的**であり、移行の実行経路ではない）
- **移行スクリプトの配置**：`scripts/migrate_to_postgres.py`として、通常の安全なブランチ運用（`feature/xxx`→staging→PR→レビュー→承認→main）でアプリと一緒にデプロイする。特別な公開エンドポイントは作らない
- **移行の実行**：デプロイ済みのRailway稼働コンテナに対して`railway ssh`（`--environment`・`--service`を明示指定）で接続し、コンテナ内で`python scripts/migrate_to_postgres.py`を一度だけ実行する。コンテナは元々Volume（`/app/streamlit/data`）がマウント済みで、内部用`DATABASE_URL`もそのまま利用できるため、追加の外部接続設定は不要
- **PostgreSQLのPublic Access**：有効化しない（内部ネットワーク接続のみで完結する）
- **公開Web画面に移行ボタンは作らない**。実行はoperator本人のRailway CLI操作（`railway ssh`）に限定する
- **一度だけ実行できる、再実行しても重複しない**設計（⑥参照）にすることで、誤ってもう一度走らせても壊れないようにする
- **environment/serviceの取り違え防止**：`railway ssh`・`railway volume files`いずれのコマンドも、対象の`--environment`（staging/production）と`--service`（web）を毎回明示指定し、実行前に`railway status`で接続先を確認してから実行する

---

## ⑥ 移行スクリプトの冪等性

`scripts/migrate_to_postgres.py`は、③で定義した`record_date`のUNIQUE制約を利用し、次のSQLで書き込む。

```sql
INSERT INTO records (record_date) VALUES (%s)
ON CONFLICT (record_date) DO NOTHING;
```

- 同じ日付を複数回INSERTしようとしても、2回目以降は無視されるだけで、エラーにも重複行にもならない
- スクリプトが途中で失敗して再実行しても、既に書き込み済みの行はそのまま、未書き込みの行だけが追加される（部分的な失敗からの再実行に対して安全）

---

## ⑦ 移行前後の検証（件数だけでなく、日付集合の完全一致を必須とする）

移行後、次の両方を満たさない限り、アプリの読み書き先をPostgreSQLへ切り替えない（`logic`→`db`の呼び出し元差し替えを行わない）。

1. **件数の一致**：JSONの`dates`配列の要素数と、PostgreSQLの`SELECT COUNT(*) FROM records`が一致する
2. **日付集合の完全一致**：JSONの`dates`をPythonの`set`にしたものと、PostgreSQLから取得した`record_date`の`set`が、**要素単位で完全に等しい**（`set1 == set2`）ことを確認する。件数が一致していても、日付の中身がずれている（別の日付が混入している等）ケースを検知するため、件数照合だけでは不十分とする

**照合が一致しない場合は、DB利用へ切り替えない。** 原因を特定し、必要ならPostgreSQL側のデータを一旦クリアしてから移行スクリプトを再実行する。

---

## ⑧ 移行手順（安全確認後に実施。本番DB運用.md⑤のチェックリストに準拠）

1. **バックアップ確認**：現在の`records.json`を`railway volume files download`で、Gitリポジトリ外・operator本人のPCへ外部退避する（既存の`scripts/restore_records.py`と同じ経路）。**保存先ディレクトリとファイル内容（日付件数・中身）を目視確認する**
2. **staging先行検証**：staging環境にPostgreSQLを追加し、`scripts/migrate_to_postgres.py`をブランチ運用でstagingへデプロイしたうえで、`railway ssh --environment staging --service web`でコンテナに接続し、コンテナ内で一度だけ実行する
3. **件数・日付集合の照合**：⑦の2条件を両方満たすことを確認する。満たさなければここで停止し、DB利用へ切り替えない
4. **staging実機確認**：`app.py`をPostgreSQL読み書きに切り替えたstaging環境で、記録・取り消し・連続記録表示・パズル表示が正常に動くことを確認する
5. **CI・テストが緑**：`db.py`用のテスト（`tests/test_db.py`、要新規作成。冪等性・ON CONFLICTの検証を含む）を含め、全テストPASSを確認する
6. **本番反映**：`main`へのマージ経由でのみ反映する（`app.py`の切り替え・`migrate_to_postgres.py`ともに通常のPRフローに乗せる）
7. **本番実行前の再確認**：production用の`records.json`を改めて`railway volume files download`し、**保存先とファイル内容を確認してから**production環境にPostgreSQLを追加し、`railway ssh --environment production --service web`でコンテナ内実行する
8. **件数・日付集合の再照合**：⑦の2条件をproductionでも満たすことを確認する。満たさなければDB利用へ切り替えず、原因調査を優先する
9. **反映後の実機確認**：production環境で、移行前の記録（2026-08-28〜の連続記録2日）が保持されていることを確認する
10. **移行後もJSONバックアップの仕組みは残し、しばらく並行運用してから、JSON側を段階的に廃止するかは別途判断する**。切り戻しが必要になった場合は、④の「DB→JSON復元手順」を先に実行してからJSON読み書きへ戻す

**productionへ直接、検証なしでは移行しない。**

---

## ⑨ 未確定事項（着手前に決めること）

- [x] ドライバー：Psycopg 3（`psycopg[binary]`）に決定
- [ ] Railway PostgreSQLアドオンの料金・現在のプランへの影響（Railwayの現在のプランとUsage画面を確認するまで未確定。**確認が済むまでPostgreSQLサービスは追加しない**）
- [ ] `scripts/migrate_to_postgres.py`・`scripts/restore_json_from_postgres.py`の実装（本設計の承認後に着手）
- [ ] JSON側をいつ・どう廃止するか（当面は残す前提で進める）

---

## ⑩ 推奨する移行実行方式と操作順（サマリー）

- **ドライバー**: Psycopg 3（`psycopg[binary]`）
- **JSONバックアップ**: `railway volume files download`でローカルPCへ取得（読み取り専用、実証済みの経路。移行の実行経路とは別の、安全確認のための操作）
- **移行スクリプトの配置**: `scripts/migrate_to_postgres.py`を、通常の安全なブランチ運用（`feature/xxx`→staging→PR→レビュー→承認→main）でアプリと一緒にデプロイする
- **移行の実行**: Railway内部の稼働コンテナに対して`railway ssh`（`--environment`・`--service`を明示指定）で接続し、コンテナ内で一度だけ実行する。ローカルPCからの`railway run`は使わない
- **PostgreSQL接続**: 内部用`DATABASE_URL`（コンテナは元々Railway内部ネットワーク内にあるためそのまま接続できる）
- **PostgreSQLのPublic Access**: 有効にしない
- **公開移行ボタン**: 作らない
- **冪等性**: `record_date` UNIQUE制約 + `ON CONFLICT DO NOTHING`。再実行しても安全
- **切り替え条件**: 件数一致 **かつ** 日付集合の完全一致（`set`同士の比較）。どちらか一方でも不一致ならDB利用へ切り替えない
- **environment/serviceの明示**: `railway volume files`・`railway ssh`いずれも`--environment`（staging/production）・`--service`（web）を毎回明示指定し、`railway status`で接続先を確認してから実行する。staging/production取り違え防止
- **切り戻し**: JSONを移行時点のまま残すだけでなく、切り戻す際は先に`scripts/restore_json_from_postgres.py`でPostgreSQLの最新データをJSONへ書き戻してから、アプリの読み書き先をJSONへ戻す

### staging用の操作順

1. Railwayの現在のプランとUsage画面を確認し、PostgreSQL追加による料金影響を把握する（未確認の間はPostgreSQLサービスを追加しない）
2. 確認でき、着手の判断ができたら、**staging環境**にPostgreSQLアドオンを追加する（Public Accessは有効にしない）
3. `scripts/migrate_to_postgres.py`・`scripts/restore_json_from_postgres.py`・`streamlit/db.py`・`tests/test_db.py`を実装し、`feature/xxx`→staging反映でstaging環境へデプロイする（本設計の承認後に着手）
4. `railway status`でstaging環境に接続していることを確認したうえで、`railway volume files --environment staging --service web download /records.json <ローカル保存先>`でJSONをバックアップし、保存先とファイル内容を確認する
5. `railway ssh --environment staging --service web`でコンテナへ接続し、`python scripts/migrate_to_postgres.py`を一度だけ実行する
6. 件数・日付集合の完全一致を確認する。不一致ならここで停止し、原因調査を行う（PostgreSQL側データのクリア→再実行も検討）
7. `app.py`をPostgreSQL読み書きに切り替えたstaging環境で、記録・取り消し・連続記録表示・パズル表示が正常に動くことを確認する
8. `pytest tests/ -v`で全テストPASSを確認する

### production用の操作順（stagingでの検証完了後のみ）

1. 通常のPRフロー（staging→main、ChatGPTレビュー→承認）で、アプリの切り替えと移行スクリプトをmainへ反映する
2. `railway status`で**production環境**に接続していることを確認したうえで、`railway volume files --environment production --service web download /records.json <ローカル保存先>`でJSONをバックアップし、**保存先とファイル内容を必ず目視確認する**
3. production環境にPostgreSQLアドオンを追加する（Public Accessは有効にしない）
4. `railway ssh --environment production --service web`でコンテナへ接続し、`python scripts/migrate_to_postgres.py`を一度だけ実行する
5. 件数・日付集合の完全一致を確認する。不一致ならDB利用へ切り替えず、原因調査を最優先する
6. production画面で、移行前の記録（2026-08-28〜の連続記録2日）が保持されていることを実機確認する
7. 問題がなければ移行完了とし、JSONバックアップの仕組みはしばらく並行運用する

この設計内容について、ご確認・ご指示をお願いします。ここで停止し、実装・Railway設定変更・commit・push・PR・mergeは行いません。

---

## 関連

- 本番DB運用の安全設計・チェックリスト：[`本番DB運用.md`](./本番DB運用.md)
- 障害発生時の対応：[`障害復旧手順書.md`](./障害復旧手順書.md)
- 全体の型：[`開発運用の型.md`](./開発運用の型.md)
