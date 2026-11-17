"""Verify the optional Kubernetes lab; only --exercise-recovery deletes a demo Pod."""

import argparse
import json
import math
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


def kubectl(*args):
    result = subprocess.run(
        ["/usr/local/bin/k3s", "kubectl", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout


def get_json(url):
    with urlopen(url, timeout=15) as response:
        return json.load(response)


def ready(pod):
    return any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in pod.get("status", {}).get("conditions", [])
    ) and not pod["metadata"].get("deletionTimestamp")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument("--output", default="reports/kubernetes-acceptance.json")
    parser.add_argument("--exercise-recovery", action="store_true")
    args = parser.parse_args()
    base = args.prometheus_url.rstrip("/")
    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    if not nodes or not all(ready(node) for node in nodes):
        raise RuntimeError("Kubernetes nodes are not Ready")
    targets = get_json(base + "/api/v1/targets?state=active")["data"]["activeTargets"]
    if not targets or any(t["health"] != "up" for t in targets):
        raise RuntimeError("At least one configured Prometheus target is not UP")
    queries = {
        "node_ready": 'kube_node_status_condition{condition="Ready",status="true"}',
        "node_cpu": 'rate(node_cpu_seconds_total{mode="idle"}[2m])',
        "node_memory": "node_memory_MemAvailable_bytes",
        "node_disk": 'node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"}',
        "pod_cpu": (
            'rate(container_cpu_usage_seconds_total{namespace="sre-k8s-demo",'
            'container!="",container!="POD"}[2m])'
        ),
        "pod_memory": (
            'container_memory_working_set_bytes{namespace="sre-k8s-demo",'
            'container!="",container!="POD"}'
        ),
        "pod_restarts": 'kube_pod_container_status_restarts_total{namespace="sre-k8s-demo"}',
        "deployment_available": (
            'kube_deployment_status_replicas_available{namespace="sre-k8s-demo"}'
        ),
    }
    samples = {}
    for name, query in queries.items():
        result = get_json(base + "/api/v1/query?" + urlencode({"query": query}))
        if result.get("status") != "success" or not result["data"]["result"]:
            raise RuntimeError("Missing metric samples: " + name)
        if not all(math.isfinite(float(s["value"][1])) for s in result["data"]["result"]):
            raise RuntimeError("Non-finite metric samples: " + name)
        samples[name] = result["data"]["result"]
    report = {
        "verified_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 -- host Python 3.9
        "nodes": [
            {
                "name": n["metadata"]["name"],
                "architecture": n["status"]["nodeInfo"]["architecture"],
                "kubelet_version": n["status"]["nodeInfo"]["kubeletVersion"],
            }
            for n in nodes
        ],
        "targets": [{"labels": t["labels"], "health": t["health"]} for t in targets],
        "metrics": samples,
        "recovery": {"tested": False},
    }
    if args.exercise_recovery:
        pods = json.loads(kubectl("-n", "sre-k8s-demo", "get", "pods", "-o", "json"))["items"]
        candidates = [
            pod
            for pod in pods
            if ready(pod)
            and any(r["kind"] == "ReplicaSet" for r in pod["metadata"].get("ownerReferences", []))
        ]
        if len(candidates) != 1:
            raise RuntimeError("Expected one Ready ReplicaSet-owned demo Pod")
        old = candidates[0]
        controller_uid = old["metadata"]["ownerReferences"][0]["uid"]
        started = time.monotonic()
        kubectl("-n", "sre-k8s-demo", "delete", "pod", old["metadata"]["name"], "--wait=true")
        while time.monotonic() - started < 180:
            pods = json.loads(kubectl("-n", "sre-k8s-demo", "get", "pods", "-o", "json"))["items"]
            replacements = [
                pod
                for pod in pods
                if ready(pod)
                and pod["metadata"]["uid"] != old["metadata"]["uid"]
                and any(
                    r["uid"] == controller_uid for r in pod["metadata"].get("ownerReferences", [])
                )
            ]
            if replacements:
                report["recovery"] = {
                    "tested": True,
                    "old_pod": old["metadata"]["name"],
                    "old_uid": old["metadata"]["uid"],
                    "new_pod": replacements[0]["metadata"]["name"],
                    "new_uid": replacements[0]["metadata"]["uid"],
                    "ready_after_seconds": round(time.monotonic() - started, 2),
                    "mechanism": "Kubernetes ReplicaSet reconciliation after demo Pod deletion",
                }
                break
            time.sleep(2)
        else:
            raise RuntimeError("Demo Pod replacement did not become Ready within 180 seconds")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(output),
                "targets_up": len(targets),
                "metric_groups": len(samples),
                "recovery": report["recovery"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
