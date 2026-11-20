# 第四章　在 Compose 上演练真实自愈、蓝绿发布与回滚

完成第一章后，在虚机的 18000 端口可以查询 Compose 商品服务，活动版本为 `0.3.0`，父、子 systemd 服务都在运行。以下命令在专用实验虚机的 root Bash 执行。真实验收会切换 Nginx 流量，并短暂停止该项目的 Redis 和活动 API。在演练窗口内串行执行，避免与其他人同时发布或重启。Kubernetes 的副本、镜像和流量入口有独立的操作流程。[第一章](01-linux-compose.md) · [Kubernetes 商品演练](03-product-rollout-hpa.md)

## 1. 识别入口和运行状态

```bash
cd /opt/sre-failure-lab
systemctl is-active sre-lab sre-lab-healer
python3 -m ops.stack status
python3 -m ops.deploy status
python3 -m ops.heal status
curl --noproxy '*' -fsSi http://127.0.0.1:18000/version
curl --noproxy '*' -fsS http://127.0.0.1:18000/health/ready
```

检查 `phase=idle`、`all_running=true`、Redis 就绪，自愈状态中 `services.api` 和 `services.redis` 均未锁定。入口标头 `X-Deployment-Slot` 是活动槽，响应正文的 `version` 是镜像内应用版本。如果现有部署为其他版本或正在恢复，只做状态检查，保留当前发布，待专用环境达到本章起点再演练。

| 实际配置 | 怎么读 |
| --- | --- |
| 活动入口、同机诊断端口 | `18000` 是 Nginx 入口。`18001`、`18002` 分别直达 blue、green。首次初始化只有 blue，首次发布之后才有保留槽。 |
| 发布边界 | `runtime/state.json` 记载活动槽、前一槽、发布 ID 和阶段，`runtime/images.env` 保存镜像 ID，`runtime/nginx/upstream.conf` 保存路由。下一次发布会复用非活动槽并覆盖其旧制品。 |
| 发布检查 | 先检查候选容器镜像、版本、存活、就绪和商品 1，再校验 Nginx 配置并切流。切流后检查入口版本及槽标头。`--observe-seconds` 默认 30，可设为 1–300 秒。观察期间候选或入口检查失败，脚本尝试恢复稳定槽并验证。 |
| 自愈采样 | `ops.heal run` 每轮结束后等待 `--interval 5` 秒。Redis 用 `redis-cli PING`，最长 3 秒；活动 API 检查存活和商品 1，每个 HTTP 请求最长 2 秒。探测时间计入实际恢复时间。 |
| 自愈动作 | 默认 `--failure-threshold 3`、`--cooldown 30` 秒、`--max-attempts 2`。达到连续失败阈值才动作，每轮最多重启一个目标。后续连续两次成功才记录恢复。Redis 故障优先处理，存活正常而商品返回 429 时记录过载。 |
| 进程与锁 | `sre-lab.service` 启动完整 Compose 栈，`sre-lab-healer.service` 用默认参数运行真实控制器。发布、自愈和整栈启停协调使用 `runtime/operations.lock`。控制器预算、冷却和最近探测持久化于 `runtime/healing.json`。 |

配置与动作实现见 [Compose 运行清单](../../compose.runtime.yaml)、[发布器](../../ops/deploy.py)、[自愈策略](../../ops/heal.py)和[自愈 systemd 单元](../../ops/sre-lab-healer.service)。

## 2. 手动发布，再恢复原版本

确认活动版本为 0.3.0。运行以下命令，在非活动槽启动 0.3.1 并观察 30 秒。蓝绿切换复用相同的共享 Redis。

```bash
(
  set -euo pipefail
  cd /opt/sre-failure-lab
  python3 -c 'from ops.common import read_state; s=read_state(); assert s.get("phase")=="idle" and s["slots"][s["active"]]["version"]=="0.3.0", "先核对当前版本和发布阶段"'
  docker image inspect sre-lab-api:0.3.1 >/dev/null 2>&1 ||
    docker build --build-arg APP_VERSION=0.3.1 -t sre-lab-api:0.3.1 .
  python3 -m ops.deploy deploy --image sre-lab-api:0.3.1 --observe-seconds 30
  curl --noproxy '*' -fsSi http://127.0.0.1:18000/version
  curl --noproxy '*' -fsS http://127.0.0.1:18000/products/1
  python3 -m ops.deploy rollback
  curl --noproxy '*' -fsSi http://127.0.0.1:18000/version
)
```

验收时应看到版本从 0.3.0 变为 0.3.1，再回到原版本及槽。发布器核对实际镜像 ID；手动回滚验证保留槽镜像和就绪状态后才切流。若某步失败，先查 `ops.deploy status` 和下面的事件记录。处于 `switching`、`observing`、`recovery_required` 等中间阶段时，人工核对稳定槽与镜像后，使用 `ops.deploy rollback` 协调恢复。[中断恢复说明](../real-deployment.md)

## 3. 验证自动回退和真实 Docker 重启

在一个终端跟踪新的审计事件，按 Ctrl-C 可以随时停止查看。

