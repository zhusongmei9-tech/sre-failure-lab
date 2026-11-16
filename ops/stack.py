"""Start/stop the initialized real lab without creating a new release (Python 3.9+)."""

import argparse
import json
import re
import subprocess
import time
from collections import Counter
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from ops import common, deploy

LOCK_SECONDS = 30
START_SECONDS = 90
STOP_SECONDS = 45
PROJECT_LABEL = "com.docker.compose.project=sre-lab"
MONITORING = ("prometheus", "grafana", "node-exporter", "cadvisor")
INFRASTRUCTURE = ("redis", "edge") + MONITORING


def _remaining(deadline, maximum=90):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("Stack operation deadline exceeded")
    return min(remaining, maximum)


@contextmanager
def bounded_lock(seconds=LOCK_SECONDS):
    """Share the release/healer lock, without waiting indefinitely for its owner."""
    deadline = time.monotonic() + seconds
    while True:
        lock = common.operation_lock(blocking=False)
        try:
            lock.__enter__()
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Another lab operation holds the lock; retry after it finishes")
            time.sleep(min(0.25, _remaining(deadline)))
        else:
            break
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _run(arguments, deadline, maximum=15):
    return subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        timeout=_remaining(deadline, maximum),
    ).stdout.strip()


def _compose(deadline, *arguments):
    return common.compose(
        "--profile", "slots", *arguments, timeout=_remaining(deadline)
    ).stdout.strip()


def _validate_state(state):
    if not isinstance(state, dict) or state.get("active") not in ("blue", "green"):
        raise RuntimeError("Runtime is not initialized; initialize a release before starting")
    if state.get("phase") != "idle":
        raise RuntimeError(
            "Interrupted deployment: state is not idle; operator recovery is required. "
            "Stack start will not select or roll back a release automatically"
        )
    slots = state.get("slots")
    if not isinstance(slots, dict) or not slots or set(slots) - {"blue", "green"}:
        raise RuntimeError("Invalid saved deployment slots")
    if state["active"] not in slots:
        raise RuntimeError("Saved active slot has no release artifact")
    previous = state.get("previous")
    if previous is not None and (
        not isinstance(previous, str) or previous not in slots or previous == state["active"]
    ):
        raise RuntimeError("Invalid saved rollback slot")
    if not isinstance(state.get("release_id"), str) or not state["release_id"]:
        raise RuntimeError("Saved release identity is missing")
    for metadata in slots.values():
        if not isinstance(metadata, dict):
            raise RuntimeError("Invalid saved release metadata")  # noqa: TRY004
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(metadata.get("image_id", ""))):
            raise RuntimeError("Saved release must reference an immutable local image ID")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(metadata.get("version", ""))):
            raise RuntimeError("Invalid saved release version")


def _assert_local_images(state, deadline):
    for slot, metadata in state["slots"].items():
        image_id = metadata["image_id"]
        actual = _run(["docker", "image", "inspect", "--format", "{{.Id}}", image_id], deadline)
        if actual != image_id:
            raise RuntimeError("Local immutable image differs from saved " + slot + " artifact")


def _assert_runtime_images(state, deadline):
    for slot, metadata in state["slots"].items():
        containers = _compose(deadline, "ps", "-q", "api-" + slot).splitlines()
        if len(containers) != 1 or not containers[0]:
            raise RuntimeError("Expected exactly one running container for " + slot)
        actual = _run(["docker", "inspect", "--format", "{{.Image}}", containers[0]], deadline)
        if actual != metadata["image_id"]:
            raise RuntimeError("Running image differs from saved " + slot + " artifact")


def _running_containers(deadline):
    output = _run(
        [
            "docker",
            "ps",
            "--filter",
            "label=" + PROJECT_LABEL,
            "--format",
            '{{.Label "com.docker.compose.service"}}\t{{.State}}',
        ],
        deadline,
    )
    return [line.split("\t", 1) for line in output.splitlines() if line]


