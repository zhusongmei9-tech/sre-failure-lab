import json

import pytest

from scripts import capacity


def counters(**values):
    return {k: {"values": {"count": n}} for k, n in values.items()}


def normal_metrics():
    metrics = counters(
        cap_started=1000,
        cap_completed=1000,
        iterations=1000,
        http_reqs=1000,
        cap_valid=1000,
        cap_http_2xx=1000,
        cap_cache_hit=997,
        cap_cache_miss=3,
        cap_initial_misses=3,
    )
    metrics["cap_client_ms"] = {"values": {"p(95)": 75, "p(99)": 120}}
    return metrics


def test_real_time_run_counts_started_drops_incorrect_payload_and_cold_evidence():
    good = capacity.summarize(normal_metrics(), "cold-start", 100, 10)
    assert good["passed"] and good["cache"]["MISS"] == 3 and good["p99_ms"] == 120
    metrics = normal_metrics()
    metrics.update(
        counters(
            cap_started=998,
            cap_completed=998,
            http_reqs=998,
            iterations=998,
            cap_http_2xx=996,
            cap_valid=995,
            cap_http_429=1,
            cap_timeouts=1,
            cap_transport_errors=1,
            dropped_iterations=2,
            cap_initial_misses=2,
        )
    )
    row = capacity.summarize(metrics, "cold-start", 100, 10)
    assert not row["passed"] and row["actual_rps"] == 99.8
    assert row["http_429"] == row["invalid_2xx"] == row["timeouts"] == row["transport_errors"] == 1
    assert row["checks"] == {
        "all_requests_completed": True,
        "no_dropped_iterations": False,
        "offered_target": False,
        "valid_product_ratio": False,
        "p95_ms": True,
        "verified_first_three_misses": False,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"cap_started": 1001},
        {
            "cap_started": 999,
            "cap_completed": 999,
            "iterations": 999,
            "http_reqs": 999,
            "cap_http_2xx": 999,
            "cap_valid": 999,
        },
        {"dropped_iterations": 1},
    ],
)
def test_missed_offers_and_unfinished_iterations_cannot_pass_on_fast_responses(change):
    metrics = normal_metrics()
    metrics.update(counters(**change))
    assert not capacity.summarize(metrics, "hot", 100, 10)["passed"]


def test_empty_run_retains_null_latency_and_success():
    result = capacity.summarize({}, "hot", 10, 1)
    assert not result["passed"]
    assert result["p99_ms"] is result["valid_product_ratio"] is None


def test_short_smoke_excluded_from_capacity_and_compared_within_scenario():
    rows = [
        {"scenario": name, "target_rps": rps, "seconds": duration, "passed": ok}
        for name, rps, duration, ok in [
            ("hot", 40, 600, True),
            ("hot", 80, 600, False),
            ("hot", 120, 30, True),
            ("mixed", 80, 600, True),
        ]
    ]
    assert capacity.highest(rows, capacity.SCENARIOS, 600) == {
        "hot": 40,
        "mixed": 80,
        "cold-start": None,
    }
    assert capacity.highest(rows, capacity.SCENARIOS, 0)["hot"] == 120


def test_prepare_uses_reads_only_and_cold_start_waits_without_preheating(monkeypatch):
    calls = []
    monkeypatch.setattr(capacity.time, "sleep", lambda n: calls.append(n))

    def get(_, product):
        calls.append(product)
        return {"id": int(product[-1])}, "HIT"

    monkeypatch.setattr(capacity, "get", get)
    capacity.prepare("local", "mixed", 65)
    capacity.prepare("local", "cold-start", 65)
    assert calls == ["/products/1", "/products/2", "/products/3", 65]


def test_preflight_rejects_other_api_and_warmup_requires_real_hit(monkeypatch):
    monkeypatch.setattr(capacity, "get", lambda *_: ({}, "MISS"))
    with pytest.raises(ValueError, match="lab's product"):
        capacity.identify("http://127.0.0.1")
    with pytest.raises(ValueError, match="warm cache"):
        capacity.prepare("http://127.0.0.1", "hot", 0)


def test_local_plan_is_offline_and_error_report_can_preserve_completed_stages(
    tmp_path, monkeypatch
):
    out = tmp_path / "result.json"
    args = ["--rates", "1,2", "--seconds", "2", "--scenarios", "hot", "--output", str(out)]
    assert capacity.main(args) == 0 and not out.exists()
    monkeypatch.setattr(capacity.subprocess, "check_output", lambda *a, **kw: "k6 v1.3.0")
    monkeypatch.setattr(capacity, "identify", lambda _: "demo")

    def stage(args, scenario, rate):
        if rate == 2:
            raise ValueError("broken")
        return {"scenario": scenario, "target_rps": rate, "seconds": args.seconds, "passed": True}

    monkeypatch.setattr(capacity, "stage", stage)
    assert capacity.main(["--run", *args]) == 2
    result = json.loads(out.read_text())
    assert result["status"] == "error" and result["error"] == "broken"
    assert result["highest_passed_rps"] == {"hot": 1}
    assert result["capacity_rps"] == {"hot": None}
    with pytest.raises(SystemExit):
        capacity.main(["--run", *args])
    assert json.loads(out.read_text()) == result


@pytest.mark.parametrize(
    "argv",
    [
        ["--rates", "60,40"],
        ["--rates", "0"],
        ["--scenarios", "cold"],
        ["--seconds", "0"],
        ["--base-url", "http://user:password@localhost:8000"],
        ["--base-url", "http://localhost/api"],
    ],
)
def test_invalid_config_before_io(argv):
    with pytest.raises(SystemExit):
        capacity.main(argv)
