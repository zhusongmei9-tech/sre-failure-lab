# 第三章　在 Kubernetes 上发布商品服务并验证 HPA

完成条件是用自己的流量回答三个问题：哪个版本在接请求，错误的候选 Pod 是否影响用户，扩容后延迟发生了什么变化。沿用上一章已就绪的单节点 K3s、Metrics Server、Prometheus 和 Grafana。除标明「电脑」的步骤外，命令均在虚机的 root Bash 中运行。[上一章](02-k3s-monitoring.md)

## 1. 检查起点，准备两个版本的镜像

```bash
cd /opt/sre-failure-lab
k3s kubectl get nodes
k3s kubectl top node
k3s kubectl -n sre-monitoring get prometheus
test -f monitoring/kubernetes/app-lab/api.yaml
```

节点应为 `Ready`，`top node` 应输出 CPU、内存，监控命名空间里应有 Prometheus 对象。如果找不到 `api.yaml`，先核对源码来自 GitHub 的 `main`，或按[教程首页](README.md)核对 Release 中项目上传的源码包及其提交号。Docker Engine 与 K3s 使用各自的镜像存储；需要把两个应用镜像和 Redis 一次性导入 K3s。镜像标签属于演练业务，API 会在响应中返回构建时的 `APP_VERSION`。[Dockerfile](../../Dockerfile)

```bash
(
  set -euo pipefail
  for version in 0.3.0 0.3.1; do
    if ! docker image inspect "sre-lab-api:$version" >/dev/null 2>&1; then
      docker build --build-arg "APP_VERSION=$version" -t "sre-lab-api:$version" .
    fi
  done
  docker image inspect redis:7.4-alpine >/dev/null 2>&1 || docker pull redis:7.4-alpine
  docker save sre-lab-api:0.3.0 sre-lab-api:0.3.1 redis:7.4-alpine | k3s ctr images import -
  k3s crictl images | grep -E 'sre-lab-api|redis'
)
```

预期可见 API 的两个标签和 Redis 的 `7.4-alpine`。后续发布使用这两个明确的镜像名，制作自定义版本时也需要同步更新演练脚本支持的版本。

## 2. 对照源文件理解安装配置

### 隔离、资源预算和发现规则

| 文件与字段 | 仓库取值 | 作用 |
| --- | --- | --- |
| Namespace / Pod Security | `sre-app-lab`，`restricted`，策略版本 `v1.37` | 商品实验与 Compose、`sre-k8s-demo` 分开。API 和 Redis 使用非 root 进程、`RuntimeDefault` seccomp、只读根文件系统、禁止提权、丢弃全部 capabilities，且不挂载服务账号令牌。 |
| 所有权标签 | `app.kubernetes.io/part-of=sre-failure-lab`、`app.kubernetes.io/managed-by=sre-failure-lab`、`sre-lab-role=product-lab` | 安装器核对同名对象的归属。服务路由另用 `app=product-api` 和 `app=redis` 匹配各自的 Pod。 |
| ResourceQuota / hard | `product-budget`；Pod 数 7；requests 总量 250m CPU、384Mi 内存；limits 总量 1400m CPU、1152Mi 内存 | 整个命名空间共享额度。预算为两个 API Pod、Redis、发布时的临时副本和退出中的 Pod 留出空间。1000m 表示一个 CPU 核。 |

配置原文见 [namespace.yaml](../../monitoring/kubernetes/app-lab/namespace.yaml)。

### 商品 API、Redis 和网络

