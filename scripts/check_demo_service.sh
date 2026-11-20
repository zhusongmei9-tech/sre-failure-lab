#!/usr/bin/env bash
# Run once on the lab's K3s host or with an explicitly selected kubectl context.
set -euo pipefail

if command -v k3s >/dev/null; then
    client=(k3s kubectl)
else
    client=(kubectl)
fi
if [[ $# == 2 && "$1" == --context && -n "$2" ]]; then
    client+=(--context "$2")
elif [[ $# != 0 ]]; then
    echo "Usage: bash scripts/check_demo_service.sh [--context NAME]" >&2
    exit 2
fi
client+=(--namespace sre-k8s-demo --request-timeout=10s)
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
job=$("${client[@]}" create -f "$root/monitoring/kubernetes/service-smoke.yaml" -o name)
cleanup() {
    "${client[@]}" delete "$job" --ignore-not-found --cascade=foreground --timeout=25s
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
echo "Checking $job"

condition="{range .status.conditions[?(@.status==\"True\")]}{.type}{' '}{end}"
deadline=$((SECONDS + 85))
while ((SECONDS < deadline)); do
    states=$("${client[@]}" get "$job" -o "jsonpath=$condition")
    case " $states " in
        *" Complete "*) "${client[@]}" logs "$job"; exit 0 ;;
        *" Failed "*) break ;;
    esac
    sleep 1
done

echo "Job failed or exceeded its time limit" >&2
"${client[@]}" logs "$job" --pod-running-timeout=3s || true
"${client[@]}" get pods -l "batch.kubernetes.io/job-name=${job#*/}" -o wide >&2 || true
exit 1
