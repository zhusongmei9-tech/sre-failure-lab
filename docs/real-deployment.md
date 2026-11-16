# Linux 真实蓝绿发布与回滚

`ops/deploy.py` 在宿主机调用固定的 Docker Compose 操作、检查实际 HTTP 响应，并修改 Nginx 上游；它与 `/lab/deployment/*` 的内存模拟器是两个独立入口。真实业务版本由镜像内 `APP_VERSION` 决定，模拟发布不会改变真实服务。

宿主机需要 Python 3.9+、Docker Engine 与 Compose 插件。运行目录包含 `compose.runtime.yaml`、私有 `.env` 和 `runtime/`。CLI 不接受任意 shell 命令，只允许本地 `sre-lab-api:<version>` 镜像；每个版本必须使用独立标签。镜像的真实 ID 会写入部署配置，标签后续被移动也不会替换已经选中的发布制品。

## 首次部署

先构建带明确版本号的镜像，再初始化：

```bash
docker build --build-arg APP_VERSION=0.3.0 -t sre-lab-api:0.3.0 .
python3 -m ops.deploy init --image sre-lab-api:0.3.0
python3 -m ops.deploy status
```

初始化会启动 Redis、blue 槽和 Nginx 入口。blue 本地端口为 18001、green 为 18002，入口为 18000。初始化必须通过存活、完整就绪、版本一致性和商品查询验证；降级返回 200 不足以通过候选就绪检查。首次失败不会伪称“已回滚”；修复环境后可再次执行 init。

初始化后使用统一 systemd 安装器接管整套服务：

```bash
cd /opt/sre-failure-lab
sudo bash scripts/install_systemd.sh
python3 -m ops.stack status
```

`sre-lab.service` 负责恢复已保存的部署、监控和自愈子服务；正常关机后使用 `systemctl start sre-lab`，不要重复 init。主服务不会 build、pull 或发布新镜像。当前只有 blue 记录的新环境只恢复该槽；137 已保留两槽时恢复两个槽。完整机制见 [统一 systemd 管理](systemd.md)，实测范围见 [本轮验收记录](systemd-validation-137.md)。

## 正常发布

先确认 `sre-lab` 已启动，且没有同时进行整套服务的 stop/restart。版本发布由下面的 CLI 明确执行；systemd restart 只是恢复已保存的版本，不是发布操作。

```bash
docker build --build-arg APP_VERSION=0.3.1 -t sre-lab-api:0.3.1 .
python3 -m ops.deploy deploy --image sre-lab-api:0.3.1 --observe-seconds 30
curl http://127.0.0.1:18000/version
```

流程为：获取主机操作锁 → 校验本地镜像 ID → 重建非活动槽 → 验证真实容器镜像及业务 → 写入 Nginx 配置 → `nginx -t` → reload → 检查入口版本 → 连续观察候选与入口 → 提交状态。

入口验证同时核对版本响应与 `X-Deployment-Slot`，防止 Nginx reload 尚未生效时误把旧槽响应当成切换成功。初始化重试也会重新加载入口，刷新候选容器重建后的地址。

观察期中出现失败，会切回保留的旧槽，并重新验证旧版本的入口响应。发布命令仍以非零状态退出，表示该次发布失败；不能把“回滚成功”当成“新版本发布成功”。该入口提供逐次 HTTP 探测，不代表完整生产流量的错误率分析或零中断保证。

## 手动回滚与中断恢复

```bash
python3 -m ops.deploy rollback
```

普通回滚使用另一个槽中保留的版本，先验证容器镜像、就绪和业务，再切换流量。已开始下一次发布并复用旧槽时，被覆盖版本不再是可直接回切副本；需要重新构建/发布才可再次使用。

如果 CLI 被终止或主机崩溃，持久状态会保留 `preparing`、`switching`、`observing` 或 `recovery_required`。新发布会被拒绝；主服务仍在运行时，自愈进程暂停 API 槽动作，发布锁释放后仍允许恢复共享 Redis。`rollback` 根据记录的稳定槽恢复入口，并验证后解除锁定。若没有稳定旧版、旧容器消失或恢复检查失败，命令报错并保留待处理状态，不宣称恢复成功。

统一启动也拒绝非 idle 状态，不会替用户自动选一个版本继续发布或回滚。通过 systemctl 启动失败时，主服务会尝试停止该项目容器；它不会删除发布记录或监控卷。若整个运行栈已经停止，应先检查依赖与镜像的恢复条件，完成必要的人工协调及 `ops.deploy rollback`，再启动主服务。不要把状态手工改成 idle 来绕过检查。

## 状态与审计

- `runtime/state.json`：真实 active/previous、镜像 ID、发布 ID、操作阶段和结果。
- `runtime/images.env`：两个槽对应的不可变本地镜像 ID，不保存登录凭据。
- `runtime/nginx/upstream.conf`：入口的实际上游配置。
- `runtime/events.jsonl`：发布开始、切换、完成、失败和回滚事件，带 UTC 时间且标识非模拟。
- `runtime/prometheus/active.json`：当前活动槽的 Prometheus file_sd 目标，包含槽、版本和镜像 ID 标签，避免跨发布版本混用同一计数器身份。该文件不含凭据，允许监控容器只读访问。
- `runtime/operations.lock`：发布、自愈和整套服务启停共用的主机文件锁。启停最多等待锁 30 秒；正常操作不要将发布与 stop/restart 并行执行。

状态和配置采用同目录临时文件加原子替换；跨文件变更通过记录中间阶段恢复。它不是跨主机分布式事务，当前仅支持单个 Linux 实验机、两个槽和无数据库迁移的只读业务。旧版本依赖仍必须可用，才能验证回滚成功。

## 验证

`tests/test_real_deployment.py` 使用受控执行器验证切换决策、观察失败回滚、回滚失败锁定、首次失败、中断恢复和 Nginx 配置恢复。它不替代目标 Linux 上的镜像构建、真实入口切换与故障回退验收；现场记录应另存于部署验证报告。