| 组件 | 字段和取值 | 对演练的影响 |
| --- | --- | --- |
| API Deployment | `product-api`；容器 `api`；初始镜像 `sre-lab-api:0.3.0`，`imagePullPolicy: Never`；UID/GID 10001 | 从本节点已导入的镜像启动。初始副本数默认为 1，清单省略 `spec.replicas`，交给 HPA 调整。 |
| API 配置与资源 | `REDIS_URL=redis://redis:6379/0`，`LAB_ENABLED=false`，`FALLBACK_ENABLED=false`；requests 25m/64Mi，limits 250m/192Mi | 通过同命名空间的 Redis 服务读写缓存。应用内教学故障开关关闭，Redis 故障会暴露在就绪检查和商品请求上。默认缓存 TTL 为 60 秒，Redis 超时为 0.5 秒。 |
| API Service 与探针 | `ClusterIP`，8000 → 容器命名端口 `http`；每 3 秒查 `/health/ready`，超时 2 秒；启动 10 秒后每 10 秒查 `/health/live`，超时 2 秒 | 就绪检查包含 Redis；Service 将新请求路由到就绪端点。存活检查用于判断进程是否需要重启。 |
| API 更新与退出 | `RollingUpdate`，`maxUnavailable=0`、`maxSurge=1`、`minReadySeconds=3`、`progressDeadlineSeconds=90`、`revisionHistoryLimit=4`；退出宽限 15 秒，`preStop` 等 3 秒 | 正常更新可以多启动 1 个 Pod；新 Pod 持续就绪后才替掉旧副本。退出前短暂等待端点更新。演练脚本对错误候选另设 18 秒观察窗口，随后主动恢复配置。 |
| Redis Deployment | 1 副本、`Recreate`、保留 2 个历史修订；`redis:7.4-alpine`，`IfNotPresent`；UID/GID 与 `fsGroup` 都是 999；退出宽限 10 秒 | Redis 独立于 Compose 缓存；容器 requests 为 10m/16Mi，limits 为 100m/64Mi。每 5 秒运行 `redis-cli ping`，超时 2 秒。 |
| Redis 容量与数据 | `--save "" --appendonly no`；`--maxmemory 24mb --maxmemory-policy allkeys-lru`；`/data` 使用 8Mi 的 `emptyDir` | 关闭 RDB 和 AOF。达到缓存上限时按 LRU 淘汰键，重建 Pod 后重新填充缓存。商品样例由 API 提供。 |
| Redis Service 与 NetworkPolicy | `redis`，ClusterIP TCP 6379；策略 `redis-from-product-api` 选择 `app=redis`，允许同命名空间内 `app=product-api` 的 Pod 访问 TCP 6379 | 应用使用服务名访问缓存，无需配置 Pod IP。策略生效依赖集群启用网络策略控制器。 |

配置原文见 [api.yaml](../../monitoring/kubernetes/app-lab/api.yaml)、[redis.yaml](../../monitoring/kubernetes/app-lab/redis.yaml) 和 [应用默认值](../../app/config.py)。

### 扩缩容与监控

| 组件 | 配置 | 含义 |
| --- | --- | --- |
| HPA `autoscaling/v2` | 目标 `apps/v1 Deployment/product-api`；`minReplicas=1`，`maxReplicas=2`，CPU 平均利用率目标 60%，缩容稳定窗口 45 秒 | 60% 对应每个 API 副本平均约 15m CPU，即 request 25m 的 60%。HPA 从 Metrics Server 取数，指标刷新和控制周期也会影响缩容耗时。 |
| ServiceMonitor `product-api` | 对象在 `sre-monitoring`，带 `release=sre-k8s`；发现范围 `sre-app-lab`；选中带 `app=product-api` 和项目归属标签的 Service；端口 `http`，路径 `/metrics`，每 10 秒抓取，3 秒超时 | Prometheus 找到并抓取 API 端点。读不到业务指标时，依次核对 ServiceMonitor 所在命名空间、release 标签、Service 标签、端口名称和端点。 |
| Grafana 看板 | UID `sre-k8s-product`，数据源 UID `kubernetes`，每 10 秒刷新 | 四个面板显示商品总/失败 RPS（1 分钟）、服务端直方图 P95（1 分钟、ms）、每个 API Pod 的 CPU（2 分钟、mCPU）以及就绪、当前、期望副本数。 |

配置原文见 [hpa.yaml](../../monitoring/kubernetes/app-lab/hpa.yaml)、[product-monitor.yaml](../../monitoring/kubernetes/app-lab-observability/product-monitor.yaml) 和 [含七条 PromQL 查询的看板](../../monitoring/grafana/dashboards/k8s-product.json)。

## 3. 安装、验收商品和缓存

