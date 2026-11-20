# 137 实机验收 · Ansible 重复部署与 Kubernetes Service 故障恢复

2026-11-20，在已有蓝绿双槽的 Rocky Linux ARM64 虚机执行。目标集群为单节点 K3s `v1.37.2+k3s1`，探针和故障注入使用 `sre-k8s-demo` 命名空间。下表时间使用北京时间。

## 连续部署

| 控制端动作 | Ansible recap | 现场结果 |
| --- | --- | --- |
| `--check --diff` | `changed=1 failed=0` | 显示源文件差异。systemd 预览报告 `parent=none healer=none` |
| 第一次应用 | `ok=15 changed=1 failed=0` | 安装器与 unit 模板对齐，Redis 就绪检查及商品 1 查询通过 |
| 紧接着再次应用 | `ok=15 changed=0 failed=0` | 同一组业务检查再次通过 |

部署前后，主服务与自愈服务的 `ActiveEnterTimestampMonotonic` 分别保持在 `25744289`、`25791001`，前后八个 Compose 容器的 ID 完全相同。该项验证已有实例上的重复部署。首次从源码包安装到空白虚机仍需单独验收。[部署命令](ansible-deploy.md)

## Service 路由演练

| 时间 | 现场操作 | 从集群内观测到的结果 |
| --- | --- | --- |
| 注入前 | 使用独立的短期 Pod 访问 `demo-web:8080` | 1 个就绪端点，`dns=ok http=200 body=ok` |
| 14:43:10 | 将 Service 选择器改为没有匹配 Pod 的标签 | 就绪端点降为 0；Deployment 仍有 1 个 Ready 副本。探针记录 `dns=ok http=failed` |
| 14:43:19 | 恢复初始选择器 `app=demo-web` | 就绪端点恢复为 1 |
| 14:43:24 | 重新运行同一探针 | `dns=ok http=200 body=ok`。验证 Job 和 Pod 均已清理 |

实机保留了 v0.4.0 版本的 demo。正式脚本核对了三个对象的来源清单和现场配置后才开始演练。选择器由脚本恢复，自注入到验证恢复共 14 秒，其中包含 Job 启动和清理时间。商品 API 在独立的 Compose 栈运行，演练后从 18000 端口检查得到 `{"status":"ready","redis":"ok"}` 和商品 ID `1`。[探针与复现步骤](k8s-service-smoke.md)

## 开机时发现的问题

首次连接时，API Server 可以读取历史对象，但节点 Lease 仍停在 11 月 17 日。启动日志显示节点证书的生效时间晚于虚机的当前时间。核对宿主机时钟和虚机 NTP 同步状态后，重新启动 K3s，节点开始续约，`k3s certificate check` 中尚未生效的证书从 4 张降为 0。随后才进行本页的集群验证。这个过程包含人工恢复，不能作为 K3s 无人干预开机恢复的验收结果。