```bash
cd /opt/sre-failure-lab
tail -n 0 -f runtime/events.jsonl
```

在另一个虚机终端运行仓库已有的真实验收器。它需要两个本地镜像、活动 0.3.0、自愈进程和已经生成的 `runtime/healing.json`。脚本从本机 `.env` 读取实验令牌。验收报告固定写入 `runtime/acceptance.json`。下面保留本次生成的报告；若预检就失败，终端会显示原因。

```bash
(
  set -eu
  cd /opt/sre-failure-lab
  run_dir="reports/compose-acceptance/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$run_dir"
  touch "$run_dir/start.marker"
  result=0
  python3 scripts/accept_runtime.py || result=$?
  if [[ runtime/acceptance.json -nt "$run_dir/start.marker" ]]; then
    cp runtime/acceptance.json "$run_dir/"
    python3 - "$run_dir/acceptance.json" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    r = json.load(f)
print("验收", r["status"], "开始于", r["started_at"])
for step in r["checks"]:
    print(step["name"], step["passed"])
PY
  fi
  exit "$result"
)
```

完成后可按下面四组观测核对报告。

1. 正常发布 0.3.1，手动回到 0.3.0。
2. 验收器在一次发布的观察期向候选 API 注入限时错误。该次发布报失败，入口自动切回 0.3.0，随后清除候选上的注入。
3. 成功发布 0.3.1 后有意停止 Redis。短暂降级期间仍能读出商品，返回 `X-Cache: BYPASS`。自愈日志出现 `healing_incident_detected`、`healing_action_requested`、`healing_action_accepted` 和 `healing_recovery_verified`。验收器匹配同一事件 ID，复查入口、版本及就绪。
4. 验收器停止活动 API，控制器重启该容器后再次验证商品请求。每个故障单独等待最多 70 秒。成功结束时活动版本为 0.3.1，报告应为 `status=passed`。程序退出时尝试启动仍由本次演练停掉的容器并清除未撤销的故障，清理异常会写入 `cleanup_errors`。[验收脚本](../../scripts/accept_runtime.py)

若自愈预算耗尽，先查 `journalctl -u sre-lab-healer -n 50 --no-pager`、`runtime/events.jsonl` 和容器状态。只有修复了对应故障并确认需要重新允许自动操作时，才执行 `python3 -m ops.heal reset --service redis` 或 `--service api`，仅针对该服务。重置会保留上次动作的冷却时间。[自愈排查细节](../real-healing.md)

## 4. 查指标，收尾

从真实 Compose Prometheus 的 9090 端口查活动 API、宿主机与容器采集目标，再查缓存错误。原始采样和规则评估默认均为 5 秒。发布时 `runtime/prometheus/active.json` 随活动槽更新，`job="api"` 跟随当前槽。自愈控制器也会查询商品，低流量时会在请求计数中占一些样本。[采集配置](../../monitoring/prometheus/runtime.yml)

```bash
for query in 'up{job=~"api|node|cadvisor"}' 'sum(increase(lab_cache_requests_total{job="api",result="error"}[5m]))'; do
  curl --noproxy '*' -fsSG http://127.0.0.1:9090/api/v1/query --data-urlencode "query=$query"
  echo
done
python3 -m ops.stack status
curl --noproxy '*' -fsS http://127.0.0.1:18000/health/ready
```

正常采集的 `up` 序列值为 1。缓存错误查询返回的是最近五分钟的估算增量，结合演练时段、BYPASS 响应和自愈事件一起解释。Grafana 选择原有 Compose Prometheus 数据源查看主机 CPU、内存、磁盘和容器 CPU/内存。若要恢复本章起始版本，先确认刚才整套验收成功、活动为 0.3.1 且保留槽为 0.3.0，再执行 `python3 -m ops.deploy rollback` 并复查 `/version`。

| 需要控制的服务 | 在虚机执行的操作 |
| --- | --- |
| 结束一次演练，保留所有实验服务 | 等待验收器完成，结束自己启动的 `tail -f`，保存报告。正常验收已清理本次注入。 |
| 暂停或恢复整个 Compose 栈 | 演练结束后执行 `systemctl stop sre-lab`；恢复用 `systemctl start sre-lab`，再验证父、子单元和业务。父服务启停同时管理自愈子服务。 |
| 仅维护自愈进程 | 在父服务运行时临时 `systemctl stop sre-lab-healer`，维护完 `systemctl start sre-lab-healer`。 |
| 管理 K3s 及其监控转发 | 整个 K3s 节点由 `k3s.service` 管理。独立单元 `sre-lab-k8s-prometheus.service` 为 Compose Grafana 接入集群 Prometheus。分别执行 `systemctl status k3s sre-lab-k8s-prometheus --no-pager` 检查。 |

后续可选扩展包括控制端用 Ansible 执行 `--check --diff` 后连续部署两次并核对第二次的 `changed=0`，K8s 告警的 firing/resolved 验收，以及 Argo CD 配置漂移与 Git 回退演练。[Ansible](../ansible-deploy.md) · [K8s 告警](../kubernetes-alerting.md) · [GitOps](../k8s-gitops.md)
