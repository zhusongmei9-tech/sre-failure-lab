import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.deployment import DeploymentController


@pytest.fixture
def controller():
    now = [100.0]
    controller = DeploymentController(clock=lambda: now[0], observation_seconds=10)
    return controller, now


def release(controller, now, version):
    controller.begin(version)
    controller.check_candidate(
        ready=True, smoke_ok=True, release_id=controller.status()["release_id"]
    )
    now[0] += 10
    return controller.observe(healthy=True, release_id=controller.status()["release_id"])


def test_initial_state_has_no_fictional_deployment(controller):
    ctl, _ = controller
    status = ctl.status()
    assert status["mode"] == "simulation"
    assert status["real_actions_enabled"] is False
    assert status["active"] is None
    assert status["last_outcome"] == "not_deployed"
    json.dumps(status, allow_nan=False)


@pytest.mark.parametrize("ready,smoke_ok", [(False, False), (False, True), (True, False)])
def test_failed_candidate_never_switches(controller, ready, smoke_ok):
    ctl, now = controller
    release(ctl, now, "v1")
    before = ctl.status()["active"]
    ctl.begin("v3-bad")
    assert ctl.status()["active"] == before
    result = ctl.check_candidate(
        ready=ready, smoke_ok=smoke_ok, release_id=ctl.status()["release_id"]
    )
    assert result["active"] == before
    assert result["last_outcome"] == "rejected_before_switch"
    assert not result["locked"]
    assert not any(e["action"] == "deployment_rolled_back" for e in result["events"])


def test_first_deployment_failure_is_not_rollback(controller):
    ctl, _ = controller
    ctl.begin("v3-bad")
    ctl.check_candidate(ready=True, smoke_ok=True, release_id=ctl.status()["release_id"])
    result = ctl.observe(healthy=False, release_id=ctl.status()["release_id"])
    assert result["active"] is None
    assert result["previous"] is None
    assert result["last_outcome"] == "failed_no_previous"
    assert result["events"][0]["action"] == "deployment_failed_no_previous"
    with pytest.raises(ValueError, match="No previous"):
        ctl.rollback(release_id=ctl.status()["release_id"])


def test_observation_needs_health_evidence_not_only_time(controller):
    ctl, now = controller
    ctl.begin("v1")
    switched = ctl.check_candidate(ready=True, smoke_ok=True, release_id=ctl.status()["release_id"])
    assert switched["phase"] == "observing"
    assert ctl.observe(healthy=True, release_id=ctl.status()["release_id"])["phase"] == "observing"
    now[0] += 11
    assert ctl.status()["phase"] == "observing"
    assert ctl.status()["remaining_seconds"] == 0
    result = ctl.observe(healthy=True, release_id=ctl.status()["release_id"])
    assert result["phase"] == "idle"
    assert result["last_outcome"] == "completed"


def test_failed_observation_after_deadline_still_rolls_back(controller):
    ctl, now = controller
    release(ctl, now, "v1")
    ctl.begin("v2")
    ctl.check_candidate(ready=True, smoke_ok=True, release_id=ctl.status()["release_id"])
    now[0] += 100
    result = ctl.observe(healthy=False, release_id=ctl.status()["release_id"])
    assert result["active"] == {"slot": "blue", "version": "v1"}
    assert result["previous"] is None
    assert result["last_outcome"] == "rolled_back"


def test_previous_release_is_retained_for_manual_rollback(controller):
    ctl, now = controller
    release(ctl, now, "v1")
    result = release(ctl, now, "v2")
    assert result["active"] == {"slot": "green", "version": "v2"}
    assert result["previous"] == {"slot": "blue", "version": "v1"}
    result = ctl.rollback(release_id=ctl.status()["release_id"])
    assert result["active"]["version"] == "v1"
    assert result["previous"]["version"] == "v2"


def test_rollback_during_observation_discards_bad_candidate(controller):
    ctl, now = controller
    release(ctl, now, "v1")
    release(ctl, now, "v2")
    ctl.begin("v3-bad")
    ctl.check_candidate(ready=True, smoke_ok=True, release_id=ctl.status()["release_id"])
    result = ctl.rollback(release_id=ctl.status()["release_id"])
    assert result["active"]["version"] == "v2"
    assert result["previous"] is None  # v1's blue slot was overwritten by v3-bad.
    with pytest.raises(ValueError, match="No previous"):
        ctl.rollback(release_id=ctl.status()["release_id"])


@pytest.mark.parametrize("cancel", [True, False])
def test_candidate_reuse_invalidates_old_slot_even_if_canceled_or_rejected(controller, cancel):
    ctl, now = controller
    release(ctl, now, "v1")
    release(ctl, now, "v2")
    result = ctl.begin("v3-bad")
    assert result["active"] == {"slot": "green", "version": "v2"}
    assert result["candidate"] == {"slot": "blue", "version": "v3-bad"}
    assert result["previous"] is None
    assert any(e["action"] == "deployment_previous_replaced" for e in result["events"])
    with pytest.raises(ValueError, match="Cancel"):
        ctl.rollback(release_id=ctl.status()["release_id"])
    if cancel:
        ctl.cancel(release_id=ctl.status()["release_id"])
    else:
        ctl.check_candidate(ready=False, smoke_ok=False, release_id=ctl.status()["release_id"])
    assert ctl.status()["active"]["version"] == "v2"
    assert ctl.status()["previous"] is None
    with pytest.raises(ValueError, match="No previous"):
        ctl.rollback(release_id=ctl.status()["release_id"])


