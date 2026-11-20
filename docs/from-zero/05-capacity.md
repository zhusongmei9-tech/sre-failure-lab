# 05　在两台机器上测商品查询容量

本章要回答的是：在指定机器、流量和延迟目标下，商品服务能连续十分钟处理多少次查询？137 运行 K3s、商品服务和监控，135 运行压测工具 k6。两台机器可以换成自己的 Linux 虚机。负载机只需要能 SSH 登录 137 的普通账号并允许 TCP 转发，集群管理权限留在 137。先完成[单节点 K3s 与监控](02-k3s-monitoring.md)。

可对照[双机实测记录与原始数据](../experiments/k8s-capacity/README.md)理解自己的结果。读表时先核对机器规格、每个 Pod 的资源配额、访问路径、实际吞吐和测量时长。

## 1. 在 137 准备独立商品服务

在 **137 的 root Bash** 进入项目，确认源码来自你准备复现的提交或 Release 附件。容量实验创建 `sre-capacity-lab`，使用自己的 Redis 和商品 API。商品查询走 ClusterIP，API 关闭实验控制接口。此命名空间没有 HPA，分别固定 1、2 个 API 副本。第三章的自动伸缩实验仍在 `sre-app-lab`。[容量覆盖配置](../../monitoring/kubernetes/capacity-lab/kustomization.yaml) · [基础清单](../../monitoring/kubernetes/app-lab)

```bash
cd /opt/sre-failure-lab
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
k3s kubectl config current-context
k3s kubectl get nodes -o wide
k3s kubectl top node
df -h / /var/lib
free -h
k3s kubectl get namespace sre-capacity-lab --ignore-not-found --show-labels
```

全新安装时最后一条没有输出。重复执行时，确认命名空间带有 `sre-lab-role=capacity-lab` 且其中只有你准备测试的资源。节点应为 Ready，已有服务及监控均应健康。有压测正在进行时，先等它结束再调整副本或镜像。

```bash
cd /opt/sre-failure-lab
(
  set -eo pipefail
  docker build --build-arg APP_VERSION=capacity -t sre-lab-api:capacity .
  docker image inspect redis:7.4-alpine >/dev/null 2>&1 || docker pull redis:7.4-alpine
  docker save sre-lab-api:capacity redis:7.4-alpine | k3s ctr images import -
  k3s kubectl apply -k monitoring/kubernetes/capacity-lab
  k3s kubectl -n sre-capacity-lab scale deployment/product-api --replicas=1
  k3s kubectl -n sre-capacity-lab rollout status deployment/redis --timeout=180s
  k3s kubectl -n sre-capacity-lab rollout status deployment/product-api --timeout=180s
)
k3s kubectl -n sre-capacity-lab get deploy,svc,hpa,pods
CAP_IP=$(k3s kubectl -n sre-capacity-lab get service product-api -o jsonpath='{.spec.clusterIP}')
printf '商品 Service 的 ClusterIP 为 %s\n' "$CAP_IP"
curl --noproxy '*' -fsS "http://${CAP_IP}:8000/version"
curl --noproxy '*' -fsS "http://${CAP_IP}:8000/metrics" | grep '^lab_product_requests_in_flight '
```

应看到 1 个 Ready API、1 个 Ready Redis、两个 ClusterIP Service，最后两条分别输出业务构建标识和商品在途指标。若重复构建了同名镜像，原 Pod 可能还在运行旧镜像。完成导入后，在两轮测试之间执行 `k3s kubectl -n sre-capacity-lab rollout restart deployment/product-api`，等待 rollout 完成，再核对指标。记录 `k3s kubectl -n sre-capacity-lab get pods -l app=product-api -o jsonpath='{.items[*].status.containerStatuses[*].imageID}'` 以识别实际镜像。

## 2. 在 137 接入 Prometheus 和 Grafana

新看板需要 kubelet 采集容器受 CPU 配额限制的时间。用本仓库更新后的 [values.yaml](../../monitoring/kubernetes/values.yaml) 更新当前监控配置。先运行 `helm -n sre-monitoring get values sre-k8s` 并和安装记录核对。使用第二章标准配置时执行下面的第一条。已经启用了告警时，走[告警手册中的双 values 升级命令](../kubernetes-alerting.md)，同时传入更新后的基础 values 和原告警 overlay。若你还有自己的 values，用原来的覆盖顺序一起传入，避免覆盖现有配置。

```bash
# 仅第二章标准基础配置执行此条
bash scripts/install_k8s_monitoring.sh monitoring/kubernetes/values.yaml
```

完成对应监控升级后，在 137 执行以下命令。Prometheus 从 `sre-monitoring` 发现这个 ServiceMonitor，其中的 selector 指向 `sre-capacity-lab` 的商品 API；每 10 秒抓取一次 `/metrics`。[采集配置](../../monitoring/kubernetes/capacity-lab-observability/service-monitor.yaml)

