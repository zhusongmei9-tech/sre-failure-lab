"""Plan a ten-minute lab; only explicit --execute deletes one verified demo Pod.

Host runtime: Python 3.9+, standard library. No cluster access in plan mode.
"""

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "sre-k8s-demo"
DEPLOYMENT = "demo-web"
LABELS = {
    "app.kubernetes.io/part-of": "sre-failure-lab",
    "app.kubernetes.io/managed-by": "sre-failure-lab",
    "sre-lab-role": "demo",
}
BODY = b"SRE Kubernetes demo is ready\n"
BASELINE_SECONDS = 10
FINAL_SECONDS = 10
RECOVERY_SECONDS = 120
HTTP_RECOVERY_SECONDS = 20
MAX_SECONDS = 240
RPS = 2
HTTP_TIMEOUT = 1


class DemoError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017 -- Python 3.9 host


def plan():
    return {
        "mode": "plan",
        "status": "not_tested",
        "cluster_accessed": False,
        "recovery_tested": False,
        "namespace": NAMESPACE,
        "deployment": DEPLOYMENT,
        "steps": [
            {"minutes": "0-2", "action": "Read this plan; select an isolated lab context."},
            {"minutes": "2-4", "action": "Apply demo.yaml explicitly; wait for one Ready demo Pod."},
            {"minutes": "4-5", "action": "Start a loopback port-forward; verify the demo HTTP body."},
            {"minutes": "5-8", "action": "Run --execute --context EXACT --url LOOPBACK_URL."},
            {"minutes": "8-10", "action": "Review JSON: new Pod UID/Ready, errors and P95 separately."},
        ],
        "execution_bound_seconds": MAX_SECONDS,
        "traffic": {"target_rps": RPS, "workers": 1, "request_timeout_seconds": HTTP_TIMEOUT},
        "limits": [
            "Planning and mocked tests do not prove Kubernetes recovery.",
            "One replica can be unavailable; port-forward reconnect time affects HTTP results.",
            "Do not run deployments, scale changes or another demo concurrently.",
            "The script never deletes the Deployment, namespace, Service or PVC.",
        ],
    }


def loopback_url(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if host == "localhost":
            host = "127.0.0.1"  # Do not depend on DNS or the hosts file.
        valid = (
            parsed.scheme == "http"
            and host
            and ipaddress.ip_address(host).is_loopback
            and parsed.port is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path in ("", "/")
        )
        if not valid:
            raise ValueError
        address = "[" + host + "]" if ":" in host else host
        return "http://" + address + ":" + str(parsed.port) + "/"
    except (ValueError, TypeError, AttributeError):
        raise DemoError("Supply an http://loopback:PORT/ URL without credentials or query") from None


class Kubectl:
    def __init__(self, context, deadline, clock=time.monotonic):
        self.context, self.deadline, self.clock = context, deadline, clock

    def call(self, *args, scoped=True, body=None):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise DemoError("Overall execution deadline exceeded")
        command = ["kubectl"]
        if scoped:
            command += ["--context", self.context, "--request-timeout=8s"]
        command += list(args)
        try:
            result = subprocess.run(
                command, input=body, capture_output=True, text=True,
                timeout=min(10, remaining), check=True,
            )
            return result.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            # kubectl stderr can contain server URLs or credential-plugin output.
            raise DemoError("kubectl operation failed: " + type(exc).__name__) from None

    def check_context(self):
        if self.call("config", "current-context", scoped=False).strip() != self.context:
            raise DemoError("--context does not exactly match kubectl current-context")

    def get(self, kind, name=None):
        args = ["get", kind]
        if kind != "namespace":
            args += ["--namespace", NAMESPACE]
        if name:
            args.append(name)
        try:
            return json.loads(self.call(*args, "-o", "json"))
        except ValueError:
            raise DemoError("kubectl did not return valid JSON") from None

    def delete_pod(self, pod):
        metadata = pod["metadata"]
        options = {
            "apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 5,
            "preconditions": {"uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"]},
        }
        # Ordinary `kubectl delete pod NAME` has no UID/resourceVersion guard.
        self.call(
            "delete", "--raw", "/api/v1/namespaces/" + NAMESPACE + "/pods/" + metadata["name"],
            "-f", "-", body=json.dumps(options),
        )


def metadata(obj, kind, name=None):
    if obj.get("kind") != kind:
        raise DemoError("Unexpected Kubernetes resource kind")
    data = obj.get("metadata", {})
    actual = data.get("name", "")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", actual):
        raise DemoError("Unexpected resource name")
    if name is not None and actual != name:
        raise DemoError("Unexpected resource name")
    if kind != "Namespace" and data.get("namespace") != NAMESPACE:
        raise DemoError("Resource is outside the demo namespace")
    try:
        uuid.UUID(data["uid"])
        if not re.fullmatch(r"[0-9]+", data["resourceVersion"]):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise DemoError("Resource UID/resourceVersion is missing or invalid") from None
    if any(data.get("labels", {}).get(key) != value for key, value in LABELS.items()):
        raise DemoError("Resource lacks the required lab management labels")
    if kind != "Namespace" and data.get("labels", {}).get("app") != DEPLOYMENT:
        raise DemoError("Resource has the wrong app label")
    return data


