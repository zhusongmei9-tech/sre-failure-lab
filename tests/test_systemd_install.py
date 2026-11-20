"""Exercise the installer decisions with isolated unit files and a fake systemd."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install_systemd.sh"
PARENT = "sre-lab.service"
HEALER = "sre-lab-healer.service"
HARNESS = r"""
source "$1"
systemd-analyze() { [[ "$1" == verify && -f "$2" && -f "$3" ]]; }
stat() { echo 644:0:0; }
install() { cp "$7" "$8"; }
systemctl() {
    local action="$1" unit="${!#}"
    case "$action" in
        is-active) [[ -f "runtime/$unit.active" ]]; return ;;
        is-enabled)
            if [[ -f "runtime/$unit.enabled" ]]; then echo enabled
            elif [[ "$unit" == sre-lab-healer.service ]]; then echo static
            else echo disabled; return 1; fi
            return ;;
    esac
    echo "$*" >> actions
    case "$action" in
        start|restart)
            if [[ -e "fail.$action.$unit" ]]; then return 1; fi
            touch "runtime/$unit.active"
            if [[ "$unit" == sre-lab.service ]]; then rm -f degraded; fi ;;
        stop) rm -f "runtime/$unit.active" ;;
        enable) touch "runtime/$unit.enabled" ;;
        disable) rm -f "runtime/$unit.enabled" ;;
        daemon-reload) ;;
        *) return 1 ;;
    esac
}
configure_units "$PWD/units" "$PWD/runtime" "$2"
"""


@pytest.fixture
def lab(tmp_path):
    for name in ("ops", "units", "runtime"):
        (tmp_path / name).mkdir()
    for unit in (PARENT, HEALER):
        (tmp_path / "ops" / unit).write_text(unit)
    (tmp_path / "ops/stack.py").write_text(
        "from pathlib import Path\ndef status(): return {'all_running': not Path('degraded').exists()}\n"
    )
    (tmp_path / "actions").touch()
    return tmp_path


def run(lab, preview=False, check=True):
    return subprocess.run(
        ["bash", "-c", HARNESS, "test-installer", str(SCRIPT), "true" if preview else "false"],
        cwd=lab,
        capture_output=True,
        text=True,
        check=check,
    )


def actions(lab):
    log = lab / "actions"
    lines = log.read_text().splitlines()
    log.write_text("")
    return lines


def test_preview_and_second_apply_do_not_modify_units_or_services(lab):
    assert "SRE_LAB_SYSTEMD_CHANGED=1" in run(lab, preview=True).stdout
    assert not list((lab / "units").iterdir()) and not list((lab / "runtime").iterdir())
    assert actions(lab) == []

    assert "SRE_LAB_SYSTEMD_CHANGED=1" in run(lab).stdout
    assert actions(lab) == [
        "daemon-reload",
        f"enable {PARENT}",
        f"start {PARENT}",
        f"start {HEALER}",
    ]
    timestamps = {p: p.stat().st_mtime_ns for p in (lab / "units").iterdir()}
    assert "SRE_LAB_SYSTEMD_CHANGED=0" in run(lab, preview=True).stdout
    assert "SRE_LAB_SYSTEMD_CHANGED=0" in run(lab).stdout
    assert all(p.stat().st_mtime_ns == timestamp for p, timestamp in timestamps.items())
    assert actions(lab) == []


def test_legacy_boot_link_migrates_once(lab):
    (lab / "runtime" / f"{HEALER}.enabled").touch()
    (lab / "runtime" / f"{HEALER}.active").touch()
    run(lab)
    assert actions(lab) == [
        f"stop {HEALER}",
        f"disable {HEALER}",
        "daemon-reload",
        f"enable {PARENT}",
        f"start {PARENT}",
        f"start {HEALER}",
    ]
    assert "SRE_LAB_SYSTEMD_CHANGED=0" in run(lab).stdout
    assert actions(lab) == []


def test_healer_change_and_interrupted_restart_only_affect_healer(lab):
    run(lab)
    actions(lab)
    (lab / "ops" / HEALER).write_text("changed")
    fault = lab / f"fail.restart.{HEALER}"
    fault.touch()
    assert run(lab, check=False).returncode != 0
    assert actions(lab) == ["daemon-reload", f"restart {HEALER}"]
    fault.unlink()
    assert "SRE_LAB_SYSTEMD_CHANGED=1" in run(lab).stdout
    assert actions(lab) == ["daemon-reload", f"restart {HEALER}"]
    assert "SRE_LAB_SYSTEMD_CHANGED=0" in run(lab).stdout
    assert actions(lab) == []


def test_active_parent_with_missing_containers_still_repairs_stack(lab):
    run(lab)
    actions(lab)
    (lab / "degraded").touch()
    assert "SRE_LAB_SYSTEMD_CHANGED=1" in run(lab).stdout
    assert actions(lab) == [f"restart {PARENT}", f"start {HEALER}"]