```bash
k3s kubectl apply -f monitoring/kubernetes/capacity-lab-observability/service-monitor.yaml
k3s kubectl -n sre-monitoring get servicemonitor product-capacity
systemctl is-active sre-lab-k8s-prometheus
gateway=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
curl --noproxy '*' -fsSG "http://${gateway}:19090/api/v1/query" \
  --data-urlencode 'query=up{namespace="sre-capacity-lab",service="product-api"}'
```

经过至少两次抓取，`result` 中每个 Ready API 应有值为 `1` 的序列。Compose 的 Grafana 从项目 `monitoring/grafana/dashboards/` 目录自动加载看板；已有部署要把新文件放进**当前运行栈实际挂载的目录**。从电脑按[第一章的 SSH 隧道](01-linux-compose.md)访问 `http://127.0.0.1:3000/d/sre-k8s-capacity`，看板名称为 **Kubernetes 商品查询容量**，数据源为 `Kubernetes`。默认命名空间是 `sre-capacity-lab`，也可切换到原商品环境。初装数据源或桥接服务时，先完成第二章第四节。[看板定义](../../monitoring/grafana/dashboards/k8s-capacity.json)

## 3. 在 135 安装 k6，建立到 137 的 HTTP 通道

135 需要 Python 3.9+、Git、OpenSSH 客户端、curl、tar 和 sha256sum。在 137 的源码目录运行 `git rev-parse HEAD` 取得完整提交号；使用 Release 源码包时，从该包对应的发布说明读取提交号。以下命令在 135 的新目录取回同一份源码，也可将同一个 Release 附件解包到 `$HOME/sre-capacity-client`。已有目录时先核对 `git rev-parse HEAD`，继续使用提交相符的副本。

```bash
read -r -p '输入 137 上源码对应的完整 Git 提交: ' CAP_COMMIT
git clone https://github.com/zhusongmei9-tech/sre-failure-lab.git "$HOME/sre-capacity-client" &&
  git -C "$HOME/sre-capacity-client" switch --detach "$CAP_COMMIT"
python3 --version
```

