import copy
import hashlib
import json
import subprocess
from unittest.mock import Mock

import pytest

from ops import common, deploy
from scripts import install_offline as installer


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    for module in (installer, common, deploy):
        monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(installer, "INSTALL_ROOT", tmp_path)
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/" + name)
    (tmp_path / ".env.example").write_text("LAB_TOKEN=\nGRAFANA_ADMIN_PASSWORD=\nAPI_CPUS=1.0\n")
    for name in (
        "compose.runtime.yaml",
        "scripts/install_systemd.sh",
        "ops/sre-lab.service",
        "ops/sre-lab-healer.service",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test fixture\n")
    payload = b"mock compressed Docker image archive"
    (tmp_path / "images.tar.gz").write_bytes(payload)
    refs = sorted(installer.INFRA_IMAGES) + ["sre-lab-api:0.3.0", "sre-lab-api:0.3.1"]
    images = [
        {
            "reference": reference,
            "image_id": "sha256:" + str(i + 1) * 64,
            "config_digest": "sha256:" + format(i + 9, "064x"),
            "rootfs_diff_ids": ["sha256:" + "a" * 64, "sha256:" + "b" * 64],
            "architecture": "arm64",
            "os": "linux",
        }
        for i, reference in enumerate(refs)
    ]
    slots = {
        slot: {"image": image["reference"], "image_id": image["image_id"], "version": version}
        for slot, version, image in (("blue", "0.3.0", images[-2]), ("green", "0.3.1", images[-1]))
    }
    manifest = {
        "schema_version": 1,
        "platform": "linux/arm64",
        "images": images,
        "images_sha256": hashlib.sha256(payload).hexdigest(),
        "state": {
            "phase": "idle",
            "active": "green",
            "previous": "blue",
            "slots": slots,
            "release_id": "old-host-release",
            "extra_field": "must-not-be-imported",
        },
    }
    (tmp_path / "bundle-manifest.json").write_text(json.dumps(manifest))
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        assert kwargs["cwd"] == tmp_path and kwargs["capture_output"] and kwargs["check"]
        if args[:2] == ["docker", "info"]:
            output = "linux aarch64"
        elif args[:3] == ["docker", "network", "inspect"]:
            output = "fd00::1\n172.17.0.1\n"
        elif args[:3] == ["docker", "image", "inspect"]:
            image = next(image for image in images if image["reference"] == args[-1])
            output = image["image_id"] + " arm64 linux " + json.dumps(image["rootfs_diff_ids"])
        else:
            output = ""
        return subprocess.CompletedProcess(args, 0, stdout=output)

    monkeypatch.setattr(installer.subprocess, "run", run)
    return tmp_path, manifest, commands


def update_manifest(bundle, change):
    root, manifest, _ = bundle
    change(manifest)
    (root / "bundle-manifest.json").write_text(json.dumps(manifest))


def assert_uninitialized(root):
    assert not (root / ".env").exists()
    assert not (root / "runtime/state.json").exists()


def test_offline_install_verifies_all_images_and_generates_new_identity(bundle):
    root, manifest, commands = bundle
    original = copy.deepcopy(manifest)
    result = installer.install()
    state = common.read_state()
    assert state["active"] == "green" and state["previous"] == "blue"
    assert state["slots"] == manifest["state"]["slots"]
    assert state["release_id"] != "old-host-release" and len(state["release_id"]) == 32
    assert state["last_outcome"] == "offline_baseline_imported"
    assert state["phase"] == "idle" and state["persistent"] and not state["simulation"]
    assert "extra_field" not in state and manifest == original
    env = (root / ".env").read_text()
    values = dict(line.split("=", 1) for line in env.splitlines())
    assert len(values["LAB_TOKEN"]) >= 32 and len(values["GRAFANA_ADMIN_PASSWORD"]) >= 32
    assert values["LAB_TOKEN"] != values["GRAFANA_ADMIN_PASSWORD"]
    assert values["RUNTIME_BIND_HOST"] == "0.0.0.0"
    assert values["METRICS_HOST_GATEWAY"] == "172.17.0.1"
    assert (root / ".env").stat().st_mode & 0o777 == 0o600
    assert result["images_verified"] == 8
    assert ["docker", "load", "-i", str(root / "images.tar.gz")] in commands
    assert commands[-1] == ["bash", "scripts/install_systemd.sh"]
    assert len([args for args in commands if args[:3] == ["docker", "image", "inspect"]]) == 8
    assert all(
        "pull" not in args and "build" not in args and "push" not in args for args in commands
    )
    assert (
        json.loads((root / "runtime/prometheus/active.json").read_text())[0]["labels"]["slot"]
        == "green"
    )
    assert "sha256:" in (root / "runtime/images.env").read_text()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("platform", "linux/amd64"),
        ("images_sha256", "bad"),
    ],
)
def test_manifest_version_platform_and_digest_rejected_before_mutation(bundle, field, value):
    root, _, commands = bundle
    update_manifest(bundle, lambda manifest: manifest.__setitem__(field, value))
    with pytest.raises(installer.InstallError):
        installer.install()
    assert_uninitialized(root)
    assert commands == []


