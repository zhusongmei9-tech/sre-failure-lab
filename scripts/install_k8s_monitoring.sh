#!/usr/bin/env bash
# Usage: bash scripts/install_k8s_monitoring.sh [monitoring/kubernetes/values.yaml]
# Pin Helm, chart and immutable download digests; use only the local K3s kubeconfig.
set -euo pipefail
umask 077
readonly LAB_HELM_VERSION=v4.3.2
readonly LAB_CHART_VERSION=93.3.1
readonly LAB_CHART_SHA=a7fbf983aadc20548c167c40ba2e78fecbf03753830911130419a16064c91aff
readonly LAB_RELEASE=sre-k8s
readonly LAB_NAMESPACE=sre-monitoring
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cache_dir="${K3S_BOOTSTRAP_DIR:-$project_root/runtime/kubernetes/bootstrap}"
values_file="${1:-$project_root/monitoring/kubernetes/values.yaml}"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

fail() { echo "Kubernetes monitoring installation failed: $*" >&2; exit 1; }
[[ $# -le 1 ]] || fail 'supply at most one values file'
[[ "$(uname -s)" == Linux && "$EUID" -eq 0 ]] || fail 'run as root on Linux'
case "$(uname -m)" in
    aarch64|arm64)
        architecture=arm64
        helm_sha=df31cb61680ef728f82c770e5280dd565617bee6b039d37e85c677288be4bbb0
        ;;
    x86_64|amd64)
        architecture=amd64
        helm_sha=d1c0278d46beab79582d7e50f5b3dc206944ba8e577cde618513fd07daa53983
        ;;
    *) fail 'only Linux ARM64 and AMD64 are supported';;
esac
for executable in curl sha256sum python3 tar install timeout flock; do
    command -v "$executable" >/dev/null || fail "missing $executable"
done
[[ -r "$KUBECONFIG" && -x /usr/local/bin/k3s ]] || fail 'install and start K3s first'
[[ -f "$values_file" ]] || fail 'values file is missing'
exec 9>/run/lock/sre-lab-k8s-monitoring-install.lock
flock -n 9 || fail 'another monitoring installation is running'
/usr/local/bin/k3s kubectl --request-timeout=15s get nodes -o json | python3 -c '
import json,sys
nodes=json.load(sys.stdin)["items"]
if len(nodes)!=1 or nodes[0]["status"]["nodeInfo"]["architecture"]!=sys.argv[1]:
    raise SystemExit("Expected one local K3s node matching the host architecture")
if not any(c["type"]=="Ready" and c["status"]=="True" for c in nodes[0]["status"]["conditions"]):
    raise SystemExit("The local K3s node is not Ready")
' "$architecture"
python3 - <<'PY'
import pathlib,shutil
memory=dict(line.split(':',1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())
if int(memory['MemAvailable'].split()[0]) < 768*1024:
    raise SystemExit('Need at least 768 MiB available RAM before adding monitoring')
if shutil.disk_usage('/var/lib').free < 4*1024**3:
    raise SystemExit('Need at least 4 GiB free space; no automatic pruning will run')
PY

install -d -m 700 "$cache_dir"
download_verified() {
    local name="$1" digest="$2" url="$3" path="$cache_dir/$1"
    if [[ ! -f "$path" ]]; then
        curl --fail --location --silent --show-error --connect-timeout 15 --max-time 300 \
            --retry 2 --max-filesize 134217728 "$url" -o "$path.part"
        printf '%s  %s\n' "$digest" "$path.part" | sha256sum --check --status || fail "$name checksum mismatch"
        mv -- "$path.part" "$path"
    fi
    printf '%s  %s\n' "$digest" "$path" | sha256sum --check --status || fail "$name cache checksum mismatch"
}

if command -v helm >/dev/null 2>&1; then
    [[ "$(helm version --template '{{.Version}}')" == "$LAB_HELM_VERSION" ]] || fail "existing Helm differs from $LAB_HELM_VERSION; preserve it and select a reviewed version manually"
else
    download_verified "helm-v4.3.2-linux-$architecture.tar.gz" "$helm_sha" \
        "https://get.helm.sh/helm-v4.3.2-linux-$architecture.tar.gz"
    temporary_dir="$(mktemp -d "$cache_dir/.helm.XXXXXX")"
    trap 'rm -rf -- "$temporary_dir"' EXIT
    tar -xzf "$cache_dir/helm-v4.3.2-linux-$architecture.tar.gz" -C "$temporary_dir" "linux-$architecture/helm"
    install -m 755 "$temporary_dir/linux-$architecture/helm" /usr/local/bin/helm
    [[ "$(/usr/local/bin/helm version --template '{{.Version}}')" == "$LAB_HELM_VERSION" ]] || fail 'downloaded Helm version mismatch'
fi

download_verified kube-prometheus-stack-93.3.1.tgz "$LAB_CHART_SHA" \
    'https://github.com/prometheus-community/helm-charts/releases/download/kube-prometheus-stack-93.3.1/kube-prometheus-stack-93.3.1.tgz'
chart_path="$cache_dir/kube-prometheus-stack-93.3.1.tgz"
[[ "$(helm show chart "$chart_path" | awk '/^version:/ {print $2}')" == "$LAB_CHART_VERSION" ]] || fail 'chart version mismatch'
helm list --namespace "$LAB_NAMESPACE" --filter '^sre-k8s$' --output json | python3 -c '
import json,sys
items=json.load(sys.stdin)
if items:
    release=items[0]
    if release["chart"]!="kube-prometheus-stack-93.3.1":
        raise SystemExit("Existing release uses another chart version; no automatic cross-version upgrade")
    if release["status"] not in ("deployed","failed"):
        raise SystemExit("Existing Helm release is not ready for a new operation; inspect helm status")
'
helm lint "$chart_path" --values "$values_file" --quiet
helm upgrade --install "$LAB_RELEASE" "$chart_path" \
    --namespace "$LAB_NAMESPACE" --create-namespace --values "$values_file" \
    --reset-values --history-max 5 --wait=watcher --wait-for-jobs --timeout 10m \
    --rollback-on-failure --hide-notes
# Prometheus' StatefulSet is created asynchronously by the Operator, not Helm.
deadline=$((SECONDS + 180))
while ! /usr/local/bin/k3s kubectl --request-timeout=10s -n "$LAB_NAMESPACE" \
    get statefulset prometheus-sre-k8s-prometheus >/dev/null 2>&1; do
    (( SECONDS < deadline )) || fail 'Prometheus StatefulSet was not created; inspect the operator'
    sleep 3
done
/usr/local/bin/k3s kubectl -n "$LAB_NAMESPACE" rollout status \
    statefulset/prometheus-sre-k8s-prometheus --timeout=180s
/usr/local/bin/k3s kubectl --request-timeout=15s -n "$LAB_NAMESPACE" get pods,svc,pvc
echo "Monitoring release $LAB_RELEASE is installed. Validate Prometheus targets before treating collection as healthy."
