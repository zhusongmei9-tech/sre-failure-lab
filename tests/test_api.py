import asyncio
import json
import time
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with TestClient(create_app(Settings(cache_ttl_seconds=1), redis)) as client:
        yield client


def test_cache_hit_and_expiration(client):
    first = client.get("/products/1")
    assert first.status_code == 200
    assert first.headers["X-Cache"] == "MISS"
    second = client.get("/products/1")
    assert second.headers["X-Cache"] == "HIT"
    assert first.json() == second.json()
    time.sleep(1.1)
    assert client.get("/products/1").headers["X-Cache"] == "MISS"


def test_health_and_invalid_products(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").json() == {"status": "ready", "redis": "ok"}
    assert client.get("/products/999").status_code == 404
    assert client.get("/products/0").status_code == 422
    assert client.get("/products/nope").status_code == 422


def test_request_log_and_id(capsys):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with TestClient(create_app(Settings(), redis)) as client:
        response = client.get("/products/2", headers={"X-Request-ID": "demo-123"})
        assert response.headers["X-Request-ID"] == "demo-123"
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        event = next(e for e in events if e.get("request_id") == "demo-123")
        assert event["route"] == "/products/{product_id}"
        assert event["cache_result"] == "miss"
        assert event["status_code"] == 200
        assert event["duration_ms"] >= 0
        invalid = client.get("/products/1", headers={"X-Request-ID": "invalid id"})
        assert len(invalid.headers["X-Request-ID"]) == 32


def test_redis_failure_and_recovery(capsys):
    redis = AsyncMock()
    redis.get.side_effect = ConnectionError("offline")
    redis.ping.side_effect = ConnectionError("offline")
    with TestClient(create_app(Settings(), redis)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        assert client.get("/products/1").status_code == 503
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        failure = next(e for e in events if e.get("cache_result") == "error")
        assert failure["error_type"] == "ConnectionError"
        redis.get.side_effect = None
        redis.get.return_value = None
        redis.ping.side_effect = None
        assert client.get("/health/ready").status_code == 200
        assert client.get("/products/1").status_code == 200


def test_redis_hang_is_bounded():
    async def hang(*args, **kwargs):
        await asyncio.sleep(10)

    redis = AsyncMock()
    redis.get.side_effect = hang
    redis.ping.side_effect = hang
    with TestClient(create_app(Settings(redis_timeout_seconds=0.05), redis)) as client:
        started = time.perf_counter()
        assert client.get("/products/1").status_code == 503
        assert client.get("/health/ready").status_code == 503
        assert time.perf_counter() - started < 1


def test_bad_cache_is_replaced():
    redis = AsyncMock()
    redis.get.return_value = "broken json"
    with TestClient(create_app(Settings(), redis)) as client:
        response = client.get("/products/1")
        assert response.status_code == 200
        assert response.headers["X-Cache"] == "MISS"
        assert response.json()["id"] == 1
        redis.set.assert_awaited_once()


def test_cache_write_failure_returns_503():
    redis = AsyncMock()
    redis.get.return_value = None
    redis.set.side_effect = ConnectionError("offline")
    with TestClient(create_app(Settings(), redis)) as client:
        assert client.get("/products/1").status_code == 503