@pytest.mark.parametrize(
    "change",
    [
        lambda manifest: manifest["state"].update(phase="observing"),
        lambda manifest: manifest["state"]["slots"]["green"].update(image_id="nginx:latest"),
        lambda manifest: manifest["state"]["slots"]["green"].update(version="0.3.9"),
        lambda manifest: manifest["images"].pop(),
        lambda manifest: manifest["images"][0].update(architecture="amd64"),
        lambda manifest: manifest["images"].append(copy.deepcopy(manifest["images"][0])),
        lambda manifest: manifest["images"][-1].update(image_id="sha256:" + "f" * 64),
    ],
)
def test_invalid_state_or_inventory_is_rejected(bundle, change):
    root, _, commands = bundle
    update_manifest(bundle, change)
    with pytest.raises(installer.InstallError):
        installer.install()
    assert_uninitialized(root)
    assert commands == []


def test_archive_checksum_mismatch_never_loads_or_writes_identity(bundle):
    root, _, commands = bundle
    (root / "images.tar.gz").write_bytes(b"tampered archive")
    with pytest.raises(installer.InstallError, match="checksum mismatch"):
        installer.install()
    assert_uninitialized(root)
    assert commands == []


@pytest.mark.parametrize("name", [".env", "runtime/state.json"])
def test_existing_identity_refused_and_preserved(bundle, name):
    root, _, commands = bundle
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("existing identity")
    with pytest.raises(installer.InstallError, match="systemctl"):
        installer.install()
    assert target.read_text() == "existing identity"
    assert not any(args[:2] == ["docker", "load"] for args in commands)


@pytest.mark.parametrize("command", [["docker", "ps"], ["docker", "volume", "ls"]])
def test_existing_containers_or_volumes_rejected(bundle, monkeypatch, command):
    root, _, commands = bundle
    original = installer._run

    def run(args, *args2, **kwargs):
        return (
            "existing-resource"
            if args[: len(command)] == command
            else original(args, *args2, **kwargs)
        )

    monkeypatch.setattr(installer, "_run", run)
    with pytest.raises(installer.InstallError, match="Existing sre-lab"):
        installer.install()
    assert_uninitialized(root)
    assert not any(args[:2] == ["docker", "load"] for args in commands)


@pytest.mark.parametrize("failure", ["load", "image"])
def test_load_or_loaded_identity_failure_does_not_create_env_or_state(bundle, monkeypatch, failure):
    root, _, _ = bundle
    original = installer._run

    def run(args, *args2, **kwargs):
        if args[:2] == ["docker", failure]:
            if failure == "image":
                return "sha256:" + "a" * 64 + " arm64 linux"
            raise installer.InstallError("Offline image import failed")
        return original(args, *args2, **kwargs)

    monkeypatch.setattr(installer, "_run", run)
    with pytest.raises(installer.InstallError):
        installer.install()
    assert_uninitialized(root)


def test_startup_failure_keeps_generated_state_and_credentials(bundle, monkeypatch):
    root, _, _ = bundle
    original = installer._run

    def run(args, *args2, **kwargs):
        if args[0] == "bash":
            raise installer.InstallError("startup failed")
        return original(args, *args2, **kwargs)

    monkeypatch.setattr(installer, "_run", run)
    with pytest.raises(installer.InstallError, match="retained"):
        installer.install()
    assert (root / ".env").exists() and common.read_state()["phase"] == "idle"
    with pytest.raises(installer.InstallError, match="Existing installation"):
        installer.install()


@pytest.mark.parametrize(
    "setting,value",
    [
        ("root", "/wrong"),
        ("system", "Darwin"),
        ("machine", "x86_64"),
        ("euid", 1000),
        ("python", (3, 8)),
    ],
)
def test_host_path_privilege_and_python_guards(bundle, monkeypatch, setting, value):
    root, _, commands = bundle
    if setting == "root":
        monkeypatch.setattr(installer, "INSTALL_ROOT", root / value)
    elif setting in ("system", "machine"):
        monkeypatch.setattr(installer.platform, setting, lambda: value)
    elif setting == "euid":
        monkeypatch.setattr(installer.os, "geteuid", lambda: value)
    else:
        monkeypatch.setattr(installer.sys, "version_info", value)
    with pytest.raises(installer.InstallError):
        installer.install()
    assert_uninitialized(root)
    assert not commands


