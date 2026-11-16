# 137 真实运行模式

本版本新增宿主机真实控制程序 `ops`。它与 API `/lab/deployment`、`/lab/healing` 教学模拟器分开：模拟接口只改变内存状态；下列命令实际操作 Docker 容器、Nginx 和持久化运行记录。

## 机器与访问入口

- 目标：172.16.58.137，Rocky Linux 9.5，2 核、约 4.5GB 内存。
- 项目目录：`/opt/sre-failure-lab`。
- 业务入口：`http://172.16.58.137:18000/products/1`。
- API 文档：`http://172.16.58.137:18000/docs`。
- 教学只读控制台：`http://172.16.58.137:18000/console`，其中发布/自愈板块仍是模拟器，不代表宿主机控制器。
- Grafana：`http://172.16.58.137:3000`，业务看板 `/d/sre-failure-lab`，资源看板在 SRE Lab 文件夹。
- Prometheus：仅本机 9090，可用 SSH 转发访问。
- Grafana 密码及 API 实验令牌保存在主机 `.env`，不包含于源码包。

## 整套服务统一管理

2026-11-16 已安装 `sre-lab.service`，把八个运行容器和自愈子服务放在同一启停流程中。完整栈启动验证结束后才启动 healer。现场启动、停止、重启和自愈进程异常退出后的恢复均已通过；主服务已启用开机启动，但本轮未重启虚拟机。当前 green 0.3.1、上一槽 blue 0.3.0 保持不变，详见 [本轮验收记录](systemd-validation-137.md)。

在 `/opt/sre-failure-lab` 执行一次安装或单元更新：

```bash
sudo bash scripts/install_systemd.sh
```

安装器在修改前要求发布状态已是 idle，停止并取消旧 healer 的独立开机启动，安装父子单元、重载 systemd、enable 主服务，然后 start；主服务已 active 时则 restart。宿主机需有 Python 3.9+、Docker Engine 和 Compose 插件。日常只管理主服务：

```bash
sudo systemctl start sre-lab
sudo systemctl stop sre-lab
sudo systemctl restart sre-lab
systemctl status sre-lab --no-pager
sudo systemctl enable sre-lab
```

启停前结束流量验收与发布操作。停止顺序是先停 healer、再停项目全部容器；状态、镜像、卷和恢复预算保留。子服务 `sre-lab-healer` 为 static，不再单独 enable。启动按保存的槽和不可变镜像恢复，不 build、pull、init，也不自动发布或回滚。

`active (exited)` 只表示主服务的启动步骤完成。容器健康、子服务运行和 HTTP 需要另外检查，详见 [systemd 运维说明](systemd.md)。

## 真实状态和事件

```bash
cd /opt/sre-failure-lab
systemctl status sre-lab --no-pager
python3 -m ops.stack status
python3 -m ops.deploy status
python3 -m ops.heal status
tail -n 30 runtime/events.jsonl
systemctl status sre-lab-healer --no-pager
sudo journalctl -u sre-lab -u sre-lab-healer -n 100 --no-pager
```

真实发布状态、镜像 ID、活动槽和上一槽持久化在 `runtime/state.json`。恢复预算与冷却时间保存于 `runtime/healing.json`。模拟器的事件记录与此独立。

## 真实自愈

统一主服务启动成功后，systemd 才启动子服务中的 `python3 -m ops.heal run`，每轮真实检查 Redis PING 和活动 API 存活/商品接口。默认连续失败 3 次才处理，冷却 30 秒，每个事件最多尝试 2 次，修复后还需连续两次健康验证。发布和修复共用宿主机文件锁。

只允许恢复 Redis 和活动 API 容器。Redis 异常不导致无意义的 API 重启；API 进程本身死亡时仍可修复。API 默认启用缓存降级，Redis 恢复后后续请求自然重新使用缓存。

入口 Nginx 使用 Docker 进程重启策略，但此自愈控制器不覆盖入口配置错误。磁盘满、宿主机故障、网络分区也不属于当前自动修复范围。超过预算会锁定并写入事件，不发送外部消息。

