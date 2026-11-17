# 安装前检查与宿主机重启验收

本页定义可重复执行的验收流程。本轮没有连接或部署集群；新增脚本通过本地模拟测试。当前没有 Linux/K3s 环境时，只能生成计划，不能据此宣称安装、监控或重启恢复已通过。

适用范围是一个 Linux ARM64 或 AMD64 节点，K3s `v1.37.2+k3s1`、Helm `v4.3.2`、监控 chart `93.3.1`。监控 release 为 `sre-k8s`，命名空间为 `sre-monitoring`，演示 Deployment 为 `sre-k8s-demo/demo-web`。安装方法见 [Kubernetes 监控说明](kubernetes-monitoring.md)。

## 没有集群时

```bash
python3 scripts/check_k8s_host.py --plan
```

这条命令只输出版本、资源与工具要求，不探测运行它的电脑，也不需要 Kubernetes。报告固定为 `status: not_tested`、`environment_tested: false`。退出码 0 只表示计划成功生成，不是环境验收通过。

## 在新 Linux 主机上检查

安装前在目标主机的项目目录运行：

```bash
mkdir -p reports
python3 scripts/check_k8s_host.py > reports/k8s-host-preflight.json
```

脚本检查：

- Linux、ARM64/AMD64、root、systemd，以及 Python 3.9 或以上。
- 至少 2 个 CPU、当前可用内存至少 2 GiB；`/var/lib` 和 `/usr/local` 所在文件系统各至少有 6 GiB 空余。两路径可能属于同一文件系统，不应将空余量相加。
- 安装所需工具、IPv4 转发状态，以及系统报告的 NTP 同步状态。它不独立测量外部时钟偏差，无法确认同步时会失败。
- TCP 6443、10250、19100、19090 和 UDP 8472 的现有占用；检查所有本机绑定地址，结果偏保守。19090 用于 Grafana 到 Kubernetes Prometheus 的桥接。
- 是否已有 K3s 配置或服务端状态。已有安装应沿用其升级或恢复流程，不能当作全新主机。

检查只读取主机信息和执行 `timedatectl show`，不会安装软件、启停服务、同步时钟、修改防火墙、关闭 Swap、清理磁盘或调整系统参数。Swap 作为观察值保留；项目 K3s 模板使用 `fail-swap-on=false`。

顶层 `status: ready_for_install` 表示本机前置条件满足。镜像仓库可达性、证书、安装过程、集群指标和重启恢复仍未测试。任何关键检查失败或未知时，顶层状态为 `failed`，退出码为 1。

## 重启前采集基线

先完成 K3s、监控、演示 Deployment 和 Grafana 桥接的安装。两项 systemd 服务都必须处于 `active/running`，并且 `UnitFileState=enabled`：

```text
k3s.service
sre-lab-k8s-prometheus.service
```

基线命令在该 Linux 节点本机运行；默认使用 `k3s kubectl` 和 `/etc/rancher/k3s/k3s.yaml`。也可明确选择 `--client kubectl --kubeconfig /实际路径`，但 kubeconfig 必须指向该主机的回环 K3s API `https://127.0.0.1:6443`、`localhost:6443` 或 `[::1]:6443`，不能用另一台机器的集群完成本机重启验收。

```bash
LAB_DOCKER_GATEWAY=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
python3 scripts/accept_k8s_reboot.py before \
  --prometheus-url "http://${LAB_DOCKER_GATEWAY}:19090" \
  --output reports/k8s-reboot-before.json
```

如果桥网关输出不是本机实际 IPv4，请按现有桥接配置填写正确地址。脚本会核实该 IP 属于本机接口，拒绝其他机器的地址、URL 凭据、代理及 HTTP 重定向。

基线必须包含真实且健康的证据：

