# 第二阶段：持续流量与监控

启动四个服务（API、Redis、Prometheus、Grafana）：

```bash
docker compose up --build -d --wait
docker compose ps
make traffic
make traffic-logs
```

没有 make 时，使用 `docker compose --profile traffic up -d loadgen` 和 `docker compose --profile traffic logs -f loadgen`。

## 从电脑访问虚拟机

监控端口固定绑定虚拟机本地地址。在电脑上运行（替换 SSH 登录地址）：

```bash
ssh -N -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 -L 8000:127.0.0.1:8000 USER@VM_IP
```

- Grafana：<http://localhost:3000/d/sre-failure-lab>。初始用户 `admin`，密码为 `.env` 中的 `GRAFANA_ADMIN_PASSWORD`（首次初始化脚本在本机随机生成，没有默认密码）。
- Prometheus：<http://localhost:9090>，在 Targets 页面确认 `api:8000` 为 UP。
- 原始指标：<http://localhost:8000/metrics>。

Grafana 数据源和十个面板自动加载，无需手动创建。密码环境变量仅在首次初始化数据卷时生效；后续改密码使用 Grafana 管理界面。本次升级新增必填 LAB_TOKEN，并取消 Grafana 默认密码；旧版 .env 必须按 security.md 补充。

## 十分钟演练

1. 开启流量后，等待至少 1 分钟，观察请求速率、错误率、P95、缓存命中率。
2. 执行 `docker compose pause redis`，保持约 60 秒。
3. 观察错误率上升、缓存失败增多。API 指标抓取仍可能为 1，因为 API 进程还活着。
4. 执行 `docker compose unpause redis`，确认业务恢复。此命令必须手动执行。
5. 查看 `docker compose logs --tail=30 api` 和流量生成器的窗口统计。
6. 执行 `make traffic-stop` 停止流量；执行 `make down` 停止全部实验服务。

看板采用过去 1 分钟的 `rate()`，因此恢复后需要等待旧样本移出窗口，曲线才逐渐恢复正常。刚启动尚无足够采样时，面板可能显示无数据。没有请求时，错误率、命中率和 P95 没有定义，不伪装成 0。

## 数据口径

| 指标 | 含义 |
| --- | --- |
| `lab_http_requests_total{status_code}` | 商品路由的请求计数，包含 404 和参数错误；不含健康检查、文档和指标抓取 |
| `lab_http_request_duration_seconds` | 商品请求耗时直方图，单位秒，记录到响应头产生 |
| `lab_cache_requests_total{result}` | 业务请求的缓存结果：hit / miss / invalid / error，每次请求最多计一次 |
| `up{job="api"}` | Prometheus 是否成功抓取指标；不等同于业务就绪 |

P95 是直方图估算值；服务端错误率只计 5xx。网络错误无法由未收到请求的服务器计数，因此流量生成器另外记录 `transport_error`、实际 RPS、成功率和客户端 P95（包括读取响应正文）。两个 P95 的测量范围和计算方式不同。

## 控制流量

`.env` 默认 `LOAD_RPS=10`、`LOAD_WORKERS=10`，轮流查询商品 1～3。修改后再次运行 `make traffic`。

使用有限并发、按每个 worker 节奏发送请求；目标 RPS 是上限意图，延迟过高时实际 RPS 会下降，不是固定到达率压测工具。查看日志的 `actual_rps` 判断实际负载。

只运行 60 秒：

```bash
make traffic-stop
docker compose --profile traffic run --rm loadgen python -u /loadgen/main.py --rps 10 --workers 10 --duration 60
```

本地 Python 也能运行（无需额外 Python 包）：

```bash
python3 loadgen/main.py --base-url http://127.0.0.1:8000 --rps 10 --duration 60
```

## 存储和边界

Prometheus 数据保留最多 7 天或约 512 MB（先达到限制者生效，实际磁盘还包含 WAL 等开销）。Grafana 和 Prometheus 使用命名数据卷，普通 `make down` 保留数据。API 仍采用单进程指标；暂不支持多 worker 汇总。

已增加故障控制接口、自动到期、降级和告警规则，见 [演练手册](runbooks.md)。通知渠道未接入；操作入口是 CLI 与 FastAPI `/docs`。

配置依据：[Prometheus 查询函数](https://prometheus.io/docs/prometheus/latest/querying/functions/)、[Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/)。
