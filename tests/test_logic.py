import json
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streamlit"))

import logic  # noqa: E402
from logic import (  # noqa: E402
    RecordsFileCorruptedError,
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


def test_load_dates_raises_on_syntax_error(tmp_path):
    data_file = tmp_path / "records.json"
    data_file.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(RecordsFileCorruptedError):
        load_dates(data_file=data_file)


def test_load_dates_raises_on_unknown_schema_version(tmp_path):
    data_file = tmp_path / "records.json"
    data_file.write_text(
        json.dumps({"schema_version": 99, "dates": []}), encoding="utf-8"
    )
    with pytest.raises(RecordsFileCorruptedError):
        load_dates(data_file=data_file)


def test_load_dates_raises_when_dates_is_not_a_list(tmp_path):
    data_file = tmp_path / "records.json"
    data_file.write_text(
        json.dumps({"schema_version": 2, "dates": "2026-08-01"}), encoding="utf-8"
    )
    with pytest.raises(RecordsFileCorruptedError):
        load_dates(data_file=data_file)


def test_load_dates_raises_on_invalid_date_string(tmp_path):
    data_file = tmp_path / "records.json"
    data_file.write_text(
        json.dumps({"schema_version": 2, "dates": ["not-a-date"]}), encoding="utf-8"
    )
    with pytest.raises(RecordsFileCorruptedError):
        load_dates(data_file=data_file)


def test_save_dates_leaves_original_untouched_when_atomic_replace_fails(
    tmp_path, monkeypatch
):
    data_file = tmp_path / "records.json"
    save_dates({"2026-08-01"}, data_file=data_file)
    original_text = data_file.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("simulated failure")

    monkeypatch.setattr(logic.os, "replace", boom)

    with pytest.raises(OSError):
        save_dates({"2026-08-01", "2026-08-02"}, data_file=data_file)

    assert data_file.read_text(encoding="utf-8") == original_text
    assert list(data_file.parent.glob("*.tmp")) == []


def test_save_dates_no_stray_tmp_files_after_success(tmp_path):
    data_file = tmp_path / "records.json"
    save_dates({"2026-08-01"}, data_file=data_file)
    save_dates({"2026-08-01", "2026-08-02"}, data_file=data_file)
    assert list(data_file.parent.glob("*.tmp")) == []


def test_restore_data_creates_pre_restore_backup(tmp_path):
    data_file = tmp_path / "records.json"
    backup_dir = tmp_path / "backups"
    save_dates({"2026-08-01"}, data_file=data_file, backup_dir=backup_dir)
    save_dates({"2026-08-01", "2026-08-02"}, data_file=data_file, backup_dir=backup_dir)

    target_backup = list_backups(data_file=data_file, backup_dir=backup_dir)[0]
    before_count = len(list_backups(data_file=data_file, backup_dir=backup_dir))

    restore_data(target_backup, data_file=data_file, backup_dir=backup_dir)

    after_count = len(list_backups(data_file=data_file, backup_dir=backup_dir))
    assert after_count == before_count + 1  # 復元前の状態も新たにバックアップされている


def test_restore_data_rejects_corrupted_backup_and_leaves_data_file_unchanged(tmp_path):
    data_file = tmp_path / "records.json"
    save_dates({"2026-08-01"}, data_file=data_file)
    original_text = data_file.read_text(encoding="utf-8")

    bad_backup = tmp_path / "bad_backup.json"
    bad_backup.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(RecordsFileCorruptedError):
        restore_data(bad_backup, data_file=data_file)

    assert data_file.read_text(encoding="utf-8") == original_text


def test_load_dates_raises_on_invalid_utf8(tmp_path):
    data_file = tmp_path / "records.json"
    # 0x80単独はUTF-8として不正なバイト列
    data_file.write_bytes(b'{"schema_version": 2, "dates": ["2026-08-0\x80"]}')
    with pytest.raises(RecordsFileCorruptedError):
        load_dates(data_file=data_file)


def test_load_dates_raises_when_date_element_is_not_a_string(tmp_path):
    data_file = tmp_path / "records.json"
    data_file.write_text(
        json.dumps({"schema_version": 2, "dates": [20260801]}), encoding="utf-8"
    )
    with pytest.raises(RecordsFileCorruptedError):
        load_dates(data_file=data_file)


def test_load_dates_raises_when_top_level_is_neither_list_nor_dict(tmp_path):
    data_file = tmp_path / "records.json"
    data_file.write_text(json.dumps("just a string"), encoding="utf-8")
    with pytest.raises(RecordsFileCorruptedError):
        load_dates(data_file=data_file)


def test_restore_data_raises_on_invalid_utf8_backup_and_leaves_data_file_unchanged(
    tmp_path,
):
    data_file = tmp_path / "records.json"
    save_dates({"2026-08-01"}, data_file=data_file)
    original_bytes = data_file.read_bytes()

    bad_backup = tmp_path / "bad_backup.json"
    bad_backup.write_bytes(b'{"schema_version": 2, "dates": ["2026-08-0\x80"]}')

    with pytest.raises(RecordsFileCorruptedError):
        restore_data(bad_backup, data_file=data_file)

    assert data_file.read_bytes() == original_bytes


def test_restore_data_leaves_data_file_unchanged_when_atomic_replace_fails(
    tmp_path, monkeypatch
):
    data_file = tmp_path / "records.json"
    backup_dir = tmp_path / "backups"
    save_dates({"2026-08-01"}, data_file=data_file, backup_dir=backup_dir)
    save_dates({"2026-08-01", "2026-08-02"}, data_file=data_file, backup_dir=backup_dir)
    original_bytes = data_file.read_bytes()

    target_backup = list_backups(data_file=data_file, backup_dir=backup_dir)[-1]
    backups_before_restore = len(list_backups(data_file=data_file, backup_dir=backup_dir))

    # restore_data()内では、①復元前バックアップの作成(os.replaceを1回目に使用)
    # ②復元本体の置換(os.replaceを2回目に使用)の順でos.replaceが呼ばれる。
    # ここでは②(復元本体の置換)だけを失敗させ、①は成功させる。
    real_replace = logic.os.replace
    call_count = 0

    def fail_on_restore_replace(src, dst):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("simulated restore replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(logic.os, "replace", fail_on_restore_replace)

    with pytest.raises(OSError):
        restore_data(target_backup, data_file=data_file, backup_dir=backup_dir)

    assert call_count == 2  # 復元本体の置換で実際に失敗したことを確認
    assert data_file.read_bytes() == original_bytes
    assert list(data_file.parent.glob("*.tmp")) == []

    # 復元前バックアップ(①)は成功して残っている
    backups_after_restore = list_backups(data_file=data_file, backup_dir=backup_dir)
    assert len(backups_after_restore) == backups_before_restore + 1


def test_save_dates_refuses_to_overwrite_corrupted_existing_file(tmp_path):
    data_file = tmp_path / "records.json"
    data_file.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(RecordsFileCorruptedError):
        save_dates({"2026-08-01"}, data_file=data_file)

    # 上書きされていない(壊れた内容のまま)
    assert data_file.read_text(encoding="utf-8") == "{not valid json"


def test_save_dates_backs_up_corrupted_existing_file_as_raw_bytes_before_refusing(
    tmp_path,
):
    data_file = tmp_path / "records.json"
    backup_dir = tmp_path / "backups"
    corrupted_bytes = b'{"schema_version": 2, "dates": ["2026-08-0\x80"]}'
    data_file.write_bytes(corrupted_bytes)

    with pytest.raises(RecordsFileCorruptedError):
        save_dates({"2026-08-01"}, data_file=data_file, backup_dir=backup_dir)

    backups = list_backups(data_file=data_file, backup_dir=backup_dir)
    assert len(backups) == 1
    assert backups[0].read_bytes() == corrupted_bytes  # 生バイトのまま退避されている


def test_atomic_write_uses_unique_temp_file_names_across_multiple_calls(
    tmp_path, monkeypatch
):
    data_file = tmp_path / "records.json"
    seen_names = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        seen_names.append(name)
        return fd, name

    monkeypatch.setattr(logic.tempfile, "mkstemp", spy_mkstemp)

    save_dates({"2026-08-01"}, data_file=data_file)
    save_dates({"2026-08-01", "2026-08-02"}, data_file=data_file)
    save_dates({"2026-08-01", "2026-08-02", "2026-08-03"}, data_file=data_file)

    assert len(seen_names) >= 3
    assert len(set(seen_names)) == len(seen_names)  # 呼び出しごとに一意
