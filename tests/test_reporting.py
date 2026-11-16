import json
import math

import pytest

from app.reporting import build_report, render_markdown, summarize_slo


def test_429_and_all_non_2xx_consume_request_budget():
    report = summarize_slo({"200": 990, "429": 5, "503": 5}, duration_seconds=100)
    assert report["success_ratio"] == 0.99
    assert report["target_met"] is True
    assert report["error_budget_consumed_ratio"] == pytest.approx(1)
    assert report["error_budget_remaining_ratio"] == pytest.approx(0, abs=1e-12)
    assert report["actual_rps"] == 10


def test_budget_overrun_and_empty_data():
    report = summarize_slo({"200": 90, "404": 10})
    assert report["error_budget_consumed_ratio"] == pytest.approx(10)
    assert report["error_budget_overrun_ratio"] == pytest.approx(9)
    assert report["error_budget_remaining_ratio"] == 0
    assert report["target_met"] is False
    for counts in ({}, {"200": 0}):
        empty = summarize_slo(counts)
        assert not empty["has_data"]
        for key in ("success_ratio", "target_met", "error_budget_remaining_ratio", "p95_seconds"):
            assert empty[key] is None


@pytest.mark.parametrize("counts", [{"200": -1}, {"200": 1.0}, {"200": True}, {"no": 2}, {"600": 1}, {200: 1, "200": 1}])
def test_invalid_counts_rejected(counts):
    with pytest.raises(ValueError):
        summarize_slo(counts)


@pytest.mark.parametrize("target", [0, 1, -0.1, 1.1, math.nan, math.inf, True, "0.99"])
def test_invalid_target_rejected(target):
    with pytest.raises(ValueError):
        summarize_slo({"200": 1}, target=target)


def test_latency_is_explicit_and_finite():
    with pytest.raises(ValueError):
        summarize_slo({"200": 1}, p95_seconds=0.2)
    with pytest.raises(ValueError):
        summarize_slo({"200": 1}, p95_seconds=math.nan, p95_method="exact")
    value = summarize_slo({"200": 1}, p95_seconds=.2, p95_method="prometheus_histogram_estimate")
    assert value["p95_seconds"] == .2
    assert "estimate" in value["p95_method"]


def test_export_is_allowlisted_and_uses_timezone():
    secret = "Bearer some-super-private-secret"
    report = build_report({
        "status_counts": {"200": 5}, "started_at": "2026-11-14T08:00:00+08:00",
        "ended_at": "2026-11-14T08:01:00+08:00", "duration_seconds": 60,
        "LAB_TOKEN": secret, "headers": {"Authorization": secret},
    }, [{
        "event_id": "a" * 32, "action": "fault_started", "timestamp": "2026-11-14T00:00:00Z",
        "details": {"token": secret, "traceback": secret}, "Authorization": secret,
    }], scope="component")
    assert report["validation_scope"] == "component"
    assert report["window"]["start"] == "2026-11-14T00:00:00+00:00"
    assert secret not in json.dumps(report)
    assert secret not in render_markdown(report)
    assert report["events"][0]["action"] == "fault_started"


def test_report_window_invalid_or_mismatched():
    with pytest.raises(ValueError):
        build_report({"window_start": "2026-11-14T00:00:00Z"}, [])
    with pytest.raises(ValueError):
        build_report({"window_start": "2026-11-14T00:00:00", "window_end": "2026-11-14T00:01:00"}, [])
    with pytest.raises(ValueError):
        build_report({"window_start": "2026-11-14T00:00:00Z", "window_end": "2026-11-14T00:01:00Z", "duration_seconds": 10}, [])


def test_markdown_escapes_arbitrary_cells_and_limits_events():
    events = [{"action": "fault_started", "timestamp": "2026-11-14T00:00:00Z"}] * 300
    report = build_report({}, events)
    assert len(report["events"]) == 200
    report["events"][0]["action"] = "<script> | injected\nline"
    markdown = render_markdown(report)
    assert "<script>" not in markdown
    assert "&lt;script&gt; &#124; injected line" in markdown
    assert "无数据" in markdown
