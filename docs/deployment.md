# 蓝绿发布与回滚：先验证控制逻辑，再接真实执行器

## 当前边界

这是**内存状态机模拟器**，不是已经可向真实集群发布的部署平台。它不会构建镜像、启动容器、
调用 Docker、修改 Nginx、切换真实流量或执行任意命令。`active` 只表示模拟的流量指针。
状态明确返回 `mode=simulation`、`real_actions_enabled=false`。最初 `active=null`，
不会假装 v1 已部署。当前业务 API 的真实版本也不会随这个模拟指针变化。

这样可以先验证最容易出事故的控制规则，再确认目标机器和执行权限。版本 `v1`、`v2`、
`v3-bad` 都是标签，模拟器不会根据名称猜健康状态；探测结果必须明确传入。

## 规则

每次 `begin(version)` 返回唯一的 `release_id`。后续候选校验、观察、取消和回滚都必须
携带这个 ID；检查在状态锁内进行，ID 与当前发布不一致时拒绝操作，且不修改任何状态或事件。
HTTP 缺失/格式错误返回 422，过期 ID 返回 409。客户端应保存开始发布时拿到的 ID，
不能在提交旧探测结果时重新获取当前 ID 来绕过关联校验。

1. `begin(version)` 占用发布锁，把候选放入不活动的蓝/绿槽，进入 validating。此时不切流。
   如果该槽原来保存 previous 版本，它会被候选覆盖，立即失去回滚资格，并记录
   `deployment_previous_replaced`。候选取消或校验失败不会凭空找回已覆盖的旧副本。
2. `check_candidate(ready, smoke_ok, release_id=...)` 必须同时通过就绪和业务检查，才切换**模拟**指针。
   任一失败则拒绝候选，旧版本不动，这叫“发布被拒绝”，不是“回滚成功”。
3. 切换后进入 observing，保留旧版本并继续持有发布锁。
4. `observe(healthy, release_id=...)` 提交后续探测证据。失败立即回切；没有旧版本时报告
   `failed_no_previous` 并清空模拟指针，不能报告已回滚。
5. 经过观察窗口还需要一个健康样本才完成。仅时间过去或读取 status 不会自动成功。
   超过窗口后才提交的失败样本也仍会导致回滚。
6. `rollback(release_id=...)` 手动切回保留的已接受版本；观察期内回滚不会把坏候选保存成回滚目标。
   已完成发布后手动回滚会交换新旧已接受版本，可用于演示来回切换。
7. `cancel(release_id=...)` 只取消 validating 阶段的未接流候选，不会误改已活动的版本；但不会恢复该槽
   在 begin 时已被覆盖的 previous。若需要重新使用那个版本，必须重新 begin 并完成检查。

这里 previous 表示**仍占用另一个槽的可回切实例**，不是长期镜像档案。例如 v1 在 blue、
v2 在 green，开始把 v3 放入 blue 后，v1 已不再可直接回切。v3 若切流后失败，可以回到
green 的 v2，但不能再宣称 blue 中还存在 v1。当前模拟器不额外保存第三套实例或重建旧镜像。

默认观察窗口 30 秒，可在构造控制器时设置 1–3600 秒。时钟使用 monotonic，记录事件使用 UTC。
没有后台自动探测任务；人工提供的样本只用于模拟，不能当成来自真实服务的健康证据。
发布锁只在同一个进程/控制器内有效，当前不支持多 worker 或多控制器并行管理同一部署。

## 教学链路：故障 → 指标 → 告警 → 排查 → 恢复 → 预期输出

| 环节 | 操作与判断 |
| --- | --- |
| 故障 | v1 已模拟完成，再发布 v3-bad；候选检查先通过，观察期提交 unhealthy |
| 指标 | 查看 phase、active/previous、remaining_seconds 与事件时间线。这里没有真实容器流量指标 |
| 告警 | `deployment_rolled_back` / `deployment_failed_no_previous` 是控制事件；当前未连接外部告警渠道 |
| 排查 | 用 release_id 关联 candidate_checked、switched、observed。检查失败在切流前还是切流后 |
| 恢复 | 观察失败时状态机自动回切到先前已接受版本，或手动 rollback；无旧版则明确失败 |
| 预期输出 | 有旧版本：active.version=v1、last_outcome=rolled_back、locked=false；首次发布：active=null、last_outcome=failed_no_previous |

