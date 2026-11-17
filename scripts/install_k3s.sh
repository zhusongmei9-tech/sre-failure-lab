#!/usr/bin/env bash
# Install one pinned ARM64/AMD64 K3s server without changing the Compose runtime.
# Official sources: docs.k3s.io/installation/configuration and /requirements.
set -euo pipefail
umask 077

readonly LAB_K3S_VERSION='v1.37.2+k3s1'
readonly LAB_INSTALL_SHA='ed01f89fd977bf20ac1516bbebf8370bf3ddbaa55dac8aba610956a4c78cc00b'
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cache_dir="${K3S_BOOTSTRAP_DIR:-$project_root/runtime/kubernetes/bootstrap}"
node_name="${K3S_NODE_NAME:-sre-k8s-node}"
config_path=/etc/rancher/k3s/config.yaml
api_allowed_cidr="${K3S_API_ALLOWED_CIDR:-}"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

fail() { echo "K3s installation failed: $*" >&2; exit 1; }
[[ "$(uname -s)" == Linux && "$EUID" -eq 0 ]] || fail 'run as root on Linux'
case "$(uname -m)" in
    aarch64|arm64)
        binary_name=k3s-arm64
        binary_sha=a779ccf1e5e2e33a62f5a29c2f1c0bbf99431e100579d4c10f0aa0742a6aa84b
        ;;
    x86_64|amd64)
        binary_name=k3s
        binary_sha=265b3ee6a3198ef14033c63ef46cbb32370d83795199fb6d1b01f21575eddbda
        ;;
    *) fail 'only Linux ARM64 and AMD64 are supported';;
esac
for executable in curl sha256sum python3 systemctl install timeout flock; do
    command -v "$executable" >/dev/null || fail "missing $executable"
