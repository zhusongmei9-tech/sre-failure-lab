import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.check_demo_identity import LABELS, service_identity

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "monitoring/kubernetes/service-smoke.yaml"
RUNNER = ROOT / "scripts/check_demo_service.sh"
JOB = yaml.safe_load(MANIFEST.read_text())
PROBE = JOB["spec"]["template"]["spec"]["containers"][0]


def command(directory, name, script):
    path = directory / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)


@pytest.mark.parametrize(
    ("case", "code", "checks"),
    [
        ("ok", 0, ["dns=ok", "http=200", "body=ok"]),
        ("dns", 10, ["dns=failed"]),
        ("connection", 20, ["dns=ok", "http=failed"]),
        ("redirect", 20, ["dns=ok", "http=failed expected_status=200"]),
        ("body", 30, ["dns=ok", "http=200", "body=failed"]),
    ],
)
def test_probe_distinguishes_stages(tmp_path, case, code, checks):
    command(tmp_path, "timeout", 'shift; exec "$@"')
    command(tmp_path, "nslookup", '[ "$CASE" != dns ]')
    command(tmp_path, "wget", """
        if [ "$CASE" = connection ]; then exit 1; fi
        if [ "$CASE" = redirect ]; then echo '  HTTP/1.1 302 Found' >&2; fi
        echo '  HTTP/1.1 200 OK' >&2
        if [ "$CASE" = body ]; then echo wrong; else echo 'SRE Kubernetes demo is ready'; fi > "$WORK/body"
    """)
    result = subprocess.run(
        [*PROBE["command"], PROBE["args"][0].replace("/work/", '"$WORK"/')],
        env=dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", CASE=case,
                 WORK=str(tmp_path), CLUSTER_DOMAIN="cluster.local"),
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == code, result.stderr
    assert result.stdout.splitlines() == checks


def test_probe_matches_demo_without_joining_service():
    demo = list(yaml.safe_load_all((MANIFEST.parent / "demo.yaml").read_text()))
    service = next(obj for obj in demo if obj["kind"] == "Service")
    deploy = next(obj for obj in demo if obj["kind"] == "Deployment")
    assert JOB["metadata"]["namespace"] == service["metadata"]["namespace"]
    assert PROBE["image"] == deploy["spec"]["template"]["spec"]["containers"][0]["image"]
    template_labels = JOB["spec"]["template"].get("metadata", {}).get("labels", {})
    assert not service["spec"]["selector"].items() <= template_labels.items()


