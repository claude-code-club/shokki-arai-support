import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

from logic import (
    backup_data,
    build_month_progress,
    calc_best_streak,
    calc_current_streak,
    list_backups,
    load_dates,
    restore_data,
    save_dates,
)


def test_calc_current_streak_counts_consecutive_days_ending_today():
    dates = {"2026-08-20", "2026-08-21", "2026-08-22"}
    assert calc_current_streak(dates, today=date(2026, 8, 22)) == 3


def test_calc_current_streak_zero_when_today_missing():
    dates = {"2026-08-20", "2026-08-21"}
    assert calc_current_streak(dates, today=date(2026, 8, 22)) == 0


def test_calc_current_streak_stops_at_gap():
    dates = {"2026-08-19", "2026-08-21", "2026-08-22"}
    assert calc_current_streak(dates, today=date(2026, 8, 22)) == 2


def test_calc_best_streak_empty():
    assert calc_best_streak(set()) == 0


def test_calc_best_streak_finds_longest_run():
    dates = {"2026-08-01", "2026-08-02", "2026-08-03", "2026-08-10", "2026-08-11"}
    assert calc_best_streak(dates) == 3


def test_build_month_progress_counts_filled_days():
    dates = {"2026-08-01", "2026-08-15", "2026-08-31"}
    days_in_month, filled = build_month_progress(dates, 2026, 8)
    assert days_in_month == 31
    assert filled == [1, 15, 31]


def test_load_save_dates_roundtrip(tmp_path):
    data_file = tmp_path / "records.json"
    save_dates({"2026-08-01", "2026-08-02"}, data_file=data_file)
    assert load_dates(data_file=data_file) == {"2026-08-01", "2026-08-02"}


def test_load_dates_missing_file_returns_empty_set(tmp_path):
    data_file = tmp_path / "does_not_exist.json"
    assert load_dates(data_file=data_file) == set()


def test_load_dates_reads_pre_migration_bare_array_format(tmp_path):
    # schema_version導入(第14回)前に書き出された、素の配列形式のファイル
    data_file = tmp_path / "records.json"
    data_file.write_text('["2026-08-01", "2026-08-02"]', encoding="utf-8")
    assert load_dates(data_file=data_file) == {"2026-08-01", "2026-08-02"}


def test_save_dates_writes_versioned_schema(tmp_path):
    data_file = tmp_path / "records.json"
    save_dates({"2026-08-01"}, data_file=data_file)
    saved = json.loads(data_file.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2
    assert saved["dates"] == ["2026-08-01"]


def test_save_dates_upgrades_pre_migration_file_on_next_save(tmp_path):
    data_file = tmp_path / "records.json"
    data_file.write_text('["2026-08-01"]', encoding="utf-8")
    save_dates({"2026-08-01", "2026-08-02"}, data_file=data_file)
    saved = json.loads(data_file.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2
    assert load_dates(data_file=data_file) == {"2026-08-01", "2026-08-02"}


def test_save_dates_creates_backup_of_previous_version(tmp_path):
    data_file = tmp_path / "records.json"
    backup_dir = tmp_path / "backups"
    save_dates({"2026-08-01"}, data_file=data_file, backup_dir=backup_dir)
    assert list_backups(data_file=data_file, backup_dir=backup_dir) == []

    save_dates({"2026-08-01", "2026-08-02"}, data_file=data_file, backup_dir=backup_dir)
    backups = list_backups(data_file=data_file, backup_dir=backup_dir)
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["dates"] == ["2026-08-01"]


def test_backup_data_prunes_old_backups_beyond_keep_limit(tmp_path):
    data_file = tmp_path / "records.json"
    backup_dir = tmp_path / "backups"
    for i in range(10):
        save_dates({f"2026-08-{i + 1:02d}"}, data_file=data_file, backup_dir=backup_dir)
    backups = list_backups(data_file=data_file, backup_dir=backup_dir)
    assert len(backups) == 7


def test_restore_data_overwrites_current_file_with_backup_contents(tmp_path):
    data_file = tmp_path / "records.json"
    backup_dir = tmp_path / "backups"
    save_dates({"2026-08-01"}, data_file=data_file, backup_dir=backup_dir)
    save_dates({"2026-08-01", "2026-08-02"}, data_file=data_file, backup_dir=backup_dir)

    backups = list_backups(data_file=data_file, backup_dir=backup_dir)
    restored = restore_data(backups[0], data_file=data_file)

    assert restored == {"2026-08-01"}
    assert load_dates(data_file=data_file) == {"2026-08-01"}


def test_backup_data_returns_none_when_no_existing_file(tmp_path):
    data_file = tmp_path / "records.json"
    assert backup_data(data_file=data_file) is None
