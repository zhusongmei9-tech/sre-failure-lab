"""Reproduce the CPU component experiment; mocked Redis, not a full-stack benchmark.

Run from project root: uv run --locked python -m scripts.cpu_component
"""

import json
import math
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def main():
    redis = AsyncMock()
    redis.get.return_value = None
    app = create_app(
        Settings(lab_enabled=True, lab_token="test-only-token-not-a-secret-123456"), redis
    )
    results = []
    with TestClient(
        app, headers={"Authorization": "Bearer test-only-token-not-a-secret-123456"}
    ) as client:
        try:
            for iterations in (0, 100_000, 1_000_000):
                client.delete("/lab/faults")
                if iterations:
                    response = client.post(
                        "/lab/faults",
                        json={
                            "kind": "cpu",
                            "cpu_iterations": iterations,
                            "duration_seconds": 60,
                        },
                    )
                    response.raise_for_status()
                started, cpu_started = time.perf_counter(), time.process_time()
                durations, statuses = [], []
                for _ in range(20):
                    begin = time.perf_counter()
                    response = client.get("/products/1")
                    durations.append(time.perf_counter() - begin)
                    statuses.append(response.status_code)
                results.append(
                    {
                        "requested_iterations_per_request": iterations,
                        "requests": len(statuses),
                        "statuses": {str(code): statuses.count(code) for code in set(statuses)},
                        "wall_seconds": time.perf_counter() - started,
                        "process_cpu_seconds": time.process_time() - cpu_started,
                        "p95_ms": sorted(durations)[math.ceil(0.95 * len(durations)) - 1] * 1000,
                    }
                )
        finally:
            client.delete("/lab/faults").raise_for_status()
        assert client.get("/lab/status").json()["fault"] is None
        assert client.get("/products/1").status_code == 200
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "environment": platform.platform(),
        "scope": "FastAPI TestClient, mocked Redis, sequential requests; "
        "not Docker/full-stack acceptance or a production benchmark",
        "results": results,
        "cleanup_verified": True,
    }
    target = Path("reports")
    target.mkdir(exist_ok=True)
    path = target / (datetime.now(UTC).strftime("cpu-component-%Y%m%dT%H%M%S%fZ") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
