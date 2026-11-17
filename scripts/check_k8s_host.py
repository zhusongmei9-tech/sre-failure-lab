"""Read-only preflight for a fresh single-node K3s host; --plan needs no cluster."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VERSIONS = {"k3s": "v1.37.2+k3s1", "helm": "v4.3.2", "chart": "93.3.1"}
REQUIRED_TOOLS = (
    "curl",
    "sha256sum",
    "python3",
    "systemctl",
    "install",
    "timeout",
    "flock",
    "tar",
    "ip",
)
PORTS = {"tcp": (6443, 10250, 19100, 19090), "udp": (8472,)}
GIB = 1024**3


def utc_now():
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017


def run_readonly(command):
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
    return result.stdout.strip()


def memory_info():
    rows = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return {
        key: int(rows[key].split()[0]) * 1024 for key in ("MemTotal", "MemAvailable", "SwapTotal")
    }


def occupied_ports():
    result = {"tcp": set(), "udp": set()}
    for protocol, ports in result.items():
        for family in ("", "6"):
            path = Path("/proc/net/" + protocol + family)
            if family and not path.exists():
                continue
            for row in path.read_text().splitlines()[1:]:
                fields = row.split()
                if len(fields) >= 4 and (protocol == "udp" or fields[3] == "0A"):
                    ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return {
        protocol: sorted(values.intersection(PORTS[protocol]))
        for protocol, values in result.items()
    }


def plan():
    return {
        "schema_version": 1,
        "kind": "k8s_host_preflight",
        "timestamp": utc_now(),
        "status": "not_tested",
        "mode": "plan",
        "environment_tested": False,
        "versions": VERSIONS,
        "requirements": {
            "system": "Linux with systemd; root for installation",
            "architectures": ["arm64", "amd64"],
            "cpu_count_min": 2,
            "available_memory_bytes_min": 2 * GIB,
            "free_disk_bytes_min": 6 * GIB,
            "disk_paths": ["/var/lib", "/usr/local"],
            "python_min": "3.9",
            "required_tools": list(REQUIRED_TOOLS),
            "ports": PORTS,
            "clock": "OS must report NTP synchronized; this script does not measure external offset",
            "network": "IPv4 forwarding enabled; review firewall and registry access separately",
        },
        "checks": [],
        "message": "Plan only. No host, cluster, installation or reboot acceptance was performed.",
    }


def check_host():
    report = plan()
    report.update(mode="host", environment_tested=True)
    report.pop("message")
    checks = report["checks"]

    def add(name, passed, observed, requirement):
        checks.append(
            {
                "name": name,
                "status": "passed" if passed else "failed",
                "observed": observed,
                "requirement": requirement,
            }
        )

    system = platform.system()
    architecture = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(
        platform.machine()
    )
    add("linux", system == "Linux", system, "Linux host")
    add(
        "architecture",
        architecture in ("arm64", "amd64"),
        architecture or platform.machine(),
        "arm64 or amd64",
    )
    if system != "Linux":
        report.update(status="failed", message="Linux-only checks were not run on this host.")
        return report
    add(
        "root",
        os.geteuid() == 0,
        os.geteuid(),
        "Installer requires root; checker makes no system changes",
    )
    add(
        "systemd",
        Path("/run/systemd/system").is_dir(),
        Path("/run/systemd/system").is_dir(),
        "systemd running",
    )
    add("python", sys.version_info >= (3, 9), list(sys.version_info[:3]), "Python >= 3.9")
    missing = [name for name in REQUIRED_TOOLS if shutil.which(name) is None]
    add(
        "tools",
        not missing,
        {"missing": missing},
        "Required host tools must be installed separately",
    )
    add("cpu", (os.cpu_count() or 0) >= 2, os.cpu_count(), "at least 2 CPUs")
    try:
        memory = memory_info()
        add(
            "memory",
            memory["MemAvailable"] >= 2 * GIB,
            memory,
            "at least 2 GiB currently available; swap is retained",
        )
    except (OSError, ValueError, KeyError):
        add("memory", False, "unavailable", "Readable /proc/meminfo")
    for directory in ("/var/lib", "/usr/local"):
        try:
            free = shutil.disk_usage(directory).free
            add(
                "disk:" + directory,
                free >= 6 * GIB,
                {"free_bytes": free},
                "at least 6 GiB free; no automatic cleanup",
            )
        except OSError:
            add("disk:" + directory, False, "unavailable", "Readable filesystem capacity")
    try:
        synchronized = run_readonly(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"]
        )
        add(
            "clock",
            synchronized == "yes",
            {"os_ntp_synchronized": synchronized},
            "OS must report yes; external clock offset is not measured",
        )
    except (OSError, subprocess.SubprocessError):
        add(
            "clock",
            False,
            "unknown",
            "Clock synchronization must be verifiable; no time adjustment is performed",
        )
    try:
        add(
            "ipv4_forwarding",
            Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() == "1",
            Path("/proc/sys/net/ipv4/ip_forward").read_text().strip(),
            "IPv4 forwarding enabled",
        )
    except OSError:
        add("ipv4_forwarding", False, "unknown", "IPv4 forwarding enabled")
    try:
        occupied = occupied_ports()
        add(
            "ports",
            not any(occupied.values()),
            occupied,
            "Fresh-install ports unused; all local bind addresses are checked conservatively",
        )
    except (OSError, ValueError):
        add("ports", False, "unknown", "Readable /proc/net socket tables")
    existing = [
        str(path)
        for path in (Path("/etc/rancher/k3s/config.yaml"), Path("/var/lib/rancher/k3s/server"))
        if path.exists()
    ]
    add(
        "fresh_k3s_state",
        not existing,
        existing,
        "Existing K3s state requires its own upgrade/recovery procedure",
    )
    report["status"] = (
        "ready_for_install" if all(item["status"] == "passed" for item in checks) else "failed"
    )
    report["message"] = (
        "Host preflight only; registry reachability, cluster operation and reboot recovery remain untested."
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan", action="store_true", help="Describe requirements without probing this host"
    )
    args = parser.parse_args()
    report = plan() if args.plan else check_host()
    print(json.dumps(report, indent=2))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
