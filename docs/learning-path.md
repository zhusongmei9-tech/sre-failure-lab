# 一条能讲清楚的 SRE 演练主线

本版定位：可运行的业务实验 + 可测试的运维决策模拟，不是已部署的自动运维系统。
先理解判断依据，再接真实权限。所有表格中的“模拟”都不能当作真实服务恢复证据。

## 0. 不部署也能跑完整组件演练

```bash
uv sync --locked --python 3.12
uv run --locked pytest -q
node --test tests/console.test.cjs
uv run --locked python -m scripts.simulate
```

最后一项在当前进程运行 FastAPI TestClient、模拟 Redis、模拟时钟和模拟控制器，
不启动容器，不执行 SSH，不修改系统服务。输出目录为 `reports/rehearsal-时间/`，含
report.json、report.md 和 summary.json。测试令牌只是进程内固定测试字符串，不用于部署。

预期 summary：6 次商品请求，4 次 200、2 次注入 500；模拟 Redis 恢复验证为 true；
故障发布回切 v1；`real_actions_executed=false`。这是一段很短的演示，不足以触发双窗口
SLO 告警，不能把组件报告称为长时间可用性统计。

## 1. 业务慢了：先确认 CPU 还是等待

| 环节 | 要做什么 / 证据 |
| --- | --- |
| 故障 | 相同流量下分别注入 latency 和 cpu，避免混用 |
| 指标 | 比较业务 P95、进程 CPU 核数、完成请求量与 429；CPU 实验才应增加真实计算 |
| 告警 | 查看现有延迟规则，考虑最小流量与持续时间；瞬间变慢不保证 firing |
| 排查 | 请求 ID → JSON 日志 → fault 参数 → CPU/限流曲线，别把 sleep 当作高 CPU |
| 恢复 | 清除故障或等待到期，停止新增 CPU 工作；已开始的工作有时间预算 |
| 预期输出 | fault=null，CPU 迭代数停止增加，P95/rate 随窗口逐步回落 |

详细步骤见 [CPU 实验](cpu-lab.md)。主机/容器图表须完成 [资源监控验收](resources.md) 后才能作为证据。

## 2. 服务异常：重启动作不等于恢复

| 环节 | 要做什么 / 证据 |
| --- | --- |
| 故障 | 向模拟控制器连续输入 api/redis 失败；单次失败不触发动作 |
| 指标 | consecutive_failures、attempts、phase、cooldown_remaining_seconds |
| 告警 | 预算耗尽产生 healing_budget_exhausted 事件；未配置外部通知 |
| 排查 | 找 incident_detected → action_simulated → 后续 probe，检查是否冷却/锁定 |
| 恢复 | 连续两次健康观察才记恢复；锁定不会自动重新武装 |
| 预期输出 | verifying → healthy；一直失败最终 locked_out，不无限尝试 |

见 [自愈状态机](healing.md)。真实恢复执行器与可信探测器都尚未接入。

## 3. 新版本出错：验证、切换、观察、回退

| 环节 | 要做什么 / 证据 |
| --- | --- |
| 故障 | 发布 v3-bad 的模拟候选，并提交失败探测结果 |
| 指标 | phase、candidate/active/previous、release_id、locked |
| 告警 | deployment_rejected / deployment_rolled_back 等事件；不是已送达的外部通知 |
| 排查 | 分清候选未接管就失败，还是模拟切流后观察失败；核对旧版本是否仍占槽 |
| 恢复 | 有旧活动版本则回切；首次部署失败为 failed_no_previous，不伪称回滚 |
| 预期输出 | active 回到旧版本，phase=idle；观察期内第二个发布请求返回 409 |

见 [蓝绿发布模拟](deployment.md)。模拟槽位不是真实容器；没有代理配置、真实探测或流量切换。

## 4. 用 SLO 和报告收尾

成功率统一为 **2xx / 全部完成商品请求**，包含 429、404、422、5xx 为失败。
它与旧的“5xx 错误率”面板不同，也不包括没到达 API 的客户端网络失败。
默认演示目标 99%，无流量是无数据，不能显示 100% 健康。

真实 Prometheus 规则用 5m/1h 双窗口和最小请求数，报告则是当前进程启动以来的累积
计数。两者不能当作同一统计窗口比较。报告接口的 target 参数只改变本次报告目标，
不会修改 Prometheus 固定 99% 的规则。

[SLI/SLO 与报告](slo-reports.md) 说明错误预算、分位数、告警门槛及测试命令。
完整复盘必须写：故障何时发生、发现依据、为什么采取动作、动作后什么证据证明恢复，
以及尚未验证的环节。单次演练不能宣称平均 MTTR。

## 5. 只读控制台

以后完成本地实验服务启动后访问 `http://localhost:8000/console`。这是项目内路由，
不是已经发布的网址。输入实验令牌后，手动读取业务概览、模拟自愈、模拟发布和事件；
可下载 Markdown 报告，没有恢复/发布/注入按钮。令牌只在页面内存使用，断开/刷新需重输。

可在 `/docs` 的 simulation & reports 分组调用模拟控制接口：

| 路径 | 方法 | 用途 |
| --- | --- | --- |
| /lab/overview | GET | 只读聚合快照 |
| /lab/healing | GET | 模拟自愈状态 |
| /lab/healing/config | POST | enabled 开关，仅模拟 |
| /lab/healing/probe | POST | service + healthy 模拟观察 |
| /lab/healing/reset | POST | 明确重置某服务模拟预算 |
| /lab/deployment | GET | 模拟发布状态 |
| /lab/deployment/begin | POST | 指定模拟版本，返回本次 release_id |
| /lab/deployment/candidate-check | POST | release_id + ready + smoke_ok |
| /lab/deployment/observe | POST | release_id + healthy 观察，不自动后台探测 |
| /lab/deployment/rollback | POST | 携带 release_id 模拟手动回退 |
| /lab/deployment/cancel | POST | 携带 release_id 取消未切流候选 |
| /lab/report?format=json 或 markdown | GET | 当前进程报告 |

所有这些 API 均需 Bearer 认证；服务端响应 no-store。模拟布尔字段必须是真正的
JSON true/false，字符串不被默默转换。实际代码版本见 `/version` 或 X-App-Version，
它不会因为改变模拟发布版本而变化。

部署后续操作必须使用对应 begin 返回的 release_id；缺失或格式错误返回 422，
过期 ID 返回 409。不要给旧检查结果换上新发布 ID 后重试。

## 接入真实环境前必须完成

- Linux/Docker/Redis/Prometheus/Grafana 全栈验收。
- 明确允许自动修复的服务、动作、权限和失败后的人工接管。
- 实际蓝绿资源预算、真实入口代理、健康探测和持久化发布状态。
- 选择告警接收渠道并验证送达；确定审计保留期。
- 浏览器桌面/移动视觉验收。当前仅完成 DOM 行为单测，不宣称界面视觉已验收。
