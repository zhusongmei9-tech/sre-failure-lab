import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.events import EventStore
from app.main import create_app

TOKEN = "test-only-token-not-a-secret-123456"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def make_app():
    return create_app(Settings(lab_enabled=True, lab_token=TOKEN), AsyncMock())


@pytest.mark.parametrize("token", ["", "short", " " * 40, "中" * 40])
def test_lab_fails_closed_without_usable_token(token):
    with pytest.raises(ValueError, match="LAB_TOKEN"):
        Settings(lab_enabled=True, lab_token=token)


def test_token_is_not_in_settings_repr():
    assert TOKEN not in repr(Settings(lab_enabled=True, lab_token=TOKEN))


@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic dGVzdA=="])
def test_all_lab_routes_require_bearer(header):
    app = make_app()
    with TestClient(app) as client:
        headers = {"Authorization": header} if header else {}
        for method, path, body in [
            ("GET", "/lab/status", None),
            ("GET", "/lab/events", None),
            ("POST", "/lab/faults", {"kind": "cpu"}),
            ("DELETE", "/lab/faults", None),
            ("PATCH", "/lab/config", {"fallback_enabled": True}),
        ]:
            result = client.request(method, path, json=body, headers=headers)
            assert result.status_code == 401
            assert result.headers["www-authenticate"] == "Bearer"
        assert app.state.faults.active() is None
        assert not app.state.fallback_enabled
        assert app.state.events.latest() == []
        assert client.get("/health/live").status_code == 200
        assert client.get("/metrics").status_code == 200


def test_audited_changes_conflict_and_expiration(capsys):
    app = make_app()
    now = [10]
    app.state.faults.clock = lambda: now[0]
    with TestClient(app, headers=HEADERS) as client:
        assert (
            client.post("/lab/faults", json={"kind": "cpu", "duration_seconds": 1}).status_code
            == 200
        )
        assert client.post("/lab/faults", json={"kind": "errors"}).status_code == 409
        assert client.get("/lab/status").json()["fault"]["kind"] == "cpu"
        now[0] = 12
        events = client.get("/lab/events").json()
        assert not events["persistent"]
        assert [e["action"] for e in events["events"]] == ["fault_expired", "fault_started"]
        client.patch("/lab/config", json={"fallback_enabled": True})
        client.post("/lab/faults", json={"kind": "errors"})
        client.delete("/lab/faults")
        recent = client.get("/lab/events?limit=2").json()["events"]
        assert [e["action"] for e in recent] == ["fault_cleared", "fault_started"]
        assert len({e["event_id"] for e in recent}) == 2
        assert client.get("/lab/events?limit=201").status_code == 422
        assert client.get("/lab/events?limit=0").status_code == 422
        assert TOKEN not in json.dumps(events)
        assert TOKEN not in capsys.readouterr().out


def test_history_is_bounded():
    store = EventStore(capacity=2)
    for n in range(3):
        store.append("test", {"n": n})
    assert [e["details"]["n"] for e in store.latest()] == [2, 1]


def test_unknown_control_fields_are_rejected():
    with TestClient(make_app(), headers=HEADERS) as client:
        assert client.post("/lab/faults", json={"kind": "cpu", "duration": 100}).status_code == 422
        assert (
            client.patch("/lab/config", json={"fallback_enabled": True, "oops": 1}).status_code
            == 422
        )


def test_wrong_product_in_cache_is_rebuilt():
    redis = AsyncMock()
    redis.get.return_value = json.dumps({"id": 2, "name": "wrong", "price_cents": 1, "stock": 1})
    with TestClient(create_app(Settings(), redis)) as client:
        result = client.get("/products/1")
        assert result.status_code == 200
        assert result.json()["id"] == 1
        assert result.headers["X-Cache"] == "MISS"
        redis.set.assert_awaited_once()