```bash
bash scripts/install_k8s_app_lab.sh --monitoring
k3s kubectl -n sre-app-lab get deployments,pods,services,hpa
k3s kubectl -n sre-app-lab describe quota product-budget
k3s kubectl -n sre-app-lab get endpointslice -l kubernetes.io/service-name=product-api
k3s kubectl -n sre-monitoring get servicemonitor product-api
```

安装器固定使用 `/etc/rancher/k3s/k3s.yaml`，预检 API 镜像、节点指标和已有对象归属，然后创建缺少的对象。已有实例重跑时会保留当前版本、副本数和已存在的配置；修改清单后的升级要另行应用相应变更。第一条命令完成时，安装器应输出 `product=1 redis=ok`。在新环境中 API 和 Redis 各应有 1 个就绪副本，HPA 在取得第一批 Pod CPU 指标前可能短暂显示 `<unknown>`。仅做发布、扩缩容时可省略 `--monitoring`；本章的看板验收需要该参数和上一章的数据源。[安装器](../../scripts/install_k8s_app_lab.sh)

```bash
service_ip=$(k3s kubectl -n sre-app-lab get svc/product-api -o jsonpath='{.spec.clusterIP}')
curl --noproxy '*' -fsS "http://$service_ip:8000/health/ready"
curl --noproxy '*' -fsS "http://$service_ip:8000/version"
for attempt in 1 2; do
  curl --noproxy '*' -fsi "http://$service_ip:8000/products/2"
done
```

就绪响应有 `status=ready` 和 `redis=ok`。商品响应应为 HTTP 200、JSON 中 `id=2`，并有 `X-App-Version`。冷缓存第一次 `X-Cache: MISS`、第二次 `HIT`，已有缓存时两次都可能是 `HIT`。

**用电脑浏览器查看。** 在虚机的第二个终端中运行以下端口转发，并保持运行。

```bash
k3s kubectl -n sre-app-lab port-forward --address=127.0.0.1 svc/product-api 18080:8000
```

在电脑终端把 `user@VM_IP` 换成第一章中能登录虚机的 SSH 用户和地址，然后运行命令并保持隧道连接。浏览器打开 `http://127.0.0.1:18080/docs` 看 API，打开 `http://127.0.0.1:13000/d/sre-k8s-product` 看 Grafana，使用第一章生成的 Grafana 账号凭据。这里假设虚机的 Grafana 在回环端口 3000 可达。完成后用 Ctrl-C 结束两个转发进程。发布演练会替换 API Pod，浏览器转发中断时重新运行虚机上的转发命令。

```bash
ssh -N -o ExitOnForwardFailure=yes -L 18080:127.0.0.1:18080 -L 13000:127.0.0.1:3000 user@VM_IP
```

## 4. 跑一次成功发布和一次失败回滚

以下命令在虚机运行。第一次先查看计划，再执行；报告按执行时间存放，便于保留多次对比结果。

```bash
cd /opt/sre-failure-lab
run_dir="reports/product-k8s/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$run_dir"
python3 scripts/k8s_app_rollout.py
python3 scripts/k8s_app_rollout.py --run --output "$run_dir/rollout.json"
```

脚本从节点经稳定的 ClusterIP 约每 250ms 查询一次商品，核对 HTTP 200、商品 1 和响应版本。在初始版本上运行会切到 0.3.1，随后把候选模板的就绪路径改成测试用的无效路径。观察 18 秒并确认候选 ReplicaSet 未就绪后，脚本恢复上一个已验证模板，成功发布的版本继续接流量。再次运行会反向验证另一版本。

```bash
k3s kubectl -n sre-app-lab get rs -l app=product-api
k3s kubectl -n sre-app-lab get events --field-selector=reason=Unhealthy
python3 - "$run_dir/rollout.json" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    r = json.load(f)
print("通过", r["passed"], "版本", r.get("from_version"), "→", r.get("to_version"))
for phase, row in r.get("traffic", {}).items():
    print(phase, "请求", row["requests"], "失败", row["failed"], "版本分布", row["responses"])
print("回滚动作到验证恢复（秒）", r.get("rollback_seconds"))
PY
```

