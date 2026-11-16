"""End-to-end component rehearsal, with fake Redis and no real recovery/deployment.

Run: uv run --locked python -m scripts.simulate
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import fakeredis.aioredis
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TOKEN = "test-only-token-not-a-secret-123456"


def run():
    app = create_app(
        Settings(lab_enabled=True, lab_token=TOKEN),
        fakeredis.aioredis.FakeRedis(decode_responses=True),
    )
    now = [100.0]
    app.state.healing.clock = lambda: now[0]
    app.state.deployment.clock = lambda: now[0]
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:

        def post(path, body=None):
            response = client.post("/lab/" + path, json=body)
            response.raise_for_status()
            return response.json()

        # Actual API code, simulated dependencies. Three successes, two injected failures.
        for _ in range(3):
            assert client.get("/products/1").status_code == 200
        post("faults", {"kind": "errors", "error_rate": 1, "duration_seconds": 30})
        for _ in range(2):
            assert client.get("/products/1").status_code == 500
        client.delete("/lab/faults").raise_for_status()
        assert client.get("/products/1").status_code == 200

        post("healing/config", {"enabled": True})
        for _ in range(3):
            state = post("healing/probe", {"service": "redis", "healthy": False})
        assert state["services"]["redis"]["phase"] == "verifying"
        for _ in range(2):
            state = post("healing/probe", {"service": "redis", "healthy": True})
        assert state["services"]["redis"]["recovery_observed"]

        for version in ("v1", "v2"):
            release_id = post("deployment/begin", {"version": version})["release_id"]
            post(
                "deployment/candidate-check",
                {"release_id": release_id, "ready": True, "smoke_ok": True},
            )
            now[0] += 31
            post("deployment/observe", {"release_id": release_id, "healthy": True})
        # Demonstrate manual rollback before the previous slot is reused.
        assert post("deployment/rollback", {"release_id": release_id})["active"]["version"] == "v1"
        release_id = post("deployment/begin", {"version": "v3-bad"})["release_id"]
        post(
            "deployment/candidate-check",
            {"release_id": release_id, "ready": True, "smoke_ok": True},
        )
        result = post("deployment/observe", {"release_id": release_id, "healthy": False})
        assert result["active"]["version"] == "v1"
        assert result["last_outcome"] == "rolled_back"
        report = client.get("/lab/report").json()
        markdown = client.get("/lab/report?format=markdown").text
        assert report["slo"]["total_requests"] == 6
        assert report["slo"]["failed_requests"] == 2
        assert TOKEN not in json.dumps(report)
        summary = {
            "scope": "component with fake Redis and simulated controllers; no real deployment",
            "requests": 6,
            "successful_requests": 4,
            "failed_requests": 2,
            "simulated_recovery_verified": True,
            "simulated_active_version": "v1",
            "simulated_failed_release_outcome": "rolled_back",
            "real_actions_executed": False,
        }
    return report, markdown, summary


def main():
    report, markdown, summary = run()
    directory = Path("reports") / datetime.now(UTC).strftime("rehearsal-%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (directory / "report.md").write_text(markdown)
    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(summary, ensure_ascii=False))
    print(f"Reports: {directory}")


if __name__ == "__main__":
    main()
