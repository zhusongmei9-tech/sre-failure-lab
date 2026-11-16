from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app.config import Settings
from app.main import create_app


def test_metrics_exclude_scrapes_and_health_and_count_errors():
    redis = AsyncMock()
    redis.get.return_value = None
    app = create_app(Settings(), redis)
    with TestClient(app) as client:
        registry = app.state.metrics.registry
        client.get("/metrics")
        client.get("/health/live")
        client.get("/health/ready")
        client.get("/docs")
        assert registry.get_sample_value("lab_http_request_duration_seconds_count") == 0
        assert client.get("/products/1").status_code == 200
        assert registry.get_sample_value("lab_cache_requests_total", {"result": "miss"}) == 1
        redis.get.side_effect = ConnectionError("offline")
        assert client.get("/products/2").status_code == 503
        assert registry.get_sample_value("lab_http_requests_total", {"status_code": "503"}) == 1
        assert registry.get_sample_value("lab_cache_requests_total", {"result": "error"}) == 1
        assert client.get("/products/999").status_code == 404
        assert client.get("/products/invalid").status_code == 422
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert 'lab_http_request_duration_seconds_bucket{le="+Inf"} 4.0' in response.text
        assert "product_id=" not in response.text
        assert registry.get_sample_value("lab_http_request_duration_seconds_count") == 4


def test_registry_is_independent_per_app():
    first = create_app(Settings(), AsyncMock())
    second = create_app(Settings(), AsyncMock())
    first.state.metrics.observe(200, 0.1, "hit")
    assert (
        second.state.metrics.registry.get_sample_value(
            "lab_http_requests_total", {"status_code": "200"}
        )
        == 0
    )
