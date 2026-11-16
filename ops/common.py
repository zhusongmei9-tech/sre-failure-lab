"""Shared host helpers. Python 3.9+, standard library only."""

import fcntl
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def compose(*args, **kwargs):
    kwargs.setdefault("timeout", 90)
    kwargs.setdefault("text", True)
    kwargs.setdefault("capture_output", True)
    return subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            "sre-lab",
            "--env-file",
            str(ROOT / ".env"),
            "--env-file",
            str(ROOT / "runtime/images.env"),
            "-f",
            str(ROOT / "compose.runtime.yaml"),
            *args,
        ],
        cwd=ROOT,
        check=True,
        **kwargs,
    )


def read_state():
    path = ROOT / "runtime/state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def write_state(state):
    atomic_write(ROOT / "runtime/state.json", json.dumps(state, indent=2) + "\n")
    publish_active_target(state)


def publish_active_target(state):
    """Publish one active backend with release identity for Prometheus file_sd.

    Explicit release labels prevent counter rates being computed across blue/green
    switches. This file contains no environment settings or credentials.
    """
    directory = ROOT / "runtime/prometheus"
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    directory.chmod(0o755)
    slot = state.get("active")
    targets = []
    if slot is not None:
        if slot not in ("blue", "green"):
            raise ValueError("Invalid active metrics slot")
        metadata = state["slots"][slot]
        targets.append(
            {
                "targets": ["api-" + slot + ":8000"],
                "labels": {
                    "slot": slot,
                    "version": metadata["version"],
                    "image_id": metadata["image_id"],
                },
            }
        )
    atomic_write(directory / "active.json", json.dumps(targets, indent=2) + "\n", mode=0o644)


@contextmanager
def operation_lock(blocking=True):
    path = ROOT / "runtime/operations.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def event(action, **details):
    path = ROOT / "runtime/events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        "action": action,
        "simulation": False,
        "details": details,
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return row


def slot_url(slot):
    if slot not in ("blue", "green"):
        raise ValueError("slot must be blue or green")
    return "http://127.0.0.1:" + ("18001" if slot == "blue" else "18002")


def get_json(url, timeout=3, expected_slot=None):
    try:
        with urlopen(url, timeout=timeout) as response:
            if (
                expected_slot is not None
                and response.headers.get("X-Deployment-Slot") != expected_slot
            ):
                raise RuntimeError("Edge is not serving the requested deployment slot")
            return json.load(response)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise RuntimeError("HTTP probe failed: " + url) from exc


def probe_url(base_url, expected_version=None, require_ready=True, expected_slot=None):
    if get_json(base_url + "/health/live").get("status") != "ok":
        raise RuntimeError("Liveness probe did not return ok")
    if require_ready and get_json(base_url + "/health/ready").get("status") != "ready":
        raise RuntimeError("Candidate is not fully ready (degraded is insufficient)")
    version_payload = (
        get_json(base_url + "/version", expected_slot=expected_slot)
        if expected_slot is not None
        else get_json(base_url + "/version")
    )
    version = version_payload.get("version")
    if not version or (expected_version is not None and version != expected_version):
        raise RuntimeError("Running version differs from the requested version")
    product = get_json(base_url + "/products/1")
    if product.get("id") != 1 or not isinstance(product.get("name"), str):
        raise RuntimeError("Business smoke check returned an invalid product")
    return {"version": version, "product_id": product["id"]}


def probe_slot(slot, expected_version=None, require_ready=True):
    return probe_url(slot_url(slot), expected_version, require_ready)
