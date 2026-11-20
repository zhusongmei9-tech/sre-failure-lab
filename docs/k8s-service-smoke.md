# 从 Pod 内验收 demo-web

这个检查在 `sre-k8s-demo` 中启动一个短期 Job，依次查询 Service 的 DNS、经 ClusterIP 访问 8080 端口并比对 HTTP 正文。Job 使用和 demo-web 相同版本的 BusyBox，兼容非 root 策略和演示命名空间的资源配额。

在 K3s 主机的源码目录执行以下命令。需要预先部署 `monitoring/kubernetes/demo.yaml` 并等待 demo-web 就绪。远程 kubectl 用户可以通过 `--context NAME` 指定实验集群。实验集群使用的 DNS 域默认为 `cluster.local`，自定义域可在 Job 清单的 `CLUSTER_DOMAIN` 中设置。

```bash
bash scripts/check_demo_service.sh
```

成功时日志依次包含 `dns=ok`、`http=200` 和 `body=ok`，命令以 0 退出。失败时可按最后一项日志定位。

| 日志 | 检查项 |
| --- | --- |
| `dns=failed` | 确认 Service 名称、集群 DNS 域和 CoreDNS 可用性 |
| `dns=ok` 后出现 `http=failed` | 查看 Service 的 EndpointSlice、端口及后端 Ready 状态 |
| `http=200` 后出现 `body=failed` | 检查 Service 选择的应用及其实际返回内容 |
| 没有探测日志 | 查看命令输出中的 Pod 状态，定位镜像获取、调度或启动失败 |

脚本打印探测日志后会删除本次创建的 Job 和探测 Pod。Job 本身设置了 60 秒截止时间、零重试及完成后 180 秒自动清理。和现有的删 Pod 演练串行执行，让演练能识别唯一的业务 Pod。

## 演练 Service 有 DNS 但没有后端

在可短暂中断 demo-web 的实验集群运行。省略 `--run` 可预览流程。远程运行时可追加 `--context NAME`。

```bash
bash scripts/drill_demo_service.sh --run
```

脚本确认实验命名空间、单副本 Deployment 和内部 Service 后，先从 Pod 内验证成功，再把 Service 选择器临时指向没有 Pod 的标签。预期就绪端点从 1 降到 0，Deployment 仍有 1 个就绪副本，探测日志出现 `dns=ok` 和 `http=failed`。恢复原选择器后，端点回到 1，探测重新通过。脚本打印故障、恢复选择器及完成验收的 UTC 时间。

修改和恢复操作都核对 Service UID 与预期选择器。脚本在收到中断或后续检查失败时也会尝试恢复；若 API 持续不可达或 Service 被其他操作更改，日志会列出要人工核对的 UID 和选择器。

已有 v0.4.0 安装可以直接演练。对该版本缺少项目管理标签的对象，脚本核对 Namespace、Deployment 和 Service 的 `last-applied-configuration` 摘要，并逐项比对原始清单声明的字段与集群现状，无需重新部署 Pod。