```bash
# 查看和人工重新武装；reset 不执行修复
python3 -m ops.heal reset --service redis

# 仅在主服务保持运行、需要暂时暂停自愈的维护场景使用
systemctl stop sre-lab-healer
systemctl start sre-lab-healer
```

上面两条子服务命令只控制自愈，不代表整套服务启停。准备关机、停用项目或恢复全套服务时，应操作 `sre-lab`；不要重新单独 enable 子服务。

## 发布与回滚

候选镜像必须在本机存在，命名为 `sre-lab-api:<version>`，镜像内 APP_VERSION 与标签相同。代码变更发布使用项目 Dockerfile 构建完整镜像：

```bash
docker build --build-arg APP_VERSION=0.3.2 -t sre-lab-api:0.3.2 .
python3 -m ops.deploy deploy --image sre-lab-api:0.3.2 --observe-seconds 30
python3 -m ops.deploy rollback
```

程序记录不可变 image ID，启动另一槽，检查实际容器镜像、就绪、商品响应和版本，测试 Nginx 配置后重载切流，再观察候选及实际业务入口。入口版本及槽标记必须一致。观察失败时切回保留的旧版本并验证；回滚成功的失败发布仍返回非零。

这是同一虚拟机上的双容器蓝绿发布，不是跨主机高可用。状态变更只允许一个控制程序执行。复用旧槽会覆盖其原有副本；不承诺无限历史回滚。若候选占用旧槽失败，仍保留当前活动服务，但被覆盖的更早版本不会凭空恢复。

若进程中断或回滚未验证通过，状态会阻止继续发布；检查事件后执行 `python3 -m ops.deploy rollback` 协调恢复。主服务仍运行时，若 Redis 同时故障，自愈器在发布锁释放后仍会修复 Redis，但暂停 API 槽动作。主服务已停止或启动失败时，自愈子服务也不运行，需要按恢复流程处理依赖。非 idle 状态会拒绝主服务启动，并执行该项目的失败清理；不会修改状态记录来伪造一次完成的发布。

## 指标与资源

Prometheus 直接抓取活动槽，使用槽、版本和 image ID 区分时间序列。切流同步更新 file_sd 目标，避免将不同进程的累计计数拼接成虚假的请求增长。独立 `api_slots` 任务用于检查两个槽，不加入现有业务统计。

node_exporter 提供虚拟机 CPU、内存、文件系统与磁盘指标；受限 cAdvisor 提供 cgroup CPU/内存信息。面板缺失数据需结合采集器状态检查，不能视为零。Python 工作负载与控制器故障记录是不同数据源。

## 完整验收

`scripts/accept_runtime.py` 会真实发布、回滚、注入候选故障，并停止实验 API/Redis，验证控制器恢复。只在已初始化、当前为 0.3.0 的专用实验环境执行，且需本地 0.3.1 镜像与已启动的自愈服务。它不是只读检查。

报告保存为 `runtime/acceptance.json`。生产式部署的实测结论以该报告和实际业务版本响应为准，而不是教学模拟报告。

## 在另一台 Linux 机器重建

137 已完成业务初始化，不要重复 init。新机器将源码放到 `/opt/sre-failure-lab`，安装 Docker Engine、Compose 插件及 Python 3.9+ 后，先完成业务初始化，再安装统一 systemd 管理：

```bash
cd /opt/sre-failure-lab
python3 scripts/init_env.py
# 将以下两项追加到 .env；网关值先用 docker network inspect bridge 核实
printf '\nRUNTIME_BIND_HOST=0.0.0.0\nMETRICS_HOST_GATEWAY=172.17.0.1\n' >> .env
docker build --build-arg APP_VERSION=0.3.0 -t sre-lab-api:0.3.0 .
python3 -m ops.deploy init --image sre-lab-api:0.3.0
sudo bash scripts/install_systemd.sh
python3 -m ops.stack status
```

业务入口和 Grafana 绑定实验网络。`compose.yaml` 是独立的教学栈，不能用于恢复这里的真实栈。常规启停使用 `systemctl … sre-lab`；需要底层故障排查时，真实栈 Compose 参数必须与 `ops/common.py` 一致，包括 project、两个 env-file 和 runtime 配置文件，避免操作到另一个项目。
