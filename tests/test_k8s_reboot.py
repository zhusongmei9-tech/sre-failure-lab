import copy
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import accept_k8s_reboot as reboot

BOOT_A = "11111111-1111-4111-8111-111111111111"
BOOT_B = "22222222-2222-4222-8222-222222222222"
URL = "http://172.17.0.1:19090"


def object_meta(name, kind="Deployment"):
    return {"kind": kind, "metadata": {"name": name, "uid": name + "-uid", "generation": 1}}


def deployment(name, kind="Deployment"):
    obj = object_meta(name, kind)
    obj["spec"] = {"replicas": 1, "selector": {"matchLabels": {"app": name}}}
    obj["status"] = {
        "observedGeneration": 1,
        "readyReplicas": 1,
        "availableReplicas": 1,
        "updatedReplicas": 1,
    }
    if kind == "StatefulSet":
        obj["spec"]["volumeClaimTemplates"] = [{"metadata": {"name": "prometheus-db"}}]
        obj["status"].update(currentRevision="revision-1", updateRevision="revision-1")
    return obj


@pytest.fixture
def cluster(monkeypatch):
    host = {
        "machine_id_sha256": "a" * 64,
        "boot_id": BOOT_A,
        "hostname": "lab",
        "uptime_seconds": 2000,
    }
    healthy_service = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "UnitFileState": "enabled",
        "ActiveEnterTimestampMonotonic": "1000000",
    }
    objects = {}
    namespace = object_meta("kube-system", "Namespace")
    objects[("namespace", "kube-system", None)] = namespace
    node = object_meta("lab", "Node")
    node["status"] = {
        "nodeInfo": {"bootID": BOOT_A, "kubeletVersion": "v1.37.2+k3s1", "architecture": "arm64"},
        "conditions": [
            {"type": "Ready", "status": "True"},
            {"type": "DiskPressure", "status": "False"},
        ],
    }
    objects[("nodes", None, None)] = {"items": [node]}
    for namespace_name, name in reboot.DEPLOYMENTS:
        objects[("deployment", name, namespace_name)] = deployment(name)
    objects[("statefulset", reboot.PROMETHEUS_STS, "sre-monitoring")] = deployment(
        reboot.PROMETHEUS_STS, "StatefulSet"
    )
    pvc_name = "prometheus-db-" + reboot.PROMETHEUS_STS + "-0"
    pvc = object_meta(pvc_name, "PersistentVolumeClaim")
    pvc.update(
        spec={"volumeName": "pv-1", "storageClassName": "local-path"},
        status={"phase": "Bound", "capacity": {"storage": "1Gi"}},
    )
    pv = object_meta("pv-1", "PersistentVolume")
    pv.update(spec={"claimRef": {"uid": pvc["metadata"]["uid"]}}, status={"phase": "Bound"})
    objects[("pvc", pvc_name, "sre-monitoring")] = pvc
    objects[("pv", "pv-1", None)] = pv
    calls = []

    class FakeClient:
        def get(self, deadline, resource, name=None, namespace=None):
            calls.append(("get", resource, name, namespace))
            return copy.deepcopy(objects[(resource, name, namespace)])

        def text(self, deadline, *arguments):
            calls.append(arguments)
            if arguments[0] == "config":
                return "https://127.0.0.1:6443"
            if arguments[1].startswith("--raw="):
                return "SRE Kubernetes demo is ready\n"
            assert arguments[:2] == ("get", "pods")
            label = arguments[arguments.index("--selector") + 1].split("=", 1)[1]
            pod = object_meta(label + "-pod", "Pod")
            pod["spec"] = {"containers": [{"name": "main"}]}
            pod["status"] = {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [
                    {
                        "name": "main",
                        "ready": True,
                        "restartCount": 0,
                        "state": {"running": {"startedAt": "2026-11-17T01:00:00Z"}},
                    }
                ],
            }
            return json.dumps({"items": [pod]})

    monkeypatch.setattr(reboot, "host_identity", lambda: copy.deepcopy(host))
    monkeypatch.setattr(
        reboot, "service_status", lambda unit, deadline: copy.deepcopy(healthy_service)
    )
    monkeypatch.setattr(
        reboot, "local_prometheus", lambda url, deadline: {"url": url, "http_status": 200}
    )
    clock = [0.0]
    monkeypatch.setattr(reboot.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        reboot.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    return SimpleNamespace(
        host=host,
        node=node,
        objects=objects,
        service=healthy_service,
        calls=calls,
        client=FakeClient(),
        clock=clock,
        pvc=pvc,
        pv=pv,
    )


def baseline(cluster):
    result = reboot.observe("before", cluster.client, URL, timeout=10)
    assert result["status"] == "ready_for_reboot"
    return result


def change_boot(cluster):
    cluster.host["boot_id"] = BOOT_B
    cluster.host["uptime_seconds"] = 40
    cluster.node["status"]["nodeInfo"]["bootID"] = BOOT_B


def test_before_is_a_baseline_not_a_reboot_pass(cluster):
    report = baseline(cluster)
    assert report["reboot_requested_by_script"] is False
    assert report["evidence"]["healthy"] and report["evidence"]["volumes"]
    assert report["evidence"]["pods"] and report["evidence"]["demo_http"]["expected_body_observed"]
    assert all(call[0] in ("get", "config") for call in cluster.calls)


def test_after_same_machine_and_cluster_with_new_boot_can_pass(cluster):
    before = baseline(cluster)
    change_boot(cluster)
    result = reboot.observe("after", cluster.client, baseline=before, timeout=10)
    assert result["status"] == "passed" and result["comparison_errors"] == []
    assert before["evidence"]["host"]["boot_id"] == BOOT_A


def test_same_boot_is_rejected_without_cluster_calls(cluster):
    before = baseline(cluster)
    cluster.calls.clear()
    result = reboot.observe("after", cluster.client, baseline=before, timeout=10)
    assert result["status"] == "failed" and "boot_id_unchanged" in result["error"]
    assert not cluster.calls


@pytest.mark.parametrize(
    "replacement,expected",
    [
        ("machine", "machine_changed"),
        ("cluster", "cluster_changed"),
        ("node", "node_replaced"),
        ("pvc", "persistent_volume_changed"),
        ("pv", "persistent_volume_changed"),
        ("deployment", "deployment_changed"),
        ("generation", "deployment_changed"),
        ("statefulset", "statefulset_changed"),
    ],
)
def test_replacements_are_not_accepted_as_reboot_recovery(cluster, replacement, expected):
    before = baseline(cluster)
    change_boot(cluster)
    if replacement == "machine":
        cluster.host["machine_id_sha256"] = "b" * 64
    elif replacement == "cluster":
        cluster.objects[("namespace", "kube-system", None)]["metadata"]["uid"] = "new-cluster"
    elif replacement == "node":
        cluster.node["metadata"]["uid"] = "new-node"
    elif replacement == "pvc":
        cluster.pvc["metadata"]["uid"] = "new-pvc"
        cluster.pv["spec"]["claimRef"]["uid"] = "new-pvc"
    elif replacement == "pv":
        cluster.pv["metadata"]["uid"] = "new-pv"
    elif replacement == "statefulset":
        cluster.objects[("statefulset", reboot.PROMETHEUS_STS, "sre-monitoring")]["metadata"][
            "uid"
        ] = "new-sts"
    else:
        obj = cluster.objects[("deployment", "demo-web", "sre-k8s-demo")]
        if replacement == "generation":
            obj["metadata"]["generation"] = 2
            obj["status"]["observedGeneration"] = 2
        else:
            obj["metadata"]["uid"] = "new-deployment"
    result = reboot.observe("after", cluster.client, baseline=before, timeout=10)
    assert result["status"] == "failed"
    assert expected in result.get("error", "") or any(
        expected in error for error in result.get("comparison_errors", [])
    )


@pytest.mark.parametrize(
    "failure",
    [
        "service",
        "node_boot",
        "node_ready",
        "deployment",
        "zero_replicas",
        "pvc",
        "pv_binding",
        "bridge",
    ],
)
def test_missing_or_unhealthy_evidence_never_creates_successful_baseline(
    cluster, monkeypatch, failure
):
    if failure == "service":
        cluster.service["ActiveState"] = "inactive"
    elif failure == "node_boot":
        cluster.node["status"]["nodeInfo"]["bootID"] = BOOT_B
    elif failure == "node_ready":
        cluster.node["status"]["conditions"][0]["status"] = "False"
    elif failure in ("deployment", "zero_replicas"):
        obj = cluster.objects[("deployment", "demo-web", "sre-k8s-demo")]
        if failure == "zero_replicas":
            obj["spec"]["replicas"] = 0
        else:
            obj["status"]["availableReplicas"] = 0
    elif failure == "pvc":
        cluster.pvc["status"]["phase"] = "Pending"
    elif failure == "pv_binding":
        cluster.pv["spec"]["claimRef"]["uid"] = "somebody-else"
    else:
        monkeypatch.setattr(
            reboot, "local_prometheus", Mock(side_effect=reboot.CheckError("bridge down"))
        )
    result = reboot.observe("before", cluster.client, URL, timeout=10)
    assert result["status"] == "failed" and not result["evidence"]["healthy"]


def test_running_pod_without_ready_containers_cannot_pass(cluster, monkeypatch):
    original = cluster.client.text

    def text(deadline, *args):
        output = original(deadline, *args)
        if args[:2] == ("get", "pods"):
            payload = json.loads(output)
            payload["items"][0]["status"]["containerStatuses"][0]["ready"] = False
            return json.dumps(payload)
        return output

    monkeypatch.setattr(cluster.client, "text", text)
    result = reboot.observe("before", cluster.client, URL, timeout=10)
    assert result["status"] == "failed" and result["evidence"]["error"]["stage"] == "deployments"


def test_after_waits_boundedly_but_does_not_repair(cluster, monkeypatch):
    before = baseline(cluster)
    change_boot(cluster)
    monkeypatch.setattr(
        reboot,
        "service_status",
        lambda unit, deadline: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "SubState": "dead",
            "UnitFileState": "enabled",
        },
    )
    result = reboot.observe("after", cluster.client, baseline=before, timeout=5)
    assert result["status"] == "failed" and cluster.clock[0] == 5
    assert not result["reboot_requested_by_script"]


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"status": "passed"},
        {
            "schema_version": 1,
            "kind": "k8s_reboot_acceptance",
            "phase": "before",
            "status": "ready_for_reboot",
            "evidence": {"healthy": True},
        },
    ],
)
def test_incomplete_baseline_is_rejected(cluster, value):
    assert reboot.observe("after", cluster.client, baseline=value)["status"] == "failed"