def owner(obj, kind, name, uid):
    references = obj["metadata"].get("ownerReferences", [])
    if len(references) != 1:
        raise DemoError("Expected one unambiguous controller owner")
    reference = references[0]
    if not (
        reference.get("controller") is True and reference.get("apiVersion") == "apps/v1"
        and reference.get("kind") == kind and reference.get("name") == name
        and reference.get("uid") == uid
    ):
        raise DemoError("Controller owner name/UID does not match the demo chain")


def is_ready(pod):
    return (
        not pod["metadata"].get("deletionTimestamp")
        and pod.get("status", {}).get("phase") == "Running"
        and any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in pod.get("status", {}).get("conditions", []))
    )


def snapshot(client, expected=None, stable=False):
    namespace = client.get("namespace", NAMESPACE)
    nm = metadata(namespace, "Namespace", NAMESPACE)
    deployment = client.get("deployment", DEPLOYMENT)
    dm = metadata(deployment, "Deployment", DEPLOYMENT)
    spec = deployment.get("spec", {})
    if nm.get("deletionTimestamp") or dm.get("deletionTimestamp") or dm.get("ownerReferences"):
        raise DemoError("Namespace/deployment is terminating or owned by another controller")
    if spec.get("replicas") != 1 or spec.get("paused", False):
        raise DemoError("Demo Deployment must have one replica and must not be paused")
    if spec.get("selector") != {"matchLabels": {"app": DEPLOYMENT}}:
        raise DemoError("Unexpected Deployment selector")
    labels = spec.get("template", {}).get("metadata", {}).get("labels", {})
    if labels.get("app") != DEPLOYMENT or any(labels.get(k) != v for k, v in LABELS.items()):
        raise DemoError("Deployment Pod template lacks lab labels")
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    identity = {"namespace_uid": nm["uid"], "deployment_uid": dm["uid"],
                "generation": dm.get("generation"), "spec_sha256": fingerprint}
    if not isinstance(identity["generation"], int) or identity["generation"] < 1:
        raise DemoError("Deployment generation is invalid")
    pods = client.get("pods").get("items", [])
    if stable and (len(pods) != 1 or not is_ready(pods[0])):
        raise DemoError("Expected exactly one Ready Pod in the demo namespace")
    if len(pods) > 2:
        raise DemoError("Unexpected extra Pods in the demo namespace")
    if expected:
        if any(identity[k] != expected[k] for k in identity):
            raise DemoError("Namespace/deployment changed during the experiment")
        rs_name = expected["replicaset"]
    else:
        if not pods:
            raise DemoError("No demo Pod exists")
        refs = pods[0].get("metadata", {}).get("ownerReferences", [])
        if len(refs) != 1 or refs[0].get("kind") != "ReplicaSet":
            raise DemoError("Demo Pod is not ReplicaSet-owned")
        rs_name = refs[0].get("name", "")
        if not re.fullmatch(r"demo-web-[a-z0-9]+", rs_name):
            raise DemoError("Unexpected ReplicaSet name")
    replicaset = client.get("replicaset", rs_name)
    rm = metadata(replicaset, "ReplicaSet", rs_name)
    owner(replicaset, "Deployment", DEPLOYMENT, dm["uid"])
    if rm.get("deletionTimestamp") or replicaset.get("spec", {}).get("replicas") != 1:
        raise DemoError("ReplicaSet is terminating or does not desire one replica")
    identity.update(replicaset=rs_name, replicaset_uid=rm["uid"])
    if expected and identity["replicaset_uid"] != expected["replicaset_uid"]:
        raise DemoError("ReplicaSet UID changed during the experiment")
    selector = replicaset.get("spec", {}).get("selector", {})
    match = selector.get("matchLabels", {})
    template_hash = rm.get("labels", {}).get("pod-template-hash")
    if not template_hash or selector.get("matchExpressions") or match != {
        "app": DEPLOYMENT, "pod-template-hash": template_hash,
    }:
        raise DemoError("Unexpected ReplicaSet selector")
    for pod in pods:
        pm = metadata(pod, "Pod")
        owner(pod, "ReplicaSet", rs_name, rm["uid"])
        if any(pm.get("labels", {}).get(k) != v for k, v in match.items()):
            raise DemoError("Pod labels do not match its ReplicaSet")
    if stable:
        status = deployment.get("status", {})
        if status.get("observedGeneration") != dm["generation"] or any(
            status.get(key) != 1
            for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
        ):
            raise DemoError("Deployment rollout is not stable")
    return {"identity": identity, "pods": pods}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_sample(url):
    start = time.monotonic()
    status, error, ok = 0, None, False
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(Request(url, headers={"Connection": "close"}), timeout=HTTP_TIMEOUT) as reply:
            status = reply.status
            ok = status == 200 and reply.read(len(BODY) + 1) == BODY
            if not ok:
                error = "unexpected_demo_response"
    except HTTPError as exc:
        status, error = exc.code, "http_error"
        exc.close()
    except (OSError, URLError, ValueError) as exc:
        error = type(exc).__name__
    return {"status": status, "ok": ok, "error": error,
            "latency_ms": round((time.monotonic() - start) * 1000, 3)}


