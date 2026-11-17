import copy
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest

from scripts import demo_k8s as demo


def uid(number):
    return f"00000000-0000-4000-8000-{number:012d}"


def resources():
    labels = {**demo.LABELS, "app": demo.DEPLOYMENT}

    def obj(kind, name, number, extra_labels=None):
        metadata = {"name": name, "uid": uid(number), "resourceVersion": "12",
                    "labels": {**labels, **(extra_labels or {})}}
        if kind != "Namespace":
            metadata["namespace"] = demo.NAMESPACE
        return {"apiVersion": "v1", "kind": kind, "metadata": metadata}

    namespace = obj("Namespace", demo.NAMESPACE, 1)
    deployment = obj("Deployment", demo.DEPLOYMENT, 2)
    deployment["metadata"]["generation"] = 1
    deployment["spec"] = {
        "replicas": 1, "selector": {"matchLabels": {"app": demo.DEPLOYMENT}},
        "template": {"metadata": {"labels": labels}},
    }
    deployment["status"] = {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1,
                            "readyReplicas": 1, "availableReplicas": 1}
    replicaset = obj("ReplicaSet", "demo-web-abc123", 3, {"pod-template-hash": "abc123"})
    replicaset["metadata"]["ownerReferences"] = [{
        "apiVersion": "apps/v1", "kind": "Deployment", "name": demo.DEPLOYMENT,
        "uid": uid(2), "controller": True,
    }]
    replicaset["spec"] = {"replicas": 1, "selector": {"matchLabels": {
        "app": demo.DEPLOYMENT, "pod-template-hash": "abc123",
    }}}
    pod = obj("Pod", "demo-web-abc123-old", 4, {"pod-template-hash": "abc123"})
    pod["metadata"]["ownerReferences"] = [{
        "apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "demo-web-abc123",
        "uid": uid(3), "controller": True,
    }]
    pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
    return {"namespace": namespace, "deployment": deployment, "replicaset": replicaset,
            "pods": {"items": [pod]}}


class FakeKubectl:
    def __init__(self):
        self.data = resources()
        self.deleted = []
        self.context_ok = True
        self.recover = True
        self.on_delete = None

    def get(self, kind, name=None):
        return copy.deepcopy(self.data[kind])

    def check_context(self):
        if not self.context_ok:
            raise demo.DemoError("context mismatch")

    def delete_pod(self, pod):
        self.deleted.append(copy.deepcopy(pod))
        if self.on_delete:
            self.on_delete(self)
        if self.recover:
            replacement = self.data["pods"]["items"][0]
            replacement["metadata"].update(name="demo-web-abc123-new", uid=uid(5))


def row(phase, ok=True, latency=10, crossing=False):
    return {"phase": phase, "phase_end": phase, "phase_crossing": crossing,
            "status": 200 if ok else 503, "ok": ok, "latency_ms": latency, "error": None}


class FakeTraffic:
    def __init__(self, bad_phase=None, empty=False):
        self.rows, self.error, self.dispatched = [], None, 0
        self.bad_phase, self.empty, self.stopped = bad_phase, empty, False

    def start(self):
        self.set_phase("baseline")

    def set_phase(self, phase):
        if not self.empty:
            self.rows += [row(phase, ok=phase != self.bad_phase) for _ in range(5)]
            self.dispatched = len(self.rows)

    def samples(self):
        return copy.deepcopy(self.rows)

    def stop(self):
        self.stopped = True


