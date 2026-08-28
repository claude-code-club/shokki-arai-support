"""staging限定の保守スクリプト。ローカルへ退避したrecords.jsonをバックアップから復元する。

使い方:
    python scripts/restore_records.py <records.jsonのパス> <復元したいバックアップファイルのパス>

安全処理(構造検証・復元前バックアップ・原子的置換)はlogic.restore_data()自身が行う。
このスクリプトはネットワークやRailway APIには一切アクセスしない。事前に
`railway volume files download` でファイルを取得し、復元後は `railway volume files upload`
でstagingへ書き戻す運用を前提とする。productionに対しては使用しない。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

from logic import RecordsFileCorruptedError, restore_data  # noqa: E402


def main(argv):
    if len(argv) != 3:
        print("使い方: python scripts/restore_records.py <records.jsonのパス> <バックアップファイルのパス>")
        return 1

    data_file = Path(argv[1])
    backup_file = Path(argv[2])

    if not backup_file.exists():
        print(f"バックアップファイルが見つかりません: {backup_file}")
        return 1

    backup_dir = data_file.parent / "backups"

    try:
        restored = restore_data(backup_file, data_file=data_file, backup_dir=backup_dir)
    except RecordsFileCorruptedError as e:
        print(f"復元を中止しました。指定したバックアップの内容が不正です: {e}")
        return 1

    print(f"復元しました: {data_file}")
    print(f"復元後の記録件数: {len(restored)}")
    print(f"復元前の状態は {backup_dir} に退避されています(list_backups()で確認可能)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