通过时 `passed` 为真，`rolling_update`、`rejected_readiness`、`rollback` 阶段的采样失败数均为 0。事件里通常能找到候选 Pod 的 HTTP 404。报告将候选观察期与执行回滚动作后的用时分开统计。

## 5. 制造有界流量，观察副本变动

等待发布演练退出后，在同一个虚机终端运行。在另一终端运行 `k3s kubectl -n sre-app-lab get hpa/product-api -w`，可实时观察 HPA。Grafana 时间选择演练发生的区间。

```bash
python3 scripts/k8s_app_hpa.py
python3 scripts/k8s_app_hpa.py --run --rps 40 --output "$run_dir/hpa.json"
python3 - "$run_dir/hpa.json" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    r = json.load(f)
print("通过", r["passed"], "客户端负载结果", r.get("load", {}))
print("撤流至 1 个就绪副本（秒）", r.get("seconds_from_load_stop_to_one_ready"))
PY
k3s kubectl -n sre-app-lab get deploy/product-api hpa/product-api
```

脚本等指标与单副本就绪后，以 1 RPS 跑 20 秒基线，然后用 4 个并发工作线程、目标 40 RPS 查询真实商品。可调范围为 5–60 RPS。高负载阶段最多 150 秒，观测到 2 个就绪副本连续保持约 30 秒后结束负载；每 5 秒采样副本和 HPA CPU。最后在最多 210 秒内等待缩回 1 并再次查询商品。

核对 `passed`、`load.product_200`、`load.errors`、`load.achieved_rps` 和 `load.by_ready_replicas`。后者只统计就绪副本数在前后两次采样中一致的窗口；P95 是客户端实测毫秒数。Grafana 的 P95 来自服务端直方图。137 的两轮样例在约 40 RPS 下分别完成 1,829 和 1,837 次成功请求，撤流约 71 秒后缩回 1。两次延迟变化方向不同，复现验收以你自己的副本变化、实际吞吐、错误数和采样窗口为准。[历史报告和原始 JSON](../k8s-product-lab.md)

## 6. 用现场配置排查问题

| 现象 | 现场检查 | 下一步 |
| --- | --- | --- |
| API Pod 显示 `ErrImageNeverPull` | `k3s crictl images`；`k3s kubectl -n sre-app-lab describe pod -l app=product-api` | 核对标签与本机架构，重做第 1 节的镜像导入，等待 kubelet 重试。 |
| Service 存在但查询失败 | `k3s kubectl -n sre-app-lab get svc/product-api -o yaml`；`k3s kubectl -n sre-app-lab get endpointslice -l kubernetes.io/service-name=product-api -o yaml` | 核对 `app=product-api`、`http` 端口、端点的 `conditions.ready`。再查看 `k3s kubectl -n sre-app-lab logs deploy/product-api --tail=30` 和 Redis 状态。 |
| HPA 长期显示 `<unknown>` 或一直是 1 | `k3s kubectl top pod -n sre-app-lab`；`k3s kubectl -n sre-app-lab describe hpa/product-api` | 先确认 Metrics Server 有 CPU 样本，再检查 CPU request、ScalingActive 条件和实际 RPS。若平均 CPU 低于目标，维持 1 副本符合策略。 |
| 新 Pod 长期 Pending | `k3s kubectl -n sre-app-lab describe quota/product-budget`；`k3s kubectl -n sre-app-lab get events --sort-by=.lastTimestamp` | 看配额及节点剩余 CPU/内存是否能容纳 Redis、API 和更新所需的额外副本。 |
| Grafana 有副本数，业务 P95 却没有曲线 | `k3s kubectl -n sre-monitoring get servicemonitor/product-api -o yaml`；`k3s kubectl -n sre-app-lab get svc/product-api --show-labels` | 核对选择器、`http` 端口和监控时段。发起商品请求，等待至少两个抓取周期，再查看最近 15 分钟；无流量时 P95 可以没有数据。 |

两个演练和安装器对同一安装目录互斥。出现 `Another ... operation/exercise` 时先等待正在运行的命令结束。保存本轮报告位置可运行 `printf '%s\n' "$run_dir"`，之后可从电脑通过 SSH 复制报告到自己的实验记录目录。