done
[[ -d /run/systemd/system ]] || fail 'systemd must be running'
[[ -f "$project_root/monitoring/kubernetes/k3s-config.yaml.example" ]] || fail 'K3s configuration template is missing'
[[ "$node_name" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && ${#node_name} -le 63 ]] || fail 'invalid node name'
[[ -z "${K3S_URL:-}" && -z "${K3S_TOKEN:-}" && -z "${K3S_TOKEN_FILE:-}" ]] || fail 'join credentials are not supported by this single-server installer'
exec 9>/run/lock/sre-lab-k3s-install.lock
flock -n 9 || fail 'another K3s installation is running'

wait_for_node() {
    local deadline=$((SECONDS + 240))
    while (( SECONDS < deadline )); do
        if /usr/local/bin/k3s kubectl --request-timeout=10s get --raw=/readyz >/dev/null 2>&1 \
            && [[ -n "$(/usr/local/bin/k3s kubectl --request-timeout=10s get nodes -o name 2>/dev/null)" ]]; then
            /usr/local/bin/k3s kubectl wait node --all --for=condition=Ready --timeout=180s
            /usr/local/bin/k3s kubectl get nodes -o wide
            return
        fi
        sleep 3
    done
    fail 'API did not become ready; inspect journalctl -u k3s (existing state is retained)'
}

if command -v k3s >/dev/null 2>&1; then
    [[ "$(command -v k3s)" == /usr/local/bin/k3s ]] || fail 'an existing K3s binary uses a different path; preserve it and inspect manually'
    installed_version="$(/usr/local/bin/k3s --version | awk 'NR==1 {print $3}')"
    [[ "$installed_version" == "$LAB_K3S_VERSION" ]] || fail "existing K3s version differs from pinned $LAB_K3S_VERSION; no automatic upgrade"
    if systemctl cat k3s.service >/dev/null 2>&1; then
        echo 'Existing K3s service/configuration retained; ensuring it is started.'
        timeout 180 systemctl enable --now k3s.service
        wait_for_node
        exit 0
    fi
fi

# This checks available headroom, not total RAM. It never prunes Docker data or swaps off.
python3 - <<'PY'
import os, pathlib, shutil
memory = dict(line.split(':', 1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())
available = int(memory['MemAvailable'].split()[0]) * 1024
if (os.cpu_count() or 0) < 2 or available < 2 * 1024**3:
    raise SystemExit('Need at least 2 CPUs and 2 GiB currently available RAM for the lab bootstrap')
for directory in ('/var/lib', '/usr/local'):
    if shutil.disk_usage(directory).free < 6 * 1024**3:
        raise SystemExit('Need at least 6 GiB free space; no data will be deleted automatically')
if pathlib.Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() != '1':
    raise SystemExit('IPv4 forwarding must already be enabled; check the host networking setup')
PY

if [[ -n "$api_allowed_cidr" ]]; then
    python3 - "$api_allowed_cidr" <<'PY'
import ipaddress, sys
network = ipaddress.ip_network(sys.argv[1], strict=False)
if network.version != 4 or network.prefixlen == 0:
    raise SystemExit('K3S_API_ALLOWED_CIDR must be a specific IPv4 lab network, not 0.0.0.0/0')
PY
fi

install -d -m 700 "$cache_dir"
download_verified() {
    local name="$1" digest="$2" url="$3" path="$cache_dir/$1"
    if [[ ! -f "$path" ]]; then
        curl --fail --location --silent --show-error --connect-timeout 15 --max-time 600 \
            --retry 2 --max-filesize 268435456 "$url" -o "$path.part"
        printf '%s  %s\n' "$digest" "$path.part" | sha256sum --check --status || fail "$name checksum mismatch"
        mv -- "$path.part" "$path"
    fi
    printf '%s  %s\n' "$digest" "$path" | sha256sum --check --status || fail "$name cache checksum mismatch"
}
download_verified "$binary_name" "$binary_sha" \
    "https://github.com/k3s-io/k3s/releases/download/v1.37.2%2Bk3s1/$binary_name"
download_verified install-k3s-v1.37.2.sh "$LAB_INSTALL_SHA" \
    'https://raw.githubusercontent.com/k3s-io/k3s/v1.37.2%2Bk3s1/install.sh'

if [[ ! -e "$config_path" ]]; then
    install -d -m 700 /etc/rancher/k3s
    config_temporary="$(mktemp /etc/rancher/k3s/.sre-lab-config.XXXXXX)"
    trap 'rm -f -- "${config_temporary:-}"' EXIT
    sed "s/^node-name:.*/node-name: $node_name/" \
        "$project_root/monitoring/kubernetes/k3s-config.yaml.example" > "$config_temporary"
    chmod 600 "$config_temporary"
    if command -v getenforce >/dev/null && [[ "$(getenforce)" != Disabled ]]; then
        echo 'selinux: true' >> "$config_temporary"
    fi
    mv -n -- "$config_temporary" "$config_path"
else
    echo 'Existing /etc/rancher/k3s/config.yaml retained.'
fi
# Retry these scoped rules after a partial bootstrap too. Never infer custom CIDRs.
# No firewall reload, flush, global disable or SELinux change is performed.
if systemctl is-active --quiet firewalld; then
    command -v firewall-cmd >/dev/null || fail 'firewalld is active but firewall-cmd is missing'
    if grep -qx 'cluster-cidr: 10.42.0.0/16' "$config_path" \
        && grep -qx 'service-cidr: 10.43.0.0/16' "$config_path"; then
        for source in 10.42.0.0/16 10.43.0.0/16; do
            firewall-cmd --zone=trusted --add-source="$source" >/dev/null
            firewall-cmd --permanent --zone=trusted --add-source="$source" >/dev/null
        done
    else
        echo 'Custom K3s CIDRs retained; verify their firewall rules manually.'
    fi
    if [[ -n "$api_allowed_cidr" ]]; then
        rule="rule family=\"ipv4\" source address=\"$api_allowed_cidr\" port port=\"6443\" protocol=\"tcp\" accept"
        firewall-cmd --add-rich-rule="$rule" >/dev/null
        firewall-cmd --permanent --add-rich-rule="$rule" >/dev/null
    else
        echo 'API firewall access was not widened; use local kubectl or an SSH tunnel.'
    fi
fi

install -m 755 "$cache_dir/$binary_name" /usr/local/bin/k3s
# Pinned official installer creates the service and installs required SELinux policy.
# Avoid ambient K3S_* bootstrap variables becoming accidental service configuration.
env -u K3S_NODE_NAME -u K3S_BOOTSTRAP_DIR -u K3S_API_ALLOWED_CIDR \
    INSTALL_K3S_VERSION="$LAB_K3S_VERSION" INSTALL_K3S_SKIP_DOWNLOAD=true \
    INSTALL_K3S_EXEC=server INSTALL_K3S_BIN_DIR=/usr/local/bin \
    timeout 600 sh "$cache_dir/install-k3s-v1.37.2.sh"
wait_for_node
echo "K3s $LAB_K3S_VERSION is ready. Kubeconfig: /etc/rancher/k3s/k3s.yaml (root only)."
