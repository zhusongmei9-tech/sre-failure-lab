# 十分钟 Kubernetes 演示：计划、删除一个 Pod、记录恢复

这份演示针对独立实验集群中的 `sre-k8s-demo/demo-web`，不操作原 Compose 商品业务、监控工作负载或 PVC。本轮没有连接 Kubernetes 集群：已完成本地保护逻辑与 HTTP 客户端测试，真实执行结果仍为 **`not_tested`**。历史 137 的恢复记录不能替代这个新脚本的现场验收。

## 没有集群也能运行

```bash
python3 scripts/demo_k8s.py
python3 scripts/demo_k8s.py --plan --output reports/kubernetes-demo-plan.json
```

默认模式只输出计划，不调用 kubectl、不发 HTTP 请求、不删除资源。JSON 明确写入 `status: not_tested`、`cluster_accessed: false`、`recovery_tested: false`。`--output` 拒绝覆盖已有文件；重复演示请换文件名，或省略这个参数。

## 十分钟安排

| 时间 | 操作 | 要观察的证据 |
| --- | --- | --- |
| 0–2 分钟 | 阅读计划，确认独立实验 context | 未执行时保持 `not_tested` |
| 2–4 分钟 | 明确应用演示 YAML，等待部署稳定 | 一个 Ready Pod，完整管理标签和 owner 链 |
| 4–5 分钟 | 建立本机 HTTP 转发 | 返回专用演示正文 |
| 5–8 分钟 | 显式执行一次删除和观察 | 原 UID、删除前置条件、新 UID 与 Ready 时间 |
| 8–10 分钟 | 读 JSON，对照请求与恢复阶段 | Pod 恢复、HTTP 路径恢复、错误数与 P95 分开看 |

这不是十分钟内安装完整集群的承诺。真实模式需要 Python 3.9+、kubectl、已有实验集群和正确 kubeconfig；权限需允许读取 namespace、Deployment、ReplicaSet、Pod，并且仅在演示 namespace 删除 Pod。不需要给脚本配置新令牌，也不要把 kubeconfig 放进 Git。

## 有独立实验集群后再执行

先确认 `kubectl config current-context`，将下面的示例值替换为你明确选中的当前实验 context。脚本不会替你切换 context。不要与发布、扩缩容、`kubectl apply` 或另一场演示同时运行。

```bash
kubectl config current-context
DEMO_CONTEXT='my-disposable-lab'
```

仅在新建演示 namespace，或已确认属于本项目的旧演示 namespace 上应用 YAML。不要给陌生工作负载添加管理标签来绕过脚本检查。

```bash
kubectl --context "$DEMO_CONTEXT" apply -f monitoring/kubernetes/demo.yaml
kubectl --context "$DEMO_CONTEXT" -n sre-k8s-demo rollout status deployment/demo-web --timeout=120s
kubectl --context "$DEMO_CONTEXT" -n sre-k8s-demo get pods --show-labels
```

本次 YAML 在 Namespace、Deployment 和 Pod 模板上加入 `app.kubernetes.io/part-of=sre-failure-lab`、`app.kubernetes.io/managed-by=sre-failure-lab`、`sre-lab-role=demo`。Deployment 的既有 selector 保持不变；更新 Pod 模板会发生一次正常滚动更新，需等它结束再演示。

在终端 A 运行只绑定回环地址的转发。为了让删除后能够继续观测，这个独立循环会重新建立转发；结束时用 Ctrl+C 停止它。

```bash
DEMO_CONTEXT='my-disposable-lab'
while true; do
  kubectl --context "$DEMO_CONTEXT" -n sre-k8s-demo port-forward \
    --address=127.0.0.1 service/demo-web 18080:8080
  sleep 1
done
```

在终端 B 确认正文后，明确启用真实执行：

```bash
curl --noproxy '*' -fsS http://127.0.0.1:18080/
# 预期正文：SRE Kubernetes demo is ready
python3 scripts/demo_k8s.py --execute --context 'my-disposable-lab' \
  --url http://127.0.0.1:18080/ --output reports/kubernetes-demo-run-01.json
```

