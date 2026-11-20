#!/usr/bin/env bash
# Exercise the lab's Service routing while its single demo-web Pod stays Ready.
set -euo pipefail

if [[ $# == 0 || ${1:-} == --help ]]; then
    echo "Plan: check demo-web, isolate its Service selector, verify DNS succeeds but HTTP fails, restore and retest."
    echo "Usage: bash scripts/drill_demo_service.sh --run [--context NAME]"
    exit 0
fi
if [[ $1 != --run || ! ( $# == 1 || ( $# == 3 && ${2:-} == --context && -n ${3:-} ) ) ]]; then
    echo "Usage: bash scripts/drill_demo_service.sh --run [--context NAME]" >&2
    exit 2
fi
if command -v k3s >/dev/null; then client=(k3s kubectl); else client=(kubectl); fi
context=${3:-$("${client[@]}" config current-context)}
[[ -n $context ]] || { echo 'Missing lab context' >&2; exit 1; }
k=("${client[@]}" --context "$context" --namespace sre-k8s-demo --request-timeout=6s)
scripts=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
smoke=(bash "$scripts/check_demo_service.sh" --context "$context")

uid=$("${k[@]}" get namespace/sre-k8s-demo deployment/demo-web service/demo-web -o json |
    python3 "$scripts/check_demo_identity.py")

change() {
    local patch
    patch=$(printf '[{"op":"test","path":"/metadata/uid","value":"%s"},{"op":"test","path":"/spec/selector","value":{"app":"%s"}},{"op":"replace","path":"/spec/selector/app","value":"%s"}]' "$uid" "$1" "$2")
    "${k[@]}" patch service demo-web --type=json -p "$patch"
}
wait_endpoints() {
    local expected=$1 deadline=$((SECONDS + 20)) count
    while ((SECONDS < deadline)); do
        count=$("${k[@]}" get endpointslices -l kubernetes.io/service-name=demo-web -o json | python3 -c '
import json,sys
print(sum(e.get("conditions", {}).get("ready") is True for s in json.load(sys.stdin)["items"] for e in s.get("endpoints") or []))')
        if [[ $count == "$expected" ]]; then echo "ready_endpoints=$count"; return 0; fi
        sleep 1
    done
    echo "Timed out waiting for $expected ready endpoints" >&2
    return 1
}

wait_endpoints 1
"${smoke[@]}"
label="demo-no-backend-$RANDOM-$$"
matches=$("${k[@]}" get pods -l "app=$label" -o name)
[[ -z $matches ]] || { echo 'Fault selector already has matching Pods' >&2; exit 1; }
restore_on_exit() {
    local rc=$? attempt current
    trap - EXIT
    for attempt in 1 2 3; do
        if current=$("${k[@]}" get service demo-web -o 'jsonpath={.metadata.uid}{" "}{.spec.selector.app}'); then
            if [[ "$current" == "$uid demo-web" ]]; then exit "$rc"; fi
            if [[ "$current" != "$uid $label" ]]; then break; fi
            if change "$label" demo-web; then exit "$rc"; fi
        fi
        sleep 1
    done
    echo "Could not confirm restoration. Inspect sre-k8s-demo/demo-web (original UID $uid, original selector app=demo-web, drill selector app=$label)." >&2
    exit 1
}
trap restore_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
echo "fault_begin=$(date -u +%FT%TZ)"
change demo-web "$label"
wait_endpoints 0
if output=$("${smoke[@]}" 2>&1); then echo "$output"; echo "HTTP unexpectedly passed" >&2; exit 1; fi
echo "$output"
grep -Fxq 'dns=ok' <<<"$output" && grep -Fxq 'http=failed' <<<"$output" || {
    echo "Expected healthy DNS followed by an HTTP failure" >&2; exit 1;
}
ready=$("${k[@]}" get deployment demo-web -o 'jsonpath={.status.readyReplicas}')
[[ $ready == 1 ]] || { echo "Demo Deployment lost readiness" >&2; exit 1; }
echo "deployment_ready_during_fault=$ready"
change "$label" demo-web
echo "selector_restored=$(date -u +%FT%TZ)"
wait_endpoints 1
"${smoke[@]}"
echo "recovery_checked=$(date -u +%FT%TZ)"
trap - EXIT
