#!/usr/bin/env bash
# Install the lifecycle units for the initialized Linux runtime stack.
set -euo pipefail

preflight() {
    if [[ "$(uname -s)" != Linux || "$EUID" -ne 0 ]]; then
        echo "请在 Linux 虚拟机中以 root 身份执行此脚本。" >&2
        exit 1
    fi

    cd /opt/sre-failure-lab
    for executable in docker python3 systemctl systemd-analyze install cmp stat flock; do
        command -v "$executable" >/dev/null
    done
    # Lock the existing directory inode, so a preview does not create any files.
    exec 9<.
    flock -w 30 9
    export PYTHONDONTWRITEBYTECODE=1
    docker compose version >/dev/null
    python3 - <<'PY'
import json
import sys
from pathlib import Path

if sys.version_info < (3, 9):
    raise SystemExit("宿主机需要 Python 3.9 或更新版本。")
for name in (".env", "runtime/state.json", "ops/stack.py", "ops/sre-lab.service",
             "ops/sre-lab-healer.service", "compose.runtime.yaml"):
    if not Path(name).is_file():
        raise SystemExit("缺少 " + name + "；先完成源码安装和真实栈初始化。")
state = json.loads(Path("runtime/state.json").read_text())
if state.get("phase") != "idle" or state.get("active") not in ("blue", "green"):
    raise SystemExit("发布状态未稳定；请先检查 ops.deploy status 并完成恢复。")
PY
}

unit_matches() {
    local unit="$1" dest="$unit_dir/$1"
    [[ -f "$dest" && ! -L "$dest" ]] && cmp -s "ops/$unit" "$dest" &&
        [[ "$(stat -c '%a:%u:%g' "$dest")" == 644:0:0 ]]
}

# Pending flags survive an interrupted install/reload/restart until systemd has
# applied the files. They expire on boot, when systemd loads the installed units.
needs_apply() {
    ! unit_matches "$1" || [[ -f "$pending_dir/$1.install-pending" ]]
}

configure_units() {
    local unit_dir="$1" pending_dir="$2" check_only="$3"
    local parent=sre-lab.service healer=sre-lab-healer.service
    local parent_enabled healer_enabled migrate_healer parent_action healer_action changed
    local unit running flag
    systemd-analyze verify "$PWD/ops/$parent" "$PWD/ops/$healer"

    # is-enabled also exits successfully for static units; compare the actual state.
    parent_enabled="$(systemctl is-enabled "$parent" 2>/dev/null || true)"
    healer_enabled="$(systemctl is-enabled "$healer" 2>/dev/null || true)"
    migrate_healer=false
    case "$healer_enabled" in enabled|enabled-runtime) migrate_healer=true ;; esac

    parent_action=none
    if ! systemctl is-active --quiet "$parent"; then
        parent_action=start
    elif needs_apply "$parent"; then
        parent_action=restart
    else
        # active (exited) alone cannot tell whether the containers are still running.
        running="$(python3 -c 'from ops.stack import status; print(int(status()["all_running"]))')"
        if [[ "$running" == 0 ]]; then parent_action=restart; fi
    fi
    healer_action=none
    if ! systemctl is-active --quiet "$healer"; then
        healer_action=start
    elif needs_apply "$healer"; then
        healer_action=restart
    fi
    changed=0
    if [[ "$parent_action" != none || "$healer_action" != none || "$parent_enabled" != enabled ]] ||
            "$migrate_healer"; then
        changed=1
    fi
    if "$check_only"; then
        echo "parent=$parent_action healer=$healer_action parent_boot=$parent_enabled migrate_healer=$migrate_healer"
        echo "SRE_LAB_SYSTEMD_CHANGED=$changed"
        return 0
    fi

    if [[ "$parent_action" == start && "$healer_action" == restart ]]; then
        # A previously independent healer may already be active while the parent is down.
        systemctl stop "$healer"
    fi
    for unit in "$parent" "$healer"; do
        if unit_matches "$unit"; then continue; fi
        touch "$pending_dir/$unit.install-pending"
        install -o root -g root -m 644 "ops/$unit" "$unit_dir/$unit"
    done
    if "$migrate_healer"; then
        # Clear only old independent boot links. A static child stays under its parent.
        if [[ "$healer_enabled" == enabled-runtime ]]; then
            systemctl disable --runtime "$healer"
        else
            systemctl disable "$healer"
        fi
    fi
    if needs_apply "$parent" || needs_apply "$healer"; then systemctl daemon-reload; fi
    if [[ "$parent_enabled" != enabled ]]; then systemctl enable "$parent"; fi
    if [[ "$parent_action" != none ]]; then
        systemctl "$parent_action" "$parent"
        # Wait for the child start job too; Wants= alone does not make its failure fatal.
        systemctl start "$healer"
    elif [[ "$healer_action" != none ]]; then
        systemctl "$healer_action" "$healer"
    fi
    systemctl is-active --quiet "$parent"
    systemctl is-active --quiet "$healer"
    python3 - <<'PY'
import json
from ops.stack import status

snapshot = status()
print(json.dumps(snapshot, indent=2))
if not snapshot["all_running"]:
    raise SystemExit("部分容器未运行；请查看 journalctl -u sre-lab。")
PY
    for unit in "$parent" "$healer"; do
        flag="$pending_dir/$unit.install-pending"
        if [[ -f "$flag" ]]; then rm -f -- "$flag"; fi
    done
    echo "SRE_LAB_SYSTEMD_CHANGED=$changed"
    echo "整套服务已启动；以后使用 systemctl start/stop/restart/status sre-lab 管理。"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    check_only=false
    if [[ "$#" == 1 && "$1" == --check ]]; then
        check_only=true
    elif [[ "$#" != 0 ]]; then
        echo 'Usage: install_systemd.sh [--check]' >&2
        exit 2
    fi
    preflight
    configure_units /etc/systemd/system /run "$check_only"
fi