URL 必须是带显式端口的 HTTP 回环地址和根路径；不接受账号密码、query、fragment 或非回环地址。客户端不使用环境代理，不跟随 HTTP 重定向。仅 HTTP 200 还不够，正文也必须匹配演示页面。

## 执行保护和计量

执行前和删除前均检查当前 context。每条集群命令还显式固定 `--context`。脚本核验 Namespace 管理标签与 UID、Deployment 的名称/UID/单副本/稳定状态、ReplicaSet 的 Deployment owner，以及 Pod 的 ReplicaSet owner、UID、标签和 Ready。namespace 内存在额外 Pod、并行滚动更新或任何身份变化时，拒绝删除。

删除请求固定到已核验的一个 Pod，并携带 UID 与 resourceVersion 前置条件。对象已被替换或更新时，API 应拒绝该请求；脚本不会转而删除一个新 Pod。Kubernetes 的 ownerReferences 使用 UID 表明资源归属；删除前置条件不满足时返回冲突。[资源归属说明](https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/)、[DeleteOptions](https://kubernetes.io/docs/reference/kubernetes-api/definitions/delete-options-v1-meta/)。

流量为单线程、目标 2 RPS，先观察 10 秒基线，再删除并等待新 UID Ready，随后等待 HTTP 路径恢复并观察 10 秒。请求较慢时实际吞吐会降低，不补发积压请求。恢复等待最多 120 秒，HTTP 路径另等最多 20 秒，总执行预算 240 秒；kubectl 和 HTTP 请求均有超时。结束会停止流量并有界等待线程，未完成请求或线程异常会使报告失败。

基线和最后稳定窗口必须各有至少五个完整的成功请求且无错误。跨阶段完成的请求仍计入全程统计，但不作为稳定窗口通过的证据。恢复过程允许观测到错误；零错误也只表示本轮短采样没有捕获中断。

`port-forward service/...` 实际选择一个 Pod；该 Pod 终止后原会话结束，需要重新执行转发。这意味着 HTTP 失败和恢复时间包含客户端转发重连影响，不能当作集群 Service 的纯可用性或生产 SLO。[kubectl port-forward 说明](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_port-forward/)。

## 如何读报告

| 字段 | 含义 |
| --- | --- |
| `status` | `not_tested` 为计划；`passed` 为本轮全部门槛通过；`failed` 为拒绝执行、超时或其他失败 |
| `baseline` | 原 Namespace、Deployment、ReplicaSet、Pod 身份，以及原 Pod Ready 证据 |
| `deletion.attempted / accepted` | 是否尝试删除，以及是否收到 API 成功响应；超时不能证明删除没有发生 |
| `recovery.tested / verified` | 是否收到删除成功响应，以及是否实际观察到同一控制器的新 UID Ready |
| `recovery.ready_after_seconds` | 从发起删除到客户端观察到新 Pod Ready，包含 API 和轮询延迟 |
| `http_recovery.verified` | 新 Pod Ready 后，HTTP 路径是否重新通过连续请求和最终稳定窗口 |
| `traffic.requests / errors / p95_ms` | 已完成请求总数、错误数和全部请求耗时的 nearest-rank P95；错误请求也纳入 |
| `traffic.successful_p95_ms` | 仅成功请求的 P95；没有样本为 null，不伪造为零 |
| `traffic.dispatched_requests / unfinished_requests` | 已发起请求及尚无结果的请求数量；用于识别中断和线程退出异常 |
| `phases` / `requests` | 基线、Pod 恢复、转发恢复、稳定窗口统计，以及逐请求原始证据 |

新 Pod Ready、但 port-forward 未恢复时，`recovery.verified` 可以为 true，`http_recovery.verified` 仍为 false，整体必须失败。删除后 Ready 的仍是原 UID，也不能算补建成功。

报告在关键阶段更新；可捕获失败、Ctrl+C 和 SIGTERM 会尽力保存 JSON，返回非零退出码。SIGKILL、断电或磁盘写入失败无法保证最终报告完整，此时可检查最后的 `running` 记录与集群实际状态。脚本不删除 Deployment、Service、namespace 或 PVC，不重置其他服务，也不自动重复删除。失败后先只读排查，不要把重新运行当成恢复命令。
