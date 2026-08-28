import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "streamlit"))

import scripts.restore_records as restore_records_module  # noqa: E402
from logic import list_backups, load_dates, save_dates  # noqa: E402
from scripts.restore_records import main  # noqa: E402


def test_restore_records_cli_restores_from_backup(tmp_path):
    data_file = tmp_path / "records.json"
    backup_dir = data_file.parent / "backups"
    save_dates({"2026-08-01"}, data_file=data_file, backup_dir=backup_dir)
    save_dates({"2026-08-01", "2026-08-02"}, data_file=data_file, backup_dir=backup_dir)

    backups = list_backups(data_file=data_file, backup_dir=backup_dir)
    exit_code = main(["restore_records.py", str(data_file), str(backups[0])])

    assert exit_code == 0
    assert load_dates(data_file=data_file) == {"2026-08-01"}


def test_restore_records_cli_rejects_corrupted_backup(tmp_path):
    data_file = tmp_path / "records.json"
    save_dates({"2026-08-01"}, data_file=data_file)

    bad_backup = tmp_path / "bad_backup.json"
    bad_backup.write_text("{not valid json", encoding="utf-8")

    exit_code = main(["restore_records.py", str(data_file), str(bad_backup)])

    assert exit_code == 1
    assert load_dates(data_file=data_file) == {"2026-08-01"}


def test_restore_records_cli_missing_backup_file(tmp_path):
    data_file = tmp_path / "records.json"
    save_dates({"2026-08-01"}, data_file=data_file)
    missing = tmp_path / "does_not_exist.json"

    exit_code = main(["restore_records.py", str(data_file), str(missing)])

    assert exit_code == 1


def test_restore_records_cli_wrong_arg_count():
    assert main(["restore_records.py"]) == 1


def test_restore_records_cli_handles_oserror_without_changing_data_file(
    tmp_path, monkeypatch
):
    data_file = tmp_path / "records.json"
    backup_dir = data_file.parent / "backups"
    save_dates({"2026-08-01"}, data_file=data_file, backup_dir=backup_dir)
    save_dates({"2026-08-01", "2026-08-02"}, data_file=data_file, backup_dir=backup_dir)
    backups = list_backups(data_file=data_file, backup_dir=backup_dir)
    original_bytes = data_file.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("simulated failure")

    monkeypatch.setattr(restore_records_module, "restore_data", boom)

    exit_code = restore_records_module.main(
        ["restore_records.py", str(data_file), str(backups[0])]
    )

    assert exit_code == 1
    assert data_file.read_bytes() == original_bytes
