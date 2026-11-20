# 从空白 Linux 虚机复现 SRE 故障演练实验室

准备一台能登录的 Rocky Linux 9 虚机和一台能 SSH 到它的电脑。各章给出可直接执行的命令、对应的配置文件、默认值、预期结果和排错入口。完成后，读者可以在持续商品流量下观察发布、回滚和自动扩缩容。

## 从这里开始

| 顺序 | 在哪里做 | 本章结束时应亲眼看到 |
| --- | --- | --- |
| [01　Linux、Docker 与真实 Compose 环境](01-linux-compose.md) | 虚机 | 18000 端口返回商品；第二次查询命中 Redis；systemd 主服务和自愈服务都在运行 |
| [02　单节点 K3s 与监控](02-k3s-monitoring.md) | 同一台虚机，电脑端浏览器 | 节点 Ready，Metrics Server 报出 CPU 和内存，Grafana 能查询 Kubernetes 数据，独立 Pod 能经 Service 访问演示应用 |
| [03　Kubernetes 商品发布、回滚与 HPA](03-product-rollout-hpa.md) | 同一台虚机与浏览器 | 曾接流量的旧、新镜像版本；失败候选的就绪探针；HPA 升到 2 个 Ready 副本、撤流后回到 1；两份自己的 JSON 报告 |
| [04　Compose 自愈、蓝绿发布与日常运维](04-operations.md) | 同一台虚机 | Redis 故障和恢复记录；Nginx 切流前后的镜像版本和活动槽；主机、容器和业务指标 |

第一次复现按 01 → 02 → 03 → 04 操作。只学习 Compose 的同学可以走 01 → 04。已经跑好环境的同学先做对应章节的状态检查，尤其要保留已有的 `.env`、蓝绿槽和本地镜像。虚机地址由你的网络分配，教程里的实验地址仅作为示例。

## 读取配置时，先分清操作对象

| 对象 | 配置入口 | 看实际生效值的地方 |
| --- | --- | --- |
| Compose 教学栈 | [compose.yaml](../../compose.yaml)、[环境变量模板](../../.env.example) | `docker compose ps`，宿主机教学 API 默认 8000；主教程使用下行的真实栈 |
| Compose 真实栈 | [compose.runtime.yaml](../../compose.runtime.yaml)、本机 `.env`、[systemd 单元](../../ops/sre-lab.service) | `python3 -m ops.stack status`、`curl -sS http://127.0.0.1:18000/version`、`systemctl cat sre-lab` |
| 监控与告警 | [Compose 采集配置](../../monitoring/prometheus/runtime.yml)、[K3s 监控 values](../../monitoring/kubernetes/values.yaml)、[Grafana 看板](../../monitoring/grafana/dashboards) | Prometheus Targets、Grafana 的 `Prometheus` 和 `Kubernetes` 数据源、`k3s kubectl -n sre-monitoring get pods` |
| K3s 节点 | [K3s 配置模板](../../monitoring/kubernetes/k3s-config.yaml.example)，安装后生成 `/etc/rancher/k3s/config.yaml` | `k3s kubectl describe node`、`k3s kubectl top node`、`journalctl -u k3s -b` |
| K8s 商品服务 | [Namespace、API、Redis、HPA](../../monitoring/kubernetes/app-lab)、[ServiceMonitor](../../monitoring/kubernetes/app-lab-observability/product-monitor.yaml) | `k3s kubectl -n sre-app-lab get deploy,svc,hpa,pods`，再用第三章的真实商品探针核对 |
| 可选模块 | [Ansible](../ansible-deploy.md)、[Alertmanager 与接收器](../kubernetes-alerting.md)、[Argo CD 演练](../k8s-gitops.md) | 分别查看重复执行的变更数、同一事件的 firing/resolved、应用同步到的 Git 提交。这些模块按需使用 |

## 下载、识别和重做同一份实验

首章使用 `git clone` 取得 `main`。也可以直接下载 Release 中项目上传的同名 tar.gz，包内 `docs/from-zero/README.md` 就是本教程。Release 页面记录附件所对应的完整 Git 提交和 SHA-256。固定版本号下附件会随通过 CI 的 `main` 更新，复现实验时请同时保存提交号和附件摘要。[当前 Release](https://github.com/zhusongmei9-tech/sre-failure-lab/releases/tag/v0.6.0)

在下载目录运行以下命令，核对摘要与 Release 页面一致，再检查包内教程。GitHub 自动列出的 `Source code (zip)` 和 `Source code (tar.gz)` 按历史标签生成；滚动更新的入口是项目命名的附件。

```bash
sha256sum sre-faliure-lab-v0.6.0.tar.gz
tar -tzf sre-faliure-lab-v0.6.0.tar.gz | grep 'docs/from-zero/.*\.md$'
```

使用附件安装时，先完成第 01 章的主机与 Docker 准备。在虚机上以 root 身份运行下面的命令，替代第 01 章的 `git clone` 步骤。`mkdir` 要求安装目录尚不存在；命令成功后直接接着做第 01 章的“生成私有配置”。

```bash
download_dir=$(mktemp -d) && cd "$download_dir"
curl -fL --retry 2 -o sre-faliure-lab-v0.6.0.tar.gz \
  https://github.com/zhusongmei9-tech/sre-failure-lab/releases/download/v0.6.0/sre-faliure-lab-v0.6.0.tar.gz
sha256sum sre-faliure-lab-v0.6.0.tar.gz
# 先与 Release 页面中的 SHA-256 核对，再执行解包。
mkdir /opt/sre-failure-lab && tar --no-same-owner --strip-components=1 \
  -xzf "$download_dir/sre-faliure-lab-v0.6.0.tar.gz" -C /opt/sre-failure-lab
```

以下是本实验的[137 实机参照记录](../k8s-product-lab.md)。在你自己的记录里写上虚机架构、源码提交、开始时间、请求成功与失败数、回滚耗时、扩缩容前后的副本与 P95。只要数据不同就如实记录，硬件、镜像缓存和采集间隔都会影响时间。
