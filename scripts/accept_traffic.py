"""REAL traffic acceptance for the initialized, dedicated Linux SRE lab.

Runs service outages and releases with continuous bounded traffic. Requires a
running real healer and local sre-lab-api:0.3.0 / :0.3.1 artifacts. Python 3.9+.
"""

import argparse
import json
import math
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops import common, deploy
from scripts import accept_runtime as acceptance

QUERIES = {
    "host_cpu_percent": '100 * (1 - avg(rate(node_cpu_seconds_total{job="node",mode="idle"}[30s])))',
    "host_memory_percent": '100 * (1 - node_memory_MemAvailable_bytes{job="node"} / node_memory_MemTotal_bytes{job="node"})',
    "filesystem_percent": '100 * (1 - node_filesystem_avail_bytes{job="node",fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes{job="node",fstype!~"tmpfs|overlay|squashfs"})',
    "api_cpu_cores": 'rate(process_cpu_seconds_total{job="api"}[30s])',
    "api_memory_bytes": 'process_resident_memory_bytes{job="api"}',
    "cpu_iterations_increase": 'sum(increase(lab_cpu_iterations_total{job="api"}[30s]))',
    "container_cpu_cores": 'rate(container_cpu_usage_seconds_total{job="cadvisor",id=~".*/docker/[^/]+|.*/docker-[^/]+[.]scope"}[30s])',
    "up": 'up{job=~"api|node|cadvisor"}',
    "alerts": 'ALERTS{alertname=~"Lab.*"}',
}


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * fraction) - 1] if ordered else None


def summarize(rows, duration):
    statuses = Counter(row["status"] for row in rows)
    samples = [row["elapsed_ms"] for row in rows]
    return {
        "total": len(rows),
        "2xx": sum(n for code, n in statuses.items() if 200 <= code < 300),
        "4xx": sum(n for code, n in statuses.items() if 400 <= code < 500),
        "5xx": sum(n for code, n in statuses.items() if 500 <= code < 600),
        "transport_errors": statuses.get(0, 0),
        "status_counts": dict(statuses),
        "duration_seconds": round(duration, 3),
        "actual_rps": round(len(rows) / duration, 3) if duration > 0 else None,
        "p50_ms": percentile(samples, 0.5),
        "p95_ms": percentile(samples, 0.95),
        "p99_ms": percentile(samples, 0.99),
        "versions": dict(Counter(row["version"] or "unknown" for row in rows)),
        "slots": dict(Counter(row["slot"] or "unknown" for row in rows)),
        "cache": dict(Counter(row["cache"] or "unknown" for row in rows)),
        "crossing_requests": sum(row.get("phase_crossing", False) for row in rows),
    }


