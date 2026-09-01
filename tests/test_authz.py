import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "streamlit"))

import authz  # noqa: E402


def test_require_admin_allows_admin():
    authz.require_admin("admin")  # 例外を送出しなければOK


@pytest.mark.parametrize("role", ["member", "", None, "Admin", " admin"])
def test_require_admin_rejects_non_admin(role):
    with pytest.raises(authz.NotAdminError):
        authz.require_admin(role)
