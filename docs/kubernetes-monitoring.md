# 单节点 K3s 与 Kubernetes 监控

这一阶段在现有 Docker Compose 实验室旁增加一个单节点 K3s 集群，观察节点、命名空间、Pod、Deployment 和容器资源。原商品 API、Redis、蓝绿发布与 `ops.heal` 仍由原有 Compose/systemd 流程管理，没有迁移成 Kubernetes 工作负载。

2026-11-17 已在 137 的 ARM64 虚拟机完成安装、指标查询与演示 Pod 删除恢复，见 [本轮验收记录](kubernetes-validation-137.md)。单节点不具备宿主机故障后的跨节点接管能力。

## 组件和配置

| 项目 | 固定配置 |
| --- | --- |
| Kubernetes | K3s `v1.37.2+k3s1`，使用自己的 containerd |
| Helm | `v4.3.2` |
| 监控 chart | `prometheus-community/kube-prometheus-stack` `93.3.1` |
| Helm release / namespace | `sre-k8s` / `sre-monitoring` |
| Prometheus Service | `sre-k8s-prometheus`，ClusterIP |
| 演示 namespace / Deployment | `sre-k8s-demo` / `demo-web` |
| Grafana | 复用现有 Docker Grafana，新增数据源 UID `kubernetes` |

配置文件为 [values.yaml](../monitoring/kubernetes/values.yaml) 和 [demo.yaml](../monitoring/kubernetes/demo.yaml)。本轮按 chart 93.3.1 的官方 values 和模板核对参数；该版本主 chart 没有独立的 `values.schema.json`，本地校验使用 Helm lint 与模板渲染。不要不经核对直接套用到任意 chart 新版本。

