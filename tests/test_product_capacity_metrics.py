"""Capacity signals must describe overlapping product calls and real Redis commands."""

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import yaml
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.metrics import Metrics

MONITORING = Path(__file__).resolve().parents[1] / "monitoring"


def test_gauge_observes_overlapping_requests_and_returns_to_zero():
    redis = AsyncMock()
    app = create_app(Settings(redis_timeout_seconds=3), redis)
    registry = app.state.metrics.registry
    reached_two = asyncio.Event()
    counts = []

    async def blocked_get(_key):
        counts.append(registry.get_sample_value("lab_product_requests_in_flight"))
        if len(counts) == 2:
            reached_two.set()
        await reached_two.wait()

    redis.get.side_effect = blocked_get
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(client.get, f"/products/{i}") for i in (1, 2)]
        assert [req.result(timeout=5).status_code for req in requests] == [200, 200]
    assert counts == [1, 2]
    assert registry.get_sample_value("lab_product_requests_in_flight") == 0
    for command in ("GET", "SET"):
        assert registry.get_sample_value(
            "lab_product_redis_command_duration_seconds_count", {"command": command}
        ) == 2


def test_redis_metrics_include_timeouts_and_exclude_health_and_cache_hits_from_set():
    redis = AsyncMock()
    app = create_app(Settings(redis_timeout_seconds=0.05), redis)
    registry = app.state.metrics.registry
    redis.get.return_value = '{"id":1,"name":"P","price_cents":1,"stock":1}'

    async def hang(_key):
        await asyncio.sleep(3)

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/products/1").headers["X-Cache"] == "HIT"
        redis.get.side_effect = hang
        assert client.get("/products/1").status_code == 503
    assert registry.get_sample_value("lab_product_requests_in_flight") == 0
    duration = "lab_product_redis_command_duration_seconds"
    assert registry.get_sample_value(duration + "_count", {"command": "GET"}) == 2
    assert registry.get_sample_value(duration + "_sum", {"command": "GET"}) >= 0.04
    assert registry.get_sample_value(duration + "_count", {"command": "SET"}) == 0


def test_dashboard_uses_exported_metrics_and_scraped_cluster_series():
    path = MONITORING / "grafana/dashboards/k8s-capacity.json"
    dashboard = json.loads(path.read_text())
    assert dashboard["uid"] == "sre-k8s-capacity"
    namespace, = dashboard["templating"]["list"]
    assert namespace["name"] == "namespace"
    assert namespace["current"]["value"] == "sre-capacity-lab"
    assert set(namespace["query"].split(",")) == {"sre-capacity-lab", "sre-app-lab"}
    exported = {sample.name for family in Metrics().registry.collect() for sample in family.samples}
    metrics = set()
    expressions = []
    for panel in dashboard["panels"]:
        assert panel["datasource"]["uid"] == "kubernetes"
        for target in panel["targets"]:
            expr = target["expr"]
            expressions.append(expr)
            names = re.findall(r'(\w+)\{', expr)
            for name in names:
                if name.startswith("node_"):
                    assert 'job="node-exporter"' in expr
                else:
                    assert 'namespace="${namespace}"' in expr
                if name.startswith("lab_"):
                    assert name in exported and 'service="product-api"' in expr
            metrics.update(names)
    assert {
        "lab_http_requests_total", "lab_http_request_duration_seconds_bucket",
        "lab_product_requests_in_flight", "lab_product_redis_command_duration_seconds_bucket",
        "container_cpu_cfs_throttled_seconds_total", "node_cpu_seconds_total",
        "node_memory_MemAvailable_bytes", "node_filesystem_avail_bytes",
        "kube_deployment_status_replicas_ready",
    } <= metrics
    for q in ("0.95", "0.99"):
        assert any(f"histogram_quantile({q}," in expr for expr in expressions)
    values = yaml.safe_load((MONITORING / "kubernetes/values.yaml").read_text())
    rules = values["kubelet"]["serviceMonitor"]["cAdvisorMetricRelabelings"]
    labels = {
        "__name__": "container_cpu_cfs_throttled_seconds_total",
        "pod": "product-api-demo", "id": "/kubepods/pod", "namespace": "sre-capacity-lab"
    }
    for rule in rules:
        if rule["action"] == "drop":
            value = rule.get("separator", ";").join(labels.get(k, "") for k in rule["sourceLabels"])
            assert not re.fullmatch(rule["regex"], value)