class Traffic:
    """Paced finite workers; phase is captured at dispatch, including crossing requests."""

    def __init__(self, directory, rps=20, workers=8, base_url=deploy.EDGE_URL):
        if type(rps) not in (int, float) or not math.isfinite(rps) or not 1 <= rps <= 50:
            raise ValueError("rps must be finite and in 1..50")
        if type(workers) is not int or not 1 <= workers <= 32:
            raise ValueError("workers must be in 1..32")
        self.directory = Path(directory)
        self.rps, self.workers, self.base = rps, workers, base_url
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.phase = "setup"
        self.rows = []
        self.failures = []
        self.threads = []
        self.stream = None

    def set_phase(self, phase):
        with self.lock:
            self.phase = phase

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stream = (self.directory / "requests.jsonl").open("w")
        for index in range(self.workers):
            thread = threading.Thread(
                target=self._guard, args=(index,), name="traffic-" + str(index)
            )
            thread.start()
            self.threads.append(thread)

    def _guard(self, index):
        try:
            self._worker(index)
        except Exception as exc:  # noqa: BLE001 -- transfer thread failures to the caller
            with self.lock:
                self.failures.append(type(exc).__name__)
            self.stop.set()

    def _worker(self, index):
        interval = self.workers / self.rps
        if self.stop.wait(index / self.rps):
            return
        sequence = index
        while not self.stop.is_set():
            with self.lock:
                phase = self.phase
            started = time.monotonic()
            row = {
                "timestamp": acceptance.utc_now(),
                "phase": phase,
                "product_id": sequence % 3 + 1,
                "status": 0,
                "cache": None,
                "version": None,
                "slot": None,
            }
            try:
                try:
                    response = urlopen(self.base + "/products/" + str(row["product_id"]), timeout=3)
                except HTTPError as exc:
                    response = exc
                with response:
                    response.read()
                    row.update(
                        status=response.status,
                        cache=response.headers.get("X-Cache"),
                        version=response.headers.get("X-App-Version"),
                        slot=response.headers.get("X-Deployment-Slot"),
                    )
            except (OSError, HTTPException):
                row["status"] = 0
            row["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            row["finished_at"] = acceptance.utc_now()
            with self.lock:
                row["phase_end"] = self.phase
                row["phase_crossing"] = self.phase != phase
                self.rows.append(row)
                self.stream.write(json.dumps(row) + "\n")
                self.stream.flush()
            sequence += 1
            # Never catch up a backlog after a slow response.
            self.stop.wait(max(0, interval - (time.monotonic() - started)))

    def close(self):
        self.stop.set()
        deadline = time.monotonic() + 8
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("Traffic worker did not stop within its HTTP timeout")
        if self.stream:
            self.stream.close()
        if self.failures:
            raise RuntimeError("Traffic worker failed: " + ",".join(self.failures))


class MetricsSampler:
    def __init__(self, directory, traffic, token):
        self.path = Path(directory) / "metrics.jsonl"
        self.traffic = traffic
        self.token = token
        self.stop = threading.Event()
        self.rows = []
        self.failure = None
        self.thread = threading.Thread(target=self.run, name="prometheus-sampler")

    @staticmethod
    def query(item):
        name, expression = item
        url = "http://127.0.0.1:9090/api/v1/query?" + urlencode({"query": expression})
        try:
            with urlopen(url, timeout=2) as response:
                body = json.load(response)
            if body.get("status") != "success":
                raise ValueError("Prometheus rejected query")
            result = []
            for sample in body["data"]["result"]:
                value = float(sample["value"][1])
                result.append(
                    {
                        "metric": sample["metric"],
                        "value": value if math.isfinite(value) else None,
                        "sample_timestamp": sample["value"][0],
                    }
                )
            return name, {"status": "ok", "result": result}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return name, {"status": "error", "error_type": type(exc).__name__}

    def run(self):
        try:
            with self.path.open("w") as stream, ThreadPoolExecutor(max_workers=4) as pool:
                while not self.stop.is_set():
                    started = time.monotonic()
                    with self.traffic.lock:
                        phase = self.traffic.phase
                    row = {
                        "timestamp": acceptance.utc_now(),
                        "phase": phase,
                        "queries": dict(pool.map(self.query, QUERIES.items())),
                    }
                    try:
                        active = common.read_state().get("active")
                        status, _ = acceptance.request_json(
                            common.slot_url(active), "/lab/status", self.token
                        )
                        row["application_fault"] = {
                            "slot": active,
                            "fault": status.get("fault"),
                            "remaining_seconds": status.get("remaining_seconds"),
                        }
                    except (RuntimeError, ValueError, KeyError):
                        row["application_fault"] = {"status": "unavailable"}
                    self.rows.append(row)
                    stream.write(json.dumps(row) + "\n")
                    stream.flush()
                    self.stop.wait(max(0, 5 - (time.monotonic() - started)))
        except Exception as exc:  # noqa: BLE001 -- retain background errors for main
            self.failure = type(exc).__name__

    def close(self):
        self.stop.set()
        if self.thread.ident is None:
            return
        self.thread.join(timeout=12)
        if self.thread.is_alive() or self.failure:
            raise RuntimeError("Prometheus sampler failed or did not stop: " + str(self.failure))


def evaluate(phases, rows, metrics_rows):
    failures = []
    allowed_fault_statuses = {
        "cpu": {200, 429},
        "api_outage": {0, 200, 502, 503, 504},
        "automatic_rollback": {0, 200, 500, 502, 503, 504},
    }
    for phase in phases:
        selected = [row for row in rows if row["phase"] == phase["name"]]
        phase["traffic"] = summarize(selected, phase["duration_seconds"])
        stable = [row for row in selected if not row.get("phase_crossing", False)]
        phase["traffic"]["noncrossing_samples"] = len(stable)
        phase["traffic"]["noncrossing_errors"] = sum(
            not 200 <= row["status"] < 300 for row in stable
        )
        if not selected:
            failures.append(phase["name"] + ": no traffic samples")
        elif not stable:
            failures.append(phase["name"] + ": no samples entirely within phase")
        elif not phase["expected_errors"] and phase["traffic"]["noncrossing_errors"]:
            failures.append(phase["name"] + ": unexpected failed requests")
        if phase["expected_errors"]:
            allowed = allowed_fault_statuses.get(phase["name"], {200})
            phase["allowed_statuses"] = sorted(allowed)
            phase["observed_error_count"] = sum(row["status"] != 200 for row in selected)
            unexpected = sorted({row["status"] for row in stable if row["status"] not in allowed})
            if unexpected:
                failures.append(phase["name"] + ": unexpected statuses " + str(unexpected))
        if phase["name"] == "cpu":
            if not any(row["status"] == 200 for row in stable):
                failures.append("cpu: no successful requests")
            progress = any(
                item.get("value") is not None and item["value"] > 0
                for row in metrics_rows
                if row["phase"] == "cpu"
                for item in row["queries"].get("cpu_iterations_increase", {}).get("result", [])
            )
            phase["cpu_work_observed"] = progress
            if not progress:
                failures.append("cpu: no measured CPU iteration progress")
    coverage = {}
    for phase_name in ("baseline", "final_recovery"):
        samples = [row for row in metrics_rows if row["phase"] == phase_name]
        coverage[phase_name] = {}
        for name in QUERIES:
            if name == "alerts":
                continue  # No active alerts is a valid result, especially for short faults.
            valid = any(
                row["queries"][name].get("status") == "ok"
                and any(
                    item.get("value") is not None for item in row["queries"][name].get("result", [])
                )
                for row in samples
            )
            coverage[phase_name][name] = valid
            if not valid:
                failures.append(phase_name + ": no finite metrics for " + name)
        target_jobs = set()
        for row in samples:
            for item in row["queries"]["up"].get("result", []):
                if item.get("value") == 1:
                    target_jobs.add(item["metric"].get("job"))
        if not {"api", "node", "cadvisor"}.issubset(target_jobs):
            failures.append(phase_name + ": missing healthy scrape targets")
    return failures, coverage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rps", type=float, default=20)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    if not 180 <= args.max_seconds <= 600:
        parser.error("max-seconds must be 180..600")
    directory = (
        common.ROOT
        / "runtime"
        / ("traffic-" + acceptance.utc_now().replace(":", "").replace("+", "_"))
    )
    traffic = Traffic(directory, args.rps, args.workers)
    token = acceptance.load_token()
    initial = common.read_state()
    initial_slot = initial.get("active")
    if initial.get("phase") != "idle" or initial_slot not in ("blue", "green"):
        raise RuntimeError("An initialized idle runtime is required")
    initial_version = initial["slots"][initial_slot]["version"]
    if initial_version not in ("0.3.0", "0.3.1"):
        raise RuntimeError("Only dedicated lab versions 0.3.0 and 0.3.1 are supported")
    other_version = "0.3.0" if initial_version == "0.3.1" else "0.3.1"
    for version in (initial_version, other_version):
        deploy.inspect_image("sre-lab-api:" + version)
    acceptance.edge_ok(initial_version, initial_slot)
    status, _ = acceptance.request_json(common.slot_url(initial_slot), "/lab/status", token)
    if status.get("fault") is not None or not status.get("fallback_enabled"):
        raise RuntimeError("Clear active faults and enable cache fallback before acceptance")
    directory.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "mode": "real",
        "started_at": acceptance.utc_now(),
        "target_rps": args.rps,
        "workers": args.workers,
        "max_seconds": args.max_seconds,
        "initial_version": initial_version,
        "initial_slot": initial_slot,
        "status": "running",
        "phases": [],
        "checks": [],
    }
    stopped = set()
    fault_slots = set()
    injector_stop = threading.Event()
    injector = None
    sampler = MetricsSampler(directory, traffic, token)
    started = time.monotonic()
    initial_event_offset = acceptance.event_offset()

    def save():
        common.atomic_write(directory / "report.json", json.dumps(report, indent=2) + "\n")

    @contextmanager
    def phase(name, expected_errors=False):
        entry = {
            "name": name,
            "expected_errors": expected_errors,
            "started_at": acceptance.utc_now(),
        }
        entry["error_policy"] = (
            "fault observation: failures counted explicitly; recovery must pass independent version/health checks"
            if expected_errors
            else "zero non-2xx required for requests wholly inside this phase"
        )
        begin = time.monotonic()
        traffic.set_phase(name)
        report["phases"].append(entry)
        print("PHASE: " + name, flush=True)
        try:
            yield
        finally:
            entry["ended_at"] = acceptance.utc_now()
            entry["duration_seconds"] = time.monotonic() - begin
            save()

    def sleep(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if traffic.failures or sampler.failure:
                raise RuntimeError("Background traffic or metrics sampling failed")
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))

    def set_fault(slot, kind, **parameters):
        fault_slots.add(slot)
        acceptance.request_json(
            common.slot_url(slot),
            "/lab/faults",
            token,
            "POST",
            {"kind": kind, "duration_seconds": 120, **parameters},
        )

    def clear_fault(slot):
        acceptance.request_json(common.slot_url(slot), "/lab/faults", token, "DELETE")
        fault_slots.discard(slot)

    def deadline_handler(*_):
        raise RuntimeError("Maximum traffic acceptance runtime exceeded")

    previous_alarm = signal.signal(signal.SIGALRM, deadline_handler)
    signal.alarm(args.max_seconds)
    try:
        traffic.start()
        sampler.thread.start()
        with phase("baseline"):
            sleep(30)
        with phase("latency"):
            set_fault(initial_slot, "latency", delay_ms=300)
            sleep(15)
            clear_fault(initial_slot)
        with phase("cpu", expected_errors=True):
            set_fault(initial_slot, "cpu", cpu_iterations=1_000_000)
            sleep(20)
            clear_fault(initial_slot)
        with phase("redis_outage"):
            offset = acceptance.event_offset()
            stopped.add("redis")
            common.compose("stop", "redis", timeout=20)
            product, headers = acceptance.request_json(deploy.EDGE_URL, "/products/1")
            if product.get("id") != 1 or acceptance.header(headers, "X-Cache") != "BYPASS":
                raise RuntimeError("Redis outage did not produce successful BYPASS fallback")
            evidence = acceptance.wait_recovery(
                "redis", initial_version, initial_slot, offset, timeout=70
            )
            stopped.remove("redis")
            report["checks"].append(
                {"name": "redis_self_healing", "fallback_status": 200, **evidence}
            )
        with phase("api_outage", expected_errors=True):
            offset = acceptance.event_offset()
            target = "api-" + initial_slot
            stopped.add(target)
            common.compose("stop", target, timeout=20)
            evidence = acceptance.wait_recovery(
                "api", initial_version, initial_slot, offset, timeout=70
            )
            stopped.remove(target)
            report["checks"].append({"name": "api_self_healing", **evidence})
        with phase("normal_deployment"):
            result = deploy.deploy("sre-lab-api:" + other_version, observe_seconds=20)
            acceptance.edge_ok(other_version, result["active"])
            report["checks"].append(
                {
                    "name": "normal_deployment",
                    "release_id": result["release_id"],
                    "version": other_version,
                }
            )
        with phase("manual_rollback"):
            result = deploy.rollback()
            acceptance.edge_ok(initial_version, result["active"])
            report["checks"].append(
                {
                    "name": "manual_rollback",
                    "release_id": result["release_id"],
                    "version": initial_version,
                }
            )
            # Keep enough traffic around this short operation to make its sample meaningful.
            sleep(2)
        with phase("automatic_rollback", expected_errors=True):
            previous_id = result["release_id"]
            injection = {}

            def inject():
                try:
                    while not injector_stop.wait(0.1):
                        state = common.read_state()
                        if (
                            state.get("release_id") == previous_id
                            or state.get("phase") != "observing"
                        ):
                            continue
                        slot = state["active"]
                        if state["slots"][slot]["version"] != other_version:
                            raise RuntimeError("Unexpected candidate")
                        set_fault(slot, "errors", error_rate=1)
                        injection.update(injected=True, release_id=state["release_id"], slot=slot)
                        return
                except Exception as exc:  # noqa: BLE001 -- main checks this result
                    injection["error_type"] = type(exc).__name__

            offset = acceptance.event_offset()
            injector = threading.Thread(target=inject, name="fault-injector")
            injector.start()
            result_code = None
            try:
                command = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "ops.deploy",
                        "deploy",
                        "--image",
                        "sre-lab-api:" + other_version,
                        "--observe-seconds",
                        "20",
                    ],
                    cwd=common.ROOT,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
                result_code = command.returncode
            finally:
                injector_stop.set()
                injector.join(timeout=5)
            state = common.read_state()
            rollback_events = [
                row
                for row in acceptance.read_events(offset)
                if row.get("action") == "release_rolled_back"
                and row.get("details", {}).get("release_id") == injection.get("release_id")
            ]
            if (
                injector.is_alive()
                or result_code is None
                or result_code == 0
                or not injection.get("injected")
                or not rollback_events
                or state.get("phase") != "idle"
                or state.get("last_outcome") != "rolled_back"
                or state["slots"][state["active"]]["version"] != initial_version
            ):
                raise RuntimeError(
                    "Injected post-switch failure did not cause verified automatic rollback"
                )
            acceptance.edge_ok(initial_version, state["active"])
            clear_fault(injection["slot"])
            report["checks"].append(
                {
                    "name": "automatic_rollback",
                    "injection": injection,
                    "deployment_exit_code": result_code,
                    "recovery_event": rollback_events[-1],
                }
            )
        with phase("restore_rollback_copy"):
            # The failed candidate reused the previous slot; rebuild and validate
            # a good retained copy through real routing, never forge its state.
            deploy.deploy("sre-lab-api:" + other_version, observe_seconds=5)
            restored = deploy.rollback()
            acceptance.edge_ok(initial_version, restored["active"])
            if restored.get("previous") is None:
                raise RuntimeError("Previous rollback copy was not restored")
            report["checks"].append(
                {
                    "name": "restore_rollback_copy",
                    "active": restored["active"],
                    "previous": restored["previous"],
                }
            )
        with phase("final_recovery"):
            sleep(30)
            final = common.read_state()
            acceptance.edge_ok(initial_version, final["active"])
        report["status"] = "passed"
    except BaseException as exc:  # noqa: BLE001 -- interruption must still clean up and produce failure
        report["status"] = "failed"
        report["failure_type"] = type(exc).__name__
        if isinstance(exc, RuntimeError):
            report["failure"] = str(exc)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_alarm)
        traffic.set_phase("cleanup")
        injector_stop.set()
        cleanup_errors = []
        if injector is not None:
            injector.join(timeout=5)
            if injector.is_alive():
                cleanup_errors.append("fault injector did not stop")
        for target in sorted(stopped):
            try:
                common.compose("start", target, timeout=20)
            except Exception as exc:  # noqa: BLE001 -- complete all independent cleanup actions
                cleanup_errors.append(target + ": " + type(exc).__name__)
        for slot in tuple(fault_slots):
            try:
                clear_fault(slot)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append("clear fault " + slot + ": " + type(exc).__name__)
        try:
            final = common.read_state()
            if final.get("phase") != "idle":
                final = deploy.rollback()
            if final["slots"][final["active"]]["version"] != initial_version:
                final = deploy.rollback()
            if final.get("previous") is None:
                deploy.deploy("sre-lab-api:" + other_version, observe_seconds=5)
                final = deploy.rollback()
            acceptance.edge_ok(initial_version, final["active"])
            if final["slots"][final["previous"]]["version"] != other_version:
                raise RuntimeError("The retained rollback version is not the expected version")
            report["final_health"] = {
                "healthy": True,
                "version": initial_version,
                "slot": final["active"],
                "previous_slot": final["previous"],
                "previous_version": other_version,
            }
        except Exception as exc:  # noqa: BLE001
            cleanup_errors.append("final health: " + type(exc).__name__)
            report["final_health"] = {"healthy": False, "error_type": type(exc).__name__}
        for worker in (traffic, sampler):
            try:
                worker.close()
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append("background worker: " + type(exc).__name__)
        failures, coverage = evaluate(report["phases"], traffic.rows, sampler.rows)
        events = acceptance.read_events(initial_event_offset)
        common.atomic_write(
            directory / "events.jsonl", "".join(json.dumps(row) + "\n" for row in events)
        )
        report.update(
            ended_at=acceptance.utc_now(),
            total_seconds=round(time.monotonic() - started, 3),
            metrics_coverage=coverage,
            validation_failures=failures,
            cleanup_errors=cleanup_errors,
            raw_requests=len(traffic.rows),
            metrics_snapshots=len(sampler.rows),
            events=len(events),
            overall=summarize(traffic.rows, time.monotonic() - started),
            phase_attribution="dispatch; crossing samples remain in totals but are excluded from stable-phase error gate",
            fault_duration_note="Fault windows are requested durations; the real healer may restart API and clear an in-memory CPU fault early. Inspect application_fault snapshots and controller events.",
            resource_attribution="Traffic generator and monitoring run on the same host. Host CPU is not solely API usage; API process/cgroup metrics are recorded separately.",
            deadline_scope="max_seconds bounds the workload; bounded cleanup and worker joins may add time",
        )
        if failures or cleanup_errors:
            report["status"] = "failed"
        save()
    print(
        json.dumps(
            {
                "status": report["status"],
                "requests": report["raw_requests"],
                "directory": str(directory),
                "validation_failures": report["validation_failures"],
            }
        ),
        flush=True,
    )
    if report["status"] != "passed":
        raise SystemExit(1)
    return report


if __name__ == "__main__":
    main()
