# 可选 Kubernetes 告警闭环

本模块在固定的 `kube-prometheus-stack 93.3.1` 基础监控上增加 Alertmanager、四类指标告警和一个内网 webhook 接收器。链路为：**指标 → Prometheus 规则 → Alertmanager 路由 → 接收器 firing / resolved 事件**。接收器不发邮件或聊天消息，也不执行恢复命令。

**本轮没有连接任何集群。** 已完成的本地规则、HTTP 接收器和受控执行器检查，不等于集群内通知链路已经实测。合成演示用于证明告警传递，不会制造磁盘满、内存耗尽或删除业务 Pod。

## 文件与默认行为

文件均位于 `monitoring/kubernetes/alerting/`：

| 文件 | 用途 |
| --- | --- |
| `values.yaml` | 可选 Helm 覆盖层，启用小资源 Alertmanager |
| `rules.json` | 四类指标规则的唯一来源，JSON 是合法的 YAML 子集 |
| `render_rules.py` | 将规则包装成带 `release=sre-k8s` 标签的 PrometheusRule |
| `rules.test.yml` | promtool 的持续故障、短暂尖峰、恢复及无数据测试 |
| `synthetic.rules.json` | 显式演示才使用的合成规则，不随默认渲染安装 |
| `receiver.py`、`receiver.yaml` | 标准库接收器及 Deployment、ClusterIP、ServiceMonitor、NetworkPolicy |

基础配置 `monitoring/kubernetes/values.yaml` 保持原状，默认仍不部署 Alertmanager。覆盖层只把 `module="sre-lab-optional"` 的告警路由到本模块接收器；其他默认规则继续可在 Prometheus 中查看，不混入实验接收记录。

