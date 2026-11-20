# 01　在空白 Rocky Linux 9 虚机上启动商品服务

本章从一台可以登录的 Rocky Linux 9 虚机开始。完成后，Nginx 的 18000 端口提供商品查询，Redis 缓存重复请求，Grafana 展示运行指标，整套服务可通过 `systemctl` 管理。除标注为“电脑端”的命令外，所有代码块都在**同一台 Linux 虚机**中执行。

## 1. 确认主机、磁盘和时间

登录虚机后运行 `sudo -i` 进入 root shell。已经是 root 时跳过这一步。

```bash
cat /etc/rocky-release
uname -m
nproc
free -h
df -h / /var/lib /usr/local
ip -brief -4 address
ip route
```

`aarch64` 对应 ARM64，`x86_64` 对应 AMD64；Docker 会自动选择本机架构的镜像。仅跑 Compose 可以参考 137 的历史环境（2 vCPU、约 4.5 GiB 内存）。若要在同一台机器完成后续 K3s 与监控章节，一种较宽裕的起步配置是 4 vCPU、8 GiB 内存、40 GiB 虚拟磁盘。K3s 安装前的项目预检还会检查至少 2 vCPU、当时可用内存 2 GiB，以及 `/var/lib` 和 `/usr/local` 所在文件系统各有至少 6 GiB 空闲。Docker 使用独立数据盘时也要检查该盘剩余空间。

```bash
dnf install -y git python3 ca-certificates chrony dnf-plugins-core util-linux iproute tar gzip
command -v curl >/dev/null || dnf install -y curl
systemctl enable --now chronyd
chronyc waitsync 12 0 0 5
chronyc tracking
timedatectl show --property=NTPSynchronized --value
/usr/bin/python3 --version
```

期望看到 `yes` 和宿主机 Python 3.9 或更高版本；应用的 Python 3.12 随容器镜像提供。如果 `waitsync` 超时，先用 `chronyc sources -v` 检查时间源及网络，再重试。时间偏差会影响 HTTPS 证书验证，137 的 K3s 曾因开机时间异常出现尚未生效的证书。[137 现场记录](../ansible-k8s-validation-137.md)

## 2. 用 DNF 安装 Docker Engine 与 Compose 插件

