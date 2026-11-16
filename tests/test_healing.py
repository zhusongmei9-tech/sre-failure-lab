import json

import pytest

from app.healing import HealingController


def actions(controller):
    return [e for e in controller.events if e["action"] == "healing_action_simulated"]


def state(controller, service="api"):
    return controller.status()["services"][service]


def test_default_is_disarmed_simulation_only():
    controller = HealingController()
    assert state(controller)["phase"] == "unknown"
    for _ in range(10):
        controller.observe("api", False)
    assert state(controller)["phase"] == "disabled"
    assert actions(controller) == []
    result = controller.status()
    assert result["mode"] == "simulation"
    assert result["real_actions_supported"] is False
    assert result["persistent"] is False
    json.dumps(result)


def test_continuous_failures_are_required_and_acceptance_is_not_recovery():
    controller = HealingController(enabled=True)
    controller.observe("api", False)
    controller.observe("api", False)
    assert state(controller)["phase"] == "suspect"
    controller.observe("api", True)
    controller.observe("api", False)
    assert actions(controller) == []
    controller.observe("api", False)
    controller.observe("api", False)
    assert len(actions(controller)) == 1
    assert actions(controller)[0]["accepted"] is True
    assert actions(controller)[0]["recovered"] is False
    assert state(controller)["phase"] == "verifying"
    controller.observe("api", True)
    assert state(controller)["phase"] == "verifying"
    controller.observe("api", True)
    assert state(controller)["phase"] == "healthy"
    assert state(controller)["attempts"] == 0


def test_cooldown_then_exhaustion_stays_locked_until_operator_reset():
    now = [100.0]
    controller = HealingController(enabled=True, failure_threshold=1, clock=lambda: now[0])
    controller.observe("redis", False)
    now[0] += 29
    controller.observe("redis", False)
    assert state(controller, "redis")["phase"] == "cooldown"
    assert state(controller, "redis")["cooldown_remaining_seconds"] == 1
    assert len(actions(controller)) == 1
    now[0] += 1
    controller.observe("redis", False)
    assert len(actions(controller)) == 2
    controller.observe("redis", False)
    assert state(controller, "redis")["locked_out"] is True
    for _ in range(50):
        now[0] += 100
        controller.observe("redis", False)
    assert len(actions(controller)) == 2
    controller.observe("redis", True)
    controller.observe("redis", True)
    assert state(controller, "redis")["healthy"] is True
    assert state(controller, "redis")["phase"] == "locked_out"
    assert state(controller, "redis")["attempts"] == 2
    controller.reset("redis")
    assert state(controller, "redis")["phase"] == "unknown"
    controller.observe("redis", False)
    assert len(actions(controller)) == 3


def test_services_are_isolated_and_enable_does_not_execute_immediately():
    controller = HealingController(failure_threshold=1)
    controller.observe("api", False)
    controller.set_enabled(True)
    assert actions(controller) == []
    controller.observe("redis", False)
    assert len(actions(controller)) == 1
    assert state(controller, "api")["attempts"] == 0
    assert state(controller, "redis")["attempts"] == 1
    controller.set_enabled(False)
    controller.observe("api", False)
    assert len(actions(controller)) == 1


def test_recovery_time_and_callback_are_explicitly_simulated():
    now = [0.0]
    callbacks = []
    controller = HealingController(
        enabled=True, failure_threshold=1, clock=lambda: now[0],
        on_event=lambda action, details: callbacks.append((action, details)),
    )
    controller.observe("api", False)
    now[0] = 5
    controller.observe("api", True)
    now[0] = 6
    controller.observe("api", True)
    recovered = [d for action, d in callbacks if action == "healing_recovery_verified"]
    assert len(recovered) == 1
    assert recovered[0]["elapsed_seconds"] == 6
    assert recovered[0]["simulation"] is True


@pytest.mark.parametrize("service", ["db", "API", "api;rm", "", None, []])
def test_unknown_service_is_rejected(service):
    controller = HealingController()
    with pytest.raises(ValueError):
        controller.observe(service, False)
    with pytest.raises(ValueError):
        controller.reset(service)
    assert not controller.events


@pytest.mark.parametrize("healthy", [0, 1, "true", "false", None])
def test_non_boolean_probe_is_rejected(healthy):
    with pytest.raises(ValueError):
        HealingController().observe("api", healthy)


@pytest.mark.parametrize("kwargs", [
    {"enabled": 1}, {"max_attempts": 0}, {"max_attempts": 21},
    {"failure_threshold": 1.5}, {"success_threshold": True},
    {"cooldown_seconds": 0}, {"cooldown_seconds": float("nan")},
    {"cooldown_seconds": float("inf")}, {"cooldown_seconds": "30"},
])
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        HealingController(**kwargs)


def test_event_history_is_bounded_and_new_healthy_incident_gets_a_new_budget():
    controller = HealingController(enabled=True, failure_threshold=1)
    controller.observe("api", False)
    controller.observe("api", True)
    controller.observe("api", True)
    controller.observe("api", False)
    assert state(controller)["attempts"] == 1
    for _ in range(250):
        controller.observe("redis", True)
    assert len(controller.events) == 200
    assert controller.events[-1]["sequence"] > 200


def test_status_is_a_detached_snapshot():
    controller = HealingController()
    snapshot = controller.status()
    snapshot["services"]["api"]["attempts"] = 100
    assert state(controller)["attempts"] == 0
