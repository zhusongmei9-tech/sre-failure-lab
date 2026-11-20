# Kubernetes 商品服务的发布与扩缩容

`sre-app-lab` 是单独的 Kubernetes 命名空间，运行一个商品 API Deployment、一个临时 Redis 缓存和内部 ClusterIP Service。它使用与 Compose 商品应用相同的代码，缓存、服务地址和发布状态各自独立。原有 `sre-k8s-demo` 继续用于单副本路由演练。

## 在 K3s 主机准备镜像

使用项目源码、Docker、K3s、Python 3.9+ 和可用的 Metrics Server。先检查 `k3s kubectl top node`。在本项目的 Docker 主机上执行下面的命令，复用已有镜像；首次构建时需要联网。两个演练镜像用内置版本号区分，以便从 HTTP 响应识别实际接流量的版本。

```bash
cd /opt/sre-failure-lab
for version in 0.3.0 0.3.1; do
  docker image inspect "sre-lab-api:$version" >/dev/null 2>&1 ||
    docker build --build-arg "APP_VERSION=$version" -t "sre-lab-api:$version" .
done
docker image inspect redis:7.4-alpine >/dev/null 2>&1 || docker pull redis:7.4-alpine
set -o pipefail
docker save sre-lab-api:0.3.0 sre-lab-api:0.3.1 redis:7.4-alpine | k3s ctr images import -
bash scripts/install_k8s_app_lab.sh --monitoring
```

`--monitoring` 用于已安装本项目 Kubernetes 监控的主机。安装器只创建缺少的资源，重复执行会保留当前 API 版本和 HPA 管理的副本数。已有 Grafana 从 `monitoring/grafana/dashboards/` 读取新看板，名称为 **Kubernetes 商品 API 发布与扩缩容**，路径为 `/d/sre-k8s-product`。

## 查询商品和缓存

从 137 的 shell 运行以下命令。第二次响应的 `X-Cache` 应为 `HIT`，`X-App-Version` 表示处理请求的应用版本。

```bash
ip=$(k3s kubectl -n sre-app-lab get svc product-api -o jsonpath='{.spec.clusterIP}')
curl --noproxy '*' -si "http://$ip:8000/products/1"
curl --noproxy '*' -si "http://$ip:8000/products/1"
```

如果要在自己的浏览器访问，可在 137 执行 `k3s kubectl -n sre-app-lab port-forward --address=127.0.0.1 service/product-api 18080:8000`，然后从电脑打开 SSH 隧道 `ssh -N -L 18080:127.0.0.1:18080 root@172.16.58.137`，访问 `http://127.0.0.1:18080/docs`。端口转发适合交互访问；下方演练从 137 经稳定的 ClusterIP 请求商品，能跨 Pod 更新持续采样。

## 连续运行两项演练

```bash
cd /opt/sre-failure-lab
mkdir -p reports/product-k8s
python3 scripts/k8s_app_rollout.py --run --output reports/product-k8s/rollout.json
python3 scripts/k8s_app_hpa.py --run --output reports/product-k8s/hpa.json
```

两个命令省略 `--run` 可查看计划。演练和安装器共用本机文件锁，同一时间运行一项。

1. **滚动发布与回滚。** 脚本在两个已加载的镜像之间切换，持续查询商品 1，再发布一个就绪探针路径错误的候选配置。Deployment 使用 `maxUnavailable: 0` 和 `maxSurge: 1`，旧的可用 Pod 继续服务。脚本等待候选超时、确认其 ReplicaSet 未就绪，然后显式恢复已验证的 Pod 模板。报告列出每一阶段的请求数、失败数、版本分布、客户端 P95 和回滚耗时。演练结束后保留最近一次成功发布的版本。
2. **HPA。** CPU 目标为每个 API Pod 请求量 25m 的 60%。脚本先以每秒 1 个请求采集基线，再按默认每秒 40 个请求访问商品，观察 1 → 2 个就绪副本；停止流量后等待 HPA 缩回 1。HPA 根据指标调整 Deployment 的期望副本数，脚本持续读取副本状态。报告按相同目标流量下就绪副本数为 1、2 的采样窗口分别统计 P95，同时记录失败请求。两副本共用同一台虚机，响应时间是否下降应以测量结果为准。

Grafana 的 P95 来自服务端直方图，演练报告的 P95 来自客户端 HTTP 采样。空闲时没有商品延迟样本，查看演练发生的时间段即可。Grafana 的 CPU 面板单位是 mCPU，HPA 的百分比以 API 容器的 CPU request 为分母。

## 137 实机结果 · 2026-11-20

两轮都在同一 ARM64 单节点执行。原 `sre-k8s-demo` 和 Compose 商品服务在最终检查中均健康。

| 镜像切换 | 发布就绪并验证版本 | 失败候选触发回滚后，至模板恢复并验证 HTTP | 全过程商品请求 |
| --- | ---: | ---: | ---: |
| [0.3.0 → 0.3.1](experiments/k8s-product-137/rollout-0.3.0-to-0.3.1.json) | 6.96 秒 | 0.44 秒 | 105 成功，0 失败 |
| [0.3.1 → 0.3.0](experiments/k8s-product-137/rollout-0.3.1-to-0.3.0.json) | 7.00 秒 | 0.47 秒 | 105 成功，0 失败 |

两轮 Kubernetes 事件均记录到候选 Pod 的就绪探针返回 404。回滚耗时从执行恢复开始计时；之前用于观察候选失败的 18 秒期间也持续采样商品请求。

| HPA 演练 | 高负载查询 | 1 个就绪副本 P95 | 2 个就绪副本 P95 | 撤流至观测到 1 个就绪副本 |
| --- | ---: | ---: | ---: | ---: |
| [第一轮，0.3.1](experiments/k8s-product-137/hpa-0.3.1.json) | 1,829 次，0 失败 | 9.29 ms | 7.28 ms | 71.13 秒 |
| [第二轮，0.3.0](experiments/k8s-product-137/hpa-0.3.0.json) | 1,837 次，0 失败 | 7.35 ms | 8.04 ms | 71.49 秒 |

两轮高负载均实测约为 40 请求/秒。表中 P95 仅比较同一轮、相同目标速率下，连续两次副本采样均为同一数量的窗口。两轮均观测到自动扩容和缩容，延迟变化方向不同。Grafana 四个面板的七条查询均返回有限数值，期望、当前和就绪副本曲线都覆盖了 1 与 2。[看板查询记录](experiments/k8s-product-137/grafana.json)
