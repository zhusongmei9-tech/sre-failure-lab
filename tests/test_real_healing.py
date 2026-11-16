import json
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest

from ops import heal


def test_failure_threshold_and_later_verification():
    policy = heal.HealingPolicy()
    state = heal.fresh_service()
    for now in (0, 5):
        assert policy.observe(state, False, now, "failed")[1] is False
    assert policy.observe(state, False, 10, "failed") == ("healing_action_requested", True)
    assert state["phase"] == "verifying" and state["attempts"] == 1
    assert policy.observe(state, True, 15, "ok")[0] is None
    assert policy.observe(state, True, 20, "ok")[0] == "healing_recovery_verified"
    assert state["phase"] == "healthy"


def test_cooldown_survives_flapping_and_new_incident():
    policy = heal.HealingPolicy(failure_threshold=1)
    state = heal.fresh_service()
    assert policy.observe(state, False, 0, "failed")[1]
    policy.observe(state, True, 1, "ok")
    policy.observe(state, True, 2, "ok")
    assert policy.observe(state, False, 3, "failed")[1] is False
    assert state["phase"] == "cooldown"
    assert state["last_action_at"] == 0
    assert policy.observe(state, False, 30, "failed")[1]


def test_budget_lockout_survives_health_recovery():
    policy = heal.HealingPolicy(failure_threshold=1, cooldown=1, max_attempts=2)
    state = heal.fresh_service()
    assert policy.observe(state, False, 0, "failed")[1]
    assert policy.observe(state, False, 1, "failed")[1]
    assert policy.observe(state, False, 2, "failed")[0] == "healing_budget_exhausted"
    policy.observe(state, True, 3, "ok")
    policy.observe(state, True, 4, "ok")
    assert state["locked_out"] and state["phase"] == "locked_out"
    assert not policy.observe(state, False, 100, "failed")[1]


@pytest.fixture
def runner(tmp_path, monkeypatch):
    inside = [False]

    @contextmanager
    def lock(blocking=True):
        inside[0] = True
        try:
            yield
        finally:
            inside[0] = False

    def read_state():
        assert inside[0], "Release state must be read after acquiring operation lock"
        return {"active": "green", "phase": "idle", "slots": {"green": {"image_id": "new"}}}

    events = []
    monkeypatch.setattr(heal.common, "operation_lock", lock)
    monkeypatch.setattr(heal.common, "read_state", read_state)
    monkeypatch.setattr(
        heal.common, "event", lambda action, **details: events.append((action, details))
    )
    obj = heal.Runner(
        heal.HealingPolicy(failure_threshold=1), tmp_path / "healing.json", clock=lambda: 100
    )
    obj.recorded_events = events
    monkeypatch.setattr(obj, "probe_redis", lambda: (True, "ok"))
    monkeypatch.setattr(obj, "probe_api", lambda slot: (True, "ok"))
    return obj


def test_repairs_only_current_active_slot_and_reserves_before_action(runner, monkeypatch):
    monkeypatch.setattr(runner, "probe_api", lambda slot: (False, "failed"))
    calls = []

    def restart(target):
        persisted = json.loads(runner.state_path.read_text())
        assert persisted["services"]["api"]["attempts"] == 1
        assert persisted["services"]["api"]["phase"] == "verifying"
        calls.append(target)

    monkeypatch.setattr(runner, "restart", restart)
    assert runner.tick()["action_target"] == "api-green"
    assert calls == ["api-green"]
    assert runner.recorded_events[-1][1]["recovered"] is False


def test_dependency_is_repaired_before_api(runner, monkeypatch):
    monkeypatch.setattr(runner, "probe_redis", lambda: (False, "failed"))
    monkeypatch.setattr(runner, "probe_api", lambda slot: (False, "failed"))
    calls = []
    monkeypatch.setattr(runner, "restart", calls.append)
    assert runner.tick()["action_target"] == "redis"
    # Another tick is inside cooldown; API failures must not create API repair attempts.
    runner.tick()
    assert calls == ["redis"]
    state = runner.load()["services"]["api"]
    assert state["phase"] == "blocked_by_dependency" and state["attempts"] == 0


def test_dead_api_can_recover_even_when_redis_is_locked_out(runner, monkeypatch):
    data = runner.load()
    data["services"]["redis"].update(locked_out=True, attempts=2)
    runner.save(data)
    monkeypatch.setattr(runner, "probe_redis", lambda: (False, "redis_ping_failed"))
    monkeypatch.setattr(runner, "probe_api", lambda slot: (False, "api_liveness_failed"))
    calls = []
    monkeypatch.setattr(runner, "restart", calls.append)
    assert runner.tick()["action_target"] == "api-green"
    assert calls == ["api-green"]


