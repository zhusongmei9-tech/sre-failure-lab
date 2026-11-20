"""Regression coverage for admission, concurrent writes and compensation during a rollout."""

import copy
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from scripts import k8s_app_rollout as rollout

MANIFESTS = Path(__file__).resolve().parents[1] / "monitoring/kubernetes/app-lab"


@pytest.fixture
def cluster():
    objects = {}
    for path in MANIFESTS.glob("*.yaml"):
        for obj in yaml.safe_load_all(path.read_text()):
            objects[obj["kind"].lower() + "/" + obj["metadata"]["name"]] = obj
    deploy = objects["deployment/product-api"]
    deploy["metadata"]["uid"] = "app-uid"
    objects["service/product-api"]["spec"]["clusterIP"] = "10.43.0.10"
    client = Mock()
    client.get.side_effect = lambda resource: copy.deepcopy(objects[resource])

    def patch(uid, current, desired):
        assert deploy["metadata"]["uid"] == uid
        assert deploy["spec"]["template"] == current
        deploy["spec"]["template"] = copy.deepcopy(desired)

    client.patch.side_effect = patch
    return client, objects


def test_manifest_identity_and_destination(cluster):
    client, objects = cluster
    uid, _, url = rollout.guard(client)
    assert uid == "app-uid" and url == "http://10.43.0.10:8000/products/1"
    del objects["namespace/sre-app-lab"]["metadata"]["labels"]["sre-lab-role"]
    with pytest.raises(rollout.DrillError, match="ownership"):
        rollout.guard(client)
    client.patch.assert_not_called()


def test_recovery_preserves_concurrent_edit(cluster):
    client, objects = cluster
    uid, template, _ = rollout.guard(client)
    objects["deployment/product-api"]["spec"]["template"]["metadata"]["labels"]["operator"] = "fix"
    with pytest.raises(rollout.DrillError, match="unrelated rollout"):
        rollout.restore(client, uid, template, template)
    client.patch.assert_not_called()


@pytest.mark.parametrize("verify_bad", [True, False])
def test_rollout_restores_approved_image_after_bad_revision(cluster, monkeypatch, verify_bad):
    client, objects = cluster
    bad_seen = []

    def wait(_client, _uid, template, timeout=75):
        api = template["spec"]["containers"][0]
        if api["readinessProbe"]["httpGet"]["path"] == rollout.BAD:
            assert timeout == 18
            bad_seen.append(api["image"])
            raise TimeoutError("rollout stalled")

    traffic = Mock()
    traffic.report.return_value = {"rollback": {"failed": 0}}
    monkeypatch.setattr(rollout, "Traffic", lambda url: traffic)
    monkeypatch.setattr(rollout, "wait", wait)

    def replica_set(*args):
        if not verify_bad:
            raise rollout.DrillError("missing failed candidate")
        return "rejected-rs"

    monkeypatch.setattr(rollout, "failed_candidate", replica_set)
    report = {}
    if verify_bad:
        rollout.drill(client, report)
        assert report["unready_candidate"] == "rejected-rs" and report["restored_readiness"]
    else:
        with pytest.raises(rollout.DrillError, match="missing failed candidate"):
            rollout.drill(client, report)
        assert report["cleanup_restored_version"] == "0.3.1"
    assert bad_seen == ["sre-lab-api:0.3.1"]
    active = objects["deployment/product-api"]["spec"]["template"]["spec"]["containers"][0]
    assert active["image"] == "sre-lab-api:0.3.1"
    assert active["readinessProbe"]["httpGet"]["path"] == rollout.READY


def test_traffic_summary_counts_connection_and_application_failures():
    traffic = rollout.Traffic("http://127.0.0.1:1/products/1")
    traffic.rows = [
        ("rolling_update", "0.3.0", 2.0),
        ("rolling_update", "0.3.1", 3.0),
        ("rolling_update", "HTTP_503", 4.0),
        ("rollback", "TimeoutError", 2002.0),
    ]
    report = traffic.report()
    assert report["rolling_update"] == {
        "requests": 3,
        "failed": 1,
        "p95_ms": 4.0,
        "responses": {"0.3.0": 1, "0.3.1": 1, "HTTP_503": 1},
    }
    assert report["rollback"]["failed"] == 1
