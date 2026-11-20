"""Offline tests for the product HPA traffic limits, evidence and target identity."""

import sys
from pathlib import Path

import pytest
import yaml

from scripts import k8s_app_hpa as lab

MANIFEST = Path(__file__).resolve().parents[1] / "monitoring/kubernetes/app-lab"


def objects():
    objects = [
        o
        for source in ("api.yaml", "hpa.yaml")
        for o in yaml.safe_load_all((MANIFEST / source).read_text())
    ]
    for o in objects:
        o["metadata"]["uid"] = o["kind"]
        if o["kind"] == "Service":
            o["spec"]["clusterIP"] = "10.43.0.3"
            o["spec"].setdefault("type", "ClusterIP")
        elif o["kind"] == "Deployment":
            o["spec"]["replicas"] = 1
            o["status"] = {"readyReplicas": 1}
        else:
            o["status"] = {
                "desiredReplicas": 1,
                "currentMetrics": [
                    {
                        "resource": {
                            "name": "cpu",
                            "current": {"averageUtilization": 8, "averageValue": "2046100n"},
                        }
                    }
                ],
            }
    return objects


def test_snapshot_reads_real_manifests():
    state = lab.snapshot(objects())
    assert state["avg_cpu_mcores"] == 2.046
    assert state["cpu_percent"] == 8 and state["target_percent"] == 60
    assert state["ready"] == state["requested"] == state["hpa_desired"] == 1


def test_foreign_or_unbounded_target_rejected():
    objs = objects()
    hpa = next(o for o in objs if o["kind"] == "HorizontalPodAutoscaler")
    hpa["spec"]["maxReplicas"] = 10
    with pytest.raises(ValueError, match="one to two"):
        lab.snapshot(objs)
    hpa["spec"]["maxReplicas"] = 2
    svc = next(o for o in objs if o["kind"] == "Service")
    svc["metadata"]["labels"].pop("sre-lab-role")
    with pytest.raises(ValueError, match="Expected the app-lab"):
        lab.snapshot(objs)


def test_equal_rate_ready_windows_exclude_transition_and_crossing_requests():
    samples = [
        (0.1, 0.2, "product_200"),
        (4.9, 5.1, "product_200"),
        (6.1, 6.3, "http_503"),
        (11.0, 11.05, "product_200"),
        (12.0, 13.0, "TimeoutError"),
    ]
    by_ready = lab.split_by_ready(samples, [(0, 5, 1, 1), (5, 10, 1, 2), (10, 15, 2, 2)])
    assert by_ready["1"] == {
        "seconds": 5,
        "requests": 1,
        "product_200": 1,
        "errors": {},
        "achieved_rps": 0.2,
        "p95_ms": 100,
    }
    assert by_ready["2"]["p95_ms"] == 1000
    assert by_ready["2"]["errors"] == {"TimeoutError": 1}
    assert lab.stats([], 0)["p95_ms"] is None


def test_run_validates_identity_across_phases(monkeypatch):
    original = lab.snapshot(objects())
    foreign = dict(original, uids=["replacement", "Service", "HorizontalPodAutoscaler"])
    states = iter((original, foreign))
    monkeypatch.setattr(lab, "observe", lambda: next(states))
    with pytest.raises(RuntimeError, match="identity changed"):
        lab.exercise(40, {})


def test_plan_and_out_of_range_rates_never_access_k3s(monkeypatch, tmp_path):
    def forbidden():
        pytest.fail("unexpected live kubectl call")

    monkeypatch.setattr(lab, "observe", forbidden)
    output = tmp_path / "hpa.json"
    monkeypatch.setattr(sys, "argv", ["hpa", "--output", str(output)])
    lab.main()
    assert not output.exists()
    monkeypatch.setattr(sys, "argv", ["hpa", "--run", "--rps", "1000"])
    with pytest.raises(SystemExit) as exc:
        lab.main()
    assert exc.value.code == 2


def test_get_product_checks_response_and_closes_connection(monkeypatch):
    calls = []

    class Response:
        status = 200

        def read(self, limit):
            return b'{"id": 99}'

    class Connection:
        def __init__(self, address, port, timeout):
            calls.append((address, port, timeout))

        def request(self, method, path, headers):
            calls.append((method, path))

        def getresponse(self):
            return Response()

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(lab.http.client, "HTTPConnection", Connection)
    assert lab.get_product("10.43.0.3") == "invalid_product"
    assert calls == [("10.43.0.3", 8000, 2), ("GET", "/products/1"), "closed"]