@pytest.mark.parametrize("failure", ["wrong-daemon", "no-gateway"])
def test_wrong_daemon_or_gateway_refused_before_load(bundle, monkeypatch, failure):
    root, _, commands = bundle
    original = installer._run

    def run(args, *args2, **kwargs):
        if failure == "wrong-daemon" and args[:2] == ["docker", "info"]:
            return "linux x86_64"
        if failure == "no-gateway" and args[:3] == ["docker", "network", "inspect"]:
            return "fd00::1 0.0.0.0"
        return original(args, *args2, **kwargs)

    monkeypatch.setattr(installer, "_run", run)
    with pytest.raises(installer.InstallError):
        installer.install()
    assert_uninitialized(root)
    assert not any(args[:2] == ["docker", "load"] for args in commands)


def test_subprocess_failure_output_never_leaks_secrets(bundle, monkeypatch, capsys):
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        Mock(
            side_effect=subprocess.CalledProcessError(
                1, "docker", output="LAB_TOKEN=private-token", stderr="PASSWORD=secret"
            )
        ),
    )
    monkeypatch.setattr(installer.sys, "argv", ["scripts/install_offline.py"])
    with pytest.raises(SystemExit) as exc:
        installer.main()
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert "private-token" not in output.err and "secret" not in output.err and not output.out


def test_systemd_start_runs_after_shared_lock_is_released(bundle, monkeypatch):
    original = installer._run

    def run(args, *args2, **kwargs):
        if args[0] == "bash":
            with common.operation_lock(blocking=False):
                pass
        return original(args, *args2, **kwargs)

    monkeypatch.setattr(installer, "_run", run)
    installer.install()


def test_classic_store_config_id_preserves_artifact_and_binds_actual_id(bundle, monkeypatch):
    root, manifest, _ = bundle
    original = installer._run
    source = copy.deepcopy(manifest)
    inventory = {image["reference"]: image for image in manifest["images"]}

    def run(args, *args2, **kwargs):
        if args[:3] == ["docker", "image", "inspect"]:
            image = inventory[args[-1]]
            return image["config_digest"] + " arm64 linux " + json.dumps(image["rootfs_diff_ids"])
        return original(args, *args2, **kwargs)

    monkeypatch.setattr(installer, "_run", run)
    installer.install()
    assert manifest == source
    restored = common.read_state()
    for slot, metadata in restored["slots"].items():
        expected = inventory[metadata["image"]]["config_digest"]
        assert metadata["image_id"] == expected
        assert metadata["version"] == source["state"]["slots"][slot]["version"]
        assert expected in (root / "runtime/images.env").read_text()


@pytest.mark.parametrize(
    "different", [["sha256:" + "f" * 64], ["sha256:" + "b" * 64, "sha256:" + "a" * 64]]
)
def test_loaded_rootfs_content_or_order_mismatch_refused_before_identity(
    bundle, monkeypatch, different
):
    root, manifest, _ = bundle
    original = installer._run
    inventory = {image["reference"]: image for image in manifest["images"]}

    def run(args, *args2, **kwargs):
        if args[:3] == ["docker", "image", "inspect"]:
            return inventory[args[-1]]["config_digest"] + " arm64 linux " + json.dumps(different)
        return original(args, *args2, **kwargs)

    monkeypatch.setattr(installer, "_run", run)
    with pytest.raises(installer.InstallError, match="rootfs layers differ"):
        installer.install()
    assert_uninitialized(root)


@pytest.mark.parametrize(
    "field,value", [("config_digest", "bad"), ("rootfs_diff_ids", []), ("rootfs_diff_ids", ["bad"])]
)
def test_missing_image_identity_evidence_refused(bundle, field, value):
    root, _, commands = bundle
    update_manifest(bundle, lambda manifest: manifest["images"][0].update({field: value}))
    with pytest.raises(installer.InstallError):
        installer.install()
    assert_uninitialized(root)
    assert not commands


def test_systemd_fixed_python_path_is_checked(bundle):
    _, _, commands = bundle
    installer.install()
    assert any(
        args[0] == "/usr/bin/python3" and "sys.version_info" in args[-1] for args in commands
    )


def test_template_cannot_override_generated_credentials_with_second_assignment(bundle):
    root, _, commands = bundle
    with (root / ".env.example").open("a") as stream:
        stream.write("LAB_TOKEN=old-host-token\n")
    with pytest.raises(installer.InstallError, match="exactly one blank"):
        installer.install()
    assert_uninitialized(root)
    assert not commands