class Traffic:
    def __init__(self, url, deadline):
        self.url, self.deadline = url, deadline
        self.phase, self.rows, self.error = "baseline", [], None
        self.dispatched = 0
        self.stop_event, self.lock = threading.Event(), threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True, name="k8s-demo-traffic")

    def start(self):
        self.thread.start()

    def set_phase(self, phase):
        with self.lock:
            self.phase = phase

    def run(self):
        try:
            while not self.stop_event.is_set() and time.monotonic() < self.deadline:
                start = time.monotonic()
                with self.lock:
                    phase = self.phase
                    self.dispatched += 1
                row = request_sample(self.url)
                row.update(phase=phase, timestamp=utc_now())
                with self.lock:
                    row["phase_end"] = self.phase
                    row["phase_crossing"] = phase != self.phase
                    self.rows.append(row)
                self.stop_event.wait(max(0, 1 / RPS - (time.monotonic() - start)))
        except Exception as exc:  # noqa: BLE001 -- background failures must reach the report
            self.error = type(exc).__name__

    def samples(self):
        with self.lock:
            return [dict(row) for row in self.rows]

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=HTTP_TIMEOUT + 2)
        if self.thread.is_alive():
            self.error = "request_worker_did_not_stop"


def summary(rows):
    def percentile(values):
        return sorted(values)[math.ceil(len(values) * 0.95) - 1] if values else None
    return {
        "requests": len(rows), "errors": sum(not row["ok"] for row in rows),
        "successes": sum(row["ok"] for row in rows),
        "p95_ms": percentile([row["latency_ms"] for row in rows]),
        "successful_p95_ms": percentile([row["latency_ms"] for row in rows if row["ok"]]),
    }


def phase_samples(traffic, phase):
    return [row for row in traffic.samples() if row["phase"] == phase and not row["phase_crossing"]]


def require_traffic(traffic, phase):
    samples = phase_samples(traffic, phase)
    if traffic.error or len(samples) < 5 or any(not row["ok"] for row in samples):
        raise DemoError(phase + " traffic needs at least five valid HTTP 200 demo responses, no errors")


@contextmanager
def execution_lock():
    import fcntl

    path = ROOT / "runtime/kubernetes-demo.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DemoError("Another local Kubernetes demo is running") from None
        yield