def _assert_running(state, deadline):
    rows = _running_containers(deadline)
    expected = Counter(INFRASTRUCTURE + tuple("api-" + slot for slot in state["slots"]))
    if any(len(row) != 2 or row[1] != "running" for row in rows):
        raise RuntimeError("A lab container is not running")
    if Counter(row[0] for row in rows) != expected:
        raise RuntimeError(
            "Running lab containers do not match the saved slots and monitoring stack"
        )
    return len(rows)


def _http(url, deadline, json_body=True, expected_slot=None):
    with urlopen(url, timeout=_remaining(deadline, 3)) as response:
        if response.status != 200:
            raise RuntimeError("Health endpoint did not return HTTP 200")
        if expected_slot is not None and response.headers.get("X-Deployment-Slot") != expected_slot:
            raise RuntimeError("Edge is serving a different deployment slot")
        body = response.read(65537)
        if len(body) > 65536:
            raise RuntimeError("Health endpoint response is too large")
        if not json_body:
            return None
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise RuntimeError("Health endpoint did not return a JSON object")  # noqa: TRY004
        return payload


def _probe_api(base, version, deadline, expected_slot=None):
    def get(path):
        return _http(base + path, deadline, expected_slot=expected_slot)

    if get("/health/live").get("status") != "ok":
        raise RuntimeError("API liveness failed")
    if get("/health/ready").get("status") != "ready":
        raise RuntimeError("API and Redis are not fully ready")
    if get("/version").get("version") != version:
        raise RuntimeError("API version differs from the saved release")
    product = get("/products/1")
    if product.get("id") != 1 or not isinstance(product.get("name"), str):
        raise RuntimeError("Product business probe failed")


