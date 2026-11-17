"""Plan or explicitly exercise Prometheus -> Alertmanager -> lab webhook notifications."""

import argparse
import importlib.util
import json
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "sre-monitoring"
MANAGER = "sre-lab-alert-acceptance"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise RuntimeError("Unexpected HTTP redirect")


def local_url(value):
    parts = urlsplit(value)
    if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in {"", "/"}):
        raise ValueError("Use an explicit localhost HTTP port-forward URL without credentials")
    if parts.port is None or not 1 <= parts.port <= 65535:
        raise ValueError("An explicit local port is required")
    return value.rstrip("/")


def read_json(url):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=5) as response:
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise RuntimeError("HTTP response exceeds limit")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise TypeError("Expected a JSON object")
        return payload


def kubectl(context, *arguments, payload=None):
    result = subprocess.run(
        ["kubectl", "--context=" + context, *arguments],
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True, text=True, timeout=20, check=True,
    )
    return result.stdout


def synthetic_manifest(run_id):
    path = ROOT / "monitoring/kubernetes/alerting/render_rules.py"
    spec = importlib.util.spec_from_file_location("lab_alert_rules", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.manifest(run_id)
    result["metadata"]["labels"]["app.kubernetes.io/managed-by"] = MANAGER
    result["metadata"]["labels"]["run_id"] = run_id
    return result


def firing(prometheus, run_id, get=read_json):
    query = ('ALERTS{alertname="SRELabSynthetic",alertstate="firing",run_id="'
             + run_id + '"}')
    payload = get(prometheus + "/api/v1/query?" + urlencode({"query": query}))
    data = payload.get("data", {})
    if payload.get("status") != "success" or data.get("resultType") != "vector":
        raise RuntimeError("Prometheus query did not return a successful instant vector")
    samples = data.get("result")
    if not isinstance(samples, list):
        raise TypeError("Invalid Prometheus result")
    for sample in samples:
        labels = sample.get("metric", {})
        if (labels.get("run_id") != run_id or labels.get("alertname") != "SRELabSynthetic"
                or labels.get("alertstate") != "firing" or float(sample["value"][1]) != 1):
            raise RuntimeError("Unexpected Prometheus alert identity")
    return bool(samples)


def initial_cursor(receiver, get):
    health = get(receiver + "/healthz")
    # Read only metadata, even when the bounded event ring already contains entries.
    snapshot = get(receiver + "/events?after=9223372036854775807")
    identity = snapshot.get("instance_id")
    if (health.get("status") != "ok" or not isinstance(identity, str)
            or health.get("instance_id") != identity):
        raise RuntimeError("Receiver is unavailable or restarted during baseline read")
    return {"instance_id": identity, "sequence": snapshot["last_sequence"]}


def observe_events(receiver, run_id, cursor, get):
    snapshot = get(receiver + "/events?" + urlencode({"after": cursor["sequence"], "run_id": run_id}))
    if snapshot.get("instance_id") != cursor["instance_id"]:
        raise RuntimeError("Receiver restarted; event continuity cannot be verified")
    if snapshot["oldest_sequence"] > cursor["sequence"] + 1:
        raise RuntimeError("Receiver ring evicted events during this acceptance run")
    if snapshot["last_sequence"] < cursor["sequence"]:
        raise RuntimeError("Receiver event cursor moved backwards")
    statuses = set()
    for event in snapshot["events"]:
        labels = event.get("labels", {})
        if (labels.get("run_id") == run_id and labels.get("alertname") == "SRELabSynthetic"
                and event.get("status") in {"firing", "resolved"}):
            statuses.add(event["status"])
    cursor["sequence"] = snapshot["last_sequence"]
    return statuses


def wait_phase(prometheus, receiver, run_id, cursor, expected, timeout, *, get, clock, sleep):
    deadline = clock() + timeout
    observed_prometheus = False
    observed_webhook = False
    while clock() < deadline:
        state = firing(prometheus, run_id, get)
        observed_prometheus |= state if expected == "firing" else not state
        observed_webhook |= expected in observe_events(receiver, run_id, cursor, get)
        if observed_prometheus and observed_webhook:
            return
        sleep(1)
    raise RuntimeError("Timed out verifying Prometheus and webhook " + expected)


def exercise(context, prometheus, receiver, timeout=180, *, command=kubectl, get=read_json,
             clock=time.monotonic, sleep=time.sleep):
    """Only create/mutate/delete this run's fixed-namespace synthetic rule."""
    if not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,160}", context):
        raise ValueError("An explicit valid kubeconfig context is required")
    prometheus, receiver = local_url(prometheus), local_url(receiver)
    if not 30 <= timeout <= 600:
        raise ValueError("Timeout must be between 30 and 600 seconds per phase")
    run_id = uuid.uuid4().hex[:16]
    resource = synthetic_manifest(run_id)
    name = resource["metadata"]["name"]
    report = {"mode": "synthetic", "passed": False, "namespace": NAMESPACE,
              "run_id": run_id, "rule_name": name, "checks": {}, "cleanup": "not_needed"}
    uid = None
    started = clock()
    try:
        current = command(context, "config", "current-context").strip()
        if current != context:
            raise RuntimeError("Current kubeconfig context differs from --context; refusing mutation")
        report["checks"]["context_confirmed"] = True
        cursor = initial_cursor(receiver, get)
        if firing(prometheus, run_id, get):
            raise RuntimeError("Synthetic run ID already exists")
        report["cleanup"] = "creation_unconfirmed_check_rule_manually"
        created = json.loads(command(context, "create", "-n", NAMESPACE, "-f", "-", "-o", "json",
                                     payload=resource))
        metadata = created.get("metadata", {})
        candidate = metadata.get("uid")
        if (metadata.get("name") != name or metadata.get("namespace") != NAMESPACE
                or metadata.get("labels", {}).get("app.kubernetes.io/managed-by") != MANAGER
                or metadata.get("labels", {}).get("run_id") != run_id
                or not isinstance(candidate, str) or not candidate):
            report["cleanup"] = "unconfirmed_create_check_rule_manually"
            raise RuntimeError("Cannot verify created rule identity; refusing blind mutation/deletion")
        uid = candidate
        report["rule_uid"] = uid
        wait_phase(prometheus, receiver, run_id, cursor, "firing", timeout,
                   get=get, clock=clock, sleep=sleep)
        report["checks"].update(prometheus_firing=True, webhook_firing=True)
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "test", "path": "/metadata/labels/app.kubernetes.io~1managed-by", "value": MANAGER},
            {"op": "test", "path": "/metadata/labels/run_id", "value": run_id},
            {"op": "replace", "path": "/spec/groups/0/rules/0/expr", "value": "vector(0) == 1"},
        ]
        command(context, "patch", "prometheusrule", name, "-n", NAMESPACE,
                "--type=json", "--patch", json.dumps(patch))
        wait_phase(prometheus, receiver, run_id, cursor, "resolved", timeout,
                   get=get, clock=clock, sleep=sleep)
        report["checks"].update(prometheus_resolved=True, webhook_resolved=True)
        report["passed"] = True
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    except KeyboardInterrupt:
        report["error"] = "Interrupted; attempting UID-checked cleanup"
    finally:
        if uid is not None:
            try:
                # DeleteOptions UID precondition prevents deleting a replacement object's rule.
                command(context, "delete", "--raw", "/apis/monitoring.coreos.com/v1/namespaces/"
                        + NAMESPACE + "/prometheusrules/" + name, "-f", "-",
                        payload={"apiVersion": "v1", "kind": "DeleteOptions",
                                 "preconditions": {"uid": uid}})
                report["cleanup"] = "deleted_with_uid_precondition"
            except (OSError, RuntimeError, subprocess.SubprocessError):
                report["cleanup"] = "failed_check_rule_manually"
                report["passed"] = False
        report["elapsed_seconds"] = round(clock() - started, 3)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true", help="default: no network or cluster access")
    modes.add_argument("--exercise-synthetic", action="store_true", help="explicitly modify a test rule")
    parser.add_argument("--context")
    parser.add_argument("--prometheus-url")
    parser.add_argument("--receiver-url")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.exercise_synthetic:
        if not all((args.context, args.prometheus_url, args.receiver_url)):
            parser.error("Synthetic exercise requires --context, --prometheus-url and --receiver-url")
        try:
            def interrupted(_signum, _frame):
                raise KeyboardInterrupt

            signal.signal(signal.SIGTERM, interrupted)
            report = exercise(args.context, args.prometheus_url, args.receiver_url, args.timeout)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        report = {
            "mode": "plan", "status": "not_tested", "performed": False,
            "cluster_accessed": False, "namespace": NAMESPACE,
            "steps": ["verify explicit kubeconfig context", "create uniquely labeled synthetic rule",
                      "observe Prometheus firing and same-run webhook firing", "set rule expression false",
                      "observe Prometheus resolved and same-run webhook resolved",
                      "delete this rule with UID precondition"],
            "requires": ["optional alerting module installed", "two localhost port-forwards",
                         "explicit --exercise-synthetic and --context"],
        }
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)
    return 0 if report.get("mode") == "plan" or report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
