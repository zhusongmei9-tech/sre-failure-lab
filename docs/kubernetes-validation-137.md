# Kubernetes 单节点监控验收：2026-11-17

本次在已有 Compose 实验室的 137 虚拟机上部署可选 Kubernetes 监控；不替换原商品业务运行方式。

## 环境与交付

- Rocky Linux 9.5，ARM64，2 CPU，约 4.5 GB 内存。
- K3s `v1.37.2+k3s1`，节点 `sre-k8s-137` 为 Ready。
- Helm `v4.3.2`，kube-prometheus-stack `93.3.1`，release `sre-k8s`，namespace `sre-monitoring`。
- Prometheus、Operator、kube-state-metrics 和 node_exporter 均 Ready；Prometheus 的 1 GiB PVC 已 Bound。
- 复用原 Grafana，新增数据源 `kubernetes` 和看板 `sre-lab-kubernetes`。
- `k3s` 与 `sre-lab-k8s-prometheus` 已设为开机启动；转发仅监听 Docker 网关的 19090。

## 实际验证

| 检查 | 结果 |
| --- | --- |
| Prometheus 抓取 | 8 个目标全部 UP：API Server、KSM、kubelet 两个端点、Operator、node_exporter、Prometheus 和 reloader |
| Grafana 数据源 | 通过 Grafana API 健康检查，能够从 Grafana 容器查询 Kubernetes Prometheus |
| 看板查询 | 15 个面板、16 条表达式，在全部命名空间、sre-monitoring、sre-k8s-demo 三种范围共 48 次查询成功 |
| 核心指标 | 节点 Ready、CPU、内存、磁盘，Pod CPU、内存、重启，Deployment 可用副本均有实际数据 |
| 空数据含义 | 当前无 Waiting 容器；演示 namespace 无 PVC，因此对应查询返回空，不补零伪装正常 |
| 原项目 | 商品查询与 Redis 就绪正常，原 Grafana 正常，sre-lab 保持 active |
| 主机资源快照 | 可用内存约 2.2 GB，根分区可用约 9.3 GB；仅代表验收时观测值 |

## Pod 删除恢复

通过 `scripts/accept_k8s.py --exercise-recovery` 删除专用 namespace 中的演示 Pod。

- 删除前：`demo-web-7fd57c5888-qjpjd`，UID `98ef910e-b97a-4976-9f05-65a215392a99`。
- 新建后：`demo-web-7fd57c5888-r576h`，UID `c3540bb1-5dec-48d5-aa28-d1cb1afa61b3`。
- 从发起删除到新 Pod Ready：**5.88 秒**。
- 恢复机制是 Kubernetes Deployment/ReplicaSet 调谐，未由原 `ops.heal` 执行。

这是一轮受控实验的观测值，不是恢复时间保证。一副本演示可能短暂不可用，不能解释为零中断。

## 安装时发现并修复的问题

- 137 时钟落后可信 NTP 源约 17 小时。执行 `chronyc makestep` 校准并同步硬件时钟，随后重启新安装的 K3s 刷新组件会话。部分最早创建资源的 AGE 因此偏大，不能用它代表本次实际运行时长。
- 安装器修复 Helm 4 不支持 `helm list --all` 的兼容问题，以及 chart 顶层版本解析误匹配依赖版本的问题。
- 镜像首次拉取遇到临时 DNS 解析失败，重试后成功；验收时没有 ImagePullBackOff 或未就绪的监控 Pod。

## 证据与范围

完整本地记录位于工作文件夹 `reports/kubernetes-137-2026-11-17/`，包括 `kubernetes-acceptance-2026-11-17.json` 与 `kubernetes-dashboard-validation.json`。目标机项目的 `reports/` 也保留原始输出；这些报告不含凭据。

另外完成 Shell 语法、Python lint/编译、YAML 解析、真实 Helm 4 命令参数、Helm lint 和模板渲染检查。

本轮未进行整台虚拟机重启、AMD64 实机安装、跨节点接管或浏览器截图验收；看板验证覆盖 Grafana 载入与真实数据源查询。告警规则已加载，但没有配置外部通知接收端。
