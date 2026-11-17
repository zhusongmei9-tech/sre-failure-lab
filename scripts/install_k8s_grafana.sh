#!/usr/bin/env bash
# Connect the optional Kubernetes Prometheus to the existing Compose Grafana.
set -euo pipefail

if [[ "$(uname -s)" != Linux || "$EUID" -ne 0 ]]; then
  echo 'Run as root on the Linux lab host.' >&2
  exit 1
fi
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$project_dir"
for command in docker python3 systemctl curl; do
  command -v "$command" >/dev/null || { echo "Missing prerequisite: $command" >&2; exit 1; }
done
test -x /usr/local/bin/k3s
test -f .env
test -f runtime/images.env
/usr/local/bin/k3s kubectl -n sre-monitoring get service sre-k8s-prometheus >/dev/null
bridge_address="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
python3 - "$bridge_address" <<'PY'
import ipaddress
import sys

address = ipaddress.ip_address(sys.argv[1])
if address.version != 4 or not address.is_private or address.is_unspecified:
    raise SystemExit('Expected a private IPv4 Docker bridge gateway')
PY

install -d -m 0700 /etc/sre-lab
python3 - "$project_dir" "$bridge_address" <<'PY'
import json
import sys
from pathlib import Path

from ops.common import atomic_write

root = Path(sys.argv[1])
address = sys.argv[2]
marker = '# Managed by scripts/install_k8s_grafana.sh\n'
destination = root / 'monitoring/grafana/provisioning/datasources/kubernetes.local.yml'
environment = Path('/etc/sre-lab/kubernetes-monitoring.env')
for path in (destination, environment):
    if path.is_symlink():
        raise SystemExit(f'Refusing to replace a symlink: {path}')
    if path.exists() and not path.read_text().startswith(marker):
        raise SystemExit(f'Refusing to overwrite an unmanaged configuration: {path}')
config = {
    'apiVersion': 1,
    'datasources': [{
        'name': 'Kubernetes', 'uid': 'kubernetes', 'type': 'prometheus',
        'access': 'proxy', 'url': f'http://{address}:19090',
        'isDefault': False, 'editable': False,
        'jsonData': {'timeInterval': '30s', 'httpMethod': 'POST'},
    }],
}
atomic_write(destination, marker + json.dumps(config, indent=2) + '\n', mode=0o644)
atomic_write(environment, marker + f'K8S_PROMETHEUS_BIND_ADDRESS={address}\n')
PY

unit=/etc/systemd/system/sre-lab-k8s-prometheus.service
if [[ -e "$unit" ]] && ! head -1 "$unit" | grep -Fq '# Managed by scripts/install_k8s_grafana.sh'; then
  echo "Refusing to replace unmanaged systemd unit: $unit" >&2
  exit 1
fi
install -m 0644 ops/sre-lab-k8s-prometheus.service "$unit"
systemctl daemon-reload
systemctl enable sre-lab-k8s-prometheus.service
systemctl restart sre-lab-k8s-prometheus.service
ready=false
for attempt in {1..45}; do
  if curl --noproxy '*' -fsS --max-time 3 "http://${bridge_address}:19090/-/ready" >/dev/null; then
    ready=true
    break
  fi
  sleep 2
done
if [[ "$ready" != true ]]; then
  echo 'Kubernetes Prometheus bridge is not ready; inspect journalctl -u sre-lab-k8s-prometheus.' >&2
  exit 1
fi

# Use the same project configuration and lock as existing lifecycle operations.
python3 - <<'PY'
from ops.common import compose, operation_lock

with operation_lock():
    compose('restart', 'grafana')
PY
printf 'Kubernetes Prometheus ready at http://%s:19090 (Docker bridge only).\n' "$bridge_address"
printf 'Grafana datasource: Kubernetes; dashboard: /d/sre-lab-kubernetes\n'
