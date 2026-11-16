"""REAL dedicated-lab acceptance. Requires initialized 0.3.0 and a running healer.

This script changes live routing and deliberately stops lab services. It is meant
only for this project's dedicated Linux test host, not an arbitrary deployment.
"""

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops import common, deploy


def utc_now():
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017 -- host Python 3.9


def load_token():
    for line in (common.ROOT / ".env").read_text().splitlines():
        if line.startswith("LAB_TOKEN="):
            value = line.split("=", 1)[1].strip().strip("\"'")
            if value:
                return value
    raise RuntimeError("LAB_TOKEN is missing; initialize the dedicated lab first")


def request_json(base, path, token=None, method="GET", body=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = Request(
        base + path,
        method=method,
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urlopen(request, timeout=3) as response:
            return json.load(response), dict(response.headers)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        # Deliberately exclude request headers and token from exceptions/reports.
        raise RuntimeError("Acceptance HTTP check failed: " + path) from exc


def header(headers, name):
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


def edge_ok(version, slot):
    common.probe_url(deploy.EDGE_URL, version, expected_slot=slot)


def event_offset():
    path = common.ROOT / "runtime/events.jsonl"
    return path.stat().st_size if path.exists() else 0


def read_events(offset):
    path = common.ROOT / "runtime/events.jsonl"
    if not path.exists():
        return []
    with path.open("rb") as stream:
        stream.seek(offset)
        # A daemon may be appending its last line: ignore only an incomplete tail.
        contents = stream.read()
    lines = contents.split(b"\n")
    return [json.loads(line) for line in lines[:-1] if line]


def wait_recovery(service, version, slot, offset, timeout=70):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        events = [
            row for row in read_events(offset) if row.get("details", {}).get("service") == service
        ]
        actions = [row for row in events if row.get("action") == "healing_action_accepted"]
        recoveries = [row for row in events if row.get("action") == "healing_recovery_verified"]
        accepted_incidents = {row["details"].get("incident_id") for row in actions}
        verified = [
            row for row in recoveries if row["details"].get("incident_id") in accepted_incidents
        ]
        if verified:
            try:
                edge_ok(version, slot)
            except RuntimeError:
                pass
            else:
                return {
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "actions": actions,
                    "recovery": verified[-1],
                }
        time.sleep(0.5)
    raise RuntimeError("No verified controller recovery within 70 seconds: " + service)


def main():
    token = load_token()
    initial = common.read_state()
    active = initial.get("active")
    if initial.get("phase") != "idle" or active not in ("blue", "green"):
        raise RuntimeError("Acceptance requires an initialized idle runtime")
    if initial["slots"][active]["version"] != "0.3.0":
        raise RuntimeError("Acceptance must begin with active version 0.3.0")
    # Ensure both local artifacts exist before making changes.
    deploy.inspect_image("sre-lab-api:0.3.0")
    deploy.inspect_image("sre-lab-api:0.3.1")
    edge_ok("0.3.0", active)
    healing_path = common.ROOT / "runtime/healing.json"
    if not healing_path.exists():
        raise RuntimeError("Start the real healing daemon before running acceptance")
    report = {
        "schema_version": 1,
        "mode": "real",
        "started_at": utc_now(),
        "initial_version": "0.3.0",
        "checks": [],
        "status": "running",
    }
    report_path = common.ROOT / "runtime/acceptance.json"
    stopped_targets = set()
    fault_slots = set()
    stop_injector = threading.Event()
    injector = None

    def record(name, **details):
        report["checks"].append({"name": name, "passed": True, "timestamp": utc_now(), **details})
        common.atomic_write(report_path, json.dumps(report, indent=2) + "\n")
        print("PASS: " + name, flush=True)

    try:
        started = time.monotonic()
        released = deploy.deploy("sre-lab-api:0.3.1", observe_seconds=5)
        edge_ok("0.3.1", released["active"])
        record(
            "normal_deployment",
            version="0.3.1",
            slot=released["active"],
            release_id=released["release_id"],
            elapsed_seconds=round(time.monotonic() - started, 3),
        )

        restored = deploy.rollback()
        edge_ok("0.3.0", restored["active"])
        record(
            "manual_rollback",
            version="0.3.0",
            slot=restored["active"],
            release_id=restored["release_id"],
        )

        injection_result = {}
        before_release_id = restored["release_id"]

        def inject_during_observation():
            try:
                while not stop_injector.wait(0.1):
                    state = common.read_state()
                    if state.get("release_id") == before_release_id:
                        continue
                    if state.get("phase") != "observing":
                        continue
                    slot = state["active"]
                    if state["slots"][slot]["version"] != "0.3.1":
                        raise RuntimeError("Unexpected candidate version")
                    fault_slots.add(slot)
                    request_json(
                        common.slot_url(slot),
                        "/lab/faults",
                        token,
                        "POST",
                        {
                            "kind": "errors",
                            "error_rate": 1,
                            "duration_seconds": 120,
                        },
                    )
                    injection_result.update(
                        injected=True,
                        slot=slot,
                        release_id=state["release_id"],
                        timestamp=utc_now(),
                    )
                    return
            except Exception as exc:  # noqa: BLE001 -- propagate a sanitized failure to main
                injection_result["error_type"] = type(exc).__name__

        injector = threading.Thread(target=inject_during_observation, daemon=True)
        injector.start()
        failed_as_expected = False
        try:
            deploy.deploy("sre-lab-api:0.3.1", observe_seconds=20)
        except RuntimeError:
            failed_as_expected = True
        finally:
            stop_injector.set()
            injector.join(timeout=5)
        state = common.read_state()
        if (
            not failed_as_expected
            or not injection_result.get("injected")
            or state.get("phase") != "idle"
            or state.get("last_outcome") != "rolled_back"
            or state["slots"][state["active"]]["version"] != "0.3.0"
        ):
            raise RuntimeError("Post-switch failure did not produce a verified automatic rollback")
        edge_ok("0.3.0", state["active"])
        record(
            "post_switch_automatic_rollback",
            injected=injection_result,
            active_version="0.3.0",
            active_slot=state["active"],
        )
        for slot in tuple(fault_slots):
            request_json(common.slot_url(slot), "/lab/faults", token, "DELETE")
            fault_slots.remove(slot)

        final = deploy.deploy("sre-lab-api:0.3.1", observe_seconds=5)
        final_slot = final["active"]
        edge_ok("0.3.1", final_slot)
        record(
            "final_deployment",
            version="0.3.1",
            slot=final_slot,
            previous_slot=final["previous"],
            release_id=final["release_id"],
        )

        offset = event_offset()
        stopped_targets.add("redis")
        common.compose("stop", "redis", timeout=20)
        product, headers = request_json(deploy.EDGE_URL, "/products/1")
        if product.get("id") != 1 or header(headers, "X-Cache") != "BYPASS":
            raise RuntimeError("Redis outage did not preserve successful fallback responses")
        record("redis_outage_fallback", status_code=200, cache="BYPASS")
        evidence = wait_recovery("redis", "0.3.1", final_slot, offset)
        stopped_targets.remove("redis")
        record("redis_self_healing", **evidence)

        offset = event_offset()
        target = "api-" + final_slot
        stopped_targets.add(target)
        common.compose("stop", target, timeout=20)
        evidence = wait_recovery("api", "0.3.1", final_slot, offset)
        stopped_targets.remove(target)
        record("api_self_healing", version="0.3.1", **evidence)
        report["status"] = "passed"
        report["final_state"] = common.read_state()
    except Exception as exc:
        report["status"] = "failed"
        report["failure_type"] = type(exc).__name__
        # Only fixed failure descriptions from this script are exported.
        if isinstance(exc, RuntimeError):
            report["failure"] = str(exc)
        raise
    finally:
        stop_injector.set()
        if injector is not None:
            injector.join(timeout=5)
        cleanup_errors = []
        for target in sorted(stopped_targets):
            try:
                common.compose("start", target, timeout=20)
            except Exception as exc:  # noqa: BLE001 -- preserve main failure while attempting cleanup
                cleanup_errors.append({"target": target, "error_type": type(exc).__name__})
        for slot in sorted(fault_slots):
            try:
                request_json(common.slot_url(slot), "/lab/faults", token, "DELETE")
            except Exception as exc:  # noqa: BLE001 -- fault also has a bounded expiry
                cleanup_errors.append({"slot": slot, "error_type": type(exc).__name__})
        if cleanup_errors:
            report["cleanup_errors"] = cleanup_errors
            report["status"] = "failed"
        report["ended_at"] = utc_now()
        common.atomic_write(report_path, json.dumps(report, indent=2) + "\n")
    if report["status"] != "passed":
        raise RuntimeError("Acceptance cleanup was incomplete; inspect runtime/acceptance.json")
    print("Acceptance passed; report: " + str(report_path), flush=True)


if __name__ == "__main__":
    main()
