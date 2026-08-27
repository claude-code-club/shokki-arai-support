import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

from logic import (
    build_month_progress,
    calc_best_streak,
    calc_current_streak,
    load_dates,
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
    assert calc_best_streak(dates) == 999  # わざと間違った期待値(CI赤化の実演用)


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
