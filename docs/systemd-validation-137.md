# 137 systemd 统一管理验收记录

日期：2026-11-16。目标：172.16.58.137，Rocky Linux 9.5、systemd 252。源码和运行目录为 `/opt/sre-failure-lab`。

已将八个容器及真实自愈控制器纳入 `sre-lab.service`。交付时主服务 active (exited)、enabled，自愈子服务 active (running)、static，所有容器运行，五个 Prometheus 采集目标为 up。

## 变更内容

- `ops/sre-lab.service`：整套服务的 systemd 入口，等待 Docker 与网络，启动完成后保持 active，正常停止或启动失败时调用清理。
- `ops/sre-lab-healer.service`：改为跟随主服务的子单元。迁移时已删除旧的独立开机启动链接。
- `ops/stack.py`：恢复已保存的不可变镜像、活动路由及监控，提供 start、stop、status；与发布和自愈共享操作锁。
- `scripts/install_systemd.sh`：在固定目录安装及检查单元，启用主服务并启动，检查父子服务和容器快照。
- `tests/test_stack.py`：18 项生命周期及只读状态测试。

## 实机验证

| 项目 | 结果与证据 |
| --- | --- |
| 安装检查 | `systemd-analyze verify` 通过，main enabled、healer static；旧独立开机链接已删除 |
| 初次统一启动 | 八容器全部运行；有 Docker healthcheck 的 API、Redis、cAdvisor 均 healthy |
| 业务检查 | ready 返回 ready/redis ok，商品 1 查询成功，入口版本 0.3.1 |
| 监控检查 | Grafana database ok；活动 API、两个 API 槽、node、cAdvisor 共五个 Prometheus 目标全部 up |
| `systemctl stop sre-lab` | 父子服务均 inactive，项目运行容器数为 0；等待 8 秒后没有被拉起 |
| 再次 start | 八容器及自愈恢复，入口版本、Grafana 和全部采集目标正常；时间戳确认自愈在主启动检查完成后启动 |
| `systemctl restart sre-lab` | 完整停止后重新启动，自愈 PID 更新，八容器和业务监控恢复 |
| 自愈进程异常退出 | 对 healer 主进程发送 SIGKILL；8 秒后的快照已恢复 active running，PID 更新，NRestarts 增加至 1，主服务仍 active |
| 外部访问 | 从工作电脑访问 137 的版本、商品接口和 Grafana 健康接口，均为 HTTP 200 |
| 保留发布记录 | 启停、重启前后 state.json SHA-256 相同，仍为 green 0.3.1，保留 blue 0.3.0，release_id 未改变 |
| 保留恢复预算 | Redis/API 的 attempts、locked_out、last_action_at、incident_id 前后一致；未执行 reset |
| 保留监控卷 | 两个命名卷的名称及创建时间均保持不变，没有删除或重建卷 |

原始快照和逐项布尔检查保存在 [JSON 证据](experiments/systemd-137-2026-11-16.json)。安装前的自愈单元和发布状态副本保存在目标机 `runtime/systemd-backup-20261116/`；未将凭据写入报告。

## 本地检查与边界

- Python 全套测试：207 passed，8.04 秒；启用了真实 Redis 测试，无跳过。
- Ruff：通过。安装脚本 Bash 语法检查：通过。宿主机 Python 3.9 编译检查：通过。
- 启动失败清理、非 idle 拒绝、错误镜像、监控验证失败、锁竞争以及数据保留由受控执行器测试覆盖；本轮没有在远程 systemd 单元上注入启动失败。
- 主服务已经设为开机启动，但本轮没有重启虚拟机；实际重启机器后的恢复尚未实测。
- `active (exited)` 表示启动检查已完成，不是持续健康证明。`ops.stack status` 的 all_running 只表示容器运行条件满足，健康字段、业务探测和采集目标需分别查看。
- 停止会清空未持久化的 Redis 缓存，以及 API 进程内的教学模拟状态；宿主机发布状态、恢复预算和监控数据卷保留。

日常使用 `systemctl start/stop/restart/status sre-lab`，完整操作说明见 [systemd 管理指南](systemd.md)。