使用 Rocky 官方文档给出的 RHEL RPM 仓库。在空白主机依次执行，首次安装有密钥确认提示时，将指纹与 Docker 官方文档对照后确认。遇到 GPG 校验失败时检查仓库 URL、系统时间和报错中的包来自哪个仓库，保留签名校验。[Rocky Linux 安装说明](https://docs.rockylinux.org/gemstones/containers/docker/) · [Docker 安装说明与签名指纹](https://docs.docker.com/engine/install/rhel/)

```bash
dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
dnf repolist --enabled
dnf install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker version
docker compose version
docker info --format 'DockerRootDir={{.DockerRootDir}} Architecture={{.Architecture}}'
docker run --rm hello-world
```

`docker version` 应同时有 Client 和 Server，最后一条命令应打印 `Hello from Docker!`。如果机器曾安装 Podman 或其他容器运行时，先看 DNF 拟执行的事务，确认原有工作负载及包归属后再处理冲突。镜像拉取失败时，先区分域名解析、连接超时、证书和仓库限流；本项目还要访问 Docker Hub、Quay、GHCR、PyPI 和 GitHub。

## 3. 下载项目，选定运行方式

以下从公开仓库的 `main` 下载完整源码。请从空路径开始；若 `/opt/sre-failure-lab` 已有部署，先查看其中的 `runtime/state.json` 和已有服务状态。

```bash
git clone --branch main https://github.com/zhusongmei9-tech/sre-failure-lab.git /opt/sre-failure-lab
cd /opt/sre-failure-lab
git rev-parse --short HEAD
```

| 模式 | 入口与用途 | 常用端口 |
| --- | --- | --- |
| 教学 Compose，`compose.yaml` | 单 API，适合先学缓存、故障开关、模拟发布。项目名 `sre-failure-lab` | API 8000、Grafana 3000、Prometheus 9090 |
| **本章执行的真实运行栈**，`compose.runtime.yaml` | Nginx 在 blue/green API 槽间切流，宿主机控制实际容器和自愈。项目名 `sre-lab` | Nginx 18000、槽 18001/18002、Grafana 3000、Prometheus 9090、网桥上的 node_exporter 9100 |

后面的 K3s 教程会复用真实栈的 Grafana。空白机器直接走真实运行栈即可。已经启动过教学栈时，先从项目根目录执行 `docker compose -f compose.yaml --profile traffic stop` 释放共用端口；命名卷会保留。检查 `ss -lntp` 和 `docker ps --format '{{.Names}} {{.Ports}}'`，判断 3000、9090、9100、18000–18002 是否已被其他服务占用。

## 4. 生成私有配置，理解监听地址

在虚机的项目根目录执行。第一次运行 `init_env.py` 会根据 `.env.example` 生成两个独立随机凭据，文件权限为 0600。已有 `.env` 时脚本会拒绝覆盖，沿用原配置。

```bash
cd /opt/sre-failure-lab
test -f .env || python3 scripts/init_env.py
stat -c '%a %n' .env
docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'
```

下面的命令从 Docker 的默认 bridge 网络读取实际网关，在 `.env` 中**只补缺失项**。网关是 Docker 从容器访问宿主机使用的地址，例如 `172.17.0.1`；它与前面 `ip -brief -4 address` 查到的虚机局域网地址用途不同。

```bash
python3 - <<'PY'
from pathlib import Path
import ipaddress
import subprocess

env = Path(".env")
gateway = subprocess.check_output(
    ["docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"],
    text=True,
).strip()
gateway = str(ipaddress.IPv4Address(gateway))
defined = {line.partition("=")[0].strip() for line in env.read_text().splitlines() if "=" in line}
with env.open("a") as out:
    for key, value in {"METRICS_HOST_GATEWAY": gateway, "RUNTIME_BIND_HOST": "127.0.0.1"}.items():
        if key not in defined:
            out.write(f"\n{key}={value}\n")
PY
docker compose -f compose.runtime.yaml --profile slots config --quiet
```

| 配置 | 如何填写、影响什么 |
| --- | --- |
| `LAB_TOKEN`、`GRAFANA_ADMIN_PASSWORD` | 自生成。前者用于受保护的教学控制请求，后者用于 Grafana 初始 `admin` 登录。密码只在虚机的私有 `.env` 查看，分享排错信息时用 `config --quiet` 做校验。 |
| `METRICS_HOST_GATEWAY` | 填上面测出的 Docker 网关。node_exporter 在此地址监听 9100；Prometheus 使用 Compose 的 host-gateway 映射访问它。 |
| `RUNTIME_BIND_HOST` | 本章填 `127.0.0.1`，让 18000 和 3000 仅本机可达；电脑通过 SSH 隧道访问。实验室若改为 `0.0.0.0`，要同时限定可信网络的入站访问。槽端口和真实栈的 9090 始终绑定回环地址。 |
| `API_CPUS`、`API_MEMORY_LIMIT` | 每个 API 容器的 CPU 和内存上限，模板默认 `1.0` 和 `256m`，初次运行可保留。 |
| `API_BIND_HOST`、`API_PORT`、`PROMETHEUS_PORT`、`GRAFANA_PORT` | 教学栈读取这些值。真实栈的监听端口见上表。 |
| `LOAD_RPS`、`LOAD_WORKERS` | 控制教学栈可选 traffic profile 的目标每秒请求数和并发 worker 数，模板均为 10。真实业务与 K8s 演练的压测脚本各有自己的参数。 |
| 缓存、超时和降级 | 教学栈读取 `CACHE_TTL_SECONDS`、`REDIS_TIMEOUT_SECONDS`、`FALLBACK_ENABLED`。真实栈的 Compose 配置指定 TTL 60 秒、Redis 超时 0.5 秒、开启回源。Redis 位于内部网络 `redis:6379`。 |

## 5. 建镜像，首次初始化，再交给 systemd

```bash
cd /opt/sre-failure-lab
docker compose -f compose.runtime.yaml pull redis edge prometheus grafana node-exporter cadvisor
docker build --build-arg APP_VERSION=0.3.0 -t sre-lab-api:0.3.0 .
python3 -m ops.deploy init --image sre-lab-api:0.3.0
bash scripts/install_systemd.sh
systemctl is-active sre-lab.service sre-lab-healer.service
systemctl is-enabled sre-lab.service
python3 -m ops.stack status
```

`0.3.0` 是演练业务镜像的版本，Dockerfile 将构建参数写进 HTTP `/version`。控制器会检查本地镜像、固定镜像 ID 并验证业务，再把首次活动槽记为 blue。系统已初始化时，用 `python3 -m ops.deploy status` 查看现状；启动已停止的运行栈用 `systemctl start sre-lab`，重启用 `systemctl restart sre-lab`。首次部署没有 green 容器是正常的，另一个槽在发布下一版时创建。

源码在 `/opt/sre-failure-lab`。其 `runtime/state.json` 保存发布状态，`runtime/images.env` 保存已选择的镜像标识，`runtime/events.jsonl` 记录发布与恢复事件。systemd 主、子 unit 安装在 `/etc/systemd/system/`。Prometheus 和 Grafana 数据各自在 Docker 命名卷里；Redis 用作临时缓存。

## 6. 验收 API、Redis 和 Grafana

```bash
curl -fsS http://127.0.0.1:18000/version
curl -fsS http://127.0.0.1:18000/health/ready
curl -si http://127.0.0.1:18000/products/2
curl -si http://127.0.0.1:18000/products/2
curl -fsS http://127.0.0.1:9090/-/ready
curl -fsS http://127.0.0.1:3000/api/health
```

就绪结果应包含 `"redis":"ok"`。对尚未缓存的商品，首次响应为 `X-Cache: MISS`，紧接着应为 `HIT`；已经有缓存时两次都可能是 `HIT`。两次均应为 HTTP 200 且商品 ID 为 2。真实环境的监控目标可在 Prometheus 的 `/targets` 查看。服务出错时，从 `journalctl -u sre-lab -u sre-lab-healer -n 80 --no-pager` 和 `python3 -m ops.stack status` 开始排查。

电脑端打开终端，将下一条命令中的 `user@VM_IP` 换成能登录虚机的 SSH 用户和地址。保持终端连接，浏览器分别访问 `http://127.0.0.1:18000/docs` 和 `http://127.0.0.1:3000`。如果本机端口被占用，调整 `-L` 参数左侧的端口并对应修改浏览器地址。

```bash
ssh -N -L 18000:127.0.0.1:18000 -L 3000:127.0.0.1:3000 user@VM_IP
```

后续可用 Ansible 从控制机检查及重复部署此实例。其 Playbook 需要目标机已有 systemd、Docker Engine、Compose 和 Python。已初始化目标沿用当前 `.env` 和活动镜像，第二次执行应报告 `changed=0` 并通过真实 HTTP 验收。[Ansible 使用手册](../ansible-deploy.md)