def test_automatic_rollback_does_not_resurrect_replaced_slot(controller):
    ctl, now = controller
    release(ctl, now, "v1")
    release(ctl, now, "v2")
    ctl.begin("v3-bad")
    switched = ctl.check_candidate(ready=True, smoke_ok=True, release_id=ctl.status()["release_id"])
    assert switched["active"]["version"] == "v3-bad"
    assert switched["previous"]["version"] == "v2"
    result = ctl.observe(healthy=False, release_id=ctl.status()["release_id"])
    assert result["active"] == {"slot": "green", "version": "v2"}
    assert result["previous"] is None
    assert result["last_outcome"] == "rolled_back"


def test_release_lock_spans_validation_and_observation(controller):
    ctl, now = controller
    ctl.begin("v1")
    with pytest.raises(ValueError, match="Another release"):
        ctl.begin("v2")
    ctl.check_candidate(ready=True, smoke_ok=True, release_id=ctl.status()["release_id"])
    with pytest.raises(ValueError, match="Another release"):
        ctl.begin("v2")
    with pytest.raises(ValueError, match="Only a validating"):
        ctl.cancel(release_id=ctl.status()["release_id"])
    now[0] += 10
    ctl.observe(healthy=True, release_id=ctl.status()["release_id"])
    with pytest.raises(ValueError, match="already active"):
        ctl.begin("v1")
    ctl.begin("v2")


def test_concurrent_release_admission_is_serialized(controller):
    ctl, _ = controller

    def begin(version):
        try:
            ctl.begin(version)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(begin, ["v1", "v2", "v3", "v4"]))
    assert sum(outcomes) == 1


@pytest.mark.parametrize("version", ["", "../v1", "v1;rm", "v1\n", "a" * 65, 123])
def test_invalid_versions_do_not_mutate(controller, version):
    ctl, _ = controller
    with pytest.raises(ValueError, match="version must"):
        ctl.begin(version)
    assert ctl.status()["events"] == []


def test_probe_types_and_wrong_stage_are_rejected(controller):
    ctl, _ = controller
    with pytest.raises(ValueError):
        ctl.check_candidate(True, True, release_id=ctl.status()["release_id"])
    with pytest.raises(ValueError):
        ctl.observe(True, release_id=ctl.status()["release_id"])
    ctl.begin("v1")
    with pytest.raises(ValueError, match="boolean"):
        ctl.check_candidate("true", True, release_id=ctl.status()["release_id"])
    assert ctl.status()["phase"] == "validating"


def test_events_are_bounded_unique_and_copies_are_isolated(controller):
    ctl, _ = controller
    for _ in range(120):
        ctl.begin("v1")
        ctl.cancel(release_id=ctl.status()["release_id"])
    result = ctl.status()
    assert len(result["events"]) == 200
    assert len({e["event_id"] for e in result["events"]}) == 200
    result["events"][0]["action"] = "tampered"
    assert ctl.status()["events"][0]["action"] == "deployment_canceled"


@pytest.mark.parametrize("seconds", [0, -1, 3601, float("nan"), float("inf"), True, False, "30"])
def test_invalid_observation_configuration(seconds):
    with pytest.raises(ValueError):
        DeploymentController(observation_seconds=seconds)


def test_canceled_release_evidence_cannot_promote_or_cancel_new_candidate(controller):
    ctl, _ = controller
    old_id = ctl.begin("v1")["release_id"]
    ctl.cancel(release_id=old_id)
    new_id = ctl.begin("v2-unchecked")["release_id"]
    before = ctl.status()
    for action in (
        lambda: ctl.check_candidate(True, True, release_id=old_id),
        lambda: ctl.cancel(release_id=old_id),
        lambda: ctl.rollback(release_id=old_id),
    ):
        with pytest.raises(ValueError, match="release_id"):
            action()
        assert ctl.status() == before
    assert ctl.check_candidate(True, True, release_id=new_id)["phase"] == "observing"


def test_previous_release_observation_cannot_rollback_current_release(controller):
    ctl, now = controller
    old_id = release(ctl, now, "v1")["release_id"]
    new_id = ctl.begin("v2")["release_id"]
    ctl.check_candidate(True, True, release_id=new_id)
    before = ctl.status()
    for action in (
        lambda: ctl.observe(False, release_id=old_id),
        lambda: ctl.rollback(release_id=old_id),
    ):
        with pytest.raises(ValueError, match="release_id"):
            action()
        assert ctl.status() == before
    assert ctl.observe(False, release_id=new_id)["active"]["version"] == "v1"


def test_release_guard_runs_after_acquiring_state_lock(controller):
    ctl, _ = controller
    old_id = ctl.begin("v1")["release_id"]
    queued = Event()

    def delayed_result():
        queued.set()
        return ctl.check_candidate(True, True, release_id=old_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with ctl._lock:
            future = pool.submit(delayed_result)
            assert queued.wait(timeout=1)
            ctl.cancel(release_id=old_id)
            ctl.begin("v2")
            before = ctl.status()
        with pytest.raises(ValueError, match="release_id"):
            future.result(timeout=1)
    assert ctl.status() == before


@pytest.mark.parametrize("identifier", [None, "", False, 1, [], "0" * 32])
def test_invalid_release_id_never_changes_state(controller, identifier):
    ctl, _ = controller
    ctl.begin("v1")
    before = ctl.status()
    with pytest.raises(ValueError, match="release_id"):
        ctl.check_candidate(True, True, release_id=identifier)
    assert ctl.status() == before


def test_release_id_is_required_by_python_api(controller):
    ctl, _ = controller
    ctl.begin("v1")
    for action in (
        lambda: ctl.check_candidate(True, True),
        lambda: ctl.observe(True),
        ctl.cancel,
        ctl.rollback,
    ):
        with pytest.raises(TypeError, match="release_id"):
            action()
