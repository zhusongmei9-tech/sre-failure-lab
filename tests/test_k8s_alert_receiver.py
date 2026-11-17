"""Local HTTP, event bounds and controlled-executor tests; no Kubernetes cluster needed."""

import copy
import http.client
import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


receiver = load("alert_receiver", "monitoring/kubernetes/alerting/receiver.py")
acceptance = load("alert_acceptance", "scripts/accept_k8s_alerts.py")


def payload(status="firing", run_id="localtest"):
    return {"alerts": [{"status": status,
                        "labels": {"alertname": "SRELabSynthetic", "run_id": run_id},
                        "annotations": {"summary": "local unit test"}}]}


@pytest.fixture
def endpoint():
    server = receiver.Receiver(("127.0.0.1", 0))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield "http://127.0.0.1:" + str(server.server_port), server
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def post(base, body, content_type="application/json"):
    request = Request(base + "/alerts", data=body, headers={"Content-Type": content_type})
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        with exc:
            return exc.code, json.load(exc)


def get(base, suffix):
    with urlopen(base + suffix, timeout=5) as response:
        return json.load(response)


def test_real_http_firing_resolved_and_metrics(endpoint):
    base, server = endpoint
    assert post(base, json.dumps(payload()).encode()) == (200, {"accepted": 1})
    assert post(base, json.dumps(payload("resolved")).encode()) == (200, {"accepted": 1})
    snapshot = get(base, "/events?after=0&run_id=localtest")
    assert [item["status"] for item in snapshot["events"]] == ["firing", "resolved"]
    assert snapshot["instance_id"] == get(base, "/healthz")["instance_id"]
    assert get(base, "/events?after=1")["events"][0]["sequence"] == 2
    assert get(base, "/events?run_id=another")["events"] == []
    with urlopen(base + "/metrics", timeout=5) as response:
        metrics = response.read().decode()
    assert 'sre_lab_alert_receiver_received_total{status="resolved"} 1' in metrics
    assert server.store.sequence == 2


@pytest.mark.parametrize("body,content_type,status", [
    (b"x" * (receiver.MAX_BODY + 1), "application/json", 413),
    (b"{}", "text/plain", 415),
    (b"not json", "application/json", 400),
    (b"[]", "application/json", 400),
    (b'{"alerts":[]}', "application/json", 400),
    (b'{"alerts":[{"status":[],"labels":{}}]}', "application/json", 400),
    (b'{"alerts":[{"status":"firing","labels":[]}]}', "application/json", 400),
])
def test_rejects_invalid_or_large_requests_without_events(endpoint, body, content_type, status):
    base, server = endpoint
    assert post(base, body, content_type)[0] == status
    assert server.store.sequence == 0


def test_requires_one_content_length(endpoint):
    _base, server = endpoint
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    connection.putrequest("POST", "/alerts")
    connection.putheader("Content-Type", "application/json")
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 411
    response.read()
    connection.close()


def test_retention_fields_and_batch_validation_are_bounded():
    item = payload()["alerts"][0]
    item["labels"].update(secret="not retained", pod="x" * 1000)
    item["annotations"].update(token="not retained", summary="y" * 1000)
    parsed = receiver.parse_alerts({"alerts": [item]})
    assert "secret" not in parsed[0]["labels"]
    assert "token" not in parsed[0]["annotations"]
    assert len(parsed[0]["labels"]["pod"]) == len(parsed[0]["annotations"]["summary"]) == 256
    store = receiver.EventStore()
    for _ in range(receiver.MAX_EVENTS + 4):
        store.append(parsed)
    snapshot = store.snapshot()
    assert len(snapshot["events"]) == receiver.MAX_EVENTS
    assert snapshot["dropped_events"] == 4
    assert snapshot["oldest_sequence"] == 5
    with pytest.raises(ValueError):
        receiver.parse_alerts({"alerts": [item] * (receiver.MAX_ALERTS + 1)})
    with pytest.raises(ValueError):
        receiver.parse_alerts({"alerts": [item, {"status": "invalid"}]})


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:19093", "http://example.com:8080", "http://127.0.0.1",
    "http://user:secret@127.0.0.1:8080", "http://127.0.0.1:8080/path",
    "http://127.0.0.1:8080?token=secret",
])
def test_acceptance_rejects_nonlocal_or_credential_urls(url):
    with pytest.raises(ValueError):
        acceptance.local_url(url)


def test_no_redirect_is_followed():
    with pytest.raises(RuntimeError, match="redirect"):
        acceptance.NoRedirect().redirect_request(None, None, 302, "moved", {}, "http://example.com")