下面的命令在 **135 的普通用户 Bash** 运行。它根据压测机架构选择 k6 v1.3.0，核对归档 SHA-256 后安装到用户目录。[k6 官方发行页](https://github.com/grafana/k6/releases/tag/v1.3.0)

```bash
(
  set -eu
  case "$(uname -m)" in
    x86_64) arch=amd64; digest=84d26fc1f7bc03e02f2e016b3b1b20c032e05dfe461fca82de4e3a6ebe72ddbd ;;
    aarch64|arm64) arch=arm64; digest=df35a88b5ef534b39f4ab74b22a0d60362138af219228253f042650a36af7920 ;;
    *) echo '需要 Linux AMD64 或 ARM64'; exit 1 ;;
  esac
  tmp=$(mktemp -d)
  cd "$tmp"
  package="k6-v1.3.0-linux-${arch}"
  curl -fL --retry 2 "https://github.com/grafana/k6/releases/download/v1.3.0/${package}.tar.gz" -o "${package}.tar.gz"
  printf '%s  %s\n' "$digest" "${package}.tar.gz" | sha256sum -c -
  tar -xzf "${package}.tar.gz" "${package}/k6"
  install -Dm755 "${package}/k6" "$HOME/.local/bin/k6-v1.3.0"
  "$HOME/.local/bin/k6-v1.3.0" version
)
```

在 **135 终端 A** 执行。将 `user@VM137` 替换成有权登录 137 的账号和实际地址，首次登录按两机已有的 SSH 信任关系核对主机指纹。手动输入 137 第一步输出的 ClusterIP。转发由 137 连接商品 Service，对 135 仅监听回环端口 18081；Service 照常在各 Ready Pod 间分配新连接。此测量包含 SSH 转发、网络和连接复用的影响，两轮都沿用相同路径。

```bash
read -r -p '输入 137 上商品 Service 的 ClusterIP: ' CAP_IP
ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L "127.0.0.1:18081:${CAP_IP}:8000" user@VM137
```

保持该终端打开。若提示 `administratively prohibited`，检查该 SSH 用户在 137 的 TCP 转发授权；若端口占用，同时更改隧道左侧端口、后面的 `BASE`。重新创建 Service 后需要重新读取其 ClusterIP。

## 4. 在 135 做短测，再选一个场景测十分钟

在 **135 终端 B** 使用下列命令。每次创建新的报告目录，记录两机 CPU/内存规格、源码提交、镜像 ID、固定副本数、UTC 起止时间。先看 `/version` 和 `/health/ready`，应与 137 上一致且 Redis 为 `ok`。驱动会先核对项目 OpenAPI 和就绪状态。

```bash
cd "$HOME/sre-capacity-client"
BASE=http://127.0.0.1:18081
K6_BIN="$HOME/.local/bin/k6-v1.3.0"
mkdir -p "$HOME/sre-capacity-reports"
REPORT_DIR=$(mktemp -d "$HOME/sre-capacity-reports/run.XXXXXX")
curl --noproxy '*' -fsS "$BASE/version"
curl --noproxy '*' -fsS "$BASE/health/ready"
python3 scripts/capacity.py --base-url "$BASE" --scenarios mixed --rates 40,80,120
python3 scripts/capacity.py --run --k6 "$K6_BIN" --base-url "$BASE" \
  --scenarios hot,mixed --rates 40 --seconds 15 --output "$REPORT_DIR/smoke.json"
```

省略 `--run` 只打印计划。短测用于验证链路、压测端到达率和报告字段。正式测量可先选一个档位与一种场景做双副本对照；示例取 mixed、40 RPS。先在 1 副本时运行第一份，再按下一步切到 2 副本，使用同样参数产生第二份。要找更高的达标档位，再选 `--rates 40,80,120`。每个“场景 × 速率”单独持续指定时长；三个场景、三档、每档十分钟需要至少 90 分钟，另加冷启动等待时间。[驱动及参数](../../scripts/capacity.py) · [k6 请求模型](../../loadgen/capacity.js)

```bash
# 135 终端 B，137 当前为 1 个 Ready API
python3 scripts/capacity.py --run --k6 "$K6_BIN" --base-url "$BASE" \
  --scenarios mixed --rates 40 --seconds 600 --output "$REPORT_DIR/one.json"
```

```bash
# 137，确认上一轮已停止
k3s kubectl -n sre-capacity-lab scale deployment/product-api --replicas=2
k3s kubectl -n sre-capacity-lab rollout status deployment/product-api --timeout=180s
k3s kubectl -n sre-capacity-lab get deployment/product-api
k3s kubectl -n sre-capacity-lab get endpointslices -l kubernetes.io/service-name=product-api
```

```bash
# 135 终端 B，且 137 已显示 2/2 READY
python3 scripts/capacity.py --run --k6 "$K6_BIN" --base-url "$BASE" \
  --scenarios mixed --rates 40 --seconds 600 --output "$REPORT_DIR/two.json"
```

| 场景或设置 | 实际行为 |
| --- | --- |
| `hot` | 在计时前确认商品 1 返回 HIT；测量期间反复查询商品 1。 |
| `mixed` | 提前确认三个商品均 HIT；迭代按 1、2、3 轮换。数据库模型是进程内只读商品字典。 |
| `cold-start` | 每档先静默等待，`--cold-wait` 默认 65 秒。三个商品的默认缓存 TTL 为 60 秒；测试开头的前三次查询须得到三个有效 MISS。等待期间请暂停其他商品查询，探活与指标抓取可继续。后续请求自然变热，具体计数见 `cache`。自定义 TTL 时相应增加等待。 |
| `--rates`、`--seconds` | 每秒固定发起的迭代数，每迭代一次 HTTP GET。默认 40、80、120 RPS，每档 600 秒。可以用 15 秒短测确认工具，用十分钟档记录持续容量。 |
| `--vus` | 负载机预分配的虚拟用户数，默认 32。若 `dropped_iterations` 增加，先查 135 的 CPU、内存、SSH 进程与设置，再在相同服务配置上重试；压测机未发出的流量会单独检查。 |

报表保留每档的状态。退出码 0 表示所选档位全部达标，1 表示跑完但有档位未通过，2 表示错误或中断。每次用新文件名以保留上次数据。若 137 出现 DiskPressure、MemoryPressure、原有服务掉线或磁盘日志快速增长，先在 135 终端 B 按 Ctrl-C 收流，再看 `df -h /var/lib`、`free -h`、`k3s kubectl describe node` 和容器日志。确认节点、原有服务恢复后，从最近稳定的一档开始。

## 5. 对照报告与看板，记录容量

```bash
# 135 终端 B
python3 - "$REPORT_DIR/one.json" "$REPORT_DIR/two.json" <<'PY'
import json
import sys
for name in sys.argv[1:]:
    with open(name) as f:
        d = json.load(f)
    print(name, d["status"], "10 min capacity:", d["capacity_rps"])
    for s in d["stages"]:
        print(s["started_at"], s["scenario"], s["target_rps"], s["actual_rps"],
              s["planned"], s["started"], s["completed"], s["dropped_iterations"],
              s["http_429"], s["timeouts"], s["p95_ms"], s["p99_ms"], s["passed"])
        if not s["passed"]:
            print({k: v for k, v in s["checks"].items() if not v})
PY
```

先看每档的 `planned`、`started`、`completed` 和 `dropped_iterations`。驱动同时检查所有发起的请求均已完成、最终发送量达到目标且 k6 无漏发。边界处允许额外一次到达。成功率的分子是**返回 2xx 且内容校验正确**的 `valid_products`，分母为全部 `started`。返回 429、传输失败、超时、返回 2xx 但商品不正确都会影响成功率；超时属于传输错误的一部分。达标要求成功率至少 99.9%、客户端 P95 ≤100 ms。P99 单独展示。十分钟以下的短测可出现在 `highest_passed_rps`，而 `capacity_rps` 只统计达到十分钟的通过档位。`null` 表示没有满足条件的已测档位。若测到的各档均达标，最高档只能作为硬件在该条件下的已验证下界。

把 Grafana 时间范围设成两份报告的 UTC `started_at` 到 `ended_at`。依次查看实际完成吞吐、服务端 P95/P99、每 Pod 在途数与 Ready 数、Redis GET/SET P95、API/Redis 容器 CPU 用量和限流 ms/s，最后看节点空闲 CPU、可用内存及磁盘。客户端 P95 包含传输与接收，服务端分位数是到响应头的直方图估算。商品指标约 10 秒采样，节点和 kubelet 默认 30 秒采样，曲线使用 1–2 分钟窗口。短于窗口的尖峰或完全没有 SET 的安静时段可能无分位值。双副本同处一台虚机，比较时固定镜像、速率、时长、缓存场景和客户端设置，只报告实际观测到的变化。

k6 默认复用 HTTP 连接，Kubernetes Service 可以把同一条连接的多个请求都交给同一个 Pod。做双副本对照时，检查吞吐面板中的各 Pod 曲线，或者在 Grafana Explore 的 Kubernetes 数据源执行下式。选择实际压测的时间段，两条曲线都大于零时才说明两个 Pod 在该窗口里都接到了流量。单个一分钟窗口与整档十分钟的结果要分别记录。

```promql
sum by (pod) (rate(lab_http_requests_total{namespace="sre-capacity-lab",service="product-api"}[1m]))
```

需要保留看板证据时，在 137 的项目目录导出，同一份报告会按 Pod、容器和 Redis 命令分别统计每条曲线的有效样本、最小值、均值、最大值。在两台机器分别执行 `date -u` 核对时间，可用 `chronyc tracking` 查看同步状态。若时钟有偏差，记录差值，再换算为 **137 的 UTC 时间**选取窗口。两份报告保留各自原始时间戳。在连续十分钟的流量中，可以另导出开始两分钟以后至结束前的区间，让 1–2 分钟的速率窗口完全落在恒定负载内。空采样保留为空值。[导出工具](../../scripts/export_capacity_metrics.py)

```bash
cd /opt/sre-failure-lab
gateway=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
read -r -p '137 上窗口开始 UTC，例如 2026-11-20T10:26:30Z: ' START_UTC
read -r -p '137 上窗口结束 UTC，例如 2026-11-20T10:34:00Z: ' END_UTC
python3 scripts/export_capacity_metrics.py --prometheus-url "http://${gateway}:19090" \
  --namespace sre-capacity-lab --start "$START_UTC" --end "$END_UTC" --step 10
```

工具打印新 JSON 报告路径。按 Pod 吞吐在面板 1 的 C 查询中；每条序列的 `labels.pod` 给出 Pod 名，`mean` 为该窗口内采样吞吐的算术平均（请求/秒）。需要精确计数时可在 137 对各 Pod 的商品请求计数器做起止差分，并检查区间内是否发生重启。

## 6. 撤流、恢复或移除

在 135 停止 k6 后结束终端 A 的 SSH 会话。还要复测时，在 137 将副本数恢复到实验前的数量，新建实验通常是 1，然后确认 Ready。

```bash
k3s kubectl -n sre-capacity-lab scale deployment/product-api --replicas=1
k3s kubectl -n sre-capacity-lab rollout status deployment/product-api --timeout=180s
```

要移除**本次新建**的容量实验时，先核对命名空间标识、资源和其中的临时缓存，确认报告已另存。下面的清理会移除容量命名空间内的全部对象，仅在这些对象都可丢弃时执行。采集配置位于另一个命名空间，单独移除。原商品实验、Grafana 和 Helm 监控继续供其他章节使用。

```bash
k3s kubectl get namespace sre-capacity-lab --show-labels
k3s kubectl -n sre-capacity-lab get all,pvc
# 仅确认实验归属、可清理后执行以下两条
k3s kubectl -n sre-monitoring delete servicemonitor product-capacity --ignore-not-found
k3s kubectl delete namespace sre-capacity-lab --ignore-not-found
```
