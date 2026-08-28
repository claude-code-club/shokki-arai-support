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

現在利用中のLimited Trial環境では、RailwayのVolume Backup機能を画面上で確認できなかった。そのため今回の運用では、純正のVolume Backupには依存せず、③のアプリ内バックアップとPCへの外部退避を用意する。

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

## ④ 対応3：バックアップ・復元（安全設計版）

Railway純正のBackups機能が使えないため、アプリ側に軽量なバックアップ機構を実装した。ChatGPTレビューで、ファイル破損(文字コード破損を含む)・書き込み途中断・Volume単体障害・`save_dates()`単体での破損上書きへの耐性が不足していると指摘され、以下の設計に修正した。

### バイト単位での原子的な書き込み（`_atomic_write_bytes` / `_atomic_write`）

`records.json`もバックアップファイルも、直接上書きしない。**すべてバイト列(`bytes`)で読み書きする**(UTF-8として不正なファイルもそのまま扱えるようにするため)。

1. 同一ディレクトリ内に一意な一時ファイル(`records.json.<一意な文字列>.tmp`)を作成し、バイト列を書き込む
2. `flush`・`fsync`でOSバッファからディスクへ確実に反映
3. `os.replace()`で本体ファイルへ原子的に置き換え(置換は成功か失敗のどちらかにしかならず、中途半端な状態を作らない)
4. 置換後、親ディレクトリも`fsync`し、Linux環境でのディレクトリエントリの耐久性を上げる
5. 途中で失敗した場合は一時ファイルを削除し、本体ファイルには一切触れない

JSON文字列を書く`_atomic_write(data_file, text)`は、`text.encode("utf-8")`したうえで`_atomic_write_bytes`を呼ぶ薄いラッパーにした。

### 破損検知（`RecordsFileCorruptedError`）

読み込み時、次の順で検証する。**UTF-8として読めるかどうかも検証対象**にした(以前はここが未検証で、無効なバイト列があると通常の`UnicodeDecodeError`でアプリが落ちていた)。

1. バイト列がUTF-8として読めるか
2. JSON構文として正しいか
3. 構造が正しいか
   - 旧形式は配列であること
   - 新形式はオブジェクトであり、`schema_version`が対応済みの値(現在は`2`のみ)であること
   - `dates`が配列であること
   - 各要素が文字列で、有効なISO日付であること

いずれかを満たさない場合は`RecordsFileCorruptedError`を送出し、**空データとして扱わない**。`streamlit/app.py`はこの例外を受け取ると、エラー表示のうえ`st.stop()`で記録・表示処理をすべて停止する。壊れたファイルを「0件」と誤解して上書きする事故を防ぐ。

### バックアップの仕組み（生バイトで退避）

- `save_dates()`が呼ばれるたびに、上書き前の`records.json`を`streamlit/data/backups/records_<タイムスタンプ>.json`として自動保存する(`backup_data()`)
- バックアップは`data_file.read_bytes()`で読んだ**生バイトをそのまま**(検証せず)退避する。UTF-8として壊れたファイルでも退避できる
- 直近**7世代**のみ保持し、古いものは自動的に削除する（無限に増え続けない）
- `list_backups()`で新しい順のバックアップ一覧を取得できる

**限界**:
- 「7世代」は**「7日分」ではなく、直近7回の保存操作分**である。短時間に何度も保存すると、古い世代は早く消える
- このバックアップは`records.json`と**同じVolume内**にあるため、Volume自体の障害・誤削除には対応できない。外部退避(後述)は、少なくとも本番変更前には必ず行い、通常運用でも定期的にPCへ保存する

### `save_dates()`自身も既存ファイルの破損を拒否する

`load_dates()`を先に呼んでいるかどうか(UIの呼び出し順)に安全性を依存させないため、`save_dates()`自体が保存前に次を行う。

1. 既存の`records.json`をバックアップへ退避する(壊れていても生バイトのまま)
2. 退避した内容を検証する。壊れていれば`RecordsFileCorruptedError`を送出し、**ここで処理を止める(まだ上書きしていない)**
3. 検証を通過した場合のみ、新しい内容で原子的に上書きする

