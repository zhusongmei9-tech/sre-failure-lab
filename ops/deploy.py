"""Real serialized blue/green releases. Run: python3 -m ops.deploy --help."""

import argparse
import copy
import json
import re
import subprocess
import time
import uuid

from ops.common import (
    ROOT,
    atomic_write,
    compose,
    event,
    operation_lock,
    probe_slot,
    probe_url,
    read_state,
    write_state,
)

EDGE_URL = "http://127.0.0.1:18000"


def inspect_image(image):
    match = re.fullmatch(r"sre-lab-api:([A-Za-z0-9][A-Za-z0-9._-]{0,63})", image)
    if not match:
        raise ValueError("Image must be a local sre-lab-api:<version> tag")
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    metadata = json.loads(result.stdout)[0]
    image_id = metadata["Id"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Image does not have an immutable local ID")
    return {"image": image, "version": match[1], "image_id": image_id}


def save_images(slots):
    fallback = next(iter(slots.values()))["image_id"]
    content = "".join(
        slot.upper() + "_IMAGE=" + slots.get(slot, {}).get("image_id", fallback) + "\n"
        for slot in ("blue", "green")
    )
    atomic_write(ROOT / "runtime/images.env", content)


def upstream_config(slot):
    if slot not in ("blue", "green"):
        raise ValueError("Invalid upstream slot")
    return """upstream lab_backend {
    server api-SLOT:8000;
    keepalive 16;
}
server {
    listen 8080;
    server_name _;
    location / {
        proxy_pass http://lab_backend;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_connect_timeout 2s;
        proxy_read_timeout 10s;
        proxy_next_upstream off;
        add_header X-Deployment-Slot SLOT always;
    }
}
""".replace("SLOT", slot)


def write_upstream(slot):
    atomic_write(ROOT / "runtime/nginx/upstream.conf", upstream_config(slot), mode=0o644)


def switch_upstream(slot):
    path = ROOT / "runtime/nginx/upstream.conf"
    previous = path.read_text() if path.exists() else None
    write_upstream(slot)
    try:
        compose("exec", "-T", "edge", "nginx", "-t", timeout=10)
        compose("exec", "-T", "edge", "nginx", "-s", "reload", timeout=10)
    except Exception:
        if previous is not None:
            atomic_write(path, previous, mode=0o644)
            compose("exec", "-T", "edge", "nginx", "-t", timeout=10)
            compose("exec", "-T", "edge", "nginx", "-s", "reload", timeout=10)
        raise


def wait_slot(slot, version, seconds=45):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return probe_slot(slot, version)
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def wait_edge(version, seconds=10, expected_slot=None):
    # nginx reload is asynchronous: allow workers to adopt the new configuration.
    deadline = time.monotonic() + seconds
    while True:
        try:
            return probe_url(EDGE_URL, version, expected_slot=expected_slot)
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def assert_runtime_image(slot, image_id):
    container = compose("ps", "-q", "api-" + slot).stdout.strip()
    if not container:
        raise RuntimeError("Candidate container was not created")
    actual = subprocess.run(
        ["docker", "inspect", "--format", "{{.Image}}", container],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if actual != image_id:
        raise RuntimeError("Running container image ID differs from release artifact")


def start_slot(slot, metadata):
    compose("up", "-d", "--no-deps", "--force-recreate", "api-" + slot)
    assert_runtime_image(slot, metadata["image_id"])
    wait_slot(slot, metadata["version"])


def require_idle(state):
    if not state.get("active"):
        raise RuntimeError("Runtime is not initialized; run init first")
    if state.get("phase") != "idle":
        raise RuntimeError("Unfinished operation: run rollback to recover before another release")


def clean_state(state, outcome):
    state = copy.deepcopy(state)
    state.pop("stable_state", None)
    state.pop("candidate", None)
    state["phase"] = "idle"
    state["last_outcome"] = outcome
    return state


def initialize(image):
    with operation_lock():
        old = read_state()
        if old and old.get("phase") != "failed_initial":
            raise RuntimeError("Runtime already has state; use deploy or rollback")
        metadata = inspect_image(image)
        state = {
            "active": None,
            "previous": None,
            "slots": {"blue": metadata},
            "phase": "initializing",
            "release_id": uuid.uuid4().hex,
            "persistent": True,
            "simulation": False,
        }
        save_images(state["slots"])
        write_state(state)
        event("release_initializing", release_id=state["release_id"], image=image)
        try:
            compose("up", "-d", "redis")
            start_slot("blue", metadata)
            write_upstream("blue")
            compose("up", "-d", "--no-deps", "edge")
            # Init retries can recreate blue while an older edge still holds its old IP.
            switch_upstream("blue")
            wait_edge(metadata["version"], expected_slot="blue")
        except Exception:
            state["phase"] = "failed_initial"
            write_state(state)
            event("release_initial_failed", release_id=state["release_id"])
            raise
        state["active"] = "blue"
        state = clean_state(state, "initialized")
        write_state(state)
        event("release_initialized", release_id=state["release_id"], active="blue", image=image)
        return state


def restore_stable(transaction, reason):
    stable = copy.deepcopy(transaction["stable_state"])
    slot = stable["active"]
    metadata = stable["slots"][slot]
    # Retained old slot is not rebuilt during ordinary rollback.
    assert_runtime_image(slot, metadata["image_id"])
    wait_slot(slot, metadata["version"])
    switch_upstream(slot)
    wait_edge(metadata["version"], expected_slot=slot)
    stable["release_id"] = transaction["release_id"]
    stable = clean_state(stable, "rolled_back")
    write_state(stable)
    event(
        "release_rolled_back",
        release_id=transaction["release_id"],
        active=slot,
        version=metadata["version"],
        reason=reason,
    )
    return stable


def deploy(image, observe_seconds=30):
    if not 1 <= observe_seconds <= 300:
        raise ValueError("Observation window must be 1..300 seconds")
    with operation_lock():
        state = read_state()
        require_idle(state)
        metadata = inspect_image(image)
        active = state["active"]
        if metadata["image_id"] == state["slots"][active]["image_id"]:
            raise ValueError("That immutable image is already active")
        if metadata["version"] == state["slots"][active]["version"]:
            raise ValueError(
                "Each artifact must use a new version tag; active version cannot be reused"
            )
        candidate = "green" if active == "blue" else "blue"
        stable = copy.deepcopy(state)
        # Reusing a slot destroys the old rollback copy even if validation later fails.
        if stable.get("previous") == candidate:
            stable["previous"] = None
        stable["slots"][candidate] = metadata
        transaction = copy.deepcopy(stable)
        transaction.update(
            phase="preparing", release_id=uuid.uuid4().hex, candidate=candidate, stable_state=stable
        )
        write_state(transaction)
        save_images(transaction["slots"])
        event(
            "release_started",
            release_id=transaction["release_id"],
            image=image,
            image_id=metadata["image_id"],
            candidate=candidate,
        )
        try:
            start_slot(candidate, metadata)
        except Exception:
            stable["release_id"] = transaction["release_id"]
            write_state(clean_state(stable, "rejected_before_switch"))
            event("release_rejected", release_id=transaction["release_id"], image=image)
            raise
        transaction["phase"] = "switching"
        write_state(transaction)
        try:
            switch_upstream(candidate)
            transaction.update(phase="observing", active=candidate, previous=active)
            write_state(transaction)
            wait_edge(metadata["version"], expected_slot=candidate)
            event(
                "release_switched",
                release_id=transaction["release_id"],
                active=candidate,
                version=metadata["version"],
            )
            deadline = time.monotonic() + observe_seconds
            while True:
                probe_slot(candidate, metadata["version"])
                probe_url(EDGE_URL, metadata["version"], expected_slot=candidate)
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        except Exception as exc:
            try:
                restore_stable(transaction, "failed_health_observation_or_switch")
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                transaction["phase"] = "recovery_required"
                write_state(transaction)
                event("release_rollback_failed", release_id=transaction["release_id"])
                raise RuntimeError("Release failed AND rollback could not be verified") from exc
            raise RuntimeError("Release failed; previous version restored and verified") from exc
        completed = clean_state(transaction, "completed")
        write_state(completed)
        event(
            "release_completed",
            release_id=completed["release_id"],
            active=candidate,
            version=metadata["version"],
        )
        return completed


def rollback():
    with operation_lock():
        state = read_state()
        if state.get("phase") != "idle":
            if state.get("stable_state", {}).get("active"):
                return restore_stable(state, "operator_recovered_interrupted_release")
            raise RuntimeError("No verified previous version exists; cannot claim rollback")
        require_idle(state)
        previous = state.get("previous")
        if previous is None:
            raise RuntimeError("No retained previous slot is available")
        transaction = copy.deepcopy(state)
        transaction.update(phase="switching", release_id=uuid.uuid4().hex, stable_state=state)
        write_state(transaction)
        try:
            metadata = state["slots"][previous]
            assert_runtime_image(previous, metadata["image_id"])
            wait_slot(previous, metadata["version"])
            switch_upstream(previous)
            wait_edge(metadata["version"], expected_slot=previous)
        except Exception as exc:
            try:
                restore_stable(transaction, "manual_rollback_failed_restore_current")
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                transaction["phase"] = "recovery_required"
                write_state(transaction)
                raise RuntimeError("Manual rollback failed; operator recovery required") from exc
            raise RuntimeError("Manual rollback rejected; current version restored") from exc
        transaction.update(active=previous, previous=state["active"])
        result = clean_state(transaction, "manually_rolled_back")
        write_state(result)
        event(
            "release_rolled_back",
            release_id=result["release_id"],
            active=previous,
            version=metadata["version"],
            reason="operator",
        )
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("init", "deploy"):
        command = commands.add_parser(action)
        command.add_argument("--image", required=True)
        if action == "deploy":
            command.add_argument("--observe-seconds", type=int, default=30)
    commands.add_parser("rollback")
    commands.add_parser("status")
    args = parser.parse_args()
    try:
        if args.action == "init":
            result = initialize(args.image)
        elif args.action == "deploy":
            result = deploy(args.image, args.observe_seconds)
        elif args.action == "rollback":
            result = rollback()
        else:
            result = read_state()
        print(json.dumps(result, indent=2))
    except (ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
