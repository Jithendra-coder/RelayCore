import pytest

from scripts.restore_drill import main


def test_restore_drill_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("RELAYCORE_RESTORE_DRILL", raising=False)
    with pytest.raises(RuntimeError, match="RELAYCORE_RESTORE_DRILL=1"):
        main()