def execute(context, url, report, checkpoint=lambda _: None):
    start = time.monotonic()
    deadline = start + MAX_SECONDS
    traffic = None
    try:
        if not context or context != context.strip() or context.startswith("-") or "\n" in context:
            raise DemoError("--execute requires an explicit exact --context")
        url = loopback_url(url)
        report.update(context=context, url=url)
        with execution_lock():
            client = Kubectl(context, deadline)
            client.check_context()
            initial = snapshot(client, stable=True)
            old = initial["pods"][0]
            report["baseline"] = {**initial["identity"], "pod": old["metadata"]["name"],
                                  "pod_uid": old["metadata"]["uid"], "ready": True}
            checkpoint(report)
            traffic = Traffic(url, deadline)
            traffic.start()

            def pause(seconds):
                until = min(deadline, time.monotonic() + seconds)
                while time.monotonic() < until:
                    if traffic.error:
                        raise DemoError("Traffic worker failed: " + traffic.error)
                    time.sleep(max(0, min(0.25, until - time.monotonic())))
                if time.monotonic() >= deadline:
                    raise DemoError("Overall execution deadline exceeded")

            pause(BASELINE_SECONDS)
            traffic.set_phase("guard")
            require_traffic(traffic, "baseline")
            report["baseline_traffic"] = summary(phase_samples(traffic, "baseline"))
            client.check_context()
            latest = snapshot(client, expected=initial["identity"], stable=True)
            fresh = latest["pods"][0]
            if fresh["metadata"]["uid"] != old["metadata"]["uid"]:
                raise DemoError("Pod changed before deletion; no new Pod will be deleted")
            report["deletion"] = {"attempted": True, "accepted": False,
                                  "pod": fresh["metadata"]["name"], "uid": fresh["metadata"]["uid"],
                                  "resource_version": fresh["metadata"]["resourceVersion"]}
            checkpoint(report)
            traffic.set_phase("recovery")
            deleted_at = time.monotonic()
            client.delete_pod(fresh)
            report["deletion"]["accepted"] = True
            report["recovery"]["tested"] = True
            checkpoint(report)
            until = min(deadline, deleted_at + RECOVERY_SECONDS)
            replacement = None
            while time.monotonic() < until:
                state = snapshot(client, expected=initial["identity"])
                ready = [pod for pod in state["pods"] if is_ready(pod)
                         and pod["metadata"]["uid"] != old["metadata"]["uid"]]
                if len(ready) == 1:
                    replacement = ready[0]
                    break
                pause(1)
            if replacement is None:
                raise DemoError("No new UID became Ready within the recovery deadline")
            report["recovery"] = {
                "tested": True, "verified": True, "new_pod": replacement["metadata"]["name"],
                "new_uid": replacement["metadata"]["uid"], "ready": True,
                "ready_after_seconds": round(time.monotonic() - deleted_at, 3),
                "mechanism": "ReplicaSet reconciliation; client polling observation",
            }
            checkpoint(report)
            traffic.set_phase("recovery_http")
            until = min(deadline, time.monotonic() + HTTP_RECOVERY_SECONDS)
            while time.monotonic() < until:
                recent = phase_samples(traffic, "recovery_http")[-5:]
                if len(recent) == 5 and all(row["ok"] for row in recent):
                    break
                pause(0.5)
            else:
                raise DemoError("Pod recovered but HTTP path did not; check port-forward reconnect")
            traffic.set_phase("after")
            pause(FINAL_SECONDS)
            traffic.set_phase("final_check")
            require_traffic(traffic, "after")
            final = snapshot(client, expected=initial["identity"], stable=True)
            if final["pods"][0]["metadata"]["uid"] != replacement["metadata"]["uid"]:
                raise DemoError("Replacement Pod changed again during final observation")
            report["http_recovery"] = {"verified": True}
            report["status"] = "passed"
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 -- preserve failure evidence
        report["status"] = "failed"
        report["error"] = str(exc) if isinstance(exc, DemoError) else type(exc).__name__
    finally:
        if traffic is not None:
            traffic.stop()
            report["requests"] = traffic.samples()
            report["traffic"] = summary(report["requests"])
            report["traffic"]["dispatched_requests"] = traffic.dispatched
            report["traffic"]["unfinished_requests"] = traffic.dispatched - len(report["requests"])
            report["phases"] = {phase: summary(phase_samples(traffic, phase))
                                for phase in ("baseline", "recovery", "recovery_http", "after")}
            if traffic.error:
                report.update(status="failed", traffic_error=traffic.error)
        report["duration_seconds"] = round(time.monotonic() - start, 3)
        report["finished_at"] = utc_now()
        try:
            checkpoint(report)
        except OSError as exc:
            report.update(status="failed", evidence_error=type(exc).__name__)
    return report


def save_report(path, report):
    fd, temporary = tempfile.mkstemp(prefix=".k8s-demo-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="default: print steps without cluster access")
    mode.add_argument("--execute", action="store_true", help="delete one verified demo Pod")
    parser.add_argument("--context", help="must match kubectl current-context exactly")
    parser.add_argument("--url", help="explicit http://loopback:PORT/ demo port-forward URL")
    parser.add_argument("--output", type=Path, help="new JSON evidence file; never overwrite an existing file")
    args = parser.parse_args(argv)
    report = plan()
    output = args.output
    if args.execute:
        report.update(mode="execute", status="running", started_at=utc_now(),
                      recovery={"tested": False, "verified": False},
                      http_recovery={"verified": False},
                      deletion={"attempted": False, "accepted": False})
        report.pop("cluster_accessed")
        report.pop("recovery_tested")
        output = output or ROOT / "reports" / ("kubernetes-demo-" + uuid.uuid4().hex + ".json")
    if output:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x"):
                pass
            save_report(output, report)
        except OSError as exc:
            print(json.dumps({"status": "failed", "error": "Evidence file unavailable: " + type(exc).__name__}))
            return 1
    if args.execute:
        def interrupted(signum, frame):
            raise DemoError("Interrupted by signal " + str(signum))
        previous = signal.signal(signal.SIGTERM, interrupted)
        try:
            execute(args.context, args.url, report, lambda data: save_report(output, data))
        finally:
            signal.signal(signal.SIGTERM, previous)
    printable = {k: v for k, v in report.items() if k != "requests"}
    if output:
        printable["output"] = str(output)
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0 if report["status"] in ("not_tested", "passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
