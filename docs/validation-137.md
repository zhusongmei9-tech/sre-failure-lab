# 137 真实部署验收记录

本页保留首次部署的验收记录。后续完整测试与带流量复测，以及本轮发现并修复的 429 误重启问题，见 [完整流量验收报告](traffic-validation-137.md)。

日期：2026-11-14。目标：`172.16.58.137`，Rocky Linux 9.5 / ARM64，2 个逻辑 CPU、4,806,344,704 字节内存，根文件系统容量 17,553,162,240 字节。

## 交付与线上状态

源码版本为 v0.3.0 真实运行扩展。线上活动槽为 green，业务版本 0.3.1；blue 保留 0.3.0 供回滚。0.3.1 是使用 `ops/Version.Dockerfile` 从同一业务代码构建的版本演练镜像，不代表额外业务功能。业务入口 `http://172.16.58.137:18000`，项目位于 `/opt/sre-failure-lab`。

基础 API 镜像使用锁定依赖和经过 requirements 哈希校验的 ARM64 离线 wheel 构建；网络直接安装依赖曾超时。两个实际镜像 ID 记录在验收 JSON 中，发布使用不可变 ID。

## 自动化与目标机验证

| 验证项 | 结果 |
| --- | --- |
| 本地 Python 测试 | 171 passed，1 skipped；跳过项为需独立 Redis 的集成测试 |
| Ruff | 通过 |
| 控制台 Node DOM 测试 | 4 项通过 |
| Prometheus 配置及规则语法检查 | 通过，2 个规则文件、9 条规则 |
| 过期 release_id 提交 | 目标 API 返回 HTTP 409 |
| 真实正常发布 | 0.3.0 → 0.3.1；含 5 秒观察，共 6.192 秒 |
| 真实手动回滚 | 0.3.1 → 0.3.0，通过 |
| 切流后自动回滚 | 向活动候选注入 100% 500，观察失败，实际切回 0.3.0 |
| 最终发布 | 0.3.1 green 活动，0.3.0 blue 保留 |
| Redis 停止期间业务降级 | HTTP 200，X-Cache: BYPASS |
| Redis 自愈 | 从停止到恢复验证约 21.057 秒 |
| API 自愈 | 从停止到恢复验证约 25.066 秒，恢复后仍为 0.3.1 |
| systemd 自愈服务 | enabled / active |
| Prometheus 实际目标 | api、两个 api_slots、node、cadvisor 全部 UP |
| Grafana | 两份看板已加载，登录页和带认证的查询接口可访问 |
| 资源看板查询 | 20 个面板均返回实际有限数值，覆盖主机及容器 CPU、内存、文件系统和磁盘指标 |

上述自愈不是只观察 Docker 重启策略：验收主动停止容器，并核对本次故障对应的控制器动作、后续健康验证事件和实际业务响应。耗时是本次实验观测值，不是 SLA。

机器可读证据：[`acceptance.json`](experiments/runtime-137-2026-11-14/acceptance.json)、[`observability.json`](experiments/runtime-137-2026-11-14/observability.json)。后者包含看板表达式、样本、目标状态与 Grafana 看板清单。

cAdvisor 采用只读 `/sys` 的受限采集，已在本机实际取得 Docker cgroup CPU、内存工作集及 CPU 配额限流指标。它不访问 Docker socket，图例使用 cgroup 路径及容器 ID，不提供 Docker 服务名标签和 OOM 事件。Grafana 内存限制已由初始的 256MB 提升至 512MB，并重新启动验证。

## 修复内容与使用边界

候选检查、观察、取消和回滚均要求 `release_id`。匹配检查在状态锁内执行，防止上一轮检查结果误操作下一轮发布。缺失 ID 返回 422，过期 ID 返回 409。

真实控制程序共用主机文件锁，保留镜像身份、发布阶段、回滚目标和事件；自愈有失败阈值、冷却、次数预算及后续恢复验证。失败发布即使回滚成功也以失败退出。

`/console` 和 `/lab/deployment`、`/lab/healing` 保留教学模拟。真实发布和自愈通过宿主机 `python3 -m ops.deploy`、`python3 -m ops.heal` 操作，真实事件在 `runtime/events.jsonl`。不要用模拟控制台状态判断线上活动版本。

本次未验证整机断电恢复、跨主机高可用或生产持续流量下零中断。当前自动修复仅覆盖 Redis 与活动 API 容器，磁盘满、Docker 守护进程故障和入口配置错误需人工处理。监控缺失数据不等于资源使用量为零。