def test_report_exclusive_write_protects_original_evidence(tmp_path):
    path = tmp_path / "before.json"
    reboot.save_report(path, {"status": "ready_for_reboot"})
    original = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        reboot.save_report(path, {"status": "passed"})
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "kind,prefix", [("k3s", ["/test/k3s", "kubectl"]), ("kubectl", ["/test/kubectl"])]
)
def test_explicit_client_pins_kubeconfig_and_request_timeout(monkeypatch, kind, prefix):
    monkeypatch.setattr(reboot.shutil, "which", lambda name: "/test/" + name)
    command = reboot.Client(kind, "/test/config").command("get", "nodes")
    assert command == prefix + [
        "--kubeconfig",
        "/test/config",
        "--request-timeout=8s",
        "get",
        "nodes",
    ]


def test_runner_errors_do_not_include_credentials(monkeypatch):
    monkeypatch.setattr(
        reboot.subprocess,
        "run",
        Mock(
            side_effect=subprocess.CalledProcessError(
                1, "kubectl", output="secret-token", stderr="password-value"
            )
        ),
    )
    with pytest.raises(reboot.CheckError) as error:
        reboot.run(["kubectl", "get", "nodes"], reboot.time.monotonic() + 10)
    assert "secret-token" not in str(error.value) and "password-value" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://172.17.0.1:19090",
        "http://192.168.1.2:19090",
        "http://user:pw@172.17.0.1:19090",
        "http://172.17.0.1:9090",
        "http://remote.example:19090",
        "http://172.17.0.1:19090/path",
    ],
)
def test_prometheus_requires_exact_local_bridge_url(monkeypatch, url):
    monkeypatch.setattr(
        reboot,
        "run",
        lambda *args: json.dumps([{"addr_info": [{"family": "inet", "local": "172.17.0.1"}]}]),
    )
    opener = Mock(side_effect=AssertionError("must not send HTTP request"))
    monkeypatch.setattr(reboot, "build_opener", opener)
    with pytest.raises(reboot.CheckError):
        reboot.local_prometheus(url, reboot.time.monotonic() + 10)
    opener.assert_not_called()


def test_prometheus_bypasses_environment_proxy_and_forbids_redirect(monkeypatch):
    monkeypatch.setattr(
        reboot,
        "run",
        lambda *args: json.dumps([{"addr_info": [{"family": "inet", "local": "172.17.0.1"}]}]),
    )
    response = Mock(status=200)
    response.read.return_value = b"ready"
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=None)
    opener = Mock()
    opener.open.return_value = response
    factory = Mock(return_value=opener)
    monkeypatch.setattr(reboot, "build_opener", factory)
    assert reboot.local_prometheus(URL, reboot.time.monotonic() + 10)["http_status"] == 200
    proxy, redirect = factory.call_args.args
    assert proxy.proxies == {} and isinstance(redirect, reboot.NoRedirect)
    with pytest.raises(reboot.CheckError, match="must not redirect"):
        redirect.redirect_request(None, None, 302, "Found", {}, "http://remote.example/")