Alertmanager 分组等待 5 秒、分组更新间隔 10 秒、重复间隔 30 分钟，并启用 `send_resolved`。每次 webhook 最多 20 个告警。配置遵循 [Alertmanager webhook 官方格式](https://prometheus.io/docs/alerting/latest/configuration/#webhook_config)。

## 有集群后安装

前提是已完成 [基础 Kubernetes 监控安装](kubernetes-monitoring.md)，存在 `sre-monitoring` 命名空间、`sre-k8s` Helm release 和 PrometheusRule/ServiceMonitor CRD。以下步骤暂为待执行流程，不能在没有集群时填报成功。

先确认 kubeconfig 当前上下文确实是要操作的实验集群。将下面的变量填写为该上下文名称；验收脚本会拒绝与当前上下文不一致的值。

```bash
LAB_CONTEXT='你的实验集群上下文'
kubectl config current-context
kubectl --context="$LAB_CONTEXT" get nodes
```

在项目根目录安装内网接收器和指标规则：

```bash
kubectl --context="$LAB_CONTEXT" -n sre-monitoring create configmap sre-k8s-alert-receiver-code \
  --from-file=receiver.py=monitoring/kubernetes/alerting/receiver.py \
  --dry-run=client -o yaml | kubectl --context="$LAB_CONTEXT" apply -f -
kubectl --context="$LAB_CONTEXT" apply -f monitoring/kubernetes/alerting/receiver.yaml
kubectl --context="$LAB_CONTEXT" -n sre-monitoring rollout status deployment/sre-k8s-alert-receiver --timeout=120s
python3 monitoring/kubernetes/alerting/render_rules.py | kubectl --context="$LAB_CONTEXT" apply -f -
```

再用两个 values 文件按顺序更新同一个 Helm release：

```bash
helm upgrade --install sre-k8s prometheus-community/kube-prometheus-stack \
  --version 93.3.1 --namespace sre-monitoring --kube-context "$LAB_CONTEXT" \
  -f monitoring/kubernetes/values.yaml \
  -f monitoring/kubernetes/alerting/values.yaml \
  --rollback-on-failure --wait=watcher --timeout 10m
kubectl --context="$LAB_CONTEXT" -n sre-monitoring rollout status \
  statefulset/alertmanager-sre-k8s-alertmanager --timeout=180s
```

最后检查 Prometheus 目标、规则加载情况和 Alertmanager 配置状态。更新 `receiver.py` 的 ConfigMap 后，还需显式重启接收器 Deployment 才会运行新代码；该重启会清空内存事件，不应在验收过程中进行。

## 资源与访问限制

Alertmanager 使用 1 个副本，request 为 20m CPU / 32Mi 内存，limit 为 100m / 128Mi，临时数据卷上限 128Mi；继承基础配置中的重载器资源设置。接收器 request 为 5m / 16Mi，limit 为 100m / 64Mi，使用与项目 Dockerfile 一致的官方多架构 `python:3.12-slim`。此基础标签未锁定镜像摘要，不应解释为完全可复现的生产制品。

接收器非 root、根文件系统只读、禁提权、丢弃全部 capabilities，不挂载 ServiceAccount 令牌，也不需要 Kubernetes API 权限。它不持有外发凭据。NetworkPolicy 只允许同命名空间的 Prometheus/Alertmanager Pod 访问 8080，并禁止接收器出站；需使用支持 NetworkPolicy 的 CNI 才能实际执行这些网络限制。

接收器是无鉴权的内部实验端点，不创建 Ingress 或 NodePort。排查时通过仅绑定 `127.0.0.1` 的 port-forward 访问。它只保存最近 **200 条内存事件**，最大请求体 **64 KiB**，只保留限定字段，每个字符串最多 256 字符。Pod 重启会丢失记录，旧事件满额会被淘汰；它不是持久告警数据库。

接口如下：

- `POST /alerts`：接收 Alertmanager webhook，返回接受条数，不执行其他操作。
- `GET /events?after=序号&run_id=运行ID`：查询事件及接收器实例 ID、游标和淘汰计数。
- `GET /healthz`：进程健康及实例 ID。
- `GET /metrics`：接收次数、事件淘汰次数及最近接收时间，不把 run_id 或告警正文变成指标标签。

## 显式合成演示

无集群时可以先看计划；下面的命令不会读 kubeconfig、执行 kubectl 或访问网络。输出明确标记 `status=not_tested`、`cluster_accessed=false` 和 `performed=false`，不能把计划输出当作验收成功：

```bash
python3 scripts/accept_k8s_alerts.py --plan
```

有集群且模块已安装后，在两个终端分别保持以下转发：

```bash
kubectl --context="$LAB_CONTEXT" -n sre-monitoring port-forward \
  --address=127.0.0.1 service/sre-k8s-prometheus 19091:9090
```

```bash
kubectl --context="$LAB_CONTEXT" -n sre-monitoring port-forward \
  --address=127.0.0.1 service/sre-k8s-alert-receiver 19093:8080
```

在第三个终端显式执行演示：

```bash
python3 scripts/accept_k8s_alerts.py --exercise-synthetic \
  --context "$LAB_CONTEXT" \
  --prometheus-url http://127.0.0.1:19091 \
  --receiver-url http://127.0.0.1:19093 \
  --output reports/kubernetes-alerts-acceptance.json
```

脚本固定操作 `sre-monitoring`，为本次生成唯一 run_id 与受管理的 PrometheusRule。它分别检查 Prometheus 的 `ALERTS` firing 状态和接收器同 run_id 的 firing 事件，然后将规则表达式改为 false，再检查 Prometheus 不再 firing 且接收器收到 resolved。不会通过直接 POST 接收器来冒充全链路成功。

每阶段默认最多等待 180 秒；HTTP 和 kubectl 调用也有超时。HTTP 客户端只允许显式 localhost 端口，禁用环境代理和重定向。接收器重启、事件被淘汰、只收到一侧证据或等待超时均判为失败。结束时使用 UID 前置条件删除自己创建的规则，防止删到同名替换对象；修改也包含 UID 和管理标签校验。CLI 的 raw DELETE 能力见 [kubectl 文档](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_delete/)。

Ctrl-C/SIGTERM 会尝试清理；强制终止、断电或集群失联仍可能留下规则。报告中 `cleanup` 不是 `deleted_with_uid_precondition` 时，应按 `rule_name`、`rule_uid` 和 run_id 检查遗留对象，确认归属后再处理。不要按模糊名称批量删除所有 PrometheusRule。

## 规则与 runbook

阈值是本项目的起点，应结合机器容量调整；不触发告警不等于健康。指标缺失时这些规则可能没有结果，应同时检查基础 `up` / TargetDown 和采集器状态。

<a id="node-memory"></a>
### 节点内存不足

`SRENodeMemoryPressure`：可用内存低于 15%，持续 2 分钟。先看 MemAvailable、Pod request/limit、OOM 和节点压力事件，再定位高占用工作负载。优先停止实验流量或收缩实验负载，不靠反复重启所有容器处理容量问题。内存回到阈值以上并收到 resolved 后再记录恢复。

<a id="node-disk"></a>
### 节点磁盘空间不足

`SRENodeDiskSpaceLow`：可写的非 tmpfs/overlay/squashfs 文件系统可用空间低于 15%，持续 2 分钟。检查具体挂载点、容器日志、镜像缓存、Prometheus WAL 和本地卷增长；先识别占用与保留要求，不直接删运行中的卷或填满根分区做演示。只读文件系统不由这条容量规则覆盖，应另外调查挂载故障。

<a id="pod-restarts"></a>
### Pod 反复重启

`SREPodRepeatedRestarts`：实验及监控命名空间内，同 Pod 各容器在 5 分钟内合计重启至少 3 次，并持续满足 1 分钟。查看 `describe pod`、容器上次退出原因、`logs --previous`、OOM 与探针设置。它观测容器重启，不把 Deployment 新建不同 UID 的 Pod 当成 restartCount 增长。

<a id="demo-unavailable"></a>
### 演示 Deployment 副本不足

`SREDemoDeploymentUnavailable`：`sre-k8s-demo/demo-web` 期望副本大于零，但可用副本少于期望，持续 1 分钟。检查 Pod 调度、镜像拉取、Ready 探针和 ResourceQuota；不要扩大到删除其他命名空间对象。主动缩容到零不会触发这条规则；Deployment 整体缺失也需要单独检查，不能当成可用。

<a id="synthetic"></a>
### 合成告警

`SRELabSynthetic` 只在显式演示时创建，等待 30 秒后 firing。它不表示真实业务故障。核对 run_id、Prometheus 状态、receiver 内存事件和清理结果；有 firing 无 resolved 时检查规则是否已变为 false、Alertmanager 配置是否生效、接收器是否重启或不可达。不要为制造这条告警消耗主机内存或磁盘。

## 撤销可选模块

先结束正在运行的合成验收，保存需要的事件证据，再恢复原基础 values。这里使用 `--reset-values`，避免 Helm 继续继承本次 Alertmanager 开关；若你另有自定义覆盖层，应同时保留它们。

```bash
helm upgrade sre-k8s prometheus-community/kube-prometheus-stack \
  --version 93.3.1 --namespace sre-monitoring --kube-context "$LAB_CONTEXT" \
  --reset-values -f monitoring/kubernetes/values.yaml \
  --rollback-on-failure --wait=watcher --timeout 10m
kubectl --context="$LAB_CONTEXT" -n sre-monitoring delete prometheusrule sre-k8s-optional-alerts --ignore-not-found
kubectl --context="$LAB_CONTEXT" delete -f monitoring/kubernetes/alerting/receiver.yaml --ignore-not-found
kubectl --context="$LAB_CONTEXT" -n sre-monitoring delete configmap sre-k8s-alert-receiver-code --ignore-not-found
```

这不会卸载基础监控 release、删除原 Prometheus PVC 或修改 Compose 自愈与部署状态。接收器事件及 Alertmanager 临时状态不会保留。

## 本地验证与尚待实测

```bash
promtool check rules monitoring/kubernetes/alerting/rules.json monitoring/kubernetes/alerting/synthetic.rules.json
promtool test rules monitoring/kubernetes/alerting/rules.test.yml
uv run --locked pytest -q tests/test_k8s_alert_receiver.py
```

规则测试覆盖 10 个场景，包括持续故障、短暂尖峰、恢复、只读/临时文件系统排除、counter reset、其他命名空间、主动零副本及无指标。测试格式见 [Prometheus 官方规则测试说明](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/)。接收器测试使用本机 HTTP；验收脚本的测试使用受控 kubectl/Prometheus 响应，验证 context、run_id、双侧证据和 UID 清理。

真实集群中的调度、NetworkPolicy 执行、Prometheus → Alertmanager → receiver firing/resolved，以及重启后的行为仍需未来有集群时另行验收。历史基础监控的 ARM64 验收记录不覆盖本可选模块。
