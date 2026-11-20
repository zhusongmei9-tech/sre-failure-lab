"""Render the fixed-replica workload and verify its isolation from the existing HPA."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

K8S = Path(__file__).resolve().parents[1] / "monitoring/kubernetes"


def test_capacity_overlay_reuses_the_product_resources_in_its_own_namespace():
    if shutil.which("kubectl"):
        renderer = ["kubectl", "kustomize"]
    elif shutil.which("kustomize"):
        renderer = ["kustomize", "build"]
    else:
        pytest.skip("requires kubectl or kustomize")
    rendered = subprocess.check_output([*renderer, K8S / "capacity-lab"], text=True)
    resources = {(o["kind"], o["metadata"]["name"]): o for o in yaml.safe_load_all(rendered)}
    ns = "sre-capacity-lab"
    assert resources["Namespace", ns]["metadata"]["labels"]["sre-lab-role"] == "capacity-lab"
    assert all(
        o["metadata"].get("namespace") == ns
        for (kind, _), o in resources.items()
        if kind != "Namespace"
    )
    assert not any(kind == "HorizontalPodAutoscaler" for kind, _ in resources)
    monitor = yaml.safe_load((K8S / "capacity-lab-observability/service-monitor.yaml").read_text())
    assert monitor["metadata"]["namespace"] == "sre-monitoring"
    assert monitor["spec"]["namespaceSelector"]["matchNames"] == [ns]
    service = resources["Service", "product-api"]
    assert monitor["spec"]["selector"]["matchLabels"].items() <= service["metadata"]["labels"].items()
    for name in ("product-api", "redis"):
        assert resources["Service", name]["spec"]["type"] == "ClusterIP"
        assert ("Deployment", name) in resources
    pod = resources["Deployment", "product-api"]["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == "sre-lab-api:capacity"
    assert ("NetworkPolicy", "redis-from-product-api") in resources
