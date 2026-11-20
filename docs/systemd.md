# 整套 SRE 实验室的 systemd 管理

`sre-lab.service` 是 137 真实运行栈的统一启停入口，管理业务蓝绿槽、Redis、Nginx、Prometheus、Grafana、node_exporter、cAdvisor，以及作为子服务运行的自愈控制器。当前 137 保留两个已发布槽，共八个容器。

此前主机只启用了独立的 `sre-lab-healer.service`。手工停止容器再开机后，自愈器可能恢复 API 与 Redis，但不会负责启动入口和整套监控。本轮改为先恢复完整运行栈，再启动自愈，解决这个启动职责不完整的问题。

2026-11-16 已在 137 完成安装及启动、停止、重启、自愈进程崩溃恢复的现场验收。主服务已 enabled，八个容器与五个 Prometheus 采集目标正常。本轮没有重启虚拟机，因此开机配置已核实，实际重启机器后的恢复尚未验证。完整证据见 [本轮验收记录](systemd-validation-137.md)。

## 前提

- Linux 使用 systemd，安装 Docker Engine、Docker Compose 插件和 Python 3.9 以上版本。
- 项目位于 `/opt/sre-failure-lab`；安装器和单元的默认路径固定为此处。
- 已初始化真实运行栈，私密 `.env`、`runtime/state.json`、槽对应的本地镜像和必要运行文件存在。
- 发布状态为 idle。主服务负责恢复已保存的部署，不负责首次初始化或猜测未完成发布的结果。
- 安装和 systemctl 管理命令使用 root 权限；普通状态读取仍受运行文件访问权限约束。

首次镜像构建和初始化见 [真实发布](real-deployment.md)。已部署的 137 不需要因为关机或启动失败而重新初始化。

## 安装或更新

```bash
cd /opt/sre-failure-lab
sudo bash scripts/install_systemd.sh
```

加上 `--check` 可以预览父、子服务的操作。安装器要求发布状态为 idle，检查 unit 文件、开机设置、服务状态和预期容器。配置一致且运行正常时，再次执行输出 `SRE_LAB_SYSTEMD_CHANGED=0`。主 unit 更新或预期容器缺失时会重启整栈，仅自愈 unit 更新时重启自愈器。涉及重启的更新应与发布及故障演练错开。使用 Ansible 远程执行和复查的方法见[部署手册](ansible-deploy.md)。

新的自愈单元是 static 子服务，不再独立 enable。原来的独立启动关系由安装流程迁移；以后只为 `sre-lab` 设置开机启动。

## 日常操作

| 需求 | 命令 |
| --- | --- |
| 启动整套服务 | `sudo systemctl start sre-lab` |
| 停止整套服务 | `sudo systemctl stop sre-lab` |
| 重启整套服务 | `sudo systemctl restart sre-lab` |
| 查看主服务状态 | `systemctl status sre-lab --no-pager` |
| 开机启动 | `sudo systemctl enable sre-lab` |
| 取消开机启动，当前运行不变 | `sudo systemctl disable sre-lab` |
| 现在停止并取消开机启动 | `sudo systemctl disable --now sre-lab` |

`enable` 不会立即启动服务；安装器会另外执行启动步骤。日常临时停止不会自动取消开机启动，下一次开机仍按 enabled 状态启动。若希望关机后再次开机也保持停止，使用 `disable --now`，以后再按需要 enable 和 start。

停止前结束持续流量、验收脚本与发布进程。这些按需启动的操作不属于常驻自愈子服务，主服务不会替它们完成一场未结束的发布。启停与发布共用操作锁，启停等待锁最多 30 秒；不要同时发起发布和整套服务启停。

## 启动和停止顺序

```text
start sre-lab
  → Docker 可用
  → 校验已保存的发布状态及不可变镜像
  → 恢复运行栈与活动槽，完成启动检查
  → 主服务 active (exited)
  → 启动 sre-lab-healer

stop sre-lab
  → 停止 sre-lab-healer
  → 停止该项目的全部容器（含两个 slots）
  → 保留数据卷与 runtime 记录
```

父单元采用 `Type=oneshot` 和 `RemainAfterExit=yes`，用 Requires、After 依赖并排在 Docker 之后运行。它通过 Wants 引入自愈子服务；子服务用 BindsTo、After、PartOf 关联主服务，确保主服务停止或失败时自愈不再独立干预容器。重启主服务会执行完整的停止、启动流程。

子服务本身异常退出可由自己的重启策略处理。Wants 不等于保证子服务始终健康，因此必须同时检查子服务状态，不能只看父服务的一行 active。

## 启动恢复哪些内容

`ops.stack` 根据 `runtime/state.json` 中保存的活动槽、保留槽和不可变镜像 ID 恢复运行。镜像标签即使后来改变，也不能静默替换已保存的部署。启动不执行镜像 build、pull、首次 init、发布新版本或自动回滚。

