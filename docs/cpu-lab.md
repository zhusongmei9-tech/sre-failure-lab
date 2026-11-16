# CPU 故障实验（第一阶段增量）

这不是 `sleep` 模拟延迟，而是在商品请求中执行真实整数运算。只对启用
`LAB_ENABLED=true` 的实验实例开放。不要在生产机器或共享业务机器运行。

## 安全边界

- 故障持续时间 1–600 秒，默认 120 秒；CLI 限制 1–300 秒。
- 每次请求 1–5,000,000 次迭代，默认 100,000。
- 单次工作每 1024 次迭代检查 250ms 时间预算，到期截断。调度或 CPU 限流可能让
  实际墙钟耗时超过预算，所以不是硬实时保证。
- 单 API 进程同一时刻只执行一项 CPU 工作；重叠请求返回 429，不排长队。
- CPU 工作在后台线程执行，控制接口不主动阻塞等待它；Python GIL 仍可能影响延迟。
- 清除故障会阻止后续 CPU 工作，已开始的一项会在上述边界内完成。
- Compose 默认给 API 1 个 CPU 和 256 MiB 内存上限。只支持单 API worker。

## 实验步骤

先按 README 启动并做冒烟测试，确认没有旧故障。保持默认回环绑定。

```bash
make traffic                 # 默认 10 RPS，先记录 2 分钟基线
python3 scripts/lab.py cpu --seconds 60 --cpu-iterations 100000
# 待到期恢复，记录正常区间，再增加计算量
python3 scripts/lab.py cpu --seconds 60 --cpu-iterations 1000000
python3 scripts/lab.py status
make recover                 # 提前恢复；也会恢复此前的 Redis 故障
make traffic-stop
```

只解除应用 CPU 故障、不触碰 Redis：

```bash
curl -H "Authorization: Bearer $LAB_TOKEN" -X DELETE http://127.0.0.1:8000/lab/faults
```

上述 curl 需要先在本机设置 LAB_TOKEN 环境变量（不要把实际令牌发到聊天或写入命令历史）。
通过 `/docs` 的 Authorize 填入令牌后也可以提交 `{"kind":"cpu","duration_seconds":60,"cpu_iterations":1000000}`。
实验没有流量时不会自行消耗 CPU。每组保持相同 RPS、并发、缓存状态和 CPU 配额。
改变 `.env` 的 `API_CPUS=0.5` 后执行 `docker compose up -d api` 可进行配额对比，
但这会重建 API，应单独记录为新一组实验，而非在同一组中途修改。

## 指标与解释

```promql
# API 进程消耗 CPU 的核数：1 表示约占一个逻辑 CPU，不是整机百分比
rate(process_cpu_seconds_total{job="api"}[1m])

# API 进程驻留内存（字节），不是容器内存上限
process_resident_memory_bytes{job="api"}

# P95 商品请求耗时
histogram_quantile(0.95, sum by (le) (rate(lab_http_request_duration_seconds_bucket[1m])))

# 重叠 CPU 请求被拒绝的频率
rate(lab_cpu_rejected_total[1m])

# 请求触发单次 CPU 时间预算上限的频率
rate(lab_cpu_budget_exhausted_total[1m])

# 已完成的真实迭代数，不等于提交的计算量
rate(lab_cpu_iterations_total[1m])
```

Linux 的 process 指标来自当前 API 进程的 `/proc`，非 Linux 环境可能缺少部分指标。
主机与容器监控见 [资源监控部署](resources.md)。429 是实验过载保护，不是 Redis 故障；
原有 5xx 错误率告警不包含它。比较时同时看 2xx 成功率、429、P95 和实际吞吐，
不能只看 5xx，不能只因 CPU 高就认定业务失败。

恢复验收：`/lab/status` 的 fault 为 null，CPU 迭代计数停止增长，429 停止，
进程 CPU rate 与 P95 回落。Prometheus 的 1 分钟 rate 会滞后，不能要求瞬间归零。

## 本次实际验证

`experiments/cpu-component.json` 记录在 Linux Python 3.12 下，用 FastAPI TestClient
和模拟 Redis 的 3 组顺序请求。每组 20 次，均 200；CPU 计算量提高时 CPU 时间和
P95 同时增加，清除故障后接口正常。它只证明组件行为，不代表真实 Redis、容器限流、
Prometheus 或 Grafana 全栈验收，也不能用于宣称生产吞吐。