采集保留 API Server、kubelet、kubelet 内置 cAdvisor、kube-state-metrics 和 node_exporter，以及 Prometheus/Operator 自身。K3s 把部分控制面组件放在同一进程中，本配置不创建 scheduler、controller-manager、etcd、kube-proxy 的独立监控目标，也关闭相应默认规则，避免不存在的端点产生告警。本阶段暂不采集 CoreDNS；这不等于关闭集群 DNS。K3s 的组件指标关系见 [官方指标说明](https://docs.k3s.io/reference/metrics)。

为降低开销，chart 内的 Grafana 和 Alertmanager 均关闭。Prometheus 仍评估保留的告警规则，但本阶段没有告警通知接收端；与 Alertmanager 连接相关的默认告警也显式关闭。

## 资源与权限边界

主机是约 2 核、4.5 GB 内存的实验虚拟机，同时运行原有容器。监控按 30 秒采集与评估，资源配置如下：

| 组件 | CPU request / limit | 内存 request / limit |
| --- | --- | --- |
| Prometheus | 100m / 500m | 256Mi / 512Mi |
| 配置重载器 | 10m / 100m | 32Mi / 64Mi |
| Prometheus Operator | 25m / 200m | 64Mi / 192Mi |
| kube-state-metrics | 25m / 100m | 48Mi / 128Mi |
| node_exporter | 10m / 100m | 32Mi / 96Mi |
| 演示 Web Pod | 10m / 50m | 8Mi / 32Mi |

这不是整机资源上限：K3s、containerd、系统服务和原有 Docker 容器仍消耗额外资源。增加工作负载前，应观察节点可用内存、Pod 调度结果和 OOM 事件。

Prometheus 保留 1 天、最多约 256 MB 的持久化数据块，使用 `local-path` 的 1 GiB PVC。WAL、活动数据及其他文件仍占空间，256 MB 不是整个目录的硬配额；本地卷也不是远程备份。不要为演练删除这个 PVC。

安装器支持 ARM64 与 AMD64，下载对应架构的固定版本二进制并校验摘要；本轮实机验证只覆盖 ARM64。安装前还应确认主机时钟已通过 NTP 同步，避免证书、ServiceAccount token 和监控时间轴受时间偏差影响。

Kubernetes node_exporter 使用 **19100**，保留原 Compose node_exporter 的 9100。该采集器需要主机 PID/网络与只读主机挂载来读取节点数据；它不是普通业务 Pod，也不提供修改主机文件的能力。无需为它开放公网入口。

Operator、Prometheus、kube-state-metrics 使用 chart 生成的专用 ServiceAccount 与 RBAC，不给业务 Pod 绑定 cluster-admin。Operator 只管理本次监控命名空间中的监控自定义资源；Prometheus 为发现和读取指标仍需要相应集群只读权限。Kube-state-metrics 仅启用所需资源收集器，不采集 Secret 内容。RBAC 分工可参见 [Operator 官方文档](https://prometheus-operator.dev/docs/platform/rbac/)。

演示 Pod 使用支持 ARM64/AMD64 的官方 `busybox:1.37.0`，非 root、只读根文件系统、禁提权、丢弃全部 capabilities、使用 RuntimeDefault seccomp，且不挂载 Kubernetes 访问令牌。唯一可写卷是 1 MiB 的页面内容目录。演示命名空间启用 restricted Pod Security，并设置小额资源配额。镜像版本与架构来自 [Docker Official Images 的 BusyBox 清单](https://github.com/docker-library/official-images/blob/master/library/busybox)。

## 安装与查看

在目标机的项目目录执行两个安装入口：前者准备 K3s，后者校验并安装 Helm 与固定版本监控 chart。安装器拒绝静默覆盖已有的不同版本组件或配置；重跑前先查看它的检查结果。

```bash
cd /opt/sre-failure-lab
K3S_NODE_NAME=sre-k8s-137 bash scripts/install_k3s.sh
bash scripts/install_k8s_monitoring.sh monitoring/kubernetes/values.yaml
```

`sre-k8s-137` 是本次节点名，新机器可设置自己的名称。以下是 K3s/Helm 已就绪后的监控配置命令，用于理解或后续受控更新，不要与安装器并行执行：

```bash
cd /opt/sre-failure-lab
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update prometheus-community
helm upgrade --install sre-k8s prometheus-community/kube-prometheus-stack \
  --version 93.3.1 --namespace sre-monitoring --create-namespace \
  --values monitoring/kubernetes/values.yaml --rollback-on-failure --wait=watcher --timeout 10m
kubectl apply -f monitoring/kubernetes/demo.yaml
kubectl -n sre-k8s-demo rollout status deployment/demo-web --timeout=120s
```

日常查看：

```bash
kubectl get nodes -o wide
kubectl -n sre-monitoring get pods,pvc,servicemonitor
kubectl -n sre-k8s-demo get deployment,replicaset,pod,service
kubectl -n sre-monitoring get events --sort-by=.lastTimestamp
kubectl -n sre-k8s-demo get events --sort-by=.lastTimestamp
```

## 让现有 Grafana 读取 Kubernetes 指标

Prometheus 保持 ClusterIP，不创建 NodePort、Ingress 或第二套 Grafana。宿主机 `sre-lab-k8s-prometheus.service` 运行 `kubectl port-forward`，把 `sre-monitoring/sre-k8s-prometheus:9090` 转发到本机 Docker 桥网关的 **19090**。

安装入口：

```bash
cd /opt/sre-failure-lab
bash scripts/install_k8s_grafana.sh
systemctl status sre-lab-k8s-prometheus --no-pager
journalctl -u sre-lab-k8s-prometheus -n 50 --no-pager
```

脚本动态取得 Docker 网关，生成 `monitoring/grafana/provisioning/datasources/kubernetes.local.yml`，把数据源 `kubernetes` 指向 `http://该网关:19090`。该本机文件不进入 Git；原来的 Compose Prometheus 数据源继续使用原 UID。脚本重启 Grafana 加载配置，不重启原商品 API。

转发只绑定 Docker 网关，不使用 `0.0.0.0`。Kubernetes 管理 kubeconfig 保留在宿主机，不能挂载给 Grafana。这个方案适合单机实验：选中的 Prometheus Pod 重建后，转发会话可能退出，需要 systemd 重新启动会话。它不是高可用网关或长期生产入口，参见 [Kubernetes port-forward 说明](https://kubernetes.io/docs/tasks/access-application-cluster/port-forward-access-application-cluster/)。

看板的 cluster 标识为当前数据源的逻辑名称 `sre-lab-k3s`；Prometheus 的 externalLabels 不等于本地每条时间序列都带 cluster 标签，因此看板不依赖该标签过滤。命名空间和 Pod 信息来自 kube-state-metrics，CPU/内存来自 kubelet/cAdvisor；节点资源来自 node_exporter。

## Pod 删除恢复验收

演示对象是 `sre-k8s-demo` 中唯一的 `demo-web` Pod。删除后 Deployment/ReplicaSet 控制器发现实际副本少于期望副本，创建一个新 Pod；这属于 Kubernetes 调谐。原来的 `ops.heal` 只控制 Docker Compose 的 Redis 和活动 API，不参与该过程。

项目验收脚本检查指标和抓取目标，并可执行一次受限删除：

```bash
cd /opt/sre-failure-lab
LAB_DOCKER_GATEWAY=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
python3 scripts/accept_k8s.py \
  --prometheus-url "http://${LAB_DOCKER_GATEWAY}:19090" \
  --exercise-recovery --output reports/kubernetes-acceptance.json
```

验收应保留删除前后的 Pod UID、时间及 Ready 状态，同时检查节点、命名空间、Pod、Deployment、CPU、内存等指标实际存在，而不是仅看到 Pod 为 Running。新 Pod 的 UID 必须不同；容器 `restartCount` 未必增加，因为这是创建新 Pod，并非原 Pod 中的进程重启。

一副本演示可能有短暂不可用，不能据此宣称零中断或跨主机高可用。删除演示 Pod 不应修改原 Compose 发布状态或自愈预算。完成后还需检查原商品接口和 Grafana 可用，以及主机剩余资源。

## 操作边界

`systemctl stop sre-lab` 只管理原 Compose 实验室，不代表停止 K3s。Kubernetes 监控、演示工作负载和转发服务有各自生命周期；准备关闭整台虚拟机时，应分别确认这两套服务的状态。不要用 `ops.deploy rollback` 回滚 Helm release，也不要用 Helm 操作原商品的蓝绿槽。

本轮配置可在已有单节点上验证调度、指标和 Pod 自动补齐；节点断电、集群升级、监控数据灾备与多节点故障转移需要另行设计和验收。
