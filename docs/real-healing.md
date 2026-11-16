# 真实服务自愈

`app/healing.py` 保留教学模拟器；真正操作 Linux Docker 的控制器是宿主机上的 `ops/heal.py`。它读取真实探测结果，执行限定的 Compose 重启，并在后续探测中验证业务恢复。不会把人工提交的 `healthy=true` 当作执行依据。

## 启动与查看

先按 `real-deployment.md` 初始化真实运行栈，并按 [统一 systemd 管理](systemd.md) 安装父子单元。宿主机 Python 需要 3.9 或更高版本，执行用户需有该实验 Docker 的操作权限。

```bash
sudo systemctl start sre-lab
systemctl status sre-lab --no-pager
systemctl status sre-lab-healer --no-pager
```

整套容器启动验证成功后，自愈子服务才开始工作。`sre-lab-healer` 使用 BindsTo、After、PartOf 关联主服务，是 static 子单元，不再单独 enable；普通关停使用 `systemctl stop sre-lab`，会先停自愈再停容器。

查看实际状态与日志：

```bash
cd /opt/sre-failure-lab
python3 -m ops.stack status
python3 -m ops.heal status
tail -n 30 runtime/events.jsonl
sudo journalctl -u sre-lab -u sre-lab-healer -n 100 --no-pager
```

`status` 只读。主服务的 `active (exited)` 仅表示启动步骤完成，不代表自愈进程或容器始终健康。直接运行 `python3 -m ops.heal run` 仍是前台执行入口，但仅用于明确的调试场景，不应与 systemd 子服务同时启动。

若只想暂停自愈而保持业务运行，可在主服务保持 active 时暂时 `systemctl stop sre-lab-healer`，维护结束后 `systemctl start sre-lab-healer`。这不等于整套服务启停，也无需重新 enable 子服务。2026-11-16 已实测主服务停止时自愈同时停止、主服务重启时自愈重新启动，以及自愈进程被终止后的自动恢复，见 [本轮验收记录](systemd-validation-137.md)。

## 决策与动作

- 默认每次循环结束后等待 5 秒，连续 3 次失败才执行动作；实际间隔还包括探测及 Docker 操作耗时。
- Redis 用独立、最长 3 秒的 `redis-cli PING` 检查。API 检查当前活动槽的存活接口与商品 1，两项 HTTP 探测各最多 2 秒。
- 商品探测经过真实业务路由，因此会计入商品请求指标；没有人工流量时也会有少量探测请求，不能把这些请求当成真实用户访问量。
- Redis 异常时优先修复 Redis；此时仅商品业务失败不触发 API 重启，避免因依赖异常反复重启业务。如果 API 存活检查也失败，仍可修复 API 进程，以便恢复降级服务。
- 存活检查成功、商品接口返回 429 时，记录 `api_overloaded`，健康值仍为 false，但不以重启处理主动限流，也不累计连续可重启失败。恢复预算、冷却和锁定状态保留；真正的存活失败、500 或超时仍按故障处理。
- 只允许重启 `redis`、`api-blue`、`api-green`，并且 API 只能重启当前活动槽。每轮最多执行一个动作，Docker 重启操作最多等待 20 秒。
- 每次动作后，至少两个后续连续成功探测才记为恢复。Docker 命令成功退出只记录动作被接受。
- 同一服务动作之间至少冷却 30 秒。冷却时间在正常恢复、新故障及控制器重启后保留。
- 每个故障事件最多尝试 2 次；预算耗尽后锁定，即使后来恢复健康也不自动解除。人工判断后显式重新武装：

```bash
python3 -m ops.heal reset --service redis
python3 -m ops.heal reset --service api
```

重置预算不会绕过未结束的冷却时间，也不是恢复命令。

## 持久状态和发布协调

`runtime/healing.json` 保存服务失败次数、修复预算、冷却时间、故障 ID 和恢复状态。每次真实重启前先原子写入动作预算，避免控制器崩溃后把已执行的动作遗忘。状态文件损坏时停止自动动作，不会静默清空预算。

自愈、发布和整套服务启停共用 `runtime/operations.lock`。发布持有锁时，自愈跳过该轮；自愈先拿锁再读取活动槽，避免重启已经被换下的版本。活动槽或镜像改变后，新实例重新累计失败，但保留服务级冷却时间。

如果发布进程退出后仍留下 `recovery_required`、`observing` 等非 idle 状态，且主服务和控制器仍在运行，自愈只检查和恢复共享 Redis，不探测或重启任何 API 槽，也不修改发布状态或执行回滚。这样发布时的 Redis 故障不会阻断依赖恢复。此时 `status` 显示 `api_actions_suspended=true`。Redis 恢复后，操作人员运行 `python3 -m ops.deploy rollback` 完成发布状态协调；确认发布恢复 idle 后，API 自愈才恢复。

主服务已停止或启动失败时，子服务也会停止，不再进行上述 Redis-only 恢复。统一启动会拒绝非 idle 状态并尝试停止该项目容器，需要人工检查必要依赖、镜像和稳定版本后协调恢复。尚未初始化运行栈时不执行任何自愈；主服务重启也不会清空已有修复预算或锁定。

状态中的时间戳使用系统时间以支持进程重启；请保持宿主机时钟正确。时钟向后调整时，冷却会保守延长。

## 实验验收

在独立实验机器上启动控制器后，使用相同的运行栈 Compose 参数停止当前活动 API 容器，或停止 Redis。不要删除容器或修改状态文件来模拟普通退出。

1. 确认出现 `healing_incident_detected`。
2. 连续失败后出现 `healing_action_requested` 与 `healing_action_accepted`。
3. 确认容器实际重新启动，而不是只改变模拟状态。
4. 后续两次探测成功后出现 `healing_recovery_verified`，并核对客户端商品请求恢复。
5. Redis 场景同时核对降级期间返回 BYPASS，恢复后重新出现 MISS/HIT。

事件记录在 `runtime/events.jsonl`，包含动作目标、故障 ID、原因、尝试次数及恢复耗时，不记录令牌。无法恢复时产生 `healing_budget_exhausted`；本项目尚未把该事件发送到邮件或聊天接收人。

## 边界

这是单机实验恢复执行器，不能修复镜像逻辑缺陷、宿主机磁盘满、Docker 守护进程故障或整台机器断电。它不会删除数据、拉取镜像、改变代理路由或自动解除故障注入配置。API 容器重启会清除进程内实验故障与模拟状态，这是当前实验的预期行为。需要保持注入故障用于观测时，应暂时停止真实自愈服务。