@pytest.mark.parametrize(("state", "code"), [("Complete", 0), ("Failed", 1)])
def test_runner_collects_logs_and_removes_only_created_job(tmp_path, state, code):
    log = tmp_path / "calls"
    command(tmp_path, "k3s", """
        echo "$*" >> "$CALLS"
        case " $* " in
            *" create "*) echo job.batch/demo-service-smoke-test ;;
            *" get job.batch/"*) echo "$STATE " ;;
            *" logs "*) echo pod-log ;;
        esac
    """)
    result = subprocess.run(
        ["bash", str(RUNNER), "--context", "lab"],
        env=dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", STATE=state, CALLS=str(log)),
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == code
    calls = log.read_text().splitlines()
    assert all("--context lab --namespace sre-k8s-demo" in call for call in calls)
    assert "pod-log" in result.stdout
    assert len([call for call in calls if " delete " in call]) == 1
    assert "delete job.batch/demo-service-smoke-test " in calls[-1]


@pytest.mark.parametrize(
    ("scenario", "exit_code", "patches"), [("normal", 0, 2), ("bad-dns", 1, 2), ("foreign", 1, 0)]
)
def test_service_drill_restores_after_unexpected_probe_result(tmp_path, scenario, exit_code, patches):
    fixture = []
    for obj in yaml.safe_load_all((MANIFEST.parent / "demo.yaml").read_text()):
        if obj["kind"] in ("Namespace", "Deployment", "Service"):
            fixture.append(obj)
        if obj["kind"] == "Service":
            obj["spec"]["clusterIP"] = "10.43.0.20"
            obj["metadata"]["uid"] = "12345678-0000-4000-8000-000000000001"
        if obj["kind"] == "Deployment":
            obj["status"] = {"readyReplicas": 1}
        if obj["kind"] == "Namespace" and scenario == "foreign":
            obj["metadata"]["labels"].pop("sre-lab-role")
    (tmp_path / "fixture").write_text(json.dumps({"items": fixture}))
    selector = tmp_path / "selector"
    selector.write_text("demo-web")
    log = tmp_path / "patches"
    log.touch()
    drill = tmp_path / "drill_demo_service.sh"
    drill.write_bytes((ROOT / "scripts/drill_demo_service.sh").read_bytes())
    helper = tmp_path / "check_demo_identity.py"
    helper.write_bytes((ROOT / "scripts/check_demo_identity.py").read_bytes())
    command(tmp_path, "check_demo_service.sh", """
        if [ "$(cat "$WORK/selector")" = demo-web ]; then echo "body=ok"; exit 0; fi
        if [ "$SCENARIO" = bad-dns ]; then echo "dns=failed"; else echo "dns=ok"; echo "http=failed"; fi
        exit 1
    """)
    command(tmp_path, "k3s", """
        case " $* " in
            *" get namespace/sre-k8s-demo "*) cat "$WORK/fixture" ;;
            *" get pods "*) exit 0 ;;
            *" get endpointslices "*)
                if [ "$(cat "$WORK/selector")" = demo-web ]; then
                    echo '{"items":[{"endpoints":[{"conditions":{"ready":true}}]}]}'
                else echo '{"items":[]}'; fi ;;
            *" patch service "*)
                for arg do patch=$arg; done
                echo "$patch" >> "$WORK/patches"
                echo "$patch" | python3 -c 'import json,sys;print(json.load(sys.stdin)[-1]["value"])' > "$WORK/selector" ;;
            *" get service demo-web "*) echo "12345678-0000-4000-8000-000000000001 $(cat "$WORK/selector")" ;;
            *" get deployment demo-web "*) echo 1 ;;
            *) exit 1 ;;
        esac
    """)
    result = subprocess.run(
        ["bash", str(drill), "--run", "--context", "lab"], text=True, capture_output=True,
        env=dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", SCENARIO=scenario,
                 WORK=str(tmp_path)), check=False,
    )
    assert result.returncode == exit_code, result.stderr
    assert selector.read_text().strip() == "demo-web"
    operations = [json.loads(row) for row in log.read_text().splitlines()]
    assert len(operations) == patches
    if patches:
        assert operations[0][0] == operations[1][0] == {
            "op": "test", "path": "/metadata/uid", "value": "12345678-0000-4000-8000-000000000001",
        }
        assert operations[1][1]["value"] == {"app": operations[0][-1]["value"]}


def legacy_objects():
    objects = [x for x in yaml.safe_load_all((MANIFEST.parent / "demo.yaml").read_text())
               if x["kind"] in ("Namespace", "Deployment", "Service")]
    ns, deploy, svc = objects
    for key in LABELS:
        del ns["metadata"]["labels"][key]
        del deploy["spec"]["template"]["metadata"]["labels"][key]
    del deploy["metadata"]["labels"]
    for obj in objects:
        obj["metadata"]["annotations"] = {}
        applied = json.dumps(obj, indent=1)
        obj["metadata"]["annotations"]["kubectl.kubernetes.io/last-applied-configuration"] = applied
    deploy["status"] = {"readyReplicas": 1}
    svc["spec"]["clusterIP"] = "10.43.0.20"
    svc["metadata"]["uid"] = "12345678-0000-4000-8000-000000000001"
    return objects


@pytest.mark.parametrize("change", [None, "foreign", "live-drift", "wrong-provenance", "missing"])
def test_legacy_identity_requires_original_manifest_and_matching_live_fields(change):
    ns, deploy, svc = objects = legacy_objects()
    if change == "foreign":
        ns["metadata"]["labels"]["app.kubernetes.io/part-of"] = "other-project"
    elif change == "live-drift":
        deploy["spec"]["template"]["spec"]["containers"][0]["image"] = "custom-web"
    elif change == "wrong-provenance":
        key = "kubectl.kubernetes.io/last-applied-configuration"
        svc["metadata"]["annotations"][key] = svc["metadata"]["annotations"][key].replace(
            "8080", "8888"
        )
    elif change == "missing":
        ns["metadata"].pop("annotations")
    if change:
        with pytest.raises(ValueError, match="verified v0.4.0"):
            service_identity(objects)
    else:
        assert service_identity(objects) == svc["metadata"]["uid"]
