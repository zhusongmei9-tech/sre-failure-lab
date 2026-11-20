#!/usr/bin/env bash
# Bootstrap missing objects in the local K3s product lab. Preserve existing releases.
set -euo pipefail
monitor=false
if [[ $# == 1 && $1 == --monitoring ]]; then monitor=true
elif [[ $# == 1 && $1 == --help ]]; then echo "Usage: bash scripts/install_k8s_app_lab.sh [--monitoring]"; exit 0
elif [[ $# != 0 ]]; then echo "Usage: bash scripts/install_k8s_app_lab.sh [--monitoring]" >&2; exit 2; fi

scripts=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
root="$scripts/../monitoring/kubernetes"
# All three product-lab operations lock the same directory inode on this host.
exec 9<"$scripts"
flock -n 9 || { echo "Another product-lab operation is running." >&2; exit 1; }
k=(k3s kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml)
files=()
for name in namespace redis api hpa; do files+=(-f "$root/app-lab/$name.yaml"); done
if "$monitor"; then
    "${k[@]}" --request-timeout=10s get namespace/sre-monitoring >/dev/null
    files+=(-f "$root/app-lab-observability/product-monitor.yaml")
fi
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
timeout 20s k3s ctr --namespace k8s.io images list -q > "$work/images"
for version in 0.3.0 0.3.1; do
    grep -Fxq "docker.io/library/sre-lab-api:$version" "$work/images" || {
        echo "Import sre-lab-api:$version into K3s containerd before installing." >&2
        exit 1
    }
done
"${k[@]}" --request-timeout=10s get --raw=/apis/metrics.k8s.io/v1beta1/nodes | python3 -c '
import json, sys
if not json.load(sys.stdin).get("items"):
    sys.exit("No node metrics are available for the HPA yet.")
'

# Normalize multi-document YAML with kubectl. Get all live names before writing any.
"${k[@]}" --request-timeout=15s create --dry-run=client --validate=false "${files[@]}" -o json > "$work/desired"
"${k[@]}" --request-timeout=15s get --ignore-not-found "${files[@]}" -o json > "$work/existing"
python3 - "$work" <<'PY'
import json
import sys
from pathlib import Path

work = Path(sys.argv[1])
def read_items(name):
    # kubectl create prints a JSON object per resource, while get can emit a List.
    text = (work / name).read_text().strip()
    items = []
    decoder = json.JSONDecoder()
    while text:
        obj, end = decoder.raw_decode(text)
        items.extend(obj["items"] if obj["kind"] == "List" else [obj])
        text = text[end:].lstrip()
    return items

def key(obj):
    meta = obj["metadata"]
    return obj["kind"], meta.get("namespace", ""), meta["name"]

wanted = {key(obj): obj for obj in read_items("desired")}
ownership = ("app.kubernetes.io/part-of", "app.kubernetes.io/managed-by", "sre-lab-role")
for obj in read_items("existing"):
    identity = key(obj)
    expected = wanted.pop(identity)
    meta = obj["metadata"]
    if (meta.get("deletionTimestamp") or meta.get("ownerReferences")
            or any(meta.get("labels", {}).get(label) != expected["metadata"]["labels"][label]
                   for label in ownership)):
        sys.exit(f"Refusing to adopt or replace {identity}")
if wanted:
    (work / "missing").write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": list(wanted.values())}))
PY
if [[ -e "$work/missing" ]]; then
    # Unlike apply, create fails if another operator created the name since preflight.
    "${k[@]}" --request-timeout=30s create --save-config -f "$work/missing"
fi
for name in redis product-api; do
    "${k[@]}" -n sre-app-lab rollout status deployment/"$name" --timeout=120s
done
ip=$("${k[@]}" --request-timeout=10s -n sre-app-lab get service/product-api -o json | python3 -c '
import ipaddress, json, sys
s = json.load(sys.stdin)["spec"]
if s["type"] != "ClusterIP" or s.get("selector") != {"app":"product-api"}:
    sys.exit("Unexpected product Service")
ip = ipaddress.ip_address(s["clusterIP"])
print(f"[{ip}]" if ip.version == 6 else ip)
')
for path in health/ready products/1 version; do
    curl --noproxy '*' --fail --silent --show-error --retry 3 --retry-connrefused \
        --connect-timeout 2 --max-time 4 "http://$ip:8000/$path" > "$work/${path##*/}"
done
python3 - "$work" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
ready = json.loads((p / "ready").read_text())
product = json.loads((p / "1").read_text())
version = json.loads((p / "version").read_text()).get("version")
if ready.get("status") != "ready" or ready.get("redis") != "ok" or product.get("id") != 1 or not version:
    sys.exit("Unexpected product API response")
print(f"product-api version={version} product=1 redis=ok")
PY
