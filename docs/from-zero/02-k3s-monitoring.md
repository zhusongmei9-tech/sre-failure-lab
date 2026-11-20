# 第二章 · 单节点 K3s 与 Kubernetes 监控

完成[第一章](01-linux-compose.md)后，在运行真实 Compose 栈的同一台虚机上继续。本章在 K3s 里运行演示网页和 Prometheus，并让已有 Grafana 从集群读取数据。全部 Linux 命令在虚机的 **root Bash** 中执行。固定版本由项目脚本校验，`k3s` 使用自己的 containerd，Docker 继续运行第一章的容器。

## 1. 确认这台主机适合首次安装

```bash
cd /opt/sre-failure-lab
systemctl is-active sre-lab
curl --noproxy '*' -fsS http://127.0.0.1:18000/health/ready
mkdir -p reports
python3 scripts/check_k8s_host.py > reports/k3s-host-preflight.json
python3 - <<'PY'
import json
from pathlib import Path
r = json.loads(Path("reports/k3s-host-preflight.json").read_text())
print(r["status"])
for c in r["checks"]:
    if c["status"] != "passed":
        print(c["name"], c["observed"], c["requirement"])
PY
```

看到 `active`、商品服务报告 `redis=ok` 和 `ready_for_install` 后继续。如果虚机已有 K3s，预检会报告端口占用和已有集群状态。对已经完成本章的机器，可直接从第 4 节核对现状。

