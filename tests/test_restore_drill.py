from io import BytesIO

import pytest

from scripts import compose_smoke
from scripts.restore_drill import main


def test_restore_drill_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("RELAYCORE_RESTORE_DRILL", raising=False)
    with pytest.raises(RuntimeError, match="RELAYCORE_RESTORE_DRILL=1"):
        main()


def test_compose_smoke_requires_json_object_response(monkeypatch):
    monkeypatch.setattr(compose_smoke, "urlopen", lambda *_args, **_kwargs: BytesIO(b'{"id":"run-1"}'))
    assert compose_smoke._request_json("http://localhost", "/api/workflows") == {"id": "run-1"}

    monkeypatch.setattr(compose_smoke, "urlopen", lambda *_args, **_kwargs: BytesIO(b"[]"))
    with pytest.raises(RuntimeError, match="non-object"):
        compose_smoke._request_json("http://localhost", "/api/workflows")
