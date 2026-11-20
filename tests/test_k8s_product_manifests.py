"""Integration contracts for the isolated API rollout and HPA manifests."""

import json
import re
from pathlib import Path

import yaml

from app.metrics import Metrics

MANIFESTS = Path(__file__).resolve().parents[1] / "monitoring/kubernetes/app-lab"


def objects():
    docs = [
        doc for path in MANIFESTS.glob("*.yaml") for doc in yaml.safe_load_all(path.read_text())
    ]
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}


def test_api_and_cache_use_only_the_lab_namespace_and_services():
    resources = objects()
    ns = resources["Namespace", "sre-app-lab"]
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    for (kind, _), doc in resources.items():
        if kind != "Namespace":
            assert doc["metadata"]["namespace"] == ns["metadata"]["name"]
    for name in ("product-api", "redis"):
        svc = resources["Service", name]["spec"]
        deployment = resources["Deployment", name]["spec"]
        pod = deployment["template"]
        assert svc["type"] == "ClusterIP"
        assert svc["selector"] == deployment["selector"]["matchLabels"]
        assert svc["selector"].items() <= pod["metadata"]["labels"].items()
        container = pod["spec"]["containers"][0]
        assert svc["ports"][0]["targetPort"] in {p["name"] for p in container["ports"]}
        assert pod["spec"]["automountServiceAccountToken"] is False
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
    api = resources["Deployment", "product-api"]["spec"]["template"]["spec"]["containers"][0]
    env = {var["name"]: var["value"] for var in api["env"]}
    assert env["REDIS_URL"] == "redis://redis:6379/0"
    assert env["LAB_ENABLED"] == "false" and "APP_VERSION" not in env
    assert api["imagePullPolicy"] == "Never"
    assert api["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    assert api["livenessProbe"]["httpGet"]["path"] == "/health/live"


def test_rollout_quota_covers_hpa_at_max_and_one_candidate():
    resources = objects()
    hpa = resources["HorizontalPodAutoscaler", "product-api"]["spec"]
    target = hpa["scaleTargetRef"]
    deployment = resources[target["kind"], target["name"]]["spec"]
    assert "replicas" not in deployment
    assert (hpa["minReplicas"], hpa["maxReplicas"]) == (1, 2)
    assert hpa["metrics"][0]["resource"]["name"] == "cpu"
    strategy = deployment["strategy"]["rollingUpdate"]
    assert strategy["maxUnavailable"] == 0 and strategy["maxSurge"] >= 1
    api_count = hpa["maxReplicas"] + strategy["maxSurge"]
    api = deployment["template"]["spec"]["containers"][0]["resources"]
    redis = resources["Deployment", "redis"]["spec"]["template"]["spec"]["containers"][0][
        "resources"
    ]
    quota = resources["ResourceQuota", "product-budget"]["spec"]["hard"]
    assert int(quota["pods"]) >= api_count + 1
    for group in ("requests", "limits"):
        for resource, unit in (("cpu", "m"), ("memory", "Mi")):
            api_amount = int(api[group][resource].removesuffix(unit))
            redis_amount = int(redis[group][resource].removesuffix(unit))
            assert api_amount > 0 and redis_amount > 0
            assert (
                int(quota[f"{group}.{resource}"].removesuffix(unit))
                >= api_count * api_amount + redis_amount
            )


def test_monitor_connects_existing_prometheus_to_api_metrics():
    monitor_file = MANIFESTS.parent / "app-lab-observability/product-monitor.yaml"
    monitor = yaml.safe_load(monitor_file.read_text())
    values = yaml.safe_load((MANIFESTS.parent / "values.yaml").read_text())
    selected = values["prometheus"]["prometheusSpec"]["serviceMonitorNamespaceSelector"]
    assert (
        selected["matchLabels"]["kubernetes.io/metadata.name"] == monitor["metadata"]["namespace"]
    )
    assert monitor["metadata"]["labels"]["release"] == "sre-k8s"
    svc = objects()["Service", "product-api"]
    spec = monitor["spec"]
    assert spec["namespaceSelector"]["matchNames"] == [svc["metadata"]["namespace"]]
    assert spec["selector"]["matchLabels"].items() <= svc["metadata"]["labels"].items()
    (endpoint,) = spec["endpoints"]
    assert endpoint["port"] == svc["spec"]["ports"][0]["name"] and endpoint["path"] == "/metrics"


def test_product_dashboard_queries_match_app_metrics_and_cluster_resources():
    path = MANIFESTS.parents[1] / "grafana/dashboards/k8s-product.json"
    dashboard = json.loads(path.read_text())
    resources = objects()
    ns = resources["Namespace", "sre-app-lab"]["metadata"]["name"]
    service = resources["Service", "product-api"]["metadata"]["name"]
    hpa = resources["HorizontalPodAutoscaler", "product-api"]["metadata"]["name"]
    exported = {sample.name for m in Metrics().registry.collect() for sample in m.samples}
    queries = []
    for panel in dashboard["panels"]:
        assert panel["datasource"]["uid"] == "kubernetes"
        for target in panel["targets"]:
            assert target["datasource"] == panel["datasource"]
            expr = target["expr"]
            assert f'namespace="{ns}"' in expr
            for metric in re.findall(r"(lab_\w+)\{", expr):
                assert metric in exported and f'service="{service}"' in expr
            if "kube_horizontalpodautoscaler_" in expr:
                assert f'horizontalpodautoscaler="{hpa}"' in expr
            queries.append(expr)
    assert any("histogram_quantile(0.95" in query for query in queries)
    assert any("container_cpu_usage_seconds_total" in query for query in queries)