只恢复 `state.slots` 中已记录的槽。当前 137 保留 blue、green，共八个容器；新环境若只初始化 blue，则恢复七个容器，不会为了凑齐两个槽而创建新版本。启动会验证活动 API 与入口的业务、版本和完整就绪状态，检查 Prometheus 就绪、Grafana 数据库，以及容器运行集合。

如果缺少镜像、状态损坏、发布不是 idle，或启动检查失败，主服务启动失败；父单元的 `ExecStopPost` 仍会尝试停止该项目容器，避免留下部分启动的实验栈。这包括在启动校验阶段就被拒绝的情况。清理失败也必须作为错误处理，不能仅凭 systemd 主服务失败就认定所有容器已经停止；应再检查 `ops.stack status` 和日志。

停止、重启与失败清理都不删除命名数据卷、`runtime/state.json`、`runtime/healing.json` 或事件记录，也不自动重置恢复预算。Redis 本身未配置持久化，进程停止后缓存内容会丢失；商品样例数据不受影响。API 进程内教学模拟状态和故障开关在重启后重置，这是与宿主机持久运行状态不同的一层。

## 状态与日志

```bash
cd /opt/sre-failure-lab
systemctl status sre-lab --no-pager
systemctl status sre-lab-healer --no-pager
python3 -m ops.stack status
python3 -m ops.deploy status
python3 -m ops.heal status
sudo journalctl -u sre-lab -u sre-lab-healer -n 100 --no-pager
sudo journalctl -u sre-lab -u sre-lab-healer -f
```

各入口回答不同问题：

- `systemctl status sre-lab`：启动流程是否完成、最后一次退出状态是什么。
- `ops.stack status`：该项目的 Docker 状态快照，列出每个容器的 service、state、health，以及 expected_services、missing_services、not_running、unexpected_services 和 all_running；同时显示活动槽、版本、上一槽、发布阶段和 release_id。没有配置 Docker 健康检查的容器不提供健康结果；此命令不会额外发起业务 HTTP 检查。
- `systemctl status sre-lab-healer`：自愈进程是否正在运行。
- `ops.deploy status`：已保存的活动槽、版本、镜像和发布阶段。
- `ops.heal status`：恢复预算、冷却、锁定和最近探测。
- journal：本次启动、停止和控制器进程输出；`runtime/events.jsonl` 另保存运行事件。

业务就绪还应检查 HTTP 和监控：

```bash
curl -fsS http://127.0.0.1:18000/health/ready
curl -fsS http://127.0.0.1:18000/version
curl -fsS http://127.0.0.1:18000/products/1
```

`active (exited)` 不会持续轮询容器，也不能证明监控采集完整。后续故障依赖容器运行时策略、真实自愈与监控发现；仍需观察 Grafana、Prometheus 目标状态和用户请求。

## 非 idle 状态与异常恢复

若 `runtime/state.json` 处于 preparing、switching、observing 或 recovery_required，启动拒绝自动继续。先查看事件，确认对应发布进程是否仍在运行；不要直接把 phase 改为 idle，也不要删掉状态文件重新 init。

如有可恢复的稳定旧版本，按 [真实发布与中断恢复](real-deployment.md) 操作。`python3 -m ops.deploy rollback` 会修改实际部署，执行前需确认记录、镜像及依赖具备恢复条件。恢复验证通过、状态回到 idle 后，再执行 `systemctl start sre-lab`。整套服务已停止时，可能需要先按该恢复流程恢复必要依赖；主服务不会擅自推断正确回滚目标。

若镜像丢失，应恢复已记录的同一镜像制品；不要仅把新制品改成相同标签。若自愈预算耗尽，应先排除原因，再按 [真实自愈](real-healing.md) 人工重置预算。重新启动主服务不会自动解除锁定。

## 本轮验收范围

现场通过：单元语法与依赖检查、主服务 enabled / 子服务 static、八容器启动、业务与监控可达、统一停止后不反弹、再次启动与重启、子服务被终止后自动恢复。启停前后发布状态文件 SHA-256、恢复预算关键字段、两个监控卷的名称及创建时间一致。

启动失败清理、非 idle 拒绝、错误镜像和操作锁竞争由本地受控执行器测试覆盖，没有在本轮远程单元上注入启动失败。重启虚拟机后的开机恢复尚未实测。后续验收可在计划重启主机后，检查父子服务、`ops.stack status`、HTTP 和 Prometheus 目标；检查命令见上文。

此处的配置、安装与启停均针对 `compose.runtime.yaml` 真实栈。教学栈 `compose.yaml`、`make up` 和 `/console` 模拟状态不属于这项 systemd 生命周期管理。
