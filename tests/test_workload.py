import concurrent.futures
import threading

import fakeredis.aioredis
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.workload import burn_cpu


def test_work_is_deterministic_and_bounded():
    assert burn_cpu(10, clock=lambda: 0) == burn_cpu(10, clock=lambda: 0)
    assert burn_cpu(10, clock=lambda: 0)[0] == 10
    ticks = iter([0, 0, 1])
    assert burn_cpu(50_000, clock=lambda: next(ticks))[0] == 1024


def test_cpu_fault_metrics_expiration_and_validation():
    app = create_app(
        Settings(lab_enabled=True, lab_token="test-only-token-not-a-secret-123456"),
        fakeredis.aioredis.FakeRedis(),
    )
    now = [100]
    app.state.faults.clock = lambda: now[0]
    with TestClient(
        app, headers={"Authorization": "Bearer test-only-token-not-a-secret-123456"}
    ) as client:
        for value in (0, -1, 5_000_001):
            assert (
                client.post(
                    "/lab/faults",
                    json={
                        "kind": "cpu",
                        "cpu_iterations": value,
                    },
                ).status_code
                == 422
            )
        assert (
            client.post(
                "/lab/faults",
                json={
                    "kind": "cpu",
                    "cpu_iterations": 10,
                    "duration_seconds": 1,
                },
            ).status_code
            == 200
        )
        assert client.get("/products/1").status_code == 200
        registry = app.state.metrics.registry
        assert registry.get_sample_value("lab_cpu_iterations_total") == 10
        assert 'lab_fault_active{kind="cpu"} 1.0' in client.get("/metrics").text
        now[0] = 102
        assert client.get("/products/1").status_code == 200
        assert registry.get_sample_value("lab_cpu_iterations_total") == 10
        assert client.get("/lab/status").json()["fault"] is None


def test_cpu_experiment_rejects_overlap_but_keeps_controls_available(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_work(iterations):
        entered.set()
        assert release.wait(3)
        return iterations, 0

    monkeypatch.setattr("app.main.burn_cpu", slow_work)
    app = create_app(
        Settings(lab_enabled=True, lab_token="test-only-token-not-a-secret-123456"),
        fakeredis.aioredis.FakeRedis(),
    )
    with (
        TestClient(
            app, headers={"Authorization": "Bearer test-only-token-not-a-secret-123456"}
        ) as client,
        concurrent.futures.ThreadPoolExecutor() as pool,
    ):
        client.post("/lab/faults", json={"kind": "cpu", "cpu_iterations": 10})
        first = pool.submit(client.get, "/products/1")
        try:
            assert entered.wait(3)
            assert client.get("/products/2").status_code == 429
            assert client.get("/health/live").status_code == 200
            assert client.get("/metrics").status_code == 200
            assert client.delete("/lab/faults").status_code == 200
            # Clearing prevents new CPU work; the one bounded job is still finishing.
            assert client.get("/products/3").status_code == 200
        finally:
            release.set()
        assert first.result(timeout=3).status_code == 200
        assert app.state.metrics.registry.get_sample_value("lab_cpu_rejected_total") == 1
