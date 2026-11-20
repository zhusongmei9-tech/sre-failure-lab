"""Exercise the product HPA using bounded HTTP traffic from its own K3s host."""

import argparse
import fcntl
import http.client
import ipaddress
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

NAMESPACE = "sre-app-lab"
NAME = "product-api"
POLL = 5
BASELINE_SECONDS = 20
PEAK_SECONDS = 150
HOLD_SECONDS = 30
IDLE_SECONDS = 210
LABELS = {
    "app.kubernetes.io/part-of": "sre-failure-lab",
    "app.kubernetes.io/managed-by": "sre-failure-lab",
    "sre-lab-role": "product-lab",
}


def cpu_mcores(value):
    """Kubernetes prints CPU quantities in cores or in milli/micro/nanocores."""
    if value is None:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([num]?)", str(value))
    if not match:
        raise ValueError("Unsupported HPA CPU quantity")
    return round(float(match[1]) * {"n": 1e-6, "u": 1e-3, "m": 1, "": 1000}[match[2]], 3)


def snapshot(items):
    objects = {x["kind"]: x for x in items}
    deployment, service, hpa = (
        objects[k] for k in ("Deployment", "Service", "HorizontalPodAutoscaler")
    )
    for x in (deployment, service, hpa):
        meta = x["metadata"]
        if (
            meta.get("namespace") != NAMESPACE
            or meta.get("name") != NAME
            or not LABELS.items() <= meta.get("labels", {}).items()
            or meta.get("deletionTimestamp")
        ):
            raise ValueError("Expected the app-lab's product-api objects")
    svc = service["spec"]
    if (
        svc["type"] != "ClusterIP"
        or svc.get("externalIPs")
        or svc["selector"] != {"app": NAME}
        or [p["port"] for p in svc["ports"]] != [8000]
    ):
        raise ValueError("Unexpected product Service routing")
    address = str(ipaddress.ip_address(svc["clusterIP"]))
    target = hpa["spec"]
    if target["scaleTargetRef"] != {
        "kind": "Deployment",
        "apiVersion": "apps/v1",
        "name": NAME,
    } or (target["minReplicas"], target["maxReplicas"]) != (1, 2):
        raise ValueError("Expected an HPA controlling one to two product-api replicas")
    rules = target["metrics"]
    if (
        len(rules) != 1
        or rules[0]["type"] != "Resource"
        or rules[0]["resource"]["name"] != "cpu"
        or rules[0]["resource"]["target"]["type"] != "Utilization"
    ):
        raise ValueError("Expected one CPU utilization target")
    actual = next(
        (
            m["resource"].get("current", {})
            for m in hpa.get("status", {}).get("currentMetrics", [])
            if m.get("resource", {}).get("name") == "cpu"
        ),
        {},
    )
    return {
        "ip": address,
        "uids": [x["metadata"]["uid"] for x in (deployment, service, hpa)],
        "requested": deployment["spec"]["replicas"],
        "ready": deployment.get("status", {}).get("readyReplicas", 0),
        "hpa_desired": hpa.get("status", {}).get("desiredReplicas"),
        "cpu_percent": actual.get("averageUtilization"),
        "avg_cpu_mcores": cpu_mcores(actual.get("averageValue")),
        "target_percent": rules[0]["resource"]["target"]["averageUtilization"],
    }


