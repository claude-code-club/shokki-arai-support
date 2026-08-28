"""記録データの読み書きと集計ロジック。

app.py（Streamlit UI）から分離し、UIを起動せずに単体テストできるようにする。
"""

import calendar
import json
import os
import tempfile
from datetime import date, timedelta, timezone, datetime
from pathlib import Path

DATA_FILE = Path(__file__).parent / "data" / "records.json"
JST = timezone(timedelta(hours=9))

# 第14回でrecords.jsonにschema_versionを導入。
# 導入前(素の配列)のファイルも読み込めるよう、load_datesは両形式に対応する。
SUPPORTED_SCHEMA_VERSIONS = (2,)
SCHEMA_VERSION = 2
BACKUP_KEEP = 7


class RecordsFileCorruptedError(Exception):
    """records.json（またはバックアップファイル）の内容が壊れている場合に送出される。"""


def today_jst():
    # Streamlit Community Cloud等、サーバーがUTCで動く環境でも
    # 日本時間の「今日」がずれないようにする
    return datetime.now(JST).date()


def _validate_records_structure(raw):
    """JSONとしてパースできても、records.jsonとして構造が不正なら例外を送出する。"""
    if isinstance(raw, list):
        dates = raw  # 旧形式(schema_version導入前の素の配列)
    elif isinstance(raw, dict):
        version = raw.get("schema_version")
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise RecordsFileCorruptedError(f"未対応のschema_versionです: {version!r}")
        dates = raw.get("dates")
        if not isinstance(dates, list):
            raise RecordsFileCorruptedError("'dates'が配列ではありません")
    else:
        raise RecordsFileCorruptedError(
            f"records.jsonの最上位型が不正です: {type(raw).__name__}"
        )

    for d in dates:
        if not isinstance(d, str):
            raise RecordsFileCorruptedError(f"日付が文字列ではありません: {d!r}")
        try:
            date.fromisoformat(d)
        except ValueError as e:
            raise RecordsFileCorruptedError(f"不正な日付形式です: {d!r}") from e

    return set(dates)


def _parse_records_text(text):
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise RecordsFileCorruptedError(f"JSONとして解析できません: {e}") from e
    return _validate_records_structure(raw)


def load_dates(data_file=DATA_FILE):
    if not data_file.exists():
        return set()
    try:
        text = data_file.read_text(encoding="utf-8")
    except OSError as e:
        raise RecordsFileCorruptedError(f"{data_file}を読み込めません: {e}") from e
    return _parse_records_text(text)


def _fsync_dir(dir_path):
    # ディレクトリエントリの変更(rename)自体もfsyncし、Linux環境での耐久性を高める。
    # 対応していない環境(Windows等)では何もせず無視する。
    try:
        fd = os.open(dir_path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(data_file, text):
    """一意な一時ファイルへ書き込み、flush・fsync後にos.replaceで置き換える。

    途中で失敗した場合はdata_fileを一切変更せず、一時ファイルを削除する。
    """
    data_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=data_file.parent, prefix=f"{data_file.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, data_file)
        _fsync_dir(data_file.parent)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def save_dates(dates, data_file=DATA_FILE, backup_dir=None):
    if data_file.exists():
        backup_data(data_file=data_file, backup_dir=backup_dir)
    payload = json.dumps(
        {"schema_version": SCHEMA_VERSION, "dates": sorted(dates)},
        ensure_ascii=False,
        indent=2,
    )
    _atomic_write(data_file, payload)


def backup_data(data_file=DATA_FILE, backup_dir=None, keep=BACKUP_KEEP):
    """更新前のrecords.jsonを退避する。直近keep世代のみ残し、古いものは削除する。

    壊れたファイルであっても、内容をそのまま(検証せず)退避する。
    """
    if not data_file.exists():
        return None
    backup_dir = backup_dir or (data_file.parent / "backups")
    timestamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S_%f")
    backup_file = backup_dir / f"records_{timestamp}.json"
    _atomic_write(backup_file, data_file.read_text(encoding="utf-8"))

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


def restore_data(backup_file, data_file=DATA_FILE, backup_dir=None):
    """指定したバックアップの内容でrecords.jsonを復元し、復元後の記録日を返す。

    この関数自体が安全処理を持つ:
    - 復元元の内容を構造検証する(壊れたバックアップからは復元しない)
    - 復元前の現在データをバックアップへ退避する
    - 一意な一時ファイル経由でatomicに置換する(失敗時はrecords.jsonを変更しない)
    """
    backup_file = Path(backup_file)
    text = backup_file.read_text(encoding="utf-8")
    dates = _parse_records_text(text)  # 壊れたバックアップならここで例外

    if data_file.exists():
        backup_data(data_file=data_file, backup_dir=backup_dir)

    _atomic_write(data_file, text)
    return dates


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