不能用模拟器的 switched 事件证明真实用户已访问新版本；必须等真实执行器与探测器实现后，
同时保存代理配置、版本响应、业务探测结果等证据。

## 本地可复现演示（不等待真实时间、不部署）

在项目根目录执行：

```bash
python3 - <<'PY'
from app.deployment import DeploymentController

now = [100.0]  # 仅用于演示的虚拟时钟
ctl = DeploymentController(clock=lambda: now[0], observation_seconds=10)
release_id = ctl.begin("v1")["release_id"]
ctl.check_candidate(ready=True, smoke_ok=True, release_id=release_id)
now[0] += 10
ctl.observe(healthy=True, release_id=release_id)
assert ctl.status()["active"]["version"] == "v1"

release_id = ctl.begin("v3-bad")["release_id"]
ctl.check_candidate(ready=True, smoke_ok=True, release_id=release_id)
result = ctl.observe(healthy=False, release_id=release_id)
print({key: result[key] for key in ("mode", "active", "last_outcome", "locked")})
PY
```

预期核心输出：`simulation`、`active={'slot':'blue','version':'v1'}`、
`last_outcome='rolled_back'`、`locked=False`。

将第二次 `check_candidate` 的 `smoke_ok` 改成 False 时，不再调用 observe；
预期 `last_outcome=rejected_before_switch`，v1 从未被替换。
首次就发布坏版本，双检查通过后再传入 unhealthy，预期 `failed_no_previous` 而不是 rolled_back。

## 接口与事件

Python 入口为 `app.deployment.DeploymentController`；构造参数为 `clock`、
`observation_seconds`、`on_event(action, details)`。所有对外方法返回 JSON 可序列化的
status 快照。版本只允许 1–64 位字母、数字、点、下划线、连字符，且首位必须是字母或数字。
探测值必须是布尔值，不接受字符串 `"false"` 被误当作真。

非法顺序/并发发布抛出 ValueError，API 层应明确返回冲突或参数错误。事件有唯一 event_id、
release_id、UTC 时间、action 和 mode；最多保留 200 条，重启全部丢失，不是持久化发布账本。
API 操作应沿用已有 `/lab` Bearer 认证，不新增无认证的管理入口。

例如 `/lab/deployment/candidate-check` 请求体为：

```json
{"release_id":"由 begin 返回的 32 位十六进制 ID","ready":true,"smoke_ok":true}
```

`observe` 使用 `release_id` 与 `healthy`；`cancel`、`rollback` 也需要包含 `release_id`
的 JSON 请求体。Python 方法将该字段设为必填的关键字参数，没有兼容不带 ID 的旧调用。

## 验证范围与下一步

自动测试覆盖双检查失败、首次失败、观察期完成、过期后的失败、自动/手动回滚、取消、
保留未被覆盖的旧版本、槽复用后取消/拒绝/回滚的一致性、并发锁、参数类型（拒绝布尔观察时长）、
事件容量及快照隔离。新增回归验证包括：取消旧发布后新候选不会被迟到检查放行，旧观察
不会回滚新发布，旧取消/回滚请求无效，以及等待锁期间当前发布改变后的拒绝行为。
不需要 Docker，也不会操作真实环境。

真实部署之前还需决定：目标 Linux 主机与资源预算、执行动作白名单、代理切换方式和恢复权限。
后续真实适配器必须默认禁用，使用固定参数而非任意 shell，检查镜像 digest、资源和配置，
保存真实探测证据。数据库迁移不在当前只读商品演示范围内，不能套用镜像回滚宣称数据可回滚。
