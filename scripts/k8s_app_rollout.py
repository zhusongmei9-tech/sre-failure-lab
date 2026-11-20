"""Roll a real product API between two lab images; restore a rejected readiness revision."""

import argparse
import copy
import fcntl
import ipaddress
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

NS = "sre-app-lab"
APP = "product-api"
VERSIONS = {"sre-lab-api:0.3.0": "0.3.0", "sre-lab-api:0.3.1": "0.3.1"}
READY = "/health/ready"
BAD = "/__sre_app_lab_rollout_unready__"
LABELS = {"app.kubernetes.io/part-of": "sre-failure-lab", "sre-lab-role": "product-lab"}


class DrillError(RuntimeError):
    pass


class Cluster:
    """Only the local K3s client and this namespace participate in the drill."""

    def run(self, *args):
        cmd = [
            "k3s",
            "kubectl",
            "--kubeconfig=/etc/rancher/k3s/k3s.yaml",
            "--request-timeout=6s",
            "-n",
            NS,
            *args,
        ]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=9)
        if proc.returncode:
            raise DrillError(f"kubectl {args[0]} failed: {proc.stderr.strip()[:400]}")
        return json.loads(proc.stdout)

    def get(self, resource):
        return self.run("get", resource, "-o", "json")

    def patch(self, uid, current, desired):
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "test", "path": "/spec/template", "value": current},
            {"op": "replace", "path": "/spec/template", "value": desired},
        ]
        self.run("patch", f"deployment/{APP}", "--type=json", "-p", json.dumps(patch), "-o", "json")


def guard(cluster):
    for kind, name in [("namespace", NS), ("deployment", APP), ("service", APP)]:
        obj = cluster.get(f"{kind}/{name}")
        if not LABELS.items() <= obj["metadata"].get("labels", {}).items():
            raise DrillError(f"{kind}/{name} lacks lab ownership labels")
        if kind == "deployment":
            deployment = obj
        if kind == "service":
            service = obj["spec"]
    template = deployment["spec"]["template"]
    container = template["spec"]["containers"][0]
    if not (
        deployment["spec"]["selector"]["matchLabels"] == {"app": APP}
        and len(template["spec"]["containers"]) == 1
        and container["name"] == "api"
        and container["image"] in VERSIONS
        and container["readinessProbe"]["httpGet"]["path"] in {READY, BAD}
        and deployment["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
        and service["type"] == "ClusterIP"
        and service["selector"] == {"app": APP}
        and len(service["ports"]) == 1
        and service["ports"][0]["port"] == 8000
        and not service.get("externalIPs")
    ):
        raise DrillError("Unexpected product Deployment or Service configuration")
    ip = ipaddress.ip_address(service["clusterIP"])
    address = f"[{ip}]" if ip.version == 6 else str(ip)
    return deployment["metadata"]["uid"], template, f"http://{address}:8000/products/1"


def p95(values):
    return round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 1) if values else None


class Traffic:
    def __init__(self, url):
        self.url = url
        self.opener = build_opener(ProxyHandler({}))
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self.sample, daemon=True)
        self.phase = "baseline"
        self.rows = []

    def sample(self):
        while not self.stop.is_set():
            phase = self.phase
            start = time.monotonic()
            result = "invalid_product_response"
            try:
                with self.opener.open(self.url, timeout=2) as resp:
                    version = resp.headers.get("X-App-Version")
                    if (
                        resp.status == 200
                        and version in VERSIONS.values()
                        and json.loads(resp.read(65536)).get("id") == 1
                    ):
                        result = version
            except (OSError, HTTPException, ValueError, AttributeError) as exc:
                result = "HTTP_" + str(exc.code) if hasattr(exc, "code") else type(exc).__name__
            self.rows.append((phase, result, (time.monotonic() - start) * 1000))
            self.stop.wait(0.25)

    def confirm(self, version):
        # Require a fresh sample from this phase, rather than reusing earlier traffic.
        end = time.monotonic() + 10
        index = len(self.rows)
        while time.monotonic() < end:
            if any(p == self.phase and r == version for p, r, _ in self.rows[index:]):
                return
            time.sleep(0.25)
        raise DrillError(f"Missing a successful {version} product response in {self.phase}")

    def report(self):
        summary = {}
        for phase in dict.fromkeys(row[0] for row in self.rows):
            rows = [(r, ms) for p, r, ms in self.rows if p == phase]
            summary[phase] = {
                "requests": len(rows),
                "failed": sum(result not in VERSIONS.values() for result, _ in rows),
                "responses": dict(Counter(result for result, _ in rows)),
                "p95_ms": p95([ms for _, ms in rows]),
            }
        return summary


def wait(cluster, uid, template, timeout=75):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dep = cluster.get(f"deployment/{APP}")
        if dep["metadata"]["uid"] != uid or dep["spec"]["template"] != template:
            raise DrillError("Another writer changed the product Deployment during the drill")
        n = dep["spec"]["replicas"]
        status = dep.get("status", {})
        if (
            n > 0
            and status.get("observedGeneration") == dep["metadata"]["generation"]
            and all(
                status.get(k, 0) == n
                for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
            )
        ):
            return
        time.sleep(1)
    raise TimeoutError("Product Deployment did not finish its rollout within the deadline")


