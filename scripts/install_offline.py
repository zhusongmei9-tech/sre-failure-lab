"""Install this ARM64 offline bundle once: python3 scripts/install_offline.py."""

import argparse
import copy
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops import common, deploy, stack
from scripts import init_env

INSTALL_ROOT = Path("/opt/sre-failure-lab")
INFRA_IMAGES = {
    "redis:7.4-alpine",
    "nginx:stable-alpine",
    "prom/prometheus:v3.16.0",
    "grafana/grafana:13.3.0",
    "quay.io/prometheus/node-exporter:v1.12.1",
    "ghcr.io/google/cadvisor:0.55.1",
}
PROJECT_LABEL = "com.docker.compose.project=sre-lab"


class InstallError(RuntimeError):
    """Safe, credential-free explanation suitable for terminal output."""


def _run(args, description, timeout=30):
    try:
        return subprocess.run(
            args, cwd=ROOT, check=True, capture_output=True, text=True, timeout=timeout
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        # Do not forward command output: Compose could include environment settings.
        raise InstallError(description + " failed (" + type(exc).__name__ + ")") from None


def _arm64(architecture):
    return architecture.lower() in ("arm64", "aarch64")


def _check_host():
    if ROOT.resolve() != INSTALL_ROOT:
        raise InstallError("Extract the project into /opt/sre-failure-lab before installing")
    if platform.system() != "Linux" or not _arm64(platform.machine()):
        raise InstallError("This offline bundle requires a Linux ARM64 host")
    if os.geteuid() != 0:
        raise InstallError("Run the offline installer as root")
    if sys.version_info < (3, 9):  # noqa: UP036 - host runtime supports Python 3.9+
        raise InstallError("Python 3.9 or newer is required")
    for executable in ("docker", "python3", "bash", "systemctl", "systemd-analyze", "install"):
        if shutil.which(executable) is None:
            raise InstallError("Required local program is missing: " + executable)
    for relative in (
        ".env.example",
        "compose.runtime.yaml",
        "scripts/install_systemd.sh",
        "ops/sre-lab.service",
        "ops/sre-lab-healer.service",
    ):
        if not (ROOT / relative).is_file():
            raise InstallError("Offline bundle is incomplete: " + relative)
    template = (ROOT / ".env.example").read_text()
    for key in ("LAB_TOKEN", "GRAFANA_ADMIN_PASSWORD"):
        assignments = [line for line in template.splitlines() if line.startswith(key + "=")]
        if assignments != [key + "="]:
            raise InstallError("Credential template must contain exactly one blank " + key)
    if any(
        line.startswith(("RUNTIME_BIND_HOST=", "METRICS_HOST_GATEWAY="))
        for line in template.splitlines()
    ):
        raise InstallError("Runtime network settings must be generated on the new host")


def _manifest():
    try:
        manifest = json.loads((ROOT / "bundle-manifest.json").read_text())
    except (OSError, ValueError):
        raise InstallError("Cannot read a valid bundle-manifest.json") from None
    if not isinstance(manifest, dict):
        raise InstallError("Bundle manifest must be a JSON object")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise InstallError("Unsupported offline bundle schema version")
    if manifest.get("platform") != "linux/arm64":
        raise InstallError("Bundle platform must be linux/arm64")
    checksum = manifest.get("images_sha256")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise InstallError("images_sha256 must be a lowercase SHA256 hex digest")
    state = manifest.get("state")
    try:
        stack._validate_state(state)
    except (RuntimeError, TypeError, KeyError):
        raise InstallError("Bundle release state must be initialized, idle and valid") from None
    expected = set(INFRA_IMAGES)
    for metadata in state["slots"].values():
        if (
            not isinstance(metadata["version"], str)
            or metadata.get("image") != "sre-lab-api:" + metadata["version"]
        ):
            raise InstallError("API image tag must match its saved release version")
        expected.add(metadata["image"])
    images = manifest.get("images")
    if not isinstance(images, list) or not images:
        raise InstallError("Bundle must contain its image inventory")
    inventory = {}
    for record in images:
        if not isinstance(record, dict):
            raise InstallError("Invalid image inventory record")
        reference = record.get("reference")
        if not isinstance(reference, str) or reference not in expected or reference in inventory:
            raise InstallError("Image inventory has an unexpected or duplicate reference")
        if record.get("architecture") != "arm64" or record.get("os") != "linux":
            raise InstallError("Every bundled image must be linux/arm64")
        image_id = record.get("image_id")
        if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise InstallError("Every bundled image must have an immutable SHA256 ID")
        config_digest = record.get("config_digest")
        if not isinstance(config_digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", config_digest
        ):
            raise InstallError("Every bundled image must have its exact config SHA256 digest")
        diff_ids = record.get("rootfs_diff_ids")
        if (
            not isinstance(diff_ids, list)
            or not diff_ids
            or any(
                not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                for digest in diff_ids
            )
        ):
            raise InstallError("Every bundled image must have its ordered rootfs SHA256 diff IDs")
        inventory[reference] = record
    if set(inventory) != expected:
        raise InstallError("Image inventory does not contain every required service image")
    for metadata in state["slots"].values():
        if inventory[metadata["image"]]["image_id"] != metadata["image_id"]:
            raise InstallError("Saved slot and image inventory disagree about the artifact ID")
    return manifest


def _check_archive(expected):
    archive = ROOT / "images.tar.gz"
    digest = hashlib.sha256()
    try:
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise InstallError("Cannot read images.tar.gz") from None
    if digest.hexdigest() != expected:
        raise InstallError("images.tar.gz checksum mismatch; no environment or state was created")


def _check_docker():
    _run(["docker", "compose", "version", "--short"], "Docker Compose check")
    info = _run(
        ["docker", "info", "--format", "{{.OSType}} {{.Architecture}}"], "Docker daemon check"
    )
    parts = info.split()
    if len(parts) != 2 or parts[0] != "linux" or not _arm64(parts[1]):
        raise InstallError("Docker daemon must run Linux ARM64")
    _run(
        ["/usr/bin/python3", "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"],
        "Systemd /usr/bin/python3 version check",
    )


def _require_fresh():
    for path in (ROOT / ".env", ROOT / "runtime/state.json"):
        if os.path.lexists(path):
            raise InstallError(
                "Existing installation found; manage it with systemctl, not this installer"
            )
    if _run(
        ["docker", "ps", "-aq", "--filter", "label=" + PROJECT_LABEL], "Existing container check"
    ):
        raise InstallError(
            "Existing sre-lab containers found; use systemctl to manage that installation"
        )
    if _run(
        ["docker", "volume", "ls", "-q", "--filter", "label=" + PROJECT_LABEL],
        "Existing volume check",
    ):
        raise InstallError(
            "Existing sre-lab data volumes found; refusing to replace their credentials"
        )
    names = _run(
        ["docker", "volume", "ls", "--format", "{{.Name}}"], "Volume name check"
    ).splitlines()
    if {"sre-lab_runtime-grafana", "sre-lab_runtime-prometheus"}.intersection(names):
        raise InstallError("Existing sre-lab data volume names found; installation is not fresh")


def _gateway():
    output = _run(
        [
            "docker",
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{range .IPAM.Config}}{{println .Gateway}}{{end}}",
        ],
        "Docker bridge gateway check",
    )
    for candidate in output.split():
        try:
            address = ipaddress.IPv4Address(candidate)
        except ipaddress.AddressValueError:
            continue
        if not address.is_unspecified and not address.is_loopback and not address.is_multicast:
            return str(address)
    raise InstallError("Default Docker bridge has no usable IPv4 gateway")


