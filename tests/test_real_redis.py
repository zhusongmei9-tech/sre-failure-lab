"""Optional real Redis check: TEST_REDIS_SERVER=/path/to/redis-server uv run pytest."""

import os
import signal
import socket
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.mark.skipif(not os.getenv("TEST_REDIS_SERVER"), reason="TEST_REDIS_SERVER is not set")
def test_real_redis_outage_and_recovery(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "redis.log").open("w") as log:
        process = subprocess.Popen(
            [
                os.environ["TEST_REDIS_SERVER"],
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--save",
                "",
                "--appendonly",
                "no",
            ],
            cwd=tmp_path,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            settings = Settings(
                redis_url=f"redis://127.0.0.1:{port}/0",
                redis_timeout_seconds=0.2,
                cache_ttl_seconds=1,
            )
            with TestClient(create_app(settings)) as client:
                for _ in range(50):
                    if client.get("/health/ready").status_code == 200:
                        break
                    time.sleep(0.1)
                else:
                    pytest.fail("Redis did not become ready")
                assert client.get("/products/1").headers["X-Cache"] == "MISS"
                assert client.get("/products/1").headers["X-Cache"] == "HIT"
                time.sleep(1.1)
                assert client.get("/products/1").headers["X-Cache"] == "MISS"
                process.send_signal(signal.SIGSTOP)
                started = time.perf_counter()
                assert client.get("/products/1").status_code == 503
                assert time.perf_counter() - started < 1
                assert client.get("/health/live").status_code == 200
                assert client.get("/health/ready").status_code == 503
                process.send_signal(signal.SIGCONT)
                assert client.get("/health/ready").status_code == 200
                assert client.get("/products/1").status_code == 200
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGCONT)
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
