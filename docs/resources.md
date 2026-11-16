# 可选 Linux 资源监控（待目标虚拟机验收）

本阶段增加 node_exporter 主机指标配置、受限 cAdvisor 原始 cgroup 配置和独立的 **SRE Lab · Resource Correlation** 看板。看板另外提供 API 自身导出的进程 CPU、RSS、CPU 实验 429 拒绝速率和 CPU 故障启用状态，Linux 下无需可选采集器即可观察这四项。业务错误率面板只统计 5xx，主动拒绝的 429 请看独立面板。原来的 `docker compose up` 不会启动资源采集器。默认看板会显示，但未启用资源组件的相关面板应为 No data，不能解释为资源使用量为零。

## 部署边界与权限

仅面向独立、可丢弃的 Linux 实验虚拟机上的 rootful Docker Engine。不是生产部署配置，也不把 Docker Desktop 虚拟机的指标当成 Mac 宿主机指标。rootless Docker、非标准 cgroup 布局、SELinux 限制和自定义数据目录尚未验收。

启用之前，先确认虚拟机的发行版、CPU、内存、磁盘，以及以下访问范围：

- node_exporter 使用 host 网络和 PID 命名空间，按官方主机监控方式只读挂载 `/`。以 nobody 运行、删除全部 capabilities、禁止提权。只读挂载仍能读取该用户有权读取的主机内容，不等于没有信息暴露。
- 它只绑定**你显式指定的 Docker 网桥网关地址**，不默认监听公网或校园网接口。其他能访问该网关的本机容器也可能读取指标；这不是多租户隔离方案。不要设置成 `0.0.0.0`。
- cAdvisor 仅只读挂载 `/sys` 并使用 host cgroup 命名空间，没有 host root、Docker socket、`/var/run`、Docker 数据目录、设备或 privileged 权限。不对宿主机发布 8080 端口。
- 这是刻意受限的 cAdvisor 采集模式。它可能只能得到部分原始 cgroup 指标，无法自动提供 Compose 服务名；某些内核上也可能不能启动或发现 Docker cgroup。**采集成功不等于全部指标齐全**。不要为修复缺失指标而直接增加 privileged、挂载 Docker socket 或关闭 SELinux；先检查报错，再单独评估权限。
- 监控容器配置了 CPU、内存及 PID 上限。这些是采集器自身的上限，不是业务 API 的压力实验配额。

## 先检查，后启动

在目标 Linux 虚拟机的项目根目录执行只读检查：

```bash
uname -a
cat /etc/os-release
nproc
free -h
df -h /
docker version
docker compose version
docker info --format '{{.CgroupVersion}} {{.CgroupDriver}} {{.DockerRootDir}}'
docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'
```

将最后一项返回的**宿主机 Docker 网桥网关 IPv4 地址**填入 `METRICS_HOST_GATEWAY`。例如返回 `172.17.0.1` 时，设置 `export METRICS_HOST_GATEWAY=172.17.0.1`；不要不检查就照抄示例。如果默认 bridge 不存在，或输出不是可绑定的 IPv4 地址，停止并检查实际网络，不随意开放全接口。网关需存在于 `ip -4 addr` 输出。

确认上述权限和网络范围后，先渲染配置再启动：

```bash
docker compose -f compose.yaml -f compose.resources.yaml config
docker compose -f compose.yaml -f compose.resources.yaml up -d --wait
```

没有设置变量时，Compose 会明确报错，避免意外开放采集端口。该 override 替换 Prometheus 的配置挂载，保留原有 API 抓取和告警规则，再增加 node/cadvisor 两个抓取任务。没有新增资源告警：第一步先验证指标与业务的关联，不把高 CPU 自动等同于事故。

## 验收清单

1. `docker compose -f compose.yaml -f compose.resources.yaml ps` 和 `logs --tail=100 node-exporter cadvisor prometheus` 中没有持续错误。资源采集器没有 HTTP 健康检查，`up --wait` 仅证明它们保持运行；下面的 Prometheus 检查不能省略。
2. 打开 Prometheus `/targets`，确认 API、node、cadvisor 全部 UP。查询 `node_scrape_collector_success{job="node"}`，确认启用的采集器均为 1。
3. 核对 `count(node_cpu_seconds_total{job="node",mode="idle"})` 与 `nproc`，核对内存总量与 `free`。磁盘及网络接口名必须对应 Linux 虚拟机，而非某个采集容器。
4. 查询 `container_cpu_usage_seconds_total{job="cadvisor"}`，查看实际 `id` 标签。用 `docker inspect --format '{{.Id}}' "$(docker compose ps -q api)"` 获取 API 容器完整 ID，对照其 cgroup。默认面板匹配 rootful Docker 常见的 `/docker/<ID>` 和 `docker-<ID>.scope` 路径。若布局不同，需要据实际标签调整，不要把父 cgroup 与子 cgroup 求和，避免重复计算。
5. 查看 Grafana 的资源看板，保持与业务看板一致的时间范围。先留基线，再运行受控 CPU 实验，然后停止压力，观察资源、吞吐和延迟回落。**未实际做过的步骤不要填“通过”。**
6. CPU 限流只在运行时暴露相关计数、且确实设置配额时才有意义。无数据不是 0% 限流；此受限模式不承诺 OOM、容器网络或容器磁盘指标。
7. 如 node 目标 DOWN，先核对绑定地址、宿主机路由和已有防火墙规则。不要直接关闭防火墙或放开公网端口。

## 退出资源监控

以下操作只删除新加的两项采集服务，并让 Prometheus 恢复原有配置，不删除监控数据卷：

```bash
docker compose -f compose.yaml -f compose.resources.yaml stop node-exporter cadvisor
docker compose -f compose.yaml -f compose.resources.yaml rm -f node-exporter cadvisor
docker compose -f compose.yaml up -d prometheus
```

## 指标阅读注意

- 主机 CPU 是全核平均百分比；cgroup CPU 显示使用了几个核。不要直接比较两者的数值大小。
- 主机内存使用率按 MemAvailable 计算；容器 working set 不是 Python 堆内存。
- 磁盘平均操作延迟不是 P95，磁盘吞吐不是忙碌百分比。设备与分区不要重复相加。
- 网络按接口显示，排除了常见虚拟网卡，避免一次传输被多次计数。自定义接口名需按现场调整。
- 限流周期比例不是“CPU 使用率”，也不是“被限流的 CPU 时间比例”。
- 看板不把缺失序列补成零，防止把采集失败显示为健康。

## 当前验证范围

配置及 JSON 做静态检查；尚未在目标 Linux Docker Engine 启动、抓取或进行 Grafana 浏览器验收。固定镜像版本为 node_exporter `v1.12.1`、cAdvisor `0.55.1`，没有使用 latest；未拉取验证镜像的架构支持及摘要。上线前应在目标机器验证并按需锁定 digest。

参考官方文档：[node_exporter 容器化主机监控](https://github.com/prometheus/node_exporter#docker)、[node_exporter v1.12.1](https://github.com/prometheus/node_exporter/releases/tag/v1.12.1)、[cAdvisor 容器部署](https://github.com/google/cadvisor#quick-start-running-cadvisor-in-a-docker-container)、[cAdvisor 运行选项](https://github.com/google/cadvisor/blob/master/docs/runtime_options.md)。官方 cAdvisor 完整功能示例包含更高权限；本实验配置没有照搬那些权限，故明确保留功能限制。