def _verify_images(images):
    loaded_ids = {}
    for image in images:
        actual = _run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}} {{.Architecture}} {{.Os}} {{json .RootFS.Layers}}",
                image["reference"],
            ],
            "Loaded image identity check",
        ).split(maxsplit=3)
        if (
            len(actual) != 4
            or actual[0] not in (image["image_id"], image["config_digest"])
            or actual[1:3] != ["arm64", "linux"]
        ):
            raise InstallError("Loaded image ID or platform differs from the verified inventory")
        try:
            diff_ids = json.loads(actual[3])
        except ValueError:
            raise InstallError("Cannot read the loaded image rootfs identity") from None
        if diff_ids != image["rootfs_diff_ids"]:
            raise InstallError("Loaded rootfs layers differ from the verified inventory")
        loaded_ids[image["reference"]] = actual[0]
    return loaded_ids


def _new_state(source, loaded_ids):
    # Only portable deployment fields cross into the new host. Events, fault state,
    # healing budgets and any extra manifest metadata must not be imported.
    return {
        "active": source["active"],
        "previous": source.get("previous"),
        "slots": {
            slot: {
                "image": copy.deepcopy(metadata["image"]),
                "version": copy.deepcopy(metadata["version"]),
                "image_id": loaded_ids[metadata["image"]],
            }
            for slot, metadata in source["slots"].items()
        },
        "release_id": uuid.uuid4().hex,
        "phase": "idle",
        "persistent": True,
        "simulation": False,
        "last_outcome": "offline_baseline_imported",
    }


def install():
    _check_host()
    manifest = _manifest()
    _check_archive(manifest["images_sha256"])
    _check_docker()
    gateway = _gateway()
    # Serialize first-time state creation with all other lifecycle operations. Release
    # this lock before systemd starts ops.stack, which acquires the same lock itself.
    try:
        with common.operation_lock(blocking=False):
            _require_fresh()
            _run(
                ["docker", "load", "-i", str(ROOT / "images.tar.gz")],
                "Offline image import",
                timeout=900,
            )
            loaded_ids = _verify_images(manifest["images"])
            _require_fresh()
            init_env.initialize(ROOT / ".env.example", ROOT / ".env")
            with (ROOT / ".env").open("a") as stream:
                stream.write("RUNTIME_BIND_HOST=0.0.0.0\nMETRICS_HOST_GATEWAY=" + gateway + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            state = _new_state(manifest["state"], loaded_ids)
            deploy.save_images(state["slots"])
            common.write_state(state)
    except BlockingIOError:
        raise InstallError(
            "Another lab operation is in progress; installation was not started"
        ) from None
    try:
        _run(
            ["bash", "scripts/install_systemd.sh"], "Systemd installation and startup", timeout=600
        )
    except InstallError:
        raise InstallError(
            "Systemd startup failed. Generated .env and release state were retained. "
            "Inspect journalctl -u sre-lab -u sre-lab-healer, then recover with systemctl; "
            "do not rerun the first-time installer"
        ) from None
    return {
        "active": state["active"],
        "previous": state["previous"],
        "version": state["slots"][state["active"]]["version"],
        "release_id": state["release_id"],
        "images_verified": len(manifest["images"]),
        "service": "sre-lab",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        result = install()
    except (InstallError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, InstallError) else type(exc).__name__
        parser.exit(1, "Offline installation failed: " + message + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
