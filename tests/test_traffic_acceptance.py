import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts import accept_traffic as traffic


def row(phase="baseline", status=200, elapsed=10, crossing=False):
    return {
        "phase": phase,
        "status": status,
        "elapsed_ms": elapsed,
        "version": "0.3.1",
        "slot": "green",
        "cache": "HIT",
        "phase_crossing": crossing,
    }


def metric_rows():
    output = []
    for phase in ("baseline", "cpu", "final_recovery"):
        queries = {
            name: {"status": "ok", "result": [{"metric": {}, "value": 1}]}
            for name in traffic.QUERIES
        }
        queries["alerts"]["result"] = []
        queries["up"]["result"] = [
            {"metric": {"job": job}, "value": 1} for job in ("api", "node", "cadvisor")
        ]
        output.append({"phase": phase, "queries": queries})
    return output


def phase(name, expected=False):
    return {"name": name, "expected_errors": expected, "duration_seconds": 1}


def test_summary_retains_http_and_transport_failures_and_percentiles():
    rows = [
        row(status=200, elapsed=10),
        row(status=429, elapsed=20),
        row(status=503, elapsed=30),
        row(status=0, elapsed=40, crossing=True),
    ]
    result = traffic.summarize(rows, 2)
    assert result["total"] == 4
    assert [result[key] for key in ("2xx", "4xx", "5xx", "transport_errors")] == [1, 1, 1, 1]
    assert result["actual_rps"] == 2
    assert result["p50_ms"] == 20 and result["p95_ms"] == result["p99_ms"] == 40
    assert result["crossing_requests"] == 1


def test_crossing_errors_visible_but_do_not_fail_stable_gate():
    phases = [phase("baseline"), phase("final_recovery")]
    rows = [row(), row(status=503, crossing=True), row(phase="final_recovery")]
    failures, coverage = traffic.evaluate(phases, rows, metric_rows())
    assert failures == []
    assert phases[0]["traffic"]["5xx"] == 1
    assert phases[0]["traffic"]["noncrossing_errors"] == 0
    assert all(coverage["baseline"].values())
    failures, _ = traffic.evaluate([phase("baseline")], [row(crossing=True)], metric_rows())
    assert "baseline: no samples entirely within phase" in failures


def test_expected_fault_codes_are_limited_and_cpu_work_is_required():
    phases = [phase("cpu", True), phase("automatic_rollback", True)]
    rows = [row("cpu"), row("cpu", 429), row("automatic_rollback", 500)]
    failures, _ = traffic.evaluate(phases, rows, metric_rows())
    assert failures == []
    assert phases[0]["cpu_work_observed"] is True
    rows.append(row("cpu", 503))
    rows.append(row("automatic_rollback", 404))
    failures, _ = traffic.evaluate(phases, rows, [])
    assert any("cpu: unexpected statuses" in value for value in failures)
    assert any("automatic_rollback: unexpected statuses" in value for value in failures)
    assert "cpu: no measured CPU iteration progress" in failures


def test_absent_metrics_are_not_zero_or_success():
    metrics = metric_rows()
    metrics[0]["queries"]["container_cpu_cores"]["result"] = []
    failures, _ = traffic.evaluate([phase("baseline")], [row()], metrics)
    assert "baseline: no finite metrics for container_cpu_cores" in failures
    assert not any("alerts" in failure for failure in failures)


def test_prometheus_nan_is_preserved_as_unavailable(monkeypatch):
    payload = {
        "status": "success",
        "data": {
            "result": [
                {"metric": {"job": "api"}, "value": [123, "NaN"]},
            ]
        },
    }
    monkeypatch.setattr(
        traffic, "urlopen", lambda *args, **kwargs: io.StringIO(json.dumps(payload))
    )
    name, result = traffic.MetricsSampler.query(("sample", "up"))
    assert name == "sample" and result["result"][0]["value"] is None


def test_real_worker_marks_phase_crossing_and_has_bounded_shutdown(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            release.wait(timeout=2)
            self.send_response(429)
            self.send_header("X-App-Version", "0.3.1")
            self.send_header("X-Deployment-Slot", "green")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    generator = traffic.Traffic(
        tmp_path, rps=1, workers=1, base_url=f"http://127.0.0.1:{server.server_port}"
    )
    try:
        generator.set_phase("baseline")
        generator.start()
        assert entered.wait(timeout=2)
        generator.set_phase("cpu")
        release.set()
        deadline = time.monotonic() + 2
        while not generator.rows and time.monotonic() < deadline:
            time.sleep(0.01)
        generator.close()
        saved = json.loads((tmp_path / "requests.jsonl").read_text().splitlines()[0])
        assert saved["phase"] == "baseline" and saved["phase_end"] == "cpu"
        assert saved["phase_crossing"] and saved["status"] == 429
        assert saved["slot"] == "green" and saved["version"] == "0.3.1"
        assert not any(thread.is_alive() for thread in generator.threads)
    finally:
        release.set()
        generator.stop.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


@pytest.mark.parametrize(
    "kwargs", [{"rps": 51}, {"rps": float("nan")}, {"workers": 0}, {"workers": 33}]
)
def test_unsafe_load_parameters_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):
        traffic.Traffic(tmp_path, **kwargs)
