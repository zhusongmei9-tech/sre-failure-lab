import copy
import json
import subprocess
from unittest.mock import Mock

import pytest

from ops import common, deploy


def artifact(version):
    digit = {"v1": "1", "v2": "2", "v3": "3"}[version]
    return {
        "image": "sre-lab-api:" + version,
        "version": version,
        "image_id": "sha256:" + digit * 64,
    }


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr(deploy, "inspect_image", lambda image: artifact(image.split(":")[1]))
    monkeypatch.setattr(deploy, "compose", Mock())
    monkeypatch.setattr(deploy, "start_slot", Mock())
    monkeypatch.setattr(deploy, "assert_runtime_image", Mock())
    monkeypatch.setattr(deploy, "wait_slot", Mock())
    monkeypatch.setattr(deploy, "wait_edge", Mock())
    monkeypatch.setattr(deploy, "probe_slot", Mock())
    monkeypatch.setattr(deploy, "probe_url", Mock())
    monkeypatch.setattr(deploy, "switch_upstream", Mock())
    now = [0.0]
    monkeypatch.setattr(deploy.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(deploy.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return tmp_path


def test_initial_release_and_manual_rollback_switch_real_adapter(runtime):
    initial = deploy.initialize("sre-lab-api:v1")
    assert initial["active"] == "blue" and initial["previous"] is None
    deploy.start_slot.assert_called_once_with("blue", artifact("v1"))
    completed = deploy.deploy("sre-lab-api:v2", observe_seconds=1)
    assert completed["active"] == "green" and completed["previous"] == "blue"
    assert "stable_state" not in completed
    deploy.switch_upstream.assert_called_with("green")
    restored = deploy.rollback()
    assert restored["active"] == "blue" and restored["previous"] == "green"
    deploy.switch_upstream.assert_called_with("blue")
    assert common.read_state() == restored
    env = (runtime / "runtime/images.env").read_text()
    assert "sha256:" in env and "sre-lab-api:v2" not in env


def test_candidate_business_failure_never_switches_and_loses_overwritten_copy(runtime):
    deploy.initialize("sre-lab-api:v1")
    deploy.deploy("sre-lab-api:v2", observe_seconds=1)
    deploy.switch_upstream.reset_mock()
    deploy.start_slot.side_effect = RuntimeError("business returns 500")
    with pytest.raises(RuntimeError, match="500"):
        deploy.deploy("sre-lab-api:v3", observe_seconds=1)
    state = common.read_state()
    assert state["active"] == "green"
    assert state["previous"] is None
    assert state["last_outcome"] == "rejected_before_switch"
    assert state["slots"]["blue"]["version"] == "v3"
    deploy.switch_upstream.assert_not_called()


def test_observation_failure_restores_old_route_and_reports_failure(runtime):
    deploy.initialize("sre-lab-api:v1")
    deploy.switch_upstream.reset_mock()
    deploy.probe_url.side_effect = RuntimeError("new version broke")
    with pytest.raises(RuntimeError, match="previous version restored"):
        deploy.deploy("sre-lab-api:v2", observe_seconds=1)
    state = common.read_state()
    assert state["active"] == "blue" and state["phase"] == "idle"
    assert state["previous"] is None
    assert state["last_outcome"] == "rolled_back"
    assert [call.args[0] for call in deploy.switch_upstream.call_args_list] == ["green", "blue"]
    events = [
        json.loads(line) for line in (runtime / "runtime/events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["action"] == "release_rolled_back"
    assert not any(row["action"] == "release_completed" for row in events)


def test_rollback_verification_failure_stays_dirty_and_blocks_next_release(runtime):
    deploy.initialize("sre-lab-api:v1")
    deploy.probe_url.side_effect = RuntimeError("candidate broken")
    deploy.wait_slot.side_effect = RuntimeError("old service also broken")
    with pytest.raises(RuntimeError, match="rollback could not be verified"):
        deploy.deploy("sre-lab-api:v2", observe_seconds=1)
    assert common.read_state()["phase"] == "recovery_required"
    with pytest.raises(RuntimeError, match="Unfinished operation"):
        deploy.deploy("sre-lab-api:v3", observe_seconds=1)
    deploy.wait_slot.side_effect = None
    recovered = deploy.rollback()
    assert recovered["phase"] == "idle" and recovered["active"] == "blue"


def test_first_failure_never_claims_rollback(runtime):
    deploy.wait_edge.side_effect = RuntimeError("edge failed")
    with pytest.raises(RuntimeError, match="edge failed"):
        deploy.initialize("sre-lab-api:v1")
    assert common.read_state()["phase"] == "failed_initial"
    with pytest.raises(RuntimeError, match="No verified previous"):
        deploy.rollback()


def test_interrupted_operation_restores_committed_slot(runtime):
    initial = deploy.initialize("sre-lab-api:v1")
    dirty = copy.deepcopy(initial)
    dirty.update(phase="switching", stable_state=initial, release_id="interrupted")
    common.write_state(dirty)
    recovered = deploy.rollback()
    assert recovered["active"] == "blue" and recovered["phase"] == "idle"


def test_operation_lock_excludes_healer(runtime):
    with (
        common.operation_lock(),
        pytest.raises(BlockingIOError),
        common.operation_lock(blocking=False),
    ):
        pass


def test_bad_nginx_configuration_is_restored_before_error(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    deploy.write_upstream("blue")
    original = (tmp_path / "runtime/nginx/upstream.conf").read_text()
    runner = Mock(side_effect=[subprocess.CalledProcessError(1, "nginx -t"), Mock(), Mock()])
    monkeypatch.setattr(deploy, "compose", runner)
    with pytest.raises(subprocess.CalledProcessError):
        deploy.switch_upstream("green")
    assert (tmp_path / "runtime/nginx/upstream.conf").read_text() == original
    assert runner.call_count == 3


def test_degraded_candidate_is_rejected(monkeypatch):
    replies = iter([{"status": "ok"}, {"status": "degraded"}])
    monkeypatch.setattr(common, "get_json", lambda url: next(replies))
    with pytest.raises(RuntimeError, match="degraded"):
        common.probe_slot("blue", "v1")


def test_healer_can_probe_successful_degraded_business(monkeypatch):
    replies = iter([{"status": "ok"}, {"version": "v1"}, {"id": 1, "name": "Keyboard"}])
    monkeypatch.setattr(common, "get_json", lambda url: next(replies))
    assert common.probe_slot("blue", "v1", require_ready=False)["version"] == "v1"


def test_state_publishes_active_target_identity_and_readable_permissions(runtime):
    deploy.initialize("sre-lab-api:v1")
    path = runtime / "runtime/prometheus/active.json"
    assert json.loads(path.read_text()) == [
        {
            "targets": ["api-blue:8000"],
            "labels": {"slot": "blue", "version": "v1", "image_id": artifact("v1")["image_id"]},
        }
    ]
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.parent.stat().st_mode & 0o777 == 0o755
    deploy.deploy("sre-lab-api:v2", observe_seconds=1)
    assert json.loads(path.read_text())[0]["targets"] == ["api-green:8000"]
    deploy.rollback()
    assert json.loads(path.read_text())[0]["labels"]["version"] == "v1"
    common.write_state({"active": None})
    assert json.loads(path.read_text()) == []
