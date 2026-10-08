from __future__ import annotations

import pytest

from app.store import _required_row


def test_required_row_rejects_missing_database_results():
    row = {"id": "expected"}

    assert _required_row(row) is row
    with pytest.raises(RuntimeError, match="did not return its expected row"):
        _required_row(None)
