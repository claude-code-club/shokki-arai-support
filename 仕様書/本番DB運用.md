# 本番DBの運用（第14回）

Git×Railway上級マスター応用編 第14回「データを壊さず育てる」のまとめ。

---

## ① 発覚した問題：過去の記録データが消えていた

第14回に着手する直前、本番の記録が「累計記録日数: 0日」になっていることに気付いた。調査の結果、原因は以下と判明した。

- `records.json`の保存先(`streamlit/data/`)は`.gitignore`対象で、Gitでは管理していない
- Railwayの`web`サービスに**永続ボリュームが接続されていなかった**
- そのため、PRマージのたびにコンテナが作り直され、`records.json`もリセットされていた

**残念ながら、この時点で失われた過去の記録は復元できない**(バックアップが存在しなかったため)。今回の対応は「今後同じ理由でデータが消えないようにする」ことが目的。

---

## ② 対応1：永続ボリュームの接続（データが消えない土台）

Railwayの`production`・`staging`両環境の`web`サービスに、永続ボリュームを接続した。

| 項目 | 内容 |
|---|---|
| マウント先 | `/app/streamlit/data` |
| 根拠 | ビルドログで`copy / /app`を確認。Procfileの起動コマンド`streamlit run streamlit/app.py`から、`streamlit/app.py`は`/app/streamlit/app.py`、データファイルは`/app/streamlit/data/records.json`になると特定 |
| 検証 | production環境で「今日、洗いました！」を記録→手動Redeploy実行→記録(連続記録1日)が消えずに残っていることを確認 |

**Railway純正のBackups機能(PITR)はProプラン限定で、現在のTrialプランでは使用不可**。そのため③のアプリ側バックアップで代替する。

---

## ③ 対応2：マイグレーション（`records.json`にschema_versionを導入）

`streamlit/logic.py`の`load_dates`/`save_dates`を変更し、保存形式に`schema_version`を持たせた。

**Before（導入前・素の配列）**
```json
["2026-08-01", "2026-08-02"]
```

**After（schema_version 2）**
```json
{
  "schema_version": 2,
  "dates": ["2026-08-01", "2026-08-02"]
}
```

### 安全に変更するための工夫

- `load_dates`は**両方の形式を読める**（後方互換）。旧形式のファイルが残っていても壊れない
- `save_dates`は**必ず新形式で書き出す**。旧形式のファイルを一度保存すると、自動的に新形式へアップグレードされる
- 「一括変換スクリプト」のような一発勝負の操作を行わず、通常の保存動作の延長でなだらかに移行する設計にした
- `tests/test_logic.py`に、①旧形式が読めること②新規保存が新形式になること③旧形式ファイルが次回保存でアップグレードされること、の3点をテストとして追加
- まず`staging`環境で動作確認してから、`main`経由で`production`へ反映する

---

## ④ 対応3：バックアップ・復元

Railway純正のBackups機能が使えないため、アプリ側に軽量なバックアップ機構を実装した。

### 仕組み（`streamlit/logic.py`）

- `save_dates()`が呼ばれるたびに、上書き前の`records.json`を`streamlit/data/backups/records_<タイムスタンプ>.json`として自動保存する(`backup_data()`)
- 直近**7世代**のみ保持し、古いものは自動的に削除する（無限に増え続けない）
- `list_backups()`で新しい順のバックアップ一覧を取得できる
- `restore_data(backup_file)`で、指定したバックアップの内容を`records.json`に復元できる

### 手動復元の手順

1. Railwayの対象サービス(`production`または`staging`)のConsoleタブ、または開発機のローカル環境で、`streamlit/data/backups/`配下のファイル一覧を確認する
2. 復元したい時点のバックアップファイルを選ぶ(ファイル名の日時が目安)
3. `restore_data("streamlit/data/backups/records_<選んだ日時>.json")`を実行する
4. アプリを再読み込みし、記録内容が意図した状態に戻っていることを確認する

---

## ⑤ 本番DB変更の安全な段取りチェックリスト

本番の`records.json`（またはRailwayの設定）に変更を加える前に、以下を上から順に確認する。

- [ ] **バックアップの存在確認**：直近のバックアップ(`list_backups()`)が想定通りに残っているか
- [ ] **staging先行検証**：変更は必ず`staging`環境で先に試し、想定通り動くことを確認する
- [ ] **後方互換の確認**：新しい形式は、古い形式のデータも壊さず読めるか（一発変換ではなく、なだらかな移行になっているか）
- [ ] **CI・テストが緑**：`tests/test_logic.py`が全件PASSしているか
- [ ] **本番反映**：`main`へのマージ経由でのみ反映する（直接の本番操作はしない）
- [ ] **反映後の実機確認**：本番画面で記録・連続記録・既存機能が壊れていないかを確認する

---

## 関連

- Railwayの設定変更（Wait for CI）や本番デプロイの流れは [`CI-CD流れ図.md`](./CI-CD流れ図.md) を参照
- コードのやらかし復旧（reset/revert/reflog/stash）は [`トラブル復旧ガイド.md`](./トラブル復旧ガイド.md) を参照