| 首次安装前的项目 | 条件及检查办法 |
| --- | --- |
| 系统与架构 | Linux、systemd、Python 3.9+、ARM64 或 AMD64，至少 2 个 CPU。137 的历史实机环境为 Rocky Linux 9.5 ARM64。其他发行版先检查内核、cgroup 和 SELinux 策略包能否支持固定版本。[K3s 主机要求](https://docs.k3s.io/installation/requirements) |
| **可用**内存与磁盘 | 启动 K3s 时 `/proc/meminfo` 中的 `MemAvailable` 至少 2 GiB，`/var/lib` 和 `/usr/local` 各自所在文件系统至少有 6 GiB 空间。启动监控前仍需 768 MiB 可用内存、`/var/lib` 剩余 4 GiB。先用 `free -h`、`df -h /var/lib /usr/local` 检查。|
| 时钟与网络 | `timedatectl show -p NTPSynchronized --value` 应为 `yes`。`sysctl -n net.ipv4.ip_forward` 应为 `1`。用 `ip -4 route` 和 `docker network inspect bridge` 查看主机与 Docker 网段，确认 Pod 和 Service 网段与实验网、VPN、Docker 网络互不重叠。 |
| 工具与出网 | 预检核对 curl、sha256sum、python3、systemctl、install、timeout、flock、tar、ip。脚本另使用 Bash、sed、awk 和 grep。初装需要访问 GitHub、raw.githubusercontent.com、get.helm.sh，以及 K3s、监控组件和 BusyBox 的镜像仓库。启用 SELinux 的 Rocky 主机还需要可用的策略软件包源。 |
| 本机端口 | TCP 6443 为 API Server，10250 为 kubelet，19100 为本章的 node exporter，19090 为 Grafana 使用的 Prometheus 转发；UDP 8472 为 Flannel VXLAN。首次预检检查端口是否被占用。单节点且在本机运行 kubectl 时，防火墙保持默认管理范围。 |

## 2. 安装集群并核对配置

在新机器上可以直接采用下面的节点名。如果修改，后面的示例通过 Kubernetes 自动找到节点。

```bash
cd /opt/sre-failure-lab
K3S_NODE_NAME=sre-k8s-node bash scripts/install_k3s.sh
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
k3s --version
k3s kubectl get --raw=/readyz
k3s kubectl get nodes -o wide
k3s kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s
k3s kubectl top node
```

预期 K3s 为 `v1.37.2+k3s1`、API 返回 `ok`、一个节点显示 `Ready`。`top node` 初次启动可能需要等待一次 Metrics Server 采样；有了 CPU 和内存读数再进入商品 HPA 实验。

安装脚本下载本机架构对应的二进制与官方安装脚本，核对仓库固定的 SHA-256 后写入 `/usr/local/bin/k3s`。配置从仓库模板写入初次创建的 `/etc/rancher/k3s/config.yaml`。已有同版本服务的机器会走启动和验收流程，重跑不会把模板变化同步到现场配置。[安装脚本](../../scripts/install_k3s.sh) · [完整 K3s 模板](../../monitoring/kubernetes/k3s-config.yaml.example)

| 配置项 | 项目默认值与用途 |
| --- | --- |
| `K3S_NODE_NAME`、`K3S_BOOTSTRAP_DIR` | 节点名默认 `sre-k8s-node`；下载缓存默认 `runtime/kubernetes/bootstrap/`，重试复用已核对摘要的文件。 |
| `data-dir`、`write-kubeconfig-mode`、`secrets-encryption` | 数据在 `/var/lib/rancher/k3s`；kubeconfig 权限 `0600`，由 root 使用；Kubernetes Secret 启用静态加密。 |
| `cluster-cidr`、`service-cidr`、`flannel-backend` | Pod 用 `10.42.0.0/16`，Service 用 `10.43.0.0/16`，Flannel 使用 `vxlan`。首次安装如有网段冲突，先规划并修改模板，再部署；启用 firewalld 的主机还需核对自定义网段的转发规则。脚本仅为模板默认网段自动设置 trusted 来源。启用 firewalld 时，用 `firewall-cmd --zone=trusted --list-sources` 检查当前规则，再加 `--permanent` 检查重启后使用的规则。 |
| `K3S_API_ALLOWED_CIDR` | 默认留空，管理操作从虚机本地或 SSH 进行。如果确实要从其他主机直连 API，安装时填写实际受信 IPv4 源网段；脚本在启用 firewalld 时为该网段配置 TCP 6443。 |
| `disable`、SELinux | 关闭 Traefik 和 ServiceLB，实验使用 ClusterIP。检测到主机启用 SELinux 时，脚本为新配置加上 `selinux: true`，由上游安装器处理策略包。 |
| `kubelet-arg` | `fail-swap-on=false` 允许宿主机保留 swap；`max-pods=60`，容器日志每份 `5Mi`、保留 2 份。镜像 GC 使用 75%/65% 高低水位。 |
| 资源预留与驱逐 | 为系统预留 `200m CPU / 768Mi`，为 Kubernetes 预留 `100m / 384Mi`。可用内存低于 `300Mi`、节点或镜像文件系统剩余低于 15%、节点文件系统 inode 可用低于 5% 可触发驱逐。 |

## 3. 安装监控及测试网页

```bash
cd /opt/sre-failure-lab
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
bash scripts/install_k8s_monitoring.sh monitoring/kubernetes/values.yaml
helm version --template '{{.Version}}'
helm -n sre-monitoring status sre-k8s
k3s kubectl -n sre-monitoring get pod,svc,pvc
k3s kubectl apply -f monitoring/kubernetes/demo.yaml
k3s kubectl -n sre-k8s-demo rollout status deployment/demo-web --timeout=120s
bash scripts/check_demo_service.sh
```

安装器固定 Helm `v4.3.2` 和 kube-prometheus-stack chart `93.3.1`，首次安装会自动下载并校验；若 PATH 已有其他版本 Helm，先核对 `command -v helm` 和 `helm version`。chart 安装到命名空间 `sre-monitoring`，release 名为 `sre-k8s`。验收时 Helm 状态应为 `deployed`，Prometheus、Operator、kube-state-metrics 和 node exporter Pod 都应 Ready，Prometheus 的 PVC 为 `Bound`。最后一条命令从独立探针 Pod 检查测试网页，预期 `dns=ok`、`http=200`、`body=ok`。[安装逻辑](../../scripts/install_k8s_monitoring.sh) · [Helm 完整配置](../../monitoring/kubernetes/values.yaml)

| 配置组 | 实验采用的设置与理由 |
| --- | --- |
| 组件开关 | 启用 API Server、kubelet/cAdvisor、kube-state-metrics、Linux node exporter；关闭 chart 自带 Grafana、Alertmanager、Windows 监控及内置组件的独立抓取。CoreDNS 仍提供集群 DNS，values 中的开关只控制监控。默认规则相应裁减。 |
| Operator 与发现范围 | Operator 监听 release 命名空间，admission webhook 和对应 TLS 关闭。ServiceMonitor、PodMonitor、PrometheusRule 的命名空间选择器均指向 `sre-monitoring`；项目新建的 ServiceMonitor 应带 `release: sre-k8s`，再用其自身 `namespaceSelector` 选择业务命名空间。 |
| 时间与保留 | Prometheus 1 副本、1 分片；每 30 秒抓取及评估，抓取超时 10 秒。数据保留 `1d`、`256MB` 双上限，启用 WAL 压缩。逻辑标签 `cluster=sre-lab-k3s`。 |
| 持久卷与入口 | Prometheus 使用 `local-path`、`ReadWriteOnce`、`1Gi` PVC；磁盘跟随单节点。Prometheus 使用 ClusterIP、关闭 Ingress。node exporter 使用 19100，与原 Compose 实验独立。 |
| requests → limits（CPU / 内存） | Prometheus 从 `100m / 256Mi` 到 `500m / 512Mi`；Operator 从 `25m / 64Mi` 到 `200m / 192Mi`；config reloader 从 `10m / 32Mi` 到 `100m / 64Mi`；kube-state-metrics 从 `25m / 48Mi` 到 `100m / 128Mi`；node exporter 从 `10m / 32Mi` 到 `100m / 96Mi`。 |
| 测试网页 | `sre-k8s-demo` 中的 `demo-web` 默认 1 副本，镜像 `busybox:1.37.0`，ClusterIP 端口 8080，HTTP 探针路径 `/`。命名空间启用 restricted Pod Security，最多 3 个 Pod；总 requests 为 `100m / 64Mi`，limits 为 `300m / 96Mi`。这套网页供网络和采集验收，商品实验另用 `sre-app-lab`。[网页清单](../../monitoring/kubernetes/demo.yaml) |

## 4. 连接已有 Grafana 并核对真实时间序列

```bash
cd /opt/sre-failure-lab
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
bash scripts/install_k8s_grafana.sh
systemctl is-active sre-lab-k8s-prometheus.service
GATEWAY=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
curl --noproxy '*' -fsS "http://${GATEWAY}:19090/-/ready"
no_proxy="127.0.0.1,localhost,${GATEWAY}" python3 scripts/accept_k8s.py \
  --prometheus-url "http://${GATEWAY}:19090" --output reports/k8s-monitoring.json
```

这里要求第一章已经生成项目根目录的 `.env` 与 `runtime/images.env`，并部署了真实 Compose Grafana。安装器写入本机数据源 `monitoring/grafana/provisioning/datasources/kubernetes.local.yml` 和 `/etc/sre-lab/kubernetes-monitoring.env`，启用 `sre-lab-k8s-prometheus.service`，随后重启 Grafana 加载数据源。systemd 在 Docker 桥网关地址的 19090 端口转发到 `service/sre-k8s-prometheus:9090`；UID 为 `kubernetes` 的数据源通过此路径查询集群。打开第一章的 Grafana 地址，进入 **Dashboards → SRE Lab**，选择 UID 为 `sre-lab-kubernetes` 的看板。[Grafana 接入脚本](../../scripts/install_k8s_grafana.sh) · [systemd 转发单元](../../ops/sre-lab-k8s-prometheus.service)

成功时验收报告显示抓取目标均为 `up`，节点、Pod、Deployment 共 **8 类指标**都有有限数值。新集群刚启动时 `rate(...[2m])` 还需要抓取样本，等约两分钟后重跑最后一条验收命令即可。具备这八类数据后，进入[第三章，安装真实商品服务并验证发布与 HPA](03-product-rollout-hpa.md)。

## 按现象排查

| 现象 | 在虚机执行与判断 |
| --- | --- |
| kubectl 或 Helm 报权限错误、连到 localhost:8080 | 使用 `sudo -i` 进入 root shell，再运行 `export KUBECONFIG=/etc/rancher/k3s/k3s.yaml` 和 `k3s kubectl config current-context`。保留 kubeconfig 的 0600 权限。 |
| 能读旧 Pod，节点一直没有新心跳 | 查 `journalctl -b -u k3s -n 80 --no-pager`、`date -u`、`timedatectl status`、`k3s certificate check --output table`。用 `k3s kubectl -n kube-node-lease get lease -o custom-columns=NAME:.metadata.name,RENEW:.spec.renewTime` 连续查看续约时间。137 曾在系统时间恢复同步后重启 K3s 才恢复续约；涉及重启时先留演练窗口，再验证 `top node` 与集群内 HTTP。[现场记录](../ansible-k8s-validation-137.md) |
| 监控 Pod Pending 或镜像获取失败 | `k3s kubectl -n sre-monitoring get events --sort-by=.lastTimestamp`，再对出错 Pod 运行 `k3s kubectl -n sre-monitoring describe pod POD名称`。按事件区分 CPU/内存不足、PVC 调度和访问镜像仓库失败，再查 `k3s kubectl get storageclass`、`df -h /var/lib`。 |
| 网页 Ready，探针仍失败 | `k3s kubectl -n sre-k8s-demo get endpointslices -l kubernetes.io/service-name=demo-web` 应显示后端；`dns=failed` 时查看 `k3s kubectl -n kube-system get deployment/coredns`，DNS 正常但 HTTP 失败时检查 Service 选择器、端口和就绪端点。[探针排查与故障演练](../k8s-service-smoke.md) |
| Grafana 显示 No data | 先验证上方 `/-/ready`。若不可达，查 `systemctl status sre-lab-k8s-prometheus --no-pager`、`journalctl -u sre-lab-k8s-prometheus -n 40 --no-pager`。若可达，在 Grafana Explore 选择 **Kubernetes** 数据源查询 `up`，再检查时间范围及 ServiceMonitor 标签。 |

按需扩展可参考[告警与通知验收](../kubernetes-alerting.md)、[开机恢复验收](../kubernetes-installation-acceptance.md)和[GitOps 演练](../k8s-gitops.md)。这三个分支可在基本的集群访问、业务和监控验收成功后分别进行。
