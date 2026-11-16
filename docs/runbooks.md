# 演练排查手册

首次先按 README 初始化 .env；CLI 会在容器内部读取令牌，不打印凭据。再运行 `make up`、`make traffic`，等待约 1 分钟产生基线。所有操作在项目根目录执行。`make status` 查看应用故障和降级配置。

## 接口延迟

执行 `make fault-latency`，默认给业务请求增加 800ms，120 秒后自动过期。自定义：`python3 scripts/lab.py latency --delay-ms 1000 --seconds 90`。

现象：P95 上升，错误率未必升高；`LabHighLatency` 先 pending，满足条件持续 30 秒后 firing。检查 API 日志的 duration_ms 和 `/lab/status`，确认是否为人为延迟。恢复：`make recover` 或等待到期。已开始等待的请求会完成原有延迟，后续请求不再注入。

## 随机 500

执行 `make fault-errors`，默认约 30% 的业务请求返回 500，120 秒自动过期。自定义：`python3 scripts/lab.py errors --error-rate 0.5 --seconds 90`。

现象：错误率上升，日志中 `error_type=InjectedError`，`LabHighErrorRate` 可能触发。随机概率不保证小样本恰好等于设定比例。检查故障配置、请求 ID 与状态码；执行 `make recover` 后持续观察恢复。

## Redis 不响应

执行 `make fault-redis`。使用 Redis 原生 `CLIENT PAUSE 120000 ALL` 暂停客户端命令，Redis 到期自行恢复，不依赖终端保持打开。它模拟依赖不响应，不是杀死进程或断开网络。自定义：`python3 scripts/lab.py redis --seconds 60`。

现象：基线模式商品查询约在 Redis 超时后返回 503；存活检查仍为 200，就绪检查为 503。查看 `LabCacheFailures`、缓存失败曲线和超时日志。恢复：等待原生超时，或执行 `make recover`。若 `CLIENT UNPAUSE` 在 1 秒内没有响应，则重启 Redis 容器提前恢复（会清空实验缓存）；部分 Redis 版本的 ALL 暂停也会阻塞 UNPAUSE。它也尝试恢复以前手动暂停的 Redis 容器。

应用 `/lab/status` 与故障状态面板只显示应用内延迟/500/CPU 故障；Redis 暂停通过依赖错误和恢复结果判断。不要同时从多个终端注入故障。CLI 切换故障前清理上一种故障。

## 告警

打开 Prometheus 的 Alerts 页面（SSH 转发后访问 <http://localhost:9090/alerts>）。规则：

| 告警 | 条件 | 持续时间 |
| --- | --- | --- |
| LabHighErrorRate | 过去 1 分钟 5xx >10%，请求数至少 20 | 30 秒 |
| LabHighLatency | 过去 1 分钟 P95 >500ms，请求数至少 20 | 30 秒 |
| LabCacheFailures | 过去 1 分钟缓存失败请求数 >5 | 15 秒 |
| LabAPIDown | API 指标抓取失败 | 15 秒 |

告警恢复受 1 分钟窗口和 5 秒评估周期影响，不会在清除故障瞬间归零。只有应用故障时 API 指标通常仍可抓取。告警显示在 Prometheus 和 Grafana 面板；没有配置邮件/聊天通知。

## 缓存降级实验

`python3 scripts/lab.py fallback-on` 启用降级；`fallback-off` 恢复基线。配置仅驻留当前 API 进程，重启恢复 `.env` 默认值。

降级时 Redis 失败后读取内置商品数据，返回 200 与 `X-Cache: BYPASS`；就绪检查返回 200、status=degraded。缓存失败依然计数，因此依赖故障不会被成功率掩盖。每次请求仍先等待 Redis 超时，延迟不会凭空消失；当前没有熔断器。

执行 `make compare`：停止背景流量，按相同的每秒 10 请求、每种模式 30 秒，依次测试 Redis 暂停下的基线与降级。结束后清除故障并恢复原先降级配置。报告保存到 `reports/compare-*.json`，包含实际请求数、成功率、客户端 P95、状态分布、实际 RPS、数据源读取量和开始时间。背景流量保持停止，可再执行 `make traffic`。

实验前确保没有其他客户端访问业务接口，否则源读取计数会混入额外请求。该实验的数据源是进程内字典；结果不代表真实数据库负载能力。

## 演示顺序（约 5 分钟）

1. 展示商品查询 MISS → HIT 和结构化日志。
2. 开启流量，展示正常看板。
3. 注入 60 秒 Redis 暂停，展示 503、缓存故障告警和恢复。
4. 执行 `make compare`，展示基线与降级报告。
5. 根据报告解释成功率改善、超时仍存在和数据源压力增加。

## 复盘模板

- 环境、代码版本、请求速率、并发、超时配置。
- 故障开始、首次观测异常、告警 firing、执行恢复、业务恢复的时间。
- 业务影响：成功率、P95、受影响请求数；注明服务端还是客户端口径。
- 根因和证据：请求 ID、JSON 日志、监控截图、实验报告。
- 修复效果、代价、下次验证项。

单次演练记录发现耗时/恢复耗时即可；多次事件的平均值才能称为 MTTD/MTTR。

## 操作历史

登录 /docs 的 Authorize 后读取 GET /lab/events，查看最近 200 条以内的操作事件。
记录启动、清除、到期观察和降级配置变更；只在内存保留，重启丢失，JSON 日志另可导出。
到期事件时间是系统下一次检查时的观察时间，不是故障截止瞬间。
重复注入未清除的故障返回 409，避免无提示覆盖。CLI 仍显式先恢复再开始下一种实验。
