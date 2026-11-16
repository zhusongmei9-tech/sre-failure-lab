import copy
import json
import subprocess
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from ops import common, deploy, stack


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    state = {
        "active": "green",
        "previous": "blue",
        "phase": "idle",
        "release_id": "saved-release",
        "last_outcome": "completed",
        "persistent": True,
        "simulation": False,
        "slots": {
            slot: {
                "image": "sre-lab-api:" + version,
                "version": version,
                "image_id": "sha256:" + digit * 64,
            }
            for slot, version, digit in (("blue", "0.3.0", "1"), ("green", "0.3.1", "2"))
        },
    }
    common.write_state(state)
    (tmp_path / "runtime/healing.json").write_text('{"attempts": 2}')
    (tmp_path / "runtime/fault-test.json").write_text('{"error_rate": 0.2}')
    calls = []
    running = set()

    def compose(*args, **kwargs):
        calls.append(("compose", args))
        assert args[:2] == ("--profile", "slots")
        assert 0 < kwargs["timeout"] <= 90
        if args[2] == "up":
            assert args[3:8] == ("-d", "--no-deps", "--pull", "never", "--no-build")
            running.update(args[8:])
        if args[2] == "stop":
            running.clear()
        output = "container-" + args[-1] if args[2] == "ps" else ""
        return subprocess.CompletedProcess(args, 0, stdout=output)

    def run(args, **kwargs):
        calls.append(("docker", tuple(args)))
        assert kwargs["timeout"] <= 15
        if args[1:3] == ["image", "inspect"]:
            output = args[-1]
        elif args[1] == "inspect":
            slot = args[-1].rsplit("-", 1)[-1]
            output = state["slots"][slot]["image_id"]
        else:
            assert args[1] == "ps"
            assert "label=com.docker.compose.project=sre-lab" in args
            output = "\n".join(service + "\trunning" for service in sorted(running))
        return subprocess.CompletedProcess(args, 0, stdout=output)

    def http(url, deadline, json_body=True, expected_slot=None):
        calls.append(("http", url, expected_slot))
        if url.endswith("/-/ready"):
            return None
        if url.endswith("/api/health"):
            return {"database": "ok"}
        if url.endswith("/health/live"):
            return {"status": "ok"}
        if url.endswith("/health/ready"):
            return {"status": "ready"}
        if url.endswith("/version"):
            return {"version": "0.3.1"}
        return {"id": 1, "name": "Keyboard"}

    monkeypatch.setattr(common, "compose", compose)
    monkeypatch.setattr(stack.subprocess, "run", run)
    monkeypatch.setattr(stack, "_http", http)
    clock = [0.0]
    monkeypatch.setattr(stack.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        stack.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    return tmp_path, state, calls, running, clock


def events(root):
    return [json.loads(line) for line in (root / "runtime/events.jsonl").read_text().splitlines()]


def test_start_restores_saved_route_in_order_and_preserves_release_and_budget(lab):
    root, state, calls, running, _ = lab
    original = (root / "runtime/state.json").read_bytes()
    deploy.write_upstream("blue")
    result = stack.start()
    assert result == {
        "active": "green",
        "previous": "blue",
        "version": "0.3.1",
        "release_id": "saved-release",
        "phase": "idle",
        "running_containers": 8,
    }
    assert len(running) == 8
    assert (root / "runtime/state.json").read_bytes() == original
    assert (root / "runtime/healing.json").read_text() == '{"attempts": 2}'
    assert (root / "runtime/fault-test.json").read_text() == '{"error_rate": 0.2}'
    assert "server api-green:8000" in (root / "runtime/nginx/upstream.conf").read_text()
    assert json.loads((root / "runtime/prometheus/active.json").read_text())[0]["labels"] == {
        "slot": "green",
        "version": "0.3.1",
        "image_id": state["slots"]["green"]["image_id"],
    }
    assert (root / "runtime/images.env").read_text() == (
        "BLUE_IMAGE="
        + state["slots"]["blue"]["image_id"]
        + "\nGREEN_IMAGE="
        + state["slots"]["green"]["image_id"]
        + "\n"
    )
    up = [args[8:] for kind, args, *rest in calls if kind == "compose" and args[2] == "up"]
    assert up == [("redis",), ("api-blue", "api-green"), ("edge",), stack.MONITORING]
    active_check = next(i for i, row in enumerate(calls) if row[0] == "http")
    nginx_test = next(
        i for i, row in enumerate(calls) if row[0] == "compose" and row[1][-1] == "-t"
    )
    nginx_reload = next(
        i for i, row in enumerate(calls) if row[0] == "compose" and row[1][-1] == "reload"
    )
    assert active_check < nginx_test < nginx_reload
    assert all(row[2] == "green" for row in calls if row[0] == "http" and ":18000/" in row[1])
    assert events(root)[-1]["action"] == "stack_started"


@pytest.mark.parametrize("phase", ["switching", "observing", "recovery_required", "initializing"])
def test_nonidle_start_refuses_without_mutation_or_container_cleanup(lab, phase):
    root, state, calls, running, _ = lab
    state["phase"] = phase
    common.write_state(state)
    running.add("api-green")
    before = (root / "runtime/state.json").read_bytes()
    with pytest.raises(RuntimeError, match="operator recovery"):
        stack.start()
    assert calls == [] and running == {"api-green"}
    assert (root / "runtime/state.json").read_bytes() == before
    assert events(root)[-1]["details"]["cleanup"] == "not_needed"


def test_missing_local_image_never_pulls_or_stops_existing_project(lab, monkeypatch):
    root, state, calls, running, _ = lab
    running.add("api-green")
    monkeypatch.setattr(
        stack, "_run", Mock(side_effect=subprocess.CalledProcessError(1, "inspect"))
    )
    with pytest.raises(subprocess.CalledProcessError):
        stack.start()
    assert not calls and running == {"api-green"}
    assert common.read_state() == state
    assert events(root)[-1]["details"]["stage"] == "validate_images"


def test_failed_start_stops_project_and_keeps_release_and_data(lab, monkeypatch):
    root, state, calls, running, clock = lab
    monkeypatch.setattr(stack, "START_SECONDS", 2)
    monkeypatch.setattr(stack, "_probe_api", Mock(side_effect=RuntimeError("injected error")))
    with pytest.raises(RuntimeError, match="Active API"):
        stack.start()
    assert clock[0] == 2 and running == set()
    assert common.read_state() == state
    assert (root / "runtime/healing.json").exists()
    assert ("compose", ("--profile", "slots", "stop", "--timeout", "20")) in calls
    assert events(root)[-1]["details"]["cleanup"] == "stopped"
    assert not any(event["action"] == "stack_started" for event in events(root))


def test_wrong_container_image_triggers_cleanup(lab, monkeypatch):
    root, _, _, running, _ = lab
    original = stack._run

    def mismatch(args, deadline, maximum=15):
        return "sha256:" + "f" * 64 if args[1] == "inspect" else original(args, deadline, maximum)

    monkeypatch.setattr(stack, "_run", mismatch)
    with pytest.raises(RuntimeError, match="Running image differs"):
        stack.start()
    assert not running
    assert events(root)[-1]["details"]["cleanup"] == "stopped"


def test_monitoring_health_failure_cleans_up(lab, monkeypatch):
    root, state, _, running, _ = lab
    original = stack._http
    monkeypatch.setattr(stack, "START_SECONDS", 1)

    def unhealthy(url, *args, **kwargs):
        return (
            {"database": "failed"}
            if url.endswith("/api/health")
            else original(url, *args, **kwargs)
        )

    monkeypatch.setattr(stack, "_http", unhealthy)
    with pytest.raises(RuntimeError, match="Stack health verification"):
        stack.start()
    assert not running and common.read_state() == state
    assert events(root)[-1]["details"]["stage"] == "verify_stack"


def test_stop_preserves_dirty_state_volumes_and_budget(lab):
    root, state, calls, running, _ = lab
    state["phase"] = "recovery_required"
    common.write_state(state)
    before = (root / "runtime/state.json").read_bytes()
    running.update(stack.INFRASTRUCTURE)
    result = stack.stop()
    assert result["data_preserved"] and not running
    assert (root / "runtime/state.json").read_bytes() == before
    assert (root / "runtime/healing.json").read_text() == '{"attempts": 2}'
    mutations = [args for kind, args in calls if kind == "compose"]
    assert mutations == [("--profile", "slots", "stop", "--timeout", "20")]
    assert events(root)[-1]["action"] == "stack_stopped"


def test_stop_reports_remaining_project_container(lab, monkeypatch):
    root, _, _, _, _ = lab
    monkeypatch.setattr(stack, "_running_containers", lambda deadline: [["orphan", "running"]])
    with pytest.raises(RuntimeError, match="remain running"):
        stack.stop()
    assert events(root)[-1]["action"] == "stack_failed"


def test_lock_wait_is_bounded_and_never_stops_another_operation(lab, monkeypatch):
    root, _, calls, _, clock = lab

    @contextmanager
    def busy(blocking=True):
        assert not blocking
        raise BlockingIOError()
        yield

    monkeypatch.setattr(common, "operation_lock", busy)
    with pytest.raises(RuntimeError, match="holds the lock"):
        stack.start()
    assert clock[0] == 30 and calls == []
    assert events(root)[-1]["details"]["stage"] == "lock"


def test_stack_holds_real_shared_lock_for_mutations(lab, monkeypatch):
    original = stack._up

    def check_lock(deadline, *services):
        with pytest.raises(BlockingIOError), common.operation_lock(blocking=False):
            pass
        original(deadline, *services)

    monkeypatch.setattr(stack, "_up", check_lock)
    stack.start()


def test_running_count_rejects_duplicates_or_orphans(lab, monkeypatch):
    _, state, _, _, _ = lab
    rows = [[name, "running"] for name in stack.INFRASTRUCTURE + ("api-blue", "api-green")]
    monkeypatch.setattr(stack, "_running_containers", lambda deadline: copy.deepcopy(rows))
    assert stack._assert_running(state, 90) == 8
    rows.append(["api-green", "running"])
    with pytest.raises(RuntimeError, match="do not match"):
        stack._assert_running(state, 90)


def test_stop_uninitialized_without_env_is_idempotent(lab, monkeypatch):
    root, _, _, _, _ = lab
    (root / "runtime/state.json").unlink()
    monkeypatch.setattr(
        common, "compose", Mock(side_effect=subprocess.CalledProcessError(1, "compose"))
    )
    assert stack.stop() == {"running_containers": 0, "data_preserved": True}
    assert not (root / "runtime/state.json").exists()
    assert stack.stop()["running_containers"] == 0


def test_status_reports_missing_stopped_and_health_without_mutation(lab, monkeypatch):
    root, _, _, _, _ = lab
    before = {path: path.read_bytes() for path in (root / "runtime").rglob("*") if path.is_file()}
    commands = []

    def run(args, deadline, maximum=15):
        commands.append(args)
        if args[1] == "ps":
            assert "-a" in args
            return "id1\tapi-green\trunning\nid2\tapi-blue\texited\nid3\tredis\trunning"
        assert args[1:3] == ["inspect", "--format"]
        assert ".State.Health" in args[3] and ".Config" not in args[3]
        return "healthy" if args[-1] != "id2" else "unhealthy"

    monkeypatch.setattr(stack, "_run", run)
    result = stack.status()
    assert result["active"] == "green" and result["version"] == "0.3.1"
    assert result["phase"] == "idle" and result["running_containers"] == 2
    assert result["not_running"] == ["api-blue"]
    assert result["missing_services"] == sorted(set(stack.INFRASTRUCTURE) - {"redis"})
    assert result["containers"][0]["health"] == "healthy"
    assert not result["all_running"] and len(commands) == 4
    assert {path: path.read_bytes() for path in before} == before
    assert not (root / "runtime/events.jsonl").exists()


def test_cleanup_failure_is_recorded_and_original_failure_propagates(lab, monkeypatch):
    root, _, _, _, _ = lab
    monkeypatch.setattr(
        stack, "_assert_runtime_images", Mock(side_effect=RuntimeError("wrong image"))
    )
    monkeypatch.setattr(stack, "_stop_project", Mock(side_effect=RuntimeError("could not stop")))
    with pytest.raises(RuntimeError, match="wrong image"):
        stack.start()
    assert events(root)[-1]["details"]["cleanup"] == "failed"


def test_cli_never_prints_subprocess_output_or_env(lab, monkeypatch, capsys):
    monkeypatch.setattr(
        stack,
        "start",
        Mock(
            side_effect=subprocess.CalledProcessError(
                1, "compose", output="LAB_TOKEN=private-token", stderr="PASSWORD=secret"
            )
        ),
    )
    monkeypatch.setattr("sys.argv", ["ops.stack", "start"])
    with pytest.raises(SystemExit) as exc:
        stack.main()
    assert exc.value.code == 1
    output = capsys.readouterr().err
    assert (
        "CalledProcessError" in output and "private-token" not in output and "secret" not in output
    )