### 復元（`restore_data`）— 安全処理は関数自身が持つ

`restore_data(backup_file)`は、呼び出し側に安全確認を委ねず、関数自体が以下をすべて行う。

1. 復元元バックアップの内容を検証する(UTF-8破損・JSON構文・構造のいずれもここで検知し、壊れたバックアップからは復元しない。検証に失敗したら`RecordsFileCorruptedError`を送出して**何も変更しない**)
2. 検証を通過したら、現在の`records.json`を生バイトのまま復元前バックアップとして退避する(復元自体もやり直せるようにする)
3. 原子的書き込みで`records.json`を置き換える(`os.replace`が失敗した場合も、`records.json`は変更されない)

将来、他の場所からこの関数を呼んでも同じ安全性が保たれる。

### 同一Volume外への退避（外部バックアップ）

Volume自体の障害・誤削除に備え、Railway CLIでボリューム内のファイルをoperatorのPCへダウンロードする手順を用いる。認証のない公開アプリにダウンロード用UIは追加しない。

### 復元手順（staging限定の保守スクリプト）

`main`のようなアプリの公開画面には復元UIを置かない(未認証の一般利用者が記録を操作できてしまうため)。復元は`scripts/restore_records.py`をoperatorのローカル環境で実行する、staging限定の保守作業として行う。`restore_data()`が送出する`RecordsFileCorruptedError`に加え、ファイルI/O由来の`OSError`もスクリプト側で捕捉し、対象ファイルを変更せずに分かるメッセージと終了コード`1`を返す。

1. `railway status`で、CLIの接続先プロジェクト・環境が`staging`であることを確認する
2. `railway volume list`で、対象Volumeの名前・IDを確認する
3. `railway volume files --volume <stagingのVolume名またはID> list /`で、実際のリモートパスを確認する(マウント先が`/app/streamlit/data`でも、CLI上はVolumeルート基準の表記になる場合があるため、書き込み前に必ず確認する。以降のパスは、この`list`結果に従う)
4. 確認したパスに沿って、`records.json`と`backups/`をローカルへ取得する
   ```bash
   railway volume files --volume <stagingのVolume名またはID> download /records.json <PC側の保存先>
   railway volume files --volume <stagingのVolume名またはID> download /backups <PC側の保存先>
   ```
5. ダウンロードが成功し、内容が読めることを確認する
6. ローカルで`python scripts/restore_records.py <records.jsonのパス> <復元したいバックアップのパス>`を実行する
7. 復元後の形式・記録件数を確認する
8. **staging環境だけに**アップロードする(`--overwrite`で上書き)
   ```bash
   railway volume files --volume <stagingのVolume名またはID> upload <PC側のrecords.json> /records.json --overwrite
   ```
9. staging画面をリロードし、記録が意図した状態に戻っていることを確認する

**productionに対しては復元実験を行わない。**

### CLI認証時の安全注意

- 認証コード・トークンはチャットへ貼らない
- 実行前に、stagingの環境名・Volume名・IDを毎回確認する
- productionでは復元実験をしない
- 作業後は`railway logout`で認証を終了する
- 必要に応じて、Railway側(GitHub/アカウント設定)のCLI認可も取り消す

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

## ⑥ このJSON方式の位置づけ（次段階への制約）

このJSON+永続Volume+アプリ内バックアップの方式は、**当面1人利用を前提とした暫定措置**である。以下に該当する前に、PostgreSQL等の実データベースへ移行する。

- 複数人が同時に読み書きする運用になる前（JSONファイルの単純な読み書きには排他制御がなく、同時書き込みで競合する可能性がある）
- 第16回で想定するマルチテナント化に着手する前

今回作った`schema_version`・バックアップ・復元の仕組みや、staging先行検証の運用手順は、PostgreSQL移行時にも安全な移行の型として引き継ぐ。

---

## 関連

- Railwayの設定変更（Wait for CI）や本番デプロイの流れは [`CI-CD流れ図.md`](./CI-CD流れ図.md) を参照
- コードのやらかし復旧（reset/revert/reflog/stash）は [`トラブル復旧ガイド.md`](./トラブル復旧ガイド.md) を参照
