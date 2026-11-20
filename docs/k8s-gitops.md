# 用 Git 恢复 demo-web 的配置

这个可选实验让 Argo CD 管理 `sre-k8s-demo` 中已有的 `demo-web` Deployment 和 Service。第一次演练手工调整副本数，观察控制器纠正现场配置。第二次将一次错误的端口映射提交到实验 Git 仓库，用集群内 HTTP 探测发现故障，再通过 `git revert` 恢复。已有 Compose 蓝绿发布仍由 `ops.deploy` 管理。

## 环境与范围

- 需要一个可用的 K3s 集群、已部署的 [demo-web](../monitoring/kubernetes/demo.yaml) 和一个 Argo CD 实例。在 137 上添加常驻组件前，先查看 `free -h`、`df -h /var/lib`、节点的 `MemoryPressure` 和 `Allocated resources`。单节点实验可以选无常驻 Web UI 的 Argo CD Core；Kubernetes 1.37 在 Argo CD 3.6 的官方测试矩阵内。安装使用固定版本，并检查 Argo 命名空间的 Pod 均已就绪。[Core 安装说明](https://argo-cd.readthedocs.io/en/stable/operator-manual/core/) · [兼容矩阵](https://argo-cd.readthedocs.io/en/stable/operator-manual/installation/#tested-versions)
- 准备一个专用的空 Git 仓库。它可以在实验网内，Argo 的 repo-server 需要能解析和访问该地址。操作者有写入权限，Argo 仅使用只读凭据；凭据配置在 Argo 的仓库 Secret 中，SSH 另需配置已核实的主机公钥。公开 GitHub 项目无需更改。[私有仓库配置](https://argo-cd.readthedocs.io/en/stable/user-guide/private-repositories/)
- 下文在项目源码目录执行。先选择实验集群的 kubectl context，核对 `kubectl config current-context` 和 `kubectl get nodes`。若使用 K3s 自带客户端，以下 `kubectl` 可换成 `k3s kubectl`。在添加应用之前先完成普通的删 Pod 和 Service 故障演练，避免两个实验同时修改 demo-web。

## 初始化实验仓库

将 `LAB_GIT_URL` 设为实际可达的专用仓库地址。以下示例用 SSH，操作者和 Argo 各自提供相应凭据。渲染 workload 需要 PyYAML，项目已有的开发虚拟环境包含该依赖。

```bash
export LAB_GIT_URL='ssh://git@git.lab.example/sre-demo.git'
export LAB_CHECKOUT="$(mktemp -d)"
git clone --origin lab "$LAB_GIT_URL" "$LAB_CHECKOUT"
git -C "$LAB_CHECKOUT" switch --orphan main
mkdir "$LAB_CHECKOUT/workload"
.venv/bin/python gitops/render_demo.py workload > "$LAB_CHECKOUT/workload/demo.yaml"
git -C "$LAB_CHECKOUT" add workload/demo.yaml
git -C "$LAB_CHECKOUT" commit -m "Deploy the known-good demo"
git -C "$LAB_CHECKOUT" push -u lab main
```

渲染器读取项目已有的 demo.yaml，按名称、命名空间和类型提取两个对象。Namespace 和 ResourceQuota 继续由原来的安装流程管理。AppProject 仅允许这个 Git 地址、实验命名空间和这两类资源，Application 仅读取 `main` 的 `workload/demo.yaml`。实验期间用 Git 修改这两个对象。

```bash
python3 gitops/render_demo.py application "$LAB_GIT_URL" | kubectl apply -f -
```

下面的函数等待控制器处理**指定提交**。在当前 shell 定义一次，每次推送后执行。基线、错误提交和回退提交各保存一次提交号、同步状态及探测输出。

```bash
wait_revision() {
    local revision="$1" app=application/sre-lab-demo
    kubectl -n argocd annotate "$app" argocd.argoproj.io/refresh=hard --overwrite &&
        kubectl -n argocd wait "$app" --for="jsonpath={.status.sync.revision}=$revision" --timeout=180s &&
        kubectl -n argocd wait "$app" --for=jsonpath='{.status.sync.status}'=Synced --timeout=180s &&
        kubectl -n argocd wait "$app" --for=jsonpath='{.status.health.status}'=Healthy --timeout=180s
}
wait_revision "$(git -C "$LAB_CHECKOUT" rev-parse HEAD)"
bash scripts/check_demo_service.sh
```

## 演练一：现场修改副本数

```bash
kubectl -n sre-k8s-demo scale deployment/demo-web --replicas=2
kubectl -n sre-k8s-demo wait deployment/demo-web --for=jsonpath='{.spec.replicas}'=1 --timeout=180s
kubectl -n sre-k8s-demo rollout status deployment/demo-web --timeout=90s
bash scripts/check_demo_service.sh
```

基线提交保持不变，副本数从 2 自动回到 Git 中的 1，Service 探测成功。可以在另一终端运行 `kubectl -n sre-k8s-demo get deploy demo-web -w` 观察变化。

## 演练二：错误的 Git 提交与回退

1. 编辑实验仓库 `$LAB_CHECKOUT/workload/demo.yaml` 中唯一的 Service，将 `spec.ports[0].targetPort` 从 `http` 改为数值 `18081`。运行 `git -C "$LAB_CHECKOUT" diff` 核实只有该端口变化。
2. 运行下列命令。Argo 可以显示 Synced 和 Healthy，但集群内探测应以非零状态退出，日志在 `dns=ok` 后报告 `http=failed`。这是本次演练的预期故障。

```bash
git -C "$LAB_CHECKOUT" commit -am "Exercise broken demo service target port"
bad_revision="$(git -C "$LAB_CHECKOUT" rev-parse HEAD)"
git -C "$LAB_CHECKOUT" push lab main
wait_revision "$bad_revision"
bash scripts/check_demo_service.sh
```

3. 恢复该提交并重新验证。即使故障探测得到了意外结果，也要执行回退，恢复基线。

```bash
git -C "$LAB_CHECKOUT" revert --no-edit "$bad_revision"
git -C "$LAB_CHECKOUT" push lab main
wait_revision "$(git -C "$LAB_CHECKOUT" rev-parse HEAD)"
bash scripts/check_demo_service.sh
```

验收以新的回退提交已同步、Deployment 可用、Pod 内得到 `dns=ok`、`http=200` 和 `body=ok` 为准。[探测说明](k8s-service-smoke.md)

## 结束演练

可以保留 Application 继续纠正漂移。若要交回手工演练，先完成回退与探测，再确认 `kubectl -n argocd get application sre-lab-demo -o jsonpath='{.metadata.finalizers}'` 结果为空。此模板创建的 Application 不设置级联删除 finalizer；此时删除下面两个 GitOps 对象，再检查 Deployment 和 Service。

```bash
kubectl -n argocd delete application sre-lab-demo
kubectl -n argocd delete appproject sre-lab-demo
kubectl -n sre-k8s-demo get deployment/demo-web service/demo-web
```