def observe():
    out = subprocess.run(
        [
            "k3s",
            "kubectl",
            "--kubeconfig=/etc/rancher/k3s/k3s.yaml",
            "-n",
            NAMESPACE,
            "--request-timeout=6s",
            "get",
            "deployment/product-api",
            "service/product-api",
            "hpa/product-api",
            "-o",
            "json",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        timeout=10,
    )
    return snapshot(json.loads(out.stdout)["items"])


def get_product(address):
    # One connection per request lets the ClusterIP distribute traffic across Ready backends.
    conn = http.client.HTTPConnection(address, 8000, timeout=2)
    try:
        conn.request("GET", "/products/1", headers={"Connection": "close"})
        response = conn.getresponse()
        if response.status != 200:
            return "http_" + str(response.status)
        body = json.loads(response.read(8192))
        return (
            "product_200" if isinstance(body, dict) and body.get("id") == 1 else "invalid_product"
        )
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return type(exc).__name__
    finally:
        conn.close()


def stats(samples, seconds):
    outcomes = Counter(item[2] for item in samples)
    latencies = sorted((end - start) * 1000 for start, end, _ in samples)
    return {
        "seconds": round(seconds, 3),
        "requests": len(samples),
        "product_200": outcomes["product_200"],
        "errors": {k: v for k, v in outcomes.items() if k != "product_200"},
        "achieved_rps": round(len(samples) / seconds, 2) if seconds > 0 else None,
        "p95_ms": round(latencies[math.ceil(0.95 * len(samples)) - 1], 2) if samples else None,
    }


def split_by_ready(samples, intervals):
    """Compare equal offered load in consecutive polls with the same Ready count."""
    result = {}
    for count in (1, 2):
        windows = [(a, b) for a, b, first, last in intervals if first == last == count]
        selection = [s for s in samples if any(a <= s[0] and s[1] <= b for a, b in windows)]
        result[str(count)] = stats(selection, sum(b - a for a, b in windows))
    return result


def run_traffic(address, rps, workers, stage, max_seconds, poll, report):
    samples = []
    intervals = []
    stop = threading.Event()
    start = time.monotonic()
    deadline = start + max_seconds
    hold_start = None
    held_two_ready = False

    def worker(i):
        local = []
        due = start + i / rps
        while due < deadline and not stop.wait(max(0, due - time.monotonic())):
            sent = time.monotonic()
            if sent >= deadline:
                break
            outcome = get_product(address)
            local.append((sent, time.monotonic(), outcome))
            # Never catch up missed arrivals in a burst.
            due = max(due + workers / rps, time.monotonic())
        return local

    with ThreadPoolExecutor(max_workers=workers) as pool:
        threads = [pool.submit(worker, i) for i in range(workers)]
        try:
            prev_at = None
            prev_ready = None
            while True:
                current = poll(stage)
                now = time.monotonic()
                if prev_at is not None:
                    intervals.append((prev_at, now, prev_ready, current["ready"]))
                prev_at, prev_ready = now, current["ready"]
                if stage == "load":
                    if current["ready"] == current["requested"] == current["hpa_desired"] == 2:
                        if hold_start is None:
                            hold_start = now
                        if now - hold_start >= HOLD_SECONDS:
                            held_two_ready = True
                            break
                    else:
                        hold_start = None
                if now >= deadline:
                    break
                stop.wait(min(POLL, deadline - now))
        finally:
            stop.set()
            for future in threads:
                samples.extend(future.result())
            summary = stats(samples, time.monotonic() - start)
            summary.update({"target_rps": rps, "workers": workers})
            if stage == "load":
                summary["by_ready_replicas"] = split_by_ready(samples, intervals)
                summary["held_two_ready"] = held_two_ready
            report[stage] = summary


def exercise(rps, report):
    started = time.monotonic()
    identity = observe()
    report.update(
        {
            "service_ip": identity["ip"],
            "target_cpu_percent": identity["target_percent"],
            "samples": [],
        }
    )

    def poll(stage):
        sample = observe()
        if sample["uids"] != identity["uids"] or sample["ip"] != identity["ip"]:
            raise RuntimeError("Workload identity changed during exercise")
        row = {k: v for k, v in sample.items() if k not in ("uids", "ip", "target_percent")}
        row.update({"stage": stage, "t_seconds": round(time.monotonic() - started, 2)})
        report["samples"].append(row)
        print(json.dumps(row), flush=True)
        return sample

    def wait_for_one(stage):
        deadline = time.monotonic() + IDLE_SECONDS
        while True:
            s = poll(stage)
            if (
                s["ready"] == s["requested"] == s["hpa_desired"] == 1
                and s["cpu_percent"] is not None
            ):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "HPA did not settle at one Ready replica with CPU metrics: " + stage
                )
            time.sleep(POLL)

    wait_for_one("preflight")
    if get_product(identity["ip"]) != "product_200":
        raise RuntimeError("The product is unavailable through the ClusterIP from this host")
    run_traffic(identity["ip"], 1, 1, "baseline", BASELINE_SECONDS, poll, report)
    if report["baseline"]["errors"] or not report["baseline"]["product_200"]:
        raise RuntimeError("The low-rate product baseline failed")
    last = report["samples"][-1]
    if last["ready"] != 1 or last["hpa_desired"] != 1:
        raise RuntimeError("Deployment was already scaling during the baseline")
    run_traffic(identity["ip"], rps, 4, "load", PEAK_SECONDS, poll, report)
    stopped = time.monotonic()
    wait_for_one("cooldown")
    report["seconds_from_load_stop_to_one_ready"] = round(time.monotonic() - stopped, 2)
    report["final_probe"] = get_product(identity["ip"])
    if (
        not report["load"]["held_two_ready"]
        or report["load"]["product_200"] == 0
        or report["final_probe"] != "product_200"
    ):
        raise RuntimeError("Did not verify sustained scale-up, real product traffic and recovery")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", action="store_true", help="send traffic to the lab in local K3s")
    p.add_argument("--rps", type=int, default=40, choices=range(5, 61), metavar="5..60")
    p.add_argument("--output", type=Path, default=Path("reports/k8s-app-hpa.json"))
    args = p.parse_args()
    if not args.run:
        print(
            "Plan: 1 rps baseline, bounded product GETs, observe HPA 1->2, stop traffic and wait for 2->1."
        )
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "namespace": NAMESPACE,
        "service": NAME,
        "passed": False,
        "started_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 -- host Python 3.9
    }
    lock = os.open(Path(__file__).resolve().parent, os.O_RDONLY)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            p.exit(1, "Another app-lab exercise is running from this installation.\n")
        exercise(args.rps, report)
        report["passed"] = True
    except (
        KeyboardInterrupt,
        RuntimeError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        report["error"] = str(exc) or type(exc).__name__
    finally:
        os.close(lock)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    os.replace(temporary, args.output)
    print(json.dumps({k: v for k, v in report.items() if k != "samples"}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":

    def interrupt(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    main()
