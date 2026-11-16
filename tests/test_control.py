from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.config import Settings
from app.control import metric_snapshot
from app.main import create_app
from app.metrics import Metrics

TOKEN = "test-only-token-not-a-secret-123456"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def make_app():
    redis = AsyncMock()
    redis.get.return_value = None
    return create_app(Settings(lab_enabled=True, lab_token=TOKEN), redis)


def test_new_controls_are_authenticated_and_no_store():
    app = make_app()
    with TestClient(app) as client:
        for path in ("overview", "healing", "deployment", "report"):
            response = client.get(f"/lab/{path}")
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
        for path, data in [
            ("healing/config", {"enabled": True}),
            ("healing/probe", {"service": "api", "healthy": False}),
            ("healing/reset", {"service": "api"}),
            ("deployment/begin", {"version": "v1"}),
            ("deployment/candidate-check", {"ready": True, "smoke_ok": True}),
            ("deployment/observe", {"healthy": True}),
            ("deployment/cancel", {}),
            ("deployment/rollback", {}),
        ]:
            assert client.post(f"/lab/{path}", json=data).status_code == 401
        assert not app.state.healing.enabled
        assert app.state.deployment.status()["active"] is None


def test_end_to_end_simulated_healing_and_release():
    app = make_app()
    now = [100.0]
    app.state.healing.clock = lambda: now[0]
    app.state.deployment.clock = lambda: now[0]
    with TestClient(app, headers=HEADERS) as client:
        assert client.post("/lab/healing/config", json={"enabled": "yes"}).status_code == 422
        assert (
            client.post(
                "/lab/healing/probe", json={"service": "host", "healthy": False}
            ).status_code
            == 422
        )
        client.post("/lab/healing/config", json={"enabled": True})
        for _ in range(3):
            response = client.post(
                "/lab/healing/probe", json={"service": "redis", "healthy": False}
            )
        assert response.json()["services"]["redis"]["attempts"] == 1
        assert not response.json()["services"]["redis"]["recovery_observed"]
        for _ in range(2):
            response = client.post("/lab/healing/probe", json={"service": "redis", "healthy": True})
        assert response.json()["services"]["redis"]["phase"] == "healthy"
        started = client.post("/lab/deployment/begin", json={"version": "v1"})
        assert started.status_code == 200
        release_id = started.json()["release_id"]
        assert client.post("/lab/deployment/begin", json={"version": "v2"}).status_code == 409
        result = client.post(
            "/lab/deployment/candidate-check",
            json={"release_id": release_id, "ready": True, "smoke_ok": True},
        )
        assert result.json()["phase"] == "observing"
        now[0] += 31
        client.post("/lab/deployment/observe", json={"release_id": release_id, "healthy": True})
        release_id = client.post("/lab/deployment/begin", json={"version": "v3-bad"}).json()[
            "release_id"
        ]
        client.post(
            "/lab/deployment/candidate-check",
            json={"release_id": release_id, "ready": True, "smoke_ok": True},
        )
        result = client.post(
            "/lab/deployment/observe", json={"release_id": release_id, "healthy": False}
        ).json()
        assert result["active"]["version"] == "v1"
        assert result["mode"] == "simulation"
        assert client.get("/version").json()["version"] == app.version
        assert client.get("/products/1").headers["X-App-Version"] == app.version


def test_reports_measure_actual_api_and_keep_secrets_out():
    with TestClient(make_app(), headers=HEADERS) as client:
        empty = client.get("/lab/report").json()
        assert empty["slo"]["success_ratio"] is None
        assert empty["slo"]["p95_seconds"] is None
        assert client.get("/products/1").status_code == 200
        assert client.get("/products/999").status_code == 404
        overview = client.get("/lab/overview").json()
        assert overview["real_actions_enabled"] is False
        assert overview["report"]["slo"]["total_requests"] == 2
        assert overview["report"]["slo"]["success_ratio"] == 0.5
        result = client.get("/lab/report?format=markdown")
        assert result.status_code == 200
        assert "attachment" in result.headers["content-disposition"]
        assert "50.00%" in result.text
        assert TOKEN not in result.text
        assert client.get("/lab/report?target=1").status_code == 422
        assert client.get("/lab/report?format=html").status_code == 422


def test_snapshot_quantile_and_no_data():
    metrics = Metrics()
    start = datetime.now(UTC)
    end = start + timedelta(seconds=1)
    assert metric_snapshot(metrics, start, end)["p95_seconds"] is None
    metrics.observe(200, 0.01, "hit")
    snapshot = metric_snapshot(metrics, start, end)
    assert snapshot["status_counts"]["200"] == 1
    assert 0.005 <= snapshot["p95_seconds"] <= 0.01
    metrics.observe(500, 100, "error")
    assert metric_snapshot(metrics, start, end)["p95_seconds"] is None


def test_console_assets_and_disabled_lab():
    with TestClient(make_app()) as client:
        result = client.get("/console")
        assert result.status_code == 200
        assert "frame-ancestors 'none'" in result.headers["content-security-policy"]
        assert TOKEN not in result.text
        assert client.get("/console/assets/console.js").status_code == 200
        assert client.get("/console/assets/control.py").status_code == 422
        assert "localStorage" not in client.get("/console/assets/console.js").text
    with TestClient(create_app(Settings(), AsyncMock())) as client:
        assert client.get("/console").status_code == 404
        assert client.get("/lab/overview").status_code == 404


def test_deployment_http_endpoints_require_and_fence_release_id():
    app = make_app()
    app.state.deployment.clock = lambda: 100.0
    with TestClient(app, headers=HEADERS) as client:
        old_id = client.post("/lab/deployment/begin", json={"version": "v1"}).json()["release_id"]
        assert client.post("/lab/deployment/cancel", json={"release_id": old_id}).status_code == 200
        new_id = client.post("/lab/deployment/begin", json={"version": "v2"}).json()["release_id"]
        before = client.get("/lab/deployment").json()
        operations = (
            ("candidate-check", {"ready": True, "smoke_ok": True}),
            ("observe", {"healthy": False}),
            ("cancel", {}),
            ("rollback", {}),
        )
        for path, payload in operations:
            assert client.post(f"/lab/deployment/{path}", json=payload).status_code == 422
            assert (
                client.post(
                    f"/lab/deployment/{path}", json={**payload, "release_id": old_id}
                ).status_code
                == 409
            )
            assert client.get("/lab/deployment").json() == before
        assert (
            client.post(
                "/lab/deployment/candidate-check",
                json={"release_id": new_id, "ready": True, "smoke_ok": True},
            ).status_code
            == 200
        )
        before = client.get("/lab/deployment").json()
        assert (
            client.post(
                "/lab/deployment/observe", json={"release_id": old_id, "healthy": False}
            ).status_code
            == 409
        )
        assert client.get("/lab/deployment").json() == before
