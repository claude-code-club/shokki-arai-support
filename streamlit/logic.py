"""記録データの読み書きと集計ロジック。

app.py（Streamlit UI）から分離し、UIを起動せずに単体テストできるようにする。
"""

import calendar
import json
from datetime import date, timedelta, timezone, datetime
from pathlib import Path

DATA_FILE = Path(__file__).parent / "data" / "records.json"
JST = timezone(timedelta(hours=9))

# 第14回でrecords.jsonにschema_versionを導入。
# 導入前(素の配列)のファイルも読み込めるよう、load_datesは両形式に対応する。
SCHEMA_VERSION = 2
BACKUP_KEEP = 7


def today_jst():
    # Streamlit Community Cloud等、サーバーがUTCで動く環境でも
    # 日本時間の「今日」がずれないようにする
    return datetime.now(JST).date()


def load_dates(data_file=DATA_FILE):
    if not data_file.exists():
        return set()
    try:
        raw = json.loads(data_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if isinstance(raw, dict):
        return set(raw.get("dates", []))
    return set(raw)


def save_dates(dates, data_file=DATA_FILE, backup_dir=None):
    data_file.parent.mkdir(parents=True, exist_ok=True)
    if data_file.exists():
        backup_data(data_file=data_file, backup_dir=backup_dir)
    data_file.write_text(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "dates": sorted(dates)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def backup_data(data_file=DATA_FILE, backup_dir=None, keep=BACKUP_KEEP):
    """更新前のrecords.jsonを退避する。直近keep世代のみ残し、古いものは削除する。"""
    if not data_file.exists():
        return None
    backup_dir = backup_dir or (data_file.parent / "backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S_%f")
    backup_file = backup_dir / f"records_{timestamp}.json"
    backup_file.write_text(data_file.read_text(encoding="utf-8"), encoding="utf-8")

    backups = sorted(backup_dir.glob("records_*.json"))
    for old in backups[:-keep]:
        old.unlink()
    return backup_file


def list_backups(data_file=DATA_FILE, backup_dir=None):
    """新しい順のバックアップファイル一覧を返す。"""
    backup_dir = backup_dir or (data_file.parent / "backups")
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob("records_*.json"), reverse=True)


def restore_data(backup_file, data_file=DATA_FILE):
    """指定したバックアップの内容でrecords.jsonを復元し、復元後の記録日を返す。"""
    backup_file = Path(backup_file)
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text(backup_file.read_text(encoding="utf-8"), encoding="utf-8")
    return load_dates(data_file=data_file)


def calc_current_streak(dates, today=None):
    streak = 0
    day = today or today_jst()
    while day.isoformat() in dates:
        streak += 1
        day -= timedelta(days=1)
    return streak


def calc_best_streak(dates):
    if not dates:
        return 0
    sorted_dates = sorted(date.fromisoformat(d) for d in dates)
    best = 1
    current = 1
    for prev, curr in zip(sorted_dates, sorted_dates[1:]):
        if (curr - prev).days == 1:
            current += 1
            best = max(best, current)
        else:
            current = 1
    return best


def build_month_progress(dates, year, month):
    days_in_month = calendar.monthrange(year, month)[1]
    filled = [
        day for day in range(1, days_in_month + 1)
        if date(year, month, day).isoformat() in dates
    ]
    return days_in_month, filled