def test_deployment_lock_contention_does_not_probe(runner, monkeypatch):
    @contextmanager
    def unavailable(blocking=True):
        raise BlockingIOError()
        yield

    monkeypatch.setattr(heal.common, "operation_lock", unavailable)
    monkeypatch.setattr(runner, "probe_redis", lambda: pytest.fail("must not probe"))
    assert runner.tick() == {"skipped": "operation_in_progress"}


@pytest.mark.parametrize("phase", ["observing", "recovery_required", "preparing"])
def test_dirty_release_allows_dependency_repair_but_never_api(runner, monkeypatch, phase):
    monkeypatch.setattr(heal.common, "read_state", lambda: {"active": "blue", "phase": phase})
    monkeypatch.setattr(runner, "probe_redis", lambda: (False, "redis_ping_failed"))
    monkeypatch.setattr(
        runner, "probe_api", lambda _: pytest.fail("must not probe an uncertain slot")
    )
    calls = []
    monkeypatch.setattr(runner, "restart", calls.append)
    result = runner.tick()
    assert result["action_target"] == "redis"
    assert calls == ["redis"]
    assert result["state"]["api_actions_suspended"] is True
    assert result["state"]["api_identity"] is None


def test_dependency_recovery_does_not_reconcile_release_or_repair_api(runner, monkeypatch):
    release = {"active": "blue", "phase": "recovery_required"}
    monkeypatch.setattr(heal.common, "read_state", lambda: dict(release))
    monkeypatch.setattr(runner, "probe_redis", lambda: (False, "redis_ping_failed"))
    monkeypatch.setattr(runner, "probe_api", lambda _: pytest.fail("must not probe uncertain API"))
    calls = []
    monkeypatch.setattr(runner, "restart", calls.append)
    runner.tick()
    monkeypatch.setattr(runner, "probe_redis", lambda: (True, "ok"))
    runner.tick()
    state = runner.tick()["state"]
    assert state["services"]["redis"]["recovery_observed"] is True
    assert state["api_actions_suspended"] is True
    assert release["phase"] == "recovery_required"
    assert calls == ["redis"]


def test_absent_release_does_not_start_dependency_repair(runner, monkeypatch):
    monkeypatch.setattr(heal.common, "read_state", dict)
    monkeypatch.setattr(
        runner, "probe_redis", lambda: pytest.fail("must not probe uninitialized stack")
    )
    assert runner.tick() == {"skipped": "deployment_not_initialized"}


def test_cooldown_and_budget_survive_daemon_recreation(runner, monkeypatch):
    monkeypatch.setattr(runner, "probe_api", lambda slot: (False, "failed"))
    monkeypatch.setattr(runner, "restart", lambda target: None)
    runner.tick()
    successor = heal.Runner(runner.policy, runner.state_path, clock=lambda: 101)
    monkeypatch.setattr(successor, "probe_redis", lambda: (True, "ok"))
    monkeypatch.setattr(successor, "probe_api", lambda slot: (False, "failed"))
    monkeypatch.setattr(successor, "restart", lambda target: pytest.fail("cooldown was lost"))
    successor.tick()
    assert successor.load()["services"]["api"]["phase"] == "cooldown"
    assert successor.load()["services"]["api"]["attempts"] == 1


def test_action_failure_consumes_budget(runner, monkeypatch):
    monkeypatch.setattr(runner, "probe_api", lambda slot: (False, "failed"))

    def fail(target):
        raise subprocess.TimeoutExpired("docker", 20)

    monkeypatch.setattr(runner, "restart", fail)
    runner.tick()
    assert runner.load()["services"]["api"]["attempts"] == 1
    assert runner.recorded_events[-1][0] == "healing_action_failed"


def test_corrupt_persistent_state_fails_closed(runner, monkeypatch):
    runner.state_path.write_text('{"schema_version": 1, "services": {}}')
    monkeypatch.setattr(runner, "restart", lambda _: pytest.fail("must not act"))
    with pytest.raises(RuntimeError, match="Invalid healing state"):
        runner.tick()


def test_operator_reset_retains_cooldown(runner):
    data = runner.load()
    data["services"]["api"].update(last_action_at=99, attempts=2, locked_out=True)
    runner.save(data)
    runner.reset("api")
    state = runner.load()["services"]["api"]
    assert state["last_action_at"] == 99
    assert state["attempts"] == 0 and not state["locked_out"]


