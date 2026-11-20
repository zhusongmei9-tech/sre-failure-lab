# 用 Ansible 安装与复核实验室

控制端使用 Python 3.12 和 ansible-core 2.19；目标机使用 systemd、正在运行的 Docker Engine、带 `pull --policy missing` 的 Docker Compose 插件和 `/usr/bin/python3` 3.9+。首次构建需要访问镜像仓库和 PyPI。固定安装位置为 `/opt/sre-failure-lab`。建议新建控制端虚拟环境。

```bash
python3.12 -m venv ~/.venvs/sre-ansible
~/.venvs/sre-ansible/bin/pip install 'ansible-core>=2.19,<2.20'
source ~/.venvs/sre-ansible/bin/activate
mkdir -p ~/.config/sre-lab
cp ansible/inventory.example.ini ~/.config/sre-lab/inventory.ini
```

将 inventory 内的示例地址换成虚机地址，填写 SSH 用户。先通过 SSH 确认主机密钥，使用 SSH 密钥或在运行时加 `--ask-pass`。普通 sudo 用户若需要提权密码，再加 `--ask-become-pass`。示例中的 inventory 存放在仓库外。

## 接管已初始化的主机

从项目根目录执行。Playbook 先预览目标机 systemd 操作，再对齐安装脚本、两个 unit 模板和 systemd 状态。已初始化主机使用原有的 `.env`、业务镜像和蓝绿槽。修改业务版本继续使用 `ops.deploy`。

```bash
ansible-playbook -i ~/.config/sre-lab/inventory.ini ansible/deploy.yml --check --diff
ansible-playbook -i ~/.config/sre-lab/inventory.ini ansible/deploy.yml
ansible-playbook -i ~/.config/sre-lab/inventory.ini ansible/deploy.yml
```

预览中的文件差异展示控制端将复制的内容，systemd 计划使用目标机当前的 unit 模板。真实执行结束时，Playbook 从虚机回环地址检查 `/health/ready` 返回 `status=ready`、`redis=ok`，以及 `/products/1` 返回 `id=1`。如果宿主机没有外部变更，连续第二次执行的 recap 应为 `changed=0 failed=0`。在两次执行之间可记录以下数据核对服务连续性，在目标机执行：

```bash
systemctl show sre-lab sre-lab-healer -p Id -p ActiveEnterTimestampMonotonic
docker ps --filter label=com.docker.compose.project=sre-lab --format '{{.ID}} {{.Names}}'
```

## 首次安装

在已具备上述依赖的干净主机上，从控制端传入可信项目 Release 的源码 tar.gz 绝对路径。压缩包应含一层根目录。若源码已按 README 摆在安装目录，可省略 `-e sre_source_archive=...`。

```bash
ansible-playbook -i ~/.config/sre-lab/inventory.ini ansible/deploy.yml \
  -e sre_source_archive=/absolute/path/to/release.tar.gz --check --diff
ansible-playbook -i ~/.config/sre-lab/inventory.ini ansible/deploy.yml \
  -e sre_source_archive=/absolute/path/to/release.tar.gz
```

新机预览只显示计划。实际执行时，Playbook 在目标机生成凭据，设置缺失的 Docker 网桥和回环绑定参数，准备镜像，以 `ops.deploy init` 初始化 blue 槽，然后安装并验证 systemd 服务。已有部署记录会跳过整段初始化。发现残留的项目容器、卷、运行目录或未完成发布时，先检查原部署状态，参考[真实发布与恢复](real-deployment.md)处理。
