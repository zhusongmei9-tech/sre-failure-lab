# 无集群准备流程验证记录：2026-11-17

本轮按“没有可用 Kubernetes 集群”完成设计和代码验证。未连接虚拟机、未创建集群、未重启主机、未在 Kubernetes 中删除 Pod 或注入告警。以下结果不能作为真实恢复时间、通知送达或安装成功的证明。

## 已实际运行

| 项目 | 本地结果 |
| --- | --- |
| 全量 Python 测试 | **362 passed、1 skipped**；跳过项是没有配置 `TEST_REDIS_SERVER` 的真实 Redis 集成测试 |
| 控制台 DOM 测试 | 4 passed；不属于浏览器渲染检查 |
| Ruff / Python 编译 | 通过 |
| 主机、演示、告警三个 `--plan` 入口 | 不要求集群；输出 `status=not_tested` |
| 基础监控配置 | Helm 4.3.2 lint/客户端模板渲染通过，51 个对象、26 组 Prometheus 规则通过语法检查 |
| 可选告警配置 | Helm lint/客户端模板渲染通过，57 个对象、27 组规则通过语法检查 |
| 自定义规则 | 4 类指标规则及独立合成规则；10 个时间序列场景通过 |
| Alertmanager 配置与路由 | amtool 0.35.0 检查实际渲染的配置；模块告警去 lab-webhook，其他告警去 discard |
| 既有业务告警/SLO | promtool 3.16.0 运行两个已有规则测试文件，通过 |
| CI 文件、文档链接 | YAML、嵌入 Shell 语法、相关本地文档链接、diff 空白检查通过 |

固定 chart 为 kube-prometheus-stack 93.3.1，校验 SHA256：`a7fbf983aadc20548c167c40ba2e78fecbf03753830911130419a16064c91aff`。

完整本地配置检查报告保存在被 Git 忽略的 `reports/kubernetes-config-validation.json`。新增的 GitHub Actions `Kubernetes configuration (no cluster)` 任务在 Linux AMD64 runner 上执行同类配置检查；每次提交的结果以对应 Actions 运行记录为准，不据此宣称 AMD64 集群安装已通过。

## 保护逻辑覆盖

- 默认计划不访问主机或集群；实际主机检查失败、未知时不能变成安装通过。
- 重启必须有原始健康基线，machine-id、集群和 Node 身份正确，boot ID 已改变；替换工作负载或 PVC/PV 不得冒充恢复。
- 演示必须选择当前 context，核对完整 owner 链、管理标签、单副本与 UID；删除使用 API 前置条件。
- 补建必须出现新 Pod UID，且 HTTP 路径另行恢复。旧 Pod、控制器变化、端口转发中断、样本不足都不能伪装成功。
- 合成告警同时要求 Prometheus 和同次 run_id 的接收器 firing/resolved；接收器重启或事件丢失、只有单侧证据均失败。
- 接收器本地 HTTP 测试覆盖大小/格式限制与内存淘汰。集群命令在单元测试中使用受控响应；没有现场 API 操作。

## 待有集群后补测

1. 干净 Linux ARM64/AMD64 主机的联网安装与真实指标检查。
2. Alertmanager 调度、配置加载、NetworkPolicy 效果、端到端 firing/resolved。
3. 在持续流量下执行新演示脚本并保留实际请求和恢复证据。
4. 同一主机手动重启后执行 before/after 比较，并另行验证监控历史数据。

步骤和判定标准见 [无集群准备与验收路线](kubernetes-readiness.md)。历史 v0.4.0 的 137 基础监控记录保持独立，不覆盖本轮新增流程。
