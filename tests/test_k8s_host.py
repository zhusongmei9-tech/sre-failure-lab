import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import check_k8s_host as host


@pytest.fixture
def linux_host(monkeypatch):
    files = {"/run/systemd/system": "directory", "/proc/sys/net/ipv4/ip_forward": "1"}

    class FakePath:
        def __init__(self, value):
            self.value = value

        def __str__(self):
            return self.value

        def exists(self):
            return self.value in files

        def is_dir(self):
            return files.get(self.value) == "directory"

        def read_text(self):
            if self.value not in files:
                raise FileNotFoundError()
            return files[self.value]

    monkeypatch.setattr(host, "Path", FakePath)
    monkeypatch.setattr(host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(host.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(host.os, "geteuid", lambda: 0)
    monkeypatch.setattr(host.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(host.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(host.shutil, "disk_usage", lambda path: SimpleNamespace(free=8 * host.GIB))
    monkeypatch.setattr(
        host,
        "memory_info",
        lambda: {"MemTotal": 5 * host.GIB, "MemAvailable": 3 * host.GIB, "SwapTotal": 2 * host.GIB},
    )
    monkeypatch.setattr(host, "occupied_ports", lambda: {"tcp": [], "udp": []})
    monkeypatch.setattr(host, "run_readonly", Mock(return_value="yes"))
    return files


def test_plan_makes_no_environment_claim_or_command_call(monkeypatch, capsys):
    monkeypatch.setattr(host.platform, "system", Mock(side_effect=AssertionError("must not probe")))
    runner = Mock(side_effect=AssertionError("must not run commands"))
    monkeypatch.setattr(host, "run_readonly", runner)
    monkeypatch.setattr(host.sys, "argv", ["check_k8s_host.py", "--plan"])
    assert host.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "not_tested" and not report["environment_tested"]
    assert report["checks"] == []
    runner.assert_not_called()


def test_fresh_host_reports_install_readiness_not_cluster_acceptance(linux_host):
    report = host.check_host()
    assert report["status"] == "ready_for_install"
    assert all(check["status"] == "passed" for check in report["checks"])
    assert "reboot recovery remain untested" in report["message"]
    assert (
        next(check for check in report["checks"] if check["name"] == "memory")["observed"][
            "SwapTotal"
        ]
        == 2 * host.GIB
    )
    host.run_readonly.assert_called_once_with(
        ["timedatectl", "show", "--property=NTPSynchronized", "--value"]
    )


@pytest.mark.parametrize(
    "problem",
    [
        "architecture",
        "cpu",
        "memory",
        "disk",
        "clock",
        "tools",
        "ports",
        "existing",
        "forwarding",
        "root",
    ],
)
def test_unready_host_is_not_accepted(linux_host, monkeypatch, problem):
    if problem == "architecture":
        monkeypatch.setattr(host.platform, "machine", lambda: "riscv64")
    elif problem == "cpu":
        monkeypatch.setattr(host.os, "cpu_count", lambda: 1)
    elif problem == "memory":
        monkeypatch.setattr(host, "memory_info", lambda: {"MemAvailable": host.GIB})
    elif problem == "disk":
        monkeypatch.setattr(host.shutil, "disk_usage", lambda path: SimpleNamespace(free=host.GIB))
    elif problem == "clock":
        monkeypatch.setattr(host, "run_readonly", Mock(return_value="no"))
    elif problem == "tools":
        monkeypatch.setattr(
            host.shutil, "which", lambda name: None if name == "curl" else "/usr/bin/" + name
        )
    elif problem == "ports":
        monkeypatch.setattr(host, "occupied_ports", lambda: {"tcp": [6443], "udp": []})
    elif problem == "existing":
        linux_host["/etc/rancher/k3s/config.yaml"] = "do not read or overwrite"
    elif problem == "forwarding":
        linux_host["/proc/sys/net/ipv4/ip_forward"] = "0"
    else:
        monkeypatch.setattr(host.os, "geteuid", lambda: 1000)
    report = host.check_host()
    assert report["status"] == "failed"
    assert any(check["status"] == "failed" for check in report["checks"])


def test_unknown_time_state_fails_closed(linux_host, monkeypatch):
    monkeypatch.setattr(
        host, "run_readonly", Mock(side_effect=subprocess.TimeoutExpired("timedatectl", 10))
    )
    report = host.check_host()
    check = next(check for check in report["checks"] if check["name"] == "clock")
    assert check["observed"] == "unknown" and report["status"] == "failed"


def test_nonlinux_does_not_attempt_linux_file_or_command_reads(monkeypatch):
    monkeypatch.setattr(host.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(host, "Path", Mock(side_effect=AssertionError("no Linux reads")))
    monkeypatch.setattr(host, "run_readonly", Mock(side_effect=AssertionError("no commands")))
    assert host.check_host()["status"] == "failed"


def test_socket_table_scan_detects_listeners_and_udp_without_binding(tmp_path, monkeypatch):
    for name in ("tcp", "tcp6", "udp", "udp6"):
        (tmp_path / name).write_text("header\n")
    (tmp_path / "tcp").write_text(
        "header\n0: 00000000:192B 00000000:0000 0A other\n1: 00000000:4A92 00000000:0000 01 other\n"
    )
    (tmp_path / "tcp6").write_text("header\n0: 00000000:4A9C 00000000:0000 0A other\n")
    (tmp_path / "udp").write_text("header\n0: 00000000:2118 00000000:0000 07 other\n")
    monkeypatch.setattr(host, "Path", lambda value: tmp_path / value.rsplit("/", 1)[-1])
    assert host.occupied_ports() == {"tcp": [6443, 19100], "udp": [8472]}


def test_command_runner_is_bounded_and_does_not_use_shell(monkeypatch):
    runner = Mock(return_value=SimpleNamespace(stdout="yes\n"))
    monkeypatch.setattr(host.subprocess, "run", runner)
    assert host.run_readonly(["timedatectl", "show"]) == "yes"
    assert runner.call_args.kwargs == {
        "check": True,
        "capture_output": True,
        "text": True,
        "timeout": 10,
    }