- 持久 `/etc/machine-id` 的 SHA256、当前内核 `boot_id`、主机名和 uptime。
- 两项 systemd 服务的装载、启用、运行状态和进入活动状态的单调时间。
- `kube-system` 命名空间 UID 作为集群身份；单个 Node 的 UID、Ready/压力状态、版本、架构和 bootID。Node 的 bootID 必须与本机内核一致。
- Operator、kube-state-metrics、demo-web Deployment 的 UID、generation、副本和 rollout 状态，以及实际 Pod Ready、容器运行状态、启动时间与重启计数。
- Prometheus StatefulSet 身份与 rollout 状态，实际 PVC/PV 的 UID、绑定关系与容量信息。
- 本机桥接 URL 的 Prometheus `/-/ready` 返回 HTTP 200；通过本机 Kubernetes API 的 Service proxy 访问演示应用，得到预期页面内容。

成功基线的状态是 **`ready_for_reboot`**，不是 `passed`。失败时保存 `failed` 报告和已取得的部分证据，不产生可用的重启基线。

报告文件以权限 0600 新建；既有文件一律拒绝覆盖。脚本不输出 kubeconfig、令牌、Secret 内容、容器环境变量或完整命令错误输出。

## 操作者手动重启后验收

保存好基线文件后，由操作者在合适的维护时间手动重启宿主机。两个脚本都没有执行 `reboot`、启停服务、删 Pod 或自动修复的能力。重启原主机后，在同一项目目录执行：

```bash
python3 scripts/accept_k8s_reboot.py after \
  --baseline reports/k8s-reboot-before.json \
  --output reports/k8s-reboot-after.json \
  --timeout 180
```

`after` 使用基线中的桥接 URL，并在最长 180 秒观察窗口内等待服务与工作负载恢复。观察超时可设置为 1～600 秒。命令失败会留下新的失败报告；修复后再次执行时，选择一个新的输出文件名，保留之前的失败记录。

只有以下条件全部满足，顶层状态才是 **`passed`**：

1. 当前 machine-id 哈希与基线相同，且内核 boot_id 确实不同。相同 boot_id 会立即失败。
2. 集群 UID、Node UID 保持不变。发现另一集群会立即失败，不等待它替代原集群。
3. 必需 Deployment 和 Prometheus StatefulSet 的 UID、generation、期望副本保持不变；StatefulSet revision 保持不变。重启期间改动工作负载，需要另建基线。
4. 原 PVC、PV UID 和绑定卷名称未改变，卷仍处于正确 Bound 状态。不能用一个新的空卷替代原卷。
5. 服务、Node、rollout、Pod、Prometheus 桥接和演示应用响应重新通过健康检查。

Pod UID 可以变化，因为控制器可能重建 Pod。验收保留 Pod 及容器证据供对照，不把正常的 Pod 重建误判为集群替换。

## 如何解释结果

| 命令 | 成功状态 | 能证明什么 |
| --- | --- | --- |
| `check_k8s_host.py --plan` | `not_tested` | 仅生成计划 |
| `check_k8s_host.py` | `ready_for_install` | 本机前置条件通过 |
| `accept_k8s_reboot.py before` | `ready_for_reboot` | 重启前真实健康基线已记录 |
| `accept_k8s_reboot.py after` | `passed` | 同一身份下观察到新启动及规定的恢复条件 |

缺少 Linux、客户端、集群、服务或合法基线，都会失败，不能产生 `passed`。自动化应读取顶层 `status`，不要将 `--plan` 的退出码 0 或某一个局部检查成功误当成现场验收。

这项验收不证明零中断、跨节点高可用或没有人工干预；它记录恢复后的状态，没有连续测量重启期间的业务可用率。PVC/PV 身份与健康检查也不等于逐字节数据完整性或 Prometheus 历史样本持久性验证。报告是本地普通文件，machine-id 是系统身份标识，不是硬件远程认证；若复制整台虚拟机及其全部身份，需要额外的资产管理证据。

指标内容与抓取目标仍应执行 [监控验收流程](kubernetes-monitoring.md#pod-删除恢复验收)。本地测试使用模拟命令和对象，验证失败判定、身份比较与只读行为，不能替代上述 Linux 现场验收。
