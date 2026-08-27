"""記録データの読み書きと集計ロジック。

app.py（Streamlit UI）から分離し、UIを起動せずに単体テストできるようにする。
"""

import calendar
import json
from datetime import date, timedelta, timezone, datetime
from pathlib import Path

DATA_FILE = Path(__file__).parent / "data" / "records.json"
JST = timezone(timedelta(hours=9))


def today_jst():
    # Streamlit Community Cloud等、サーバーがUTCで動く環境でも
    # 日本時間の「今日」がずれないようにする
    return datetime.now(JST).date()


def load_dates(data_file=DATA_FILE):
    if not data_file.exists():
        return set()
    try:
        return set(json.loads(data_file.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return set()


def save_dates(dates, data_file=DATA_FILE):
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text(
        json.dumps(sorted(dates), ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