class Pipeline:
    def __init__(self, *, current="lab", webhook=True, prom=True, changed_uid=False):
        self.current, self.webhook, self.prom = current, webhook, prom
        self.changed_uid = changed_uid
        self.time = 0
        self.phase = None
        self.resource = None
        self.commands = []
        self.store = receiver.EventStore()

    def command(self, context, *args, payload=None):
        self.commands.append((context, args, payload))
        if args[:2] == ("config", "current-context"):
            return self.current
        if args[0] == "create":
            self.resource = copy.deepcopy(payload)
            self.resource["metadata"]["uid"] = "original-uid"
            self.phase = "firing"
            if self.webhook:
                self.store.append(receiver.parse_alerts(globals()["payload"](
                    "firing", self.resource["metadata"]["labels"]["run_id"])))
            return json.dumps(self.resource)
        if args[0] == "patch":
            patch = json.loads(args[-1])
            assert patch[0] == {"op": "test", "path": "/metadata/uid", "value": "original-uid"}
            assert patch[-1]["value"] == "vector(0) == 1"
            if self.changed_uid:
                raise RuntimeError("UID precondition failed")
            self.phase = "resolved"
            if self.webhook:
                self.store.append(receiver.parse_alerts(globals()["payload"](
                    "resolved", self.resource["metadata"]["labels"]["run_id"])))
            return "{}"
        if args[0] == "delete":
            assert args[1] == "--raw" and "/namespaces/sre-monitoring/" in args[2]
            assert payload["preconditions"] == {"uid": "original-uid"}
            if self.changed_uid:
                raise RuntimeError("UID precondition failed")
            self.phase = "deleted"
            return "{}"
        raise AssertionError(args)

    def get(self, url):
        parts = urlsplit(url)
        if parts.path == "/healthz":
            return {"status": "ok", "instance_id": self.store.instance_id}
        if parts.path == "/events":
            query = parse_qs(parts.query)
            return self.store.snapshot(int(query.get("after", ["0"])[0]),
                                       query.get("run_id", [None])[0])
        if parts.path == "/api/v1/query":
            items = []
            if self.phase == "firing" and self.prom:
                items.append({"metric": {"alertname": "SRELabSynthetic", "alertstate": "firing",
                                         "run_id": self.resource["metadata"]["labels"]["run_id"]},
                              "value": [self.time, "1"]})
            return {"status": "success", "data": {"resultType": "vector", "result": items}}
        raise AssertionError(url)

    def sleep(self, seconds):
        self.time += seconds

    def run(self):
        return acceptance.exercise("lab", "http://127.0.0.1:19090", "http://127.0.0.1:19093", 30,
                                   command=self.command, get=self.get, clock=lambda: self.time,
                                   sleep=self.sleep)


def test_full_controlled_pipeline_verifies_both_phases_and_uid_cleanup():
    pipeline = Pipeline()
    report = pipeline.run()
    assert report["passed"] is True
    assert report["checks"]["prometheus_firing"] and report["checks"]["webhook_firing"]
    assert report["checks"]["prometheus_resolved"] and report["checks"]["webhook_resolved"]
    assert pipeline.phase == "deleted"
    assert report["cleanup"] == "deleted_with_uid_precondition"


@pytest.mark.parametrize("webhook,prom", [(False, True), (True, False)])
def test_one_side_of_pipeline_cannot_pass(webhook, prom):
    pipeline = Pipeline(webhook=webhook, prom=prom)
    report = pipeline.run()
    assert report["passed"] is False
    assert "Timed out" in report["error"]
    assert pipeline.phase == "deleted"


def test_mismatched_context_refuses_all_mutation():
    pipeline = Pipeline(current="production")
    report = pipeline.run()
    assert not report["passed"]
    assert len(pipeline.commands) == 1
    assert pipeline.resource is None


def test_replaced_rule_never_blindly_patched_or_deleted():
    pipeline = Pipeline(changed_uid=True)
    report = pipeline.run()
    assert not report["passed"]
    assert report["cleanup"] == "failed_check_rule_manually"
    assert pipeline.phase == "firing"


def test_receiver_restart_or_eviction_invalidates_continuity():
    store = receiver.EventStore()
    cursor = {"instance_id": "old-instance", "sequence": 0}
    with pytest.raises(RuntimeError, match="restarted"):
        acceptance.observe_events("http://127.0.0.1:8080", "x", cursor, lambda _: store.snapshot())
    cursor["instance_id"] = store.instance_id
    for _ in range(receiver.MAX_EVENTS + 1):
        store.append(receiver.parse_alerts(payload()))
    with pytest.raises(RuntimeError, match="evicted"):
        acceptance.observe_events("http://127.0.0.1:8080", "x", cursor, lambda _: store.snapshot())


def test_plan_works_without_context_urls_or_kubectl(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "scripts/accept_k8s_alerts.py"), "--plan"],
                            cwd=tmp_path, env={"PATH": ""}, text=True, capture_output=True, check=True)
    report = json.loads(result.stdout)
    assert report["performed"] is False
    assert report["cluster_accessed"] is False
    assert report["status"] == "not_tested"


def test_default_rule_render_excludes_synthetic_and_preserves_canonical_spec():
    module = load("render_rule_test", "monitoring/kubernetes/alerting/render_rules.py")
    manifest = module.manifest()
    assert manifest["spec"] == json.loads((ROOT / "monitoring/kubernetes/alerting/rules.json").read_text())
    assert all(rule["alert"] != "SRELabSynthetic" for group in manifest["spec"]["groups"]
               for rule in group["rules"])
    assert module.manifest("explicitrun")["spec"]["groups"][0]["rules"][0]["labels"]["run_id"] == "explicitrun"
