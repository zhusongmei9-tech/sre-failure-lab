"""Read-only before/after host reboot evidence. Never reboots or repairs services."""

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

SERVICES = ("k3s.service", "sre-lab-k8s-prometheus.service")
DEPLOYMENTS = (
    ("sre-monitoring", "sre-k8s-operator"),
    ("sre-monitoring", "sre-k8s-kube-state-metrics"),
    ("sre-k8s-demo", "demo-web"),
)
PROMETHEUS_STS = "prometheus-sre-k8s-prometheus"


class CheckError(RuntimeError):
    """Credential-free failure description."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CheckError("Prometheus bridge must not redirect to another endpoint")


def now():
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017


def remaining(deadline, maximum=10):
    left = deadline - time.monotonic()
    if left <= 0:
        raise CheckError("Observation deadline exceeded")
    return min(left, maximum)


def run(command, deadline):
    try:
        return subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=remaining(deadline)
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckError("Read-only command failed: " + type(exc).__name__) from None


def host_identity():
    if platform.system() != "Linux":
        raise CheckError("Reboot acceptance requires the actual Linux K3s host")
    machine = Path("/etc/machine-id").read_text().strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", machine) or machine == "0" * 32:
        raise CheckError("A valid persistent machine-id is required")
    boot = str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))
    return {
        "machine_id_sha256": hashlib.sha256(machine.encode()).hexdigest(),
        "boot_id": boot,
        "hostname": platform.node(),
        "uptime_seconds": float(Path("/proc/uptime").read_text().split()[0]),
    }


class Client:
    def __init__(self, kind="k3s", kubeconfig="/etc/rancher/k3s/k3s.yaml"):
        self.kind = kind
        self.kubeconfig = str(kubeconfig)

    def command(self, *arguments):
        binary = shutil.which(self.kind)
        if not binary:
            raise CheckError("Selected Kubernetes client is not installed")
        prefix = [binary, "kubectl"] if self.kind == "k3s" else [binary]
        return prefix + ["--kubeconfig", self.kubeconfig, "--request-timeout=8s", *arguments]

    def text(self, deadline, *arguments):
        return run(self.command(*arguments), deadline)

    def get(self, deadline, resource, name=None, namespace=None):
        arguments = ["get", resource]
        if name:
            arguments.append(name)
        if namespace:
            arguments.extend(["--namespace", namespace])
        return json.loads(self.text(deadline, *arguments, "--output=json"))


def service_status(unit, deadline):
    output = run(
        [
            "systemctl",
            "show",
            unit,
            "--no-pager",
            "--property=LoadState,ActiveState,SubState,UnitFileState,ActiveEnterTimestampMonotonic",
        ],
        deadline,
    )
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    return {
        key: fields.get(key)
        for key in (
            "LoadState",
            "ActiveState",
            "SubState",
            "UnitFileState",
            "ActiveEnterTimestampMonotonic",
        )
    }


def metadata(obj):
    data = obj["metadata"]
    if not data.get("uid") or data.get("deletionTimestamp"):
        raise CheckError("Required object is missing its UID or is being deleted")
    return {"name": data["name"], "uid": data["uid"], "generation": data.get("generation")}


def workload(obj):
    item = metadata(obj)
    desired = obj.get("spec", {}).get("replicas", 1)
    status = obj.get("status", {})
    if not isinstance(desired, int) or desired < 1:
        raise CheckError("Required workload must have at least one desired replica")
    if (
        status.get("observedGeneration", 0) < item["generation"]
        or status.get("readyReplicas", 0) < desired
        or status.get("updatedReplicas", 0) != desired
    ):
        raise CheckError("Required workload has not completed its healthy rollout")
    if obj.get("kind") == "Deployment" and status.get("availableReplicas", 0) < desired:
        raise CheckError("Required Deployment is not Available")
    item.update(
        desired_replicas=desired,
        ready_replicas=status["readyReplicas"],
        observed_generation=status["observedGeneration"],
        current_revision=status.get("currentRevision"),
        update_revision=status.get("updateRevision"),
    )
    return item


def ready_pods(client, obj, namespace, deadline):
    labels = obj.get("spec", {}).get("selector", {}).get("matchLabels", {})
    if not labels:
        raise CheckError("Required workload has no verifiable Pod selector")
    selector = ",".join(key + "=" + value for key, value in sorted(labels.items()))
    payload = json.loads(
        client.text(
            deadline,
            "get",
            "pods",
            "--namespace",
            namespace,
            "--selector",
            selector,
            "--output=json",
        )
    )
    pods = []
    for pod in payload["items"]:
        if pod["metadata"].get("deletionTimestamp"):
            continue
        status = pod.get("status", {})
        containers = status.get("containerStatuses", [])
        ready = (
            status.get("phase") == "Running"
            and any(
                c["type"] == "Ready" and c["status"] == "True" for c in status.get("conditions", [])
            )
            and bool(containers)
            and len(containers) == len(pod.get("spec", {}).get("containers", []))
            and all(c.get("ready") and c.get("state", {}).get("running") for c in containers)
        )
        pods.append(
            dict(
                metadata(pod),
                ready=ready,
                phase=status.get("phase"),
                containers=[
                    {
                        "name": c["name"],
                        "restart_count": c.get("restartCount"),
                        "started_at": c.get("state", {}).get("running", {}).get("startedAt"),
                    }
                    for c in containers
                ],
            )
        )
    if sum(pod["ready"] for pod in pods) < obj.get("spec", {}).get("replicas", 1):
        raise CheckError("Required workload lacks enough Running and Ready Pods")
    return pods


def local_prometheus(url, deadline):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.port != 19090
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise CheckError(
            "Prometheus bridge URL must be http://<local IPv4>:19090 without credentials"
        )
    try:
        address = ipaddress.IPv4Address(parsed.hostname)
    except (ipaddress.AddressValueError, TypeError):
        raise CheckError("Prometheus bridge URL requires a literal local IPv4 address") from None
    addresses = json.loads(run(["ip", "-json", "-4", "address", "show"], deadline))
    local = {
        info["local"]
        for interface in addresses
        for info in interface.get("addr_info", [])
        if info.get("family") == "inet"
    }
    if str(address) not in local:
        raise CheckError("Prometheus bridge URL does not belong to this machine")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(url.rstrip("/") + "/-/ready", timeout=remaining(deadline, 3)) as response:
        if response.status != 200 or len(response.read(65537)) > 65536:
            raise CheckError("Prometheus bridge readiness failed")
    return {"url": url.rstrip("/"), "http_status": 200}


def collect(client, prometheus_url, deadline):
    evidence = {"observed_at": now(), "healthy": False}
    stage = "host_identity"
    try:
        evidence["host"] = host_identity()
        stage = "services"
        evidence["services"] = {unit: service_status(unit, deadline) for unit in SERVICES}
        for state in evidence["services"].values():
            if (
                state["LoadState"],
                state["ActiveState"],
                state["SubState"],
                state["UnitFileState"],
            ) != ("loaded", "active", "running", "enabled"):
                raise CheckError(
                    "K3s and bridge services must both be active/running and persistently enabled"
                )
        stage = "local_cluster"
        server = client.text(
            deadline,
            "config",
            "view",
            "--minify",
            "--output=jsonpath={.clusters[0].cluster.server}",
        )
        parsed = urlsplit(server)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.port != 6443
        ):
            raise CheckError("Reboot acceptance must use this host's loopback K3s API on 6443")
        namespace = client.get(deadline, "namespace", "kube-system")
        evidence["cluster_uid"] = metadata(namespace)["uid"]
        nodes = client.get(deadline, "nodes")["items"]
        if len(nodes) != 1:
            raise CheckError("This acceptance is restricted to one K3s node")
        node = nodes[0]
        node_info = node["status"]["nodeInfo"]
        conditions = {item["type"]: item["status"] for item in node["status"]["conditions"]}
        evidence["node"] = dict(
            metadata(node),
            boot_id=node_info["bootID"],
            conditions=conditions,
            kubelet_version=node_info["kubeletVersion"],
            architecture=node_info["architecture"],
        )
        if node_info["bootID"] != evidence["host"]["boot_id"]:
            raise CheckError("Kubernetes node bootID does not match the local host")
        if conditions.get("Ready") != "True" or any(
            conditions.get(key) == "True"
            for key in ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")
        ):
            raise CheckError("Kubernetes node is not Ready or is under pressure")
        stage = "deployments"
        evidence["deployments"] = {}
        evidence["pods"] = {}
        for namespace_name, name in DEPLOYMENTS:
            obj = client.get(deadline, "deployment", name, namespace_name)
            key = namespace_name + "/" + name
            evidence["deployments"][key] = workload(obj)
            evidence["pods"][key] = ready_pods(client, obj, namespace_name, deadline)
        stage = "prometheus_statefulset"
        obj = client.get(deadline, "statefulset", PROMETHEUS_STS, "sre-monitoring")
        evidence["statefulset"] = workload(obj)
        evidence["pods"]["sre-monitoring/" + PROMETHEUS_STS] = ready_pods(
            client, obj, "sre-monitoring", deadline
        )
        if evidence["statefulset"]["desired_replicas"] != 1:
            raise CheckError("Expected one Prometheus replica")
        stage = "persistent_storage"
        templates = obj["spec"].get("volumeClaimTemplates", [])
        if not templates:
            raise CheckError("Prometheus must have a persistent volume claim")
        ordinal = obj["spec"].get("ordinals", {}).get("start", 0)
        evidence["volumes"] = {}
        for template in templates:
            name = template["metadata"]["name"] + "-" + PROMETHEUS_STS + "-" + str(ordinal)
            pvc = client.get(deadline, "pvc", name, "sre-monitoring")
            claim = metadata(pvc)
            if pvc.get("status", {}).get("phase") != "Bound" or not pvc["spec"].get("volumeName"):
                raise CheckError("Prometheus PVC is not Bound")
            pv = client.get(deadline, "pv", pvc["spec"]["volumeName"])
            volume = metadata(pv)
            if (
                pv.get("status", {}).get("phase") != "Bound"
                or pv["spec"].get("claimRef", {}).get("uid") != claim["uid"]
            ):
                raise CheckError("PV is not Bound to the expected persistent claim")
            evidence["volumes"][name] = {
                "pvc_uid": claim["uid"],
                "pv_name": volume["name"],
                "pv_uid": volume["uid"],
                "phase": "Bound",
                "capacity": pvc["status"].get("capacity", {}),
                "storage_class": pvc["spec"].get("storageClassName"),
            }
        stage = "prometheus_bridge"
        evidence["prometheus"] = local_prometheus(prometheus_url, deadline)
        stage = "demo_http"
        body = client.text(
            deadline, "get", "--raw=/api/v1/namespaces/sre-k8s-demo/services/demo-web:8080/proxy/"
        )
        if body.strip() != "SRE Kubernetes demo is ready":
            raise CheckError("Demo service did not return its expected business response")
        evidence["demo_http"] = {
            "expected_body_observed": True,
            "transport": "local Kubernetes API service proxy",
        }
        evidence["healthy"] = True
    except (CheckError, OSError, ValueError, KeyError, TypeError) as exc:
        evidence["error"] = {
            "stage": stage,
            "kind": type(exc).__name__,
            "message": str(exc)
            if isinstance(exc, CheckError)
            else "Required evidence could not be read or validated",
        }
    return evidence


def compare(before, after):
    errors = []
    if before["host"]["machine_id_sha256"] != after["host"]["machine_id_sha256"]:
        errors.append("machine_changed")
    if before["host"]["boot_id"] == after["host"]["boot_id"]:
        errors.append("boot_id_unchanged")
    if before["cluster_uid"] != after["cluster_uid"]:
        errors.append("cluster_changed")
    if before["node"]["uid"] != after["node"]["uid"]:
        errors.append("node_replaced")
    for key in ("deployments", "volumes"):
        if set(before[key]) != set(after[key]):
            errors.append(key + "_set_changed")
    for name, item in before["deployments"].items():
        other = after["deployments"].get(name, {})
        if any(
            item[field] != other.get(field) for field in ("uid", "generation", "desired_replicas")
        ):
            errors.append("deployment_changed:" + name)
    if any(
        before["statefulset"][key] != after["statefulset"][key]
        for key in ("uid", "generation", "desired_replicas", "current_revision", "update_revision")
    ):
        errors.append("statefulset_changed")
    for name, item in before["volumes"].items():
        other = after["volumes"].get(name, {})
        if any(item[key] != other.get(key) for key in ("pvc_uid", "pv_name", "pv_uid")):
            errors.append("persistent_volume_changed:" + name)
    return errors


def validate_baseline(report):
    if (
        not isinstance(report, dict)
        or type(report.get("schema_version")) is not int
        or report.get("schema_version") != 1
        or report.get("kind") != "k8s_reboot_acceptance"
        or report.get("phase") != "before"
        or report.get("status") != "ready_for_reboot"
    ):
        raise CheckError("A successful before-stage baseline is required")
    evidence = report.get("evidence", {})
    if evidence.get("healthy") is not True:
        raise CheckError("Baseline health was not verified")
    for key in (
        "host",
        "cluster_uid",
        "node",
        "deployments",
        "statefulset",
        "volumes",
        "prometheus",
        "services",
        "pods",
        "demo_http",
    ):
        if not evidence.get(key):
            raise CheckError("Baseline lacks required identity and health evidence")
    if set(evidence["deployments"]) != {ns + "/" + name for ns, name in DEPLOYMENTS}:
        raise CheckError("Baseline lacks required Deployments")
    try:
        str(uuid.UUID(evidence["host"]["boot_id"]))
        if not re.fullmatch(r"[0-9a-f]{64}", evidence["host"]["machine_id_sha256"]):
            raise ValueError()
        compare(evidence, evidence)
    except (KeyError, ValueError, TypeError):
        raise CheckError("Baseline identity records are malformed") from None
    return evidence


def observe(phase, client, prometheus_url=None, baseline=None, timeout=180):
    report = {
        "schema_version": 1,
        "kind": "k8s_reboot_acceptance",
        "phase": phase,
        "timestamp": now(),
        "status": "failed",
        "reboot_requested_by_script": False,
    }
    try:
        before = validate_baseline(baseline) if phase == "after" else None
        if before:
            host = host_identity()
            report["observed_host"] = host
            if host["machine_id_sha256"] != before["host"]["machine_id_sha256"]:
                raise CheckError("machine_changed: baseline belongs to another machine")
            if host["boot_id"] == before["host"]["boot_id"]:
                raise CheckError("boot_id_unchanged: an actual host reboot has not been observed")
            prometheus_url = before["prometheus"]["url"]
        deadline = time.monotonic() + timeout
        while True:
            evidence = collect(client, prometheus_url, deadline)
            report["evidence"] = evidence
            # A different reachable cluster must not become acceptable during retry.
            if (
                before
                and evidence.get("cluster_uid")
                and evidence["cluster_uid"] != before["cluster_uid"]
            ):
                raise CheckError("cluster_changed: refusing a replacement cluster")
            if evidence["healthy"]:
                errors = compare(before, evidence) if before else []
                report["comparison_errors"] = errors
                if not errors:
                    report["status"] = "passed" if before else "ready_for_reboot"
                break
            if phase == "before" or time.monotonic() >= deadline:
                break
            time.sleep(min(3, max(0, deadline - time.monotonic())))
    except (CheckError, OSError, ValueError, KeyError, TypeError) as exc:
        report["error"] = (
            str(exc)
            if isinstance(exc, CheckError)
            else "Required reboot evidence is missing or invalid"
        )
    return report


def save_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("before", "after"))
    parser.add_argument("--client", choices=("k3s", "kubectl"), default="k3s")
    parser.add_argument("--kubeconfig", default="/etc/rancher/k3s/k3s.yaml")
    parser.add_argument("--prometheus-url", help="before: http://local-Docker-gateway:19090")
    parser.add_argument("--baseline", type=Path, help="after: the original before report")
    parser.add_argument(
        "--output", type=Path, required=True, help="new evidence file; never overwrites"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="after readiness observation window, 1..600 seconds",
    )
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error("--timeout must be 1..600")
    if args.output.exists():
        parser.error("--output already exists; choose a new report path")
    if args.phase == "before" and (not args.prometheus_url or args.baseline):
        parser.error("before requires --prometheus-url and does not accept --baseline")
    if args.phase == "after" and (not args.baseline or args.prometheus_url):
        parser.error("after requires --baseline and reuses its recorded Prometheus URL")
    try:
        baseline = json.loads(args.baseline.read_text()) if args.baseline else None
        report = observe(
            args.phase,
            Client(args.client, args.kubeconfig),
            args.prometheus_url,
            baseline,
            args.timeout,
        )
        save_report(args.output, report)
    except (OSError, ValueError) as exc:
        parser.exit(1, "Could not read or preserve evidence: " + type(exc).__name__ + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "phase": args.phase,
                "report": str(args.output),
                "error": report.get("error"),
                "comparison_errors": report.get("comparison_errors", []),
            },
            indent=2,
        )
    )
    return 0 if report["status"] in ("passed", "ready_for_reboot") else 1


if __name__ == "__main__":
    raise SystemExit(main())