@pytest.fixture
def lab(monkeypatch, tmp_path):
    client, traffic = FakeKubectl(), FakeTraffic()
    clock = [0.0]
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.setattr(demo, "Kubectl", lambda *args: client)
    monkeypatch.setattr(demo, "Traffic", lambda *args: traffic)
    monkeypatch.setattr(demo.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(demo.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    for name, value in {"BASELINE_SECONDS": 0.5, "FINAL_SECONDS": 0.5,
                        "RECOVERY_SECONDS": 3, "HTTP_RECOVERY_SECONDS": 2,
                        "MAX_SECONDS": 20}.items():
        monkeypatch.setattr(demo, name, value)
    return client, traffic


def run_demo():
    report = {"status": "running", "recovery": {"tested": False, "verified": False},
              "http_recovery": {"verified": False},
              "deletion": {"attempted": False, "accepted": False}}
    return demo.execute("isolated-lab", "http://127.0.0.1:18080/", report)


def test_default_plan_never_accesses_cluster_or_http(monkeypatch, capsys):
    monkeypatch.setattr(demo.subprocess, "run", Mock(side_effect=AssertionError("cluster access")))
    monkeypatch.setattr(demo, "request_sample", Mock(side_effect=AssertionError("network access")))
    assert demo.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "not_tested"
    assert report["cluster_accessed"] is False
    assert report["recovery_tested"] is False


@pytest.mark.parametrize("url", [
    "http://example.com:18080/", "http://172.16.58.137:18080/", "https://127.0.0.1:18080/",
    "http://127.0.0.1/", "http://user:secret@127.0.0.1:18080/", "http://127.0.0.1:18080/?token=x",
    "http://127.0.0.1:18080/private", "http://127.0.0.1:18080/#x", None,
])
def test_non_loopback_or_credential_urls_rejected(url):
    with pytest.raises(demo.DemoError):
        demo.loopback_url(url)


def test_localhost_normalized_without_dns():
    assert demo.loopback_url("http://localhost:18080/") == "http://127.0.0.1:18080/"
    assert demo.loopback_url("http://[::1]:18080/") == "http://[::1]:18080/"


@pytest.mark.parametrize("mutate", [
    lambda d: d["namespace"]["metadata"]["labels"].clear(),
    lambda d: d["namespace"]["metadata"].update(name="sre-monitoring"),
    lambda d: d["deployment"]["metadata"].update(uid="not-a-uid"),
    lambda d: d["deployment"]["spec"].update(replicas=2),
    lambda d: d["deployment"]["spec"].update(paused=True),
    lambda d: d["deployment"]["status"].update(observedGeneration=0),
    lambda d: d["deployment"]["spec"]["template"]["metadata"]["labels"].clear(),
    lambda d: d["replicaset"]["metadata"]["ownerReferences"][0].update(uid=uid(99)),
    lambda d: d["pods"]["items"][0]["metadata"]["ownerReferences"][0].update(controller=False),
    lambda d: d["pods"]["items"][0]["metadata"]["ownerReferences"][0].update(uid=uid(99)),
    lambda d: d["pods"]["items"][0]["metadata"].update(namespace="default"),
    lambda d: d["pods"]["items"][0]["metadata"]["labels"].update(app="product-api"),
    lambda d: d["pods"]["items"][0]["metadata"]["labels"].update({"pod-template-hash": "other"}),
    lambda d: d["pods"]["items"].append(copy.deepcopy(d["pods"]["items"][0])),
    lambda d: d["pods"]["items"][0]["metadata"].update(deletionTimestamp="now"),
])
def test_strict_scope_guards_prevent_deletion(lab, mutate):
    client, _ = lab
    mutate(client.data)
    assert run_demo()["status"] == "failed"
    assert client.deleted == []


def test_context_mismatch_prevents_traffic_and_deletion(lab):
    client, traffic = lab
    client.context_ok = False
    report = run_demo()
    assert report["status"] == "failed"
    assert client.deleted == [] and traffic.rows == []


@pytest.mark.parametrize("empty", [False, True])
def test_failed_or_empty_baseline_prevents_deletion(lab, empty):
    client, traffic = lab
    traffic.bad_phase, traffic.empty = "baseline", empty
    report = run_demo()
    assert report["status"] == "failed" and not client.deleted
    assert traffic.stopped


def test_recovery_requires_new_uid_and_http_observation(lab):
    client, traffic = lab
    report = run_demo()
    assert report["status"] == "passed"
    assert len(client.deleted) == 1
    assert report["recovery"]["new_uid"] != report["baseline"]["pod_uid"]
    assert report["phases"]["recovery_http"]["requests"] == 5
    assert report["traffic"]["unfinished_requests"] == 0
    assert traffic.stopped


def test_same_old_pod_ready_is_not_recovery(lab):
    client, _ = lab
    client.recover = False
    report = run_demo()
    assert report["status"] == "failed"
    assert report["recovery"]["tested"] is True
    assert report["recovery"]["verified"] is False
    assert len(client.deleted) == 1


def test_controller_changed_after_delete_fails_without_second_delete(lab):
    client, _ = lab
    client.on_delete = lambda c: c.data["deployment"]["metadata"].update(uid=uid(99))
    report = run_demo()
    assert report["status"] == "failed"
    assert len(client.deleted) == 1


def test_pod_replaced_before_delete_is_not_targeted(lab):
    client, _ = lab
    checks = [0]

    def check_context():
        checks[0] += 1
        if checks[0] == 2:
            client.data["pods"]["items"][0]["metadata"]["uid"] = uid(99)

    client.check_context = check_context
    report = run_demo()
    assert report["status"] == "failed"
    assert not client.deleted


def test_delete_failure_preserves_attempt_without_claiming_recovery(lab):
    client, traffic = lab

    def rejected(pod):
        raise demo.DemoError("UID/resourceVersion precondition rejected")

    client.delete_pod = rejected
    report = run_demo()
    assert report["status"] == "failed"
    assert report["deletion"]["attempted"] is True
    assert report["deletion"]["accepted"] is False
    assert report["recovery"]["verified"] is False
    assert traffic.stopped


def test_ready_pod_but_dead_port_forward_remains_failed(lab):
    _, traffic = lab
    traffic.bad_phase = "recovery_http"
    report = run_demo()
    assert report["status"] == "failed"
    assert report["recovery"]["tested"] is True and report["recovery"]["ready"] is True
    assert report["http_recovery"]["verified"] is False
    assert report["phases"]["recovery_http"]["errors"] == 5
    assert traffic.stopped


def test_failed_execute_still_writes_json_and_does_not_overwrite(lab, tmp_path, capsys):
    client, _ = lab
    output = tmp_path / "evidence.json"
    assert demo.main(["--execute", "--output", str(output)]) == 1
    report = json.loads(output.read_text())
    assert report["status"] == "failed" and report["deletion"]["attempted"] is False
    assert not client.deleted
    before = output.read_bytes()
    assert demo.main(["--execute", "--output", str(output)]) == 1
    assert output.read_bytes() == before
    capsys.readouterr()


def test_kubectl_uses_fixed_context_and_delete_uid_preconditions(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="{}")

    monkeypatch.setattr(demo.subprocess, "run", run)
    client = demo.Kubectl("isolated-lab", deadline=100, clock=lambda: 0)
    pod = resources()["pods"]["items"][0]
    client.delete_pod(pod)
    command, kwargs = calls[0]
    assert command[:4] == ["kubectl", "--context", "isolated-lab", "--request-timeout=8s"]
    assert command[4:] == ["delete", "--raw", "/api/v1/namespaces/sre-k8s-demo/pods/demo-web-abc123-old", "-f", "-"]
    body = json.loads(kwargs["input"])
    assert body["preconditions"] == {"uid": uid(4), "resourceVersion": "12"}
    assert body["gracePeriodSeconds"] == 5
    assert kwargs["timeout"] <= 10
    assert "shell" not in kwargs


def test_kubectl_error_output_is_not_leaked(monkeypatch):
    monkeypatch.setattr(demo.subprocess, "run", Mock(side_effect=subprocess.CalledProcessError(
        1, "kubectl", stderr="SECRET-CREDENTIAL",
    )))
    with pytest.raises(demo.DemoError) as error:
        demo.Kubectl("lab", deadline=100, clock=lambda: 0).get("pods")
    assert "SECRET-CREDENTIAL" not in str(error.value)


def test_statistics_include_failures_but_do_not_invent_empty_success():
    data = [row("baseline", latency=n) for n in range(1, 21)]
    data[-1]["ok"] = False
    stats = demo.summary(data)
    assert stats == {"requests": 20, "errors": 1, "successes": 19, "p95_ms": 19,
                     "successful_p95_ms": 19}
    assert demo.summary([])["p95_ms"] is None
    traffic = FakeTraffic()
    traffic.rows = [row("baseline", crossing=True) for _ in range(10)]
    with pytest.raises(demo.DemoError):
        demo.require_traffic(traffic, "baseline")


def test_traffic_worker_exception_is_not_silently_ignored(monkeypatch):
    monkeypatch.setattr(demo, "request_sample", Mock(side_effect=RuntimeError("worker failed")))
    traffic = demo.Traffic("http://127.0.0.1:18080/", demo.time.monotonic() + 10)
    traffic.start()
    traffic.stop()
    assert traffic.error == "RuntimeError"
    assert traffic.dispatched == 1 and traffic.samples() == []


def test_http_disables_proxy_redirect_and_checks_demo_body(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        mode = "ok"

        def do_GET(self):
            self.send_response(302 if self.mode == "redirect" else 200)
            if self.mode == "redirect":
                self.send_header("Location", "http://192.0.2.1:1/")
            self.end_headers()
            self.wfile.write(demo.BODY if self.mode == "ok" else b"unrelated application")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:1")
    monkeypatch.setenv("http_proxy", "http://192.0.2.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    url = "http://127.0.0.1:" + str(server.server_port) + "/"
    try:
        assert demo.request_sample(url)["ok"] is True
        Handler.mode = "wrong-body"
        assert demo.request_sample(url)["ok"] is False
        Handler.mode = "redirect"
        result = demo.request_sample(url)
        assert result["status"] == 302 and result["ok"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