def _wait(check, deadline, message):
    while True:
        try:
            result = check()
            _remaining(deadline)
            return result
        except (HTTPError, URLError, OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            if time.monotonic() >= deadline:
                raise RuntimeError(message) from None
            time.sleep(min(0.5, _remaining(deadline)))


def _up(deadline, *services):
    _compose(deadline, "up", "-d", "--no-deps", "--pull", "never", "--no-build", *services)


def _stop_project(deadline):
    # stop retains containers, named volumes, networks, faults on disk and release state.
    try:
        _compose(deadline, "stop", "--timeout", "20")
    except (OSError, subprocess.SubprocessError):
        # An uninitialized/already-removed project may have no env files to parse.
        # Success is still conditional on Docker proving that nothing is running.
        if _running_containers(deadline):
            raise RuntimeError("Compose stop failed and lab containers remain running") from None
        return
    if _running_containers(deadline):
        raise RuntimeError("Lab containers remain running after compose stop")


def _report(state, containers=None):
    active = state["active"]
    result = {
        "active": active,
        "previous": state.get("previous"),
        "version": state["slots"][active]["version"],
        "release_id": state["release_id"],
        "phase": state["phase"],
    }
    if containers is not None:
        result["running_containers"] = containers
    return result


def start():
    """Restore the saved deployment; workload deadline excludes lock wait and cleanup."""
    stage = "lock"
    try:
        with bounded_lock():
            stage = "validate_state"
            state = common.read_state()
            _validate_state(state)
            deadline = time.monotonic() + START_SECONDS
            stage = "validate_images"
            _assert_local_images(state, deadline)
            stage = "publish_configuration"
            deploy.save_images(state["slots"])
            common.publish_active_target(state)
            try:
                stage = "start_backends"
                _up(deadline, "redis")
                _up(deadline, *("api-" + slot for slot in state["slots"]))
                _assert_runtime_images(state, deadline)
                active = state["active"]
                version = state["slots"][active]["version"]
                stage = "backend_readiness"
                _wait(
                    lambda: _probe_api(common.slot_url(active), version, deadline),
                    deadline,
                    "Active API did not become ready within the startup deadline",
                )
                stage = "restore_edge"
                deploy.write_upstream(active)
                _up(deadline, "edge")
                # Reload even an already-running edge: recreated backends may have new IPs.
                _compose(deadline, "exec", "-T", "edge", "nginx", "-t")
                _compose(deadline, "exec", "-T", "edge", "nginx", "-s", "reload")
                stage = "start_monitoring"
                _up(deadline, *MONITORING)
                stage = "verify_stack"

                def verify():
                    _probe_api(deploy.EDGE_URL, version, deadline, expected_slot=active)
                    _http("http://127.0.0.1:9090/-/ready", deadline, json_body=False)
                    if _http("http://127.0.0.1:3000/api/health", deadline).get("database") != "ok":
                        raise RuntimeError("Grafana database is not healthy")
                    _assert_runtime_images(state, deadline)
                    return _assert_running(state, deadline)

                count = _wait(
                    verify, deadline, "Stack health verification exceeded startup deadline"
                )
                result = _report(state, count)
                common.event("stack_started", **result)
                return result
            except BaseException as exc:
                cleanup = "stopped"
                try:
                    _stop_project(time.monotonic() + STOP_SECONDS)
                except (
                    OSError,
                    ValueError,
                    RuntimeError,
                    subprocess.SubprocessError,
                    KeyboardInterrupt,
                ):
                    cleanup = "failed"
                common.event(
                    "stack_failed",
                    operation="start",
                    stage=stage,
                    error_kind=type(exc).__name__,
                    cleanup=cleanup,
                )
                # Mark as reported without exposing subprocess output or environment settings.
                stage = "reported"
                raise
    except BaseException as exc:
        if stage != "reported":
            common.event(
                "stack_failed",
                operation="start",
                stage=stage,
                error_kind=type(exc).__name__,
                cleanup="not_needed",
            )
        raise


def stop():
    """Stop this Compose project only; interrupted release records remain untouched."""
    try:
        with bounded_lock():
            _stop_project(time.monotonic() + STOP_SECONDS)
            common.event("stack_stopped", running_containers=0, data_preserved=True)
            return {"running_containers": 0, "data_preserved": True}
    except BaseException as exc:
        common.event("stack_failed", operation="stop", error_kind=type(exc).__name__)
        raise


def status():
    """Read-only snapshot, including stopped containers; no environment is inspected."""
    state = common.read_state()
    if not isinstance(state, dict):
        raise RuntimeError("Saved state is not a JSON object")  # noqa: TRY004
    slots = state.get("slots", {})
    if not isinstance(slots, dict):
        raise RuntimeError("Saved slots are not a JSON object")  # noqa: TRY004
    deadline = time.monotonic() + 30
    output = _run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=" + PROJECT_LABEL,
            "--format",
            '{{.ID}}\t{{.Label "com.docker.compose.service"}}\t{{.State}}',
        ],
        deadline,
    )
    containers = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            raise RuntimeError("Docker returned an invalid container status")
        container_id, service, container_state = fields
        health = _run(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                container_id,
            ],
            deadline,
        )
        containers.append(
            {"id": container_id, "service": service, "state": container_state, "health": health}
        )
    expected = sorted(INFRASTRUCTURE + tuple("api-" + s for s in slots if s in ("blue", "green")))
    present = {row["service"] for row in containers}
    missing = sorted(set(expected) - present)
    not_running = sorted({row["service"] for row in containers if row["state"] != "running"})
    unexpected = sorted(present - set(expected))
    active = state.get("active")
    metadata = slots.get(active, {}) if isinstance(active, str) else {}
    version = metadata.get("version") if isinstance(metadata, dict) else None
    return {
        "active": active,
        "previous": state.get("previous"),
        "version": version,
        "phase": state.get("phase"),
        "release_id": state.get("release_id"),
        "expected_services": expected,
        "containers": containers,
        "running_containers": sum(row["state"] == "running" for row in containers),
        "missing_services": missing,
        "not_running": not_running,
        "unexpected_services": unexpected,
        "all_running": (
            state.get("phase") == "idle"
            and isinstance(active, str)
            and active in slots
            and not missing
            and not not_running
            and not unexpected
            and len(containers) == len(expected)
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status"))
    args = parser.parse_args()
    try:
        result = {"start": start, "stop": stop, "status": status}[args.action]()
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as exc:
        # Do not print CalledProcessError/TimeoutExpired: their output can include env expansion.
        message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        parser.exit(1, "Stack " + args.action + " failed: " + message + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
