# 验证记录（2026-11-12）

## 已完成

- Python 3.12：16 项测试通过，包括缓存、日志、指标、故障到期、控制接口隔离、降级、超时、真实 Redis 故障恢复和流量生成器。
- Ruff 代码检查通过。
- Prometheus 3.16.0 `promtool check rules`：4 条规则合法。
- `promtool test rules`：6 组场景通过，覆盖错误率触发与恢复、低流量抑制、无流量、API 失联与恢复、缓存故障、延迟告警。
- 原生进程集成：macOS 上启动真实 Redis 7.4.2、FastAPI 和 Prometheus，30 次商品查询被实际抓取，Grafana 配置内全部 10 条 PromQL 查询执行成功。
- 基线/降级实测：每种模式 30 次真实 HTTP 请求，目标 10 RPS，Redis 超时设置 0.2 秒。基线 30 次 503；降级 30 次 200，数据源读取增加 30 次。客户端 P95 约 206ms / 204ms。原始数据见 [对比报告](experiments/native-comparison.json)。
- Redis 原生定时暂停在到期后自动恢复，实测通过。

## 尚未完成的环境验收

Linux 虚拟机 SSH 连接超时，当前开发主机没有 Docker。因此尚未执行 Linux Docker 镜像构建、Compose 全栈启动与 Grafana 浏览器视觉验收；原生进程验证不替代这些步骤。

虚拟机可用后，在根目录执行：

```bash
docker compose config
make up
make smoke
make check-rules
make traffic
```

打开 Grafana 确认十个面板及数据源，依次执行三种故障与 `make recover`，最后执行 `make compare`。首次启动告警/速率曲线需要足够采样时间；比较测试会停止背景流量。

应用测试复现：

```bash
uv sync --locked --python 3.12
TEST_REDIS_SERVER="$(command -v redis-server)" uv run --locked pytest -q
uv run --locked ruff check .
```

没有本地 Redis 服务端时，真实 Redis 测试自动跳过，不能据此声称已经验证真实 Redis。


## 2026-11-13 增量验证（Linux 工作环境）

- 原始版本重跑：15 passed、1 skipped。与旧记录的口径分开保留。
- 新增 CPU 实验后：18 passed、1 skipped；Ruff 检查通过。
- 跳过项：真实 Redis 集成测试，当前没有 redis-server，未设置 TEST_REDIS_SERVER。
- CPU 组件实验：每组 20 次，基线/10 万/100 万迭代均返回 200；报告见 experiments/cpu-component.json。
- 自动测试新增：计算量边界、时间预算截断、自动到期、提前解除、并发拒绝以及故障期间控制接口可用。
- 当前没有 Docker CLI/daemon，因此未运行镜像构建、Compose 合并、promtool 和 Grafana 浏览器验收。
- 未修改用户机器、未启动主机指标采集器、未部署特权容器。

## 2026-11-13 第二轮：部署前优化

- 最终全量 pytest：47 passed、1 skipped（真实 Redis 集成仍未运行）。
- Ruff 与 compileall：通过。
- 新增覆盖：控制面认证、启动失败关闭、令牌不入 repr/日志、事件容量/排序/到期、重复故障冲突、未知参数拒绝、错商品缓存重建、CLI 无副作用校验与超时、初始化配置不覆盖和文件权限。
- 初始化测试仅使用固定的 test-only 测试字符串，未创建部署用真实 .env。
- 已准备 GitHub workflow，但未提交、触发或验证远端 CI。
- 没有执行任何部署，也没有运行资源采集器。容器只读文件系统和其他新限制仍需目标 Linux Docker 验收。

## 2026-11-14 完整模拟流程版 v0.2.0

- pytest 最终 **130 passed、1 skipped**（真实 Redis 集成仍跳过）。
- Node DOM 合约测试 **4 passed**：错误/正确令牌、下载、断开后过期响应不能复写、新连接优先、不持久化令牌、不发修改请求。
- Ruff、compileall、JavaScript 语法检查与 `uv sync --locked` 通过。
- 全链路组件演练通过：6 次商品请求（4 成功、2 注入 500）、模拟连续失败/恢复验证、v1/v2 发布、手动回退、v3-bad 观察失败回退。
- 原始输出存于 experiments/rehearsal-2026-11-14/。它使用假 Redis 与模拟控制器，`real_actions_executed=false`。
- 蓝绿槽复用验证：复用 inactive 槽即不再宣称旧 previous 仍运行，候选失败也不复活已覆盖版本。
- 当前没有 docker、redis-server 或 promtool：未执行构建、全栈启动、真实 Redis、规则场景或监控采集。
- 尝试浏览器验收：本地 Chromium 被环境 socket 权限阻止启动；独立浏览器访问测试端口连接拒绝。没有获得页面截图，不宣称桌面/移动视觉或真实浏览器交互通过。测试服务已停止。
- 新增控制器仅模拟，默认自愈禁用；真实执行器、后台可信探测、代理切流、持久化审计与外部通知未接入。
