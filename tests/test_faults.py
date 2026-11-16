from unittest.mock import AsyncMock

import fakeredis.aioredis
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app.config import Settings
from app.main import create_app


def test_lab_disabled_by_default():
    with TestClient(create_app(Settings(), AsyncMock())) as client:
        assert client.post("/lab/faults", json={"kind": "errors"}).status_code == 404


def test_fault_expires_and_control_plane_survives():
    app = create_app(
        Settings(lab_enabled=True, lab_token="test-only-token-not-a-secret-123456"),
        fakeredis.aioredis.FakeRedis(),
    )
    clock = [100.0]
    app.state.faults.clock = lambda: clock[0]
    with TestClient(
        app, headers={"Authorization": "Bearer test-only-token-not-a-secret-123456"}
    ) as client:
        assert (
            client.post(
                "/lab/faults", json={"kind": "errors", "error_rate": 1, "duration_seconds": 1}
            ).status_code
            == 200
        )
        assert client.get("/products/1").status_code == 500
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        assert 'lab_fault_active{kind="errors"} 1.0' in client.get("/metrics").text
        clock[0] = 102
        assert client.get("/products/1").status_code == 200
        assert client.get("/lab/status").json()["fault"] is None
        assert 'lab_fault_active{kind="errors"} 0.0' in client.get("/metrics").text
        assert (
            client.post("/lab/faults", json={"kind": "errors", "error_rate": 2}).status_code == 422
        )


def test_latency_and_clear():
    app = create_app(
        Settings(lab_enabled=True, lab_token="test-only-token-not-a-secret-123456"),
        fakeredis.aioredis.FakeRedis(),
    )
    with TestClient(
        app, headers={"Authorization": "Bearer test-only-token-not-a-secret-123456"}
    ) as client:
        client.post("/lab/faults", json={"kind": "latency", "delay_ms": 30})
        client.get("/products/1")
        assert (
            app.state.metrics.registry.get_sample_value("lab_http_request_duration_seconds_sum")
            >= 0.03
        )
        client.delete("/lab/faults")
        assert client.get("/lab/status").json()["fault"] is None


def test_fallback_switch_readiness_and_source_metrics():
    redis = AsyncMock()
    redis.get.side_effect = ConnectionError("offline")
    redis.ping.side_effect = ConnectionError("offline")
    app = create_app(
        Settings(lab_enabled=True, lab_token="test-only-token-not-a-secret-123456"), redis
    )
    with TestClient(
        app, headers={"Authorization": "Bearer test-only-token-not-a-secret-123456"}
    ) as client:
        assert client.get("/products/1").status_code == 503
        client.patch("/lab/config", json={"fallback_enabled": True})
        response = client.get("/products/1")
        assert response.status_code == 200 and response.headers["X-Cache"] == "BYPASS"
        assert client.get("/health/ready").json()["status"] == "degraded"
        assert app.state.metrics.registry.get_sample_value("lab_fallback_total") == 1
        assert app.state.metrics.registry.get_sample_value("lab_source_reads_total") == 1
        client.patch("/lab/config", json={"fallback_enabled": False})
        assert client.get("/products/1").status_code == 503


def test_fallback_write_failure_counts_source_once():
    redis = AsyncMock()
    redis.get.return_value = None
    redis.set.side_effect = ConnectionError("offline")
    app = create_app(Settings(fallback_enabled=True), redis)
    with TestClient(
        app, headers={"Authorization": "Bearer test-only-token-not-a-secret-123456"}
    ) as client:
        assert client.get("/products/1").status_code == 200
        assert app.state.metrics.registry.get_sample_value("lab_source_reads_total") == 1