def failed_candidate(cluster, uid):
    sets = cluster.get("replicasets")["items"]
    for rs in sets:
        owner = any(
            x.get("uid") == uid and x.get("controller")
            for x in rs["metadata"].get("ownerReferences", [])
        )
        if not owner or not rs["spec"]["replicas"]:
            continue
        container = rs["spec"]["template"]["spec"]["containers"][0]
        if (
            container["readinessProbe"]["httpGet"]["path"] == BAD
            and rs.get("status", {}).get("replicas", 0) > 0
            and rs["status"].get("readyReplicas", 0) == 0
        ):
            return rs["metadata"]["name"]
    raise DrillError("The bad candidate never reached the expected unready ReplicaSet state")


def restore(cluster, uid, attempted, stable):
    dep = cluster.get(f"deployment/{APP}")
    if dep["metadata"]["uid"] != uid:
        raise DrillError("Refusing recovery after the Deployment was replaced")
    current = dep["spec"]["template"]
    if current != stable:
        if current != attempted:
            raise DrillError("Refusing to overwrite an unrelated rollout")
        cluster.patch(uid, current, stable)
    wait(cluster, uid, stable)


def drill(cluster, report):
    uid, original, url = guard(cluster)
    original_api = original["spec"]["containers"][0]
    if original_api["readinessProbe"]["httpGet"]["path"] == BAD:
        # Recover a previous drill interrupted after the API server accepted its patch.
        good = copy.deepcopy(original)
        good["spec"]["containers"][0]["readinessProbe"]["httpGet"]["path"] = READY
        restore(cluster, uid, original, good)
        original, original_api = good, good["spec"]["containers"][0]
        report["recovered_interrupted_drill"] = True
    wait(cluster, uid, original)
    source = original_api["image"]
    target = next(image for image in VERSIONS if image != source)
    good = copy.deepcopy(original)
    good["spec"]["containers"][0]["image"] = target
    bad = copy.deepcopy(good)
    bad["spec"]["containers"][0]["readinessProbe"]["httpGet"]["path"] = BAD
    report.update(from_version=VERSIONS[source], to_version=VERSIONS[target], namespace=NS)
    traffic = Traffic(url)
    traffic.worker.start()
    attempted = None
    stable = original
    try:
        traffic.confirm(VERSIONS[source])
        traffic.phase = "rolling_update"
        attempted = good
        start = time.monotonic()
        cluster.patch(uid, original, good)
        wait(cluster, uid, good)
        traffic.confirm(VERSIONS[target])
        report["release_seconds"] = round(time.monotonic() - start, 2)
        stable = good
        traffic.phase = "rejected_readiness"
        attempted = bad
        cluster.patch(uid, good, bad)
        # A bounded failed rollout is the expected signal to restore the approved template.
        try:
            wait(cluster, uid, bad, timeout=18)
        except TimeoutError:
            report["unready_candidate"] = failed_candidate(cluster, uid)
        else:
            raise DrillError("Faulty revision unexpectedly became Ready")
        traffic.confirm(VERSIONS[target])
        start = time.monotonic()
        traffic.phase = "rollback"
        restore(cluster, uid, bad, good)
        traffic.confirm(VERSIONS[target])
        report["rollback_seconds"] = round(time.monotonic() - start, 2)
        report["restored_readiness"] = True
        attempted = None
    finally:
        try:
            if attempted is not None:
                restore(cluster, uid, attempted, stable)
                report["cleanup_restored_version"] = VERSIONS[
                    stable["spec"]["containers"][0]["image"]
                ]
        finally:
            traffic.stop.set()
            traffic.worker.join(timeout=4)
            report["traffic"] = traffic.report()
    if any(p["failed"] for p in report["traffic"].values()):
        raise DrillError("One or more sampled product requests failed; inspect the phase report")


def interrupt(signum, frame):
    raise KeyboardInterrupt


def utc_now():
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017 -- host Python 3.9


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="execute on the local K3s host")
    parser.add_argument("--output", type=Path, help="optional JSON report file")
    args = parser.parse_args()
    if not args.run:
        print(
            "Plan: sample product 1 throughout a two-version rollout, reject a bad readiness "
            "probe, restore the approved template and verify the released version."
        )
        return 0
    signal.signal(signal.SIGTERM, interrupt)
    fd = os.open(Path(__file__).resolve().parent, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another app-lab exercise is running on this host", file=sys.stderr)
            return 1

        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
        report = {"passed": False, "started_at": utc_now()}
        try:
            drill(Cluster(), report)
            report["passed"] = True
        except (
            DrillError,
            OSError,
            ValueError,
            KeyError,
            IndexError,
            subprocess.SubprocessError,
            KeyboardInterrupt,
        ) as exc:
            report["error"] = str(exc) or type(exc).__name__
        report["finished_at"] = utc_now()
        text = json.dumps(report, indent=2, ensure_ascii=False)
        print(text)
        if args.output:
            temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
            try:
                temporary.write_text(text + "\n")
                os.replace(temporary, args.output)
            finally:
                temporary.unlink(missing_ok=True)
        return 0 if report["passed"] else 1
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
