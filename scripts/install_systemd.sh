#!/usr/bin/env bash
# Install the lifecycle units for the initialized Linux runtime stack.
set -euo pipefail

if [[ "$(uname -s)" != Linux || "$EUID" -ne 0 ]]; then
    echo "请在 Linux 虚拟机中以 root 身份执行此脚本。" >&2
    exit 1
fi

cd /opt/sre-failure-lab
for executable in docker python3 systemctl systemd-analyze install; do
    command -v "$executable" >/dev/null
done
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

# Old installs enabled the healer independently; remove that boot entry first.
if systemctl cat sre-lab-healer.service >/dev/null 2>&1; then
    systemctl stop sre-lab-healer.service
    systemctl disable sre-lab-healer.service
fi
install -m 644 ops/sre-lab.service /etc/systemd/system/sre-lab.service
install -m 644 ops/sre-lab-healer.service /etc/systemd/system/sre-lab-healer.service
systemd-analyze verify /etc/systemd/system/sre-lab.service \
    /etc/systemd/system/sre-lab-healer.service
systemctl daemon-reload
systemctl enable sre-lab.service
systemctl restart sre-lab.service
# Wait for the child start job too; Wants= alone does not make its failure fatal.
systemctl start sre-lab-healer.service
systemctl is-active --quiet sre-lab.service
systemctl is-active --quiet sre-lab-healer.service
python3 - <<'PY'
import json
from ops.stack import status

snapshot = status()
print(json.dumps(snapshot, indent=2))
if not snapshot["all_running"]:
    raise SystemExit("部分容器未运行；请查看 journalctl -u sre-lab。")
PY
echo "整套服务已启动；以后使用 systemctl start/stop/restart/status sre-lab 管理。"