def test_healthy_probes_are_actual_recovery_evidence(runner, monkeypatch):
    monkeypatch.setattr(runner, "probe_api", lambda slot: (False, "failed"))
    monkeypatch.setattr(runner, "restart", lambda target: None)
    runner.tick()
    assert not runner.load()["services"]["api"]["recovery_observed"]
    monkeypatch.setattr(runner, "probe_api", lambda slot: (True, "ok"))
    runner.tick()
    assert not runner.load()["services"]["api"]["recovery_observed"]
    runner.tick()
    assert runner.load()["services"]["api"]["recovery_observed"]
    recovery = [
        details for name, details in runner.recorded_events if name == "healing_recovery_verified"
    ]
    assert recovery[-1]["attempts"] == 1


def test_restart_allowlist_and_actual_compose_command(monkeypatch):
    calls = []
    monkeypatch.setattr(heal.common, "compose", lambda *args, **kw: calls.append((args, kw)))
    heal.Runner.restart("api-blue")
    assert calls[0][0] == ("restart", "api-blue")
    assert calls[0][1]["timeout"] == 20
    with pytest.raises(ValueError):
        heal.Runner.restart("other-service")


def test_probe_redis_handles_stopped_or_hung_container(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker", 3)

    monkeypatch.setattr(heal.common, "compose", timeout)
    assert heal.Runner.probe_redis() == (False, "redis_ping_failed")


@pytest.mark.parametrize(
    ("live_code", "product_code", "expected"),
    [
        (200, 429, "api_overloaded"),
        (200, 500, "api_business_failed"),
        (429, 200, "api_liveness_failed"),
        (500, 200, "api_liveness_failed"),
    ],
)
def test_real_http_probe_distinguishes_overload_from_failure(
    monkeypatch, live_code, product_code, expected
):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            is_live = self.path == "/health/live"
            body = json.dumps({"status": "ok"} if is_live else {"id": 1}).encode()
            self.send_response(live_code if is_live else product_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(heal.common, "slot_url", lambda _: f"http://127.0.0.1:{server.server_port}")
    try:
        assert heal.Runner.probe_api("green") == (False, expected)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_business_timeout_is_not_misclassified_as_overload(monkeypatch):
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.read.return_value = b'{"status":"ok"}'

    def request(url, timeout):
        if url.endswith("/products/1"):
            raise TimeoutError("probe timed out")
        return response

    monkeypatch.setattr(heal, "urlopen", request)
    assert heal.Runner.probe_api("green") == (False, "api_business_failed")


def test_repeated_overload_preserves_budget_then_real_failure_can_restart(runner, monkeypatch):
    runner.policy = heal.HealingPolicy(failure_threshold=3)
    data = runner.load()
    data["api_identity"] = "green:new"
    data["services"]["api"].update(
        attempts=1,
        last_action_at=50,
        consecutive_failures=2,
        incident_started_at=40,
        incident_id="existing-incident",
    )
    runner.save(data)
    monkeypatch.setattr(runner, "probe_api", lambda _: (False, "api_overloaded"))
    calls = []
    monkeypatch.setattr(runner, "restart", calls.append)
    for _ in range(10):
        runner.tick()
    state = runner.load()["services"]["api"]
    assert calls == []
    assert state["healthy"] is False and state["phase"] == "overloaded"
    assert state["consecutive_failures"] == 0 and state["consecutive_successes"] == 0
    assert state["attempts"] == 1 and state["last_action_at"] == 50
    assert state["locked_out"] is False
    monkeypatch.setattr(runner, "probe_api", lambda _: (False, "api_liveness_failed"))
    runner.tick()
    runner.tick()
    assert calls == []
    assert runner.tick()["action_target"] == "api-green"
    assert calls == ["api-green"]
    assert runner.load()["services"]["api"]["attempts"] == 2


def test_overload_does_not_rearm_locked_service_or_clear_cooldown():
    policy = heal.HealingPolicy()
    state = heal.fresh_service(last_action_at=100)
    state.update(locked_out=True, attempts=2, consecutive_failures=3)
    assert policy.observe(state, False, 101, "api_overloaded", overloaded=True) == (None, False)
    assert state["locked_out"] is True and state["phase"] == "locked_out"
    assert state["healthy"] is False and state["last_probe"]["reason"] == "api_overloaded"
    assert state["attempts"] == 2 and state["last_action_at"] == 100
    assert state["consecutive_failures"] == 0
    assert policy.observe(state, False, 200, "api_liveness_failed")[1] is False
