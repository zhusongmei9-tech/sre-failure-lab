# 受控自愈：先验证决策，再接真实执行器

本阶段实现自愈**决策状态机与模拟执行记录**，不重启任何真实服务，不访问 Docker
socket，不启动 shell，也没有后台轮询。输入的 healthy 是模拟探测结果，不能当成
真实服务恢复的证据。`real_actions_supported=false` 明确标记这一边界。

## 默认策略

| 项目 | 默认值 | 含义 |
| --- | --- | --- |
| enabled | false | 默认不触发模拟恢复动作，仍可观察异常 |
| 服务白名单 | api、redis | 其他服务名拒绝处理 |
| 连续失败阈值 | 3 | 单次抖动不触发动作 |
| 冷却时间 | 30 秒 | 同一事件内两次动作不能过近 |
| 最大尝试 | 2 | 不无限重复重启 |
| 连续恢复验证 | 2 | 动作记录“接受”不等于恢复 |
| 事件容量 | 200 | 只保留内存最近记录，重启丢失 |

真实动作暂不支持。启用开关只启用模拟记录，不能变成真实运行时权限。
不同服务独立计数。正常恢复后下一次新事件重新获得尝试预算；预算耗尽的锁定不会
因为随后探测健康就静默重新武装，需明确调用 reset(service)。reset 只重置控制器状态，
不是修复动作，也不能证明服务健康。

## 故障 → 指标 → 告警 → 排查 → 恢复 → 预期输出

### 故障：模拟 API 持续失败

先给控制器输入连续失败。在真实系统中这个输入应来自有超时、身份校验与业务语义的
探测器；当前只做受控模拟，不能随意把用户提交的 healthy 作为真实运维依据。

### 指标：观察故障状态，而不只看进程

检查服务条目中的 `consecutive_failures`、`attempts`、`phase` 与
`cooldown_remaining_seconds`。业务 HTTP 成功率/P95 和进程存活仍是不同口径。
Redis 故障时 API 可能降级仍返回 200，因此实际接入时必须单独探测依赖，不能仅看 API UP。

### 告警：预算耗尽需要人工处理

`healing_budget_exhausted` 事件意味着停止重复动作，`phase=locked_out`。
本模块只产生日志/事件，不向邮件或聊天发送通知，尚未配置外部接收人。
不能将状态机事件称为已经送达的告警。

### 排查：按事件时间线找判断依据

依次看 `healing_incident_detected`、`healing_probe`、`healing_action_simulated`、
`healing_recovery_verified` 或 `healing_budget_exhausted`。事件全部带
`simulation=true`。模块内部 `observed_monotonic` 是进程单调时钟，不是 UTC 日期；
接入统一 EventStore 后，外层日志另提供 UTC 时间。

### 恢复：动作接受之后仍要验证

模拟动作会记录 `accepted=true` 和 `recovered=false`。只有随后连续两次健康探测，
才记录 `healing_recovery_verified`。这只是模拟情景中的验证。真实恢复未来必须核对
服务进程、业务请求和依赖恢复，不能把 restart 命令退出码 0 当成修复完成。

### 预期输出：可复制的确定性示例

在项目根目录运行以下模拟，不需要 Redis 或 Docker：

```python
from app.healing import HealingController

now = [0.0]
c = HealingController(enabled=True, clock=lambda: now[0])
for _ in range(3):
    c.observe("api", False)
print(c.status()["services"]["api"]["phase"])  # verifying
print(c.status()["services"]["api"]["attempts"])  # 1
c.observe("api", True)
print(c.status()["services"]["api"]["phase"])  # verifying
c.observe("api", True)
print(c.status()["services"]["api"]["phase"])  # healthy
```

验证无法恢复时，连续输入失败，推进模拟时钟 30 秒，再输入失败。第二次动作之后仍失败
会进入 locked_out，后续失败不会再产生动作。输入两次健康会记录恢复观察，但保持锁定；
人工判断后 `c.reset("api")` 才返回 unknown、清零预算。

运行测试：`uv run --locked pytest -q tests/test_healing.py`。

## 实际部署前仍需确定

1. 探测器如何认证、取什么指标、多久探测一次，避免旧观测和并发重复事件。
2. 谁授权恢复哪个服务，运行时执行器有哪些最小权限。
3. 动作过程中超时、进程崩溃、控制器重启后如何恢复持久状态。
4. 哪个接收人收到预算耗尽通知，如何验证通知送达。

因此这一版可验证限次、冷却和恢复判定逻辑，但不能宣称真实故障已自动修复。
