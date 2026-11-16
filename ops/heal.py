"""Real, bounded host recovery for the local Docker Compose lab.

Run explicitly with ``python3 -m ops.heal run``. This module never accepts
user-supplied shell commands, service names or probe URLs.
"""

import argparse
import json
import math
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from ops import common


def timestamp():
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017 -- host Python 3.9


def fresh_service(last_action_at=None):
    return {
        "phase": "unknown",
        "healthy": None,
        "consecutive_failures": 0,
        "consecutive_successes": 0,
        "attempts": 0,
        "locked_out": False,
        "last_action_at": last_action_at,
        "incident_started_at": None,
        "incident_id": None,
        "recovery_observed": False,
        "last_probe": None,
    }


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class HealingPolicy:
    """Persistable decision policy; the Runner owns probes and real actions."""

    def __init__(self, failure_threshold=3, cooldown=30, max_attempts=2, successes=2):
        for name, number in (
            ("failure_threshold", failure_threshold),
            ("max_attempts", max_attempts),
            ("successes", successes),
        ):
            if type(number) is not int or not 1 <= number <= 20:
                raise ValueError(name + " must be an integer in 1..20")
        if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)):
            raise ValueError("cooldown must be numeric")  # noqa: TRY004
        if not math.isfinite(cooldown) or not 1 <= cooldown <= 3600:
            raise ValueError("cooldown must be finite and in 1..3600 seconds")
        self.failures = failure_threshold
        self.cooldown = cooldown
        self.max_attempts = max_attempts
        self.successes = successes

    def observe(self, service, healthy, now, reason, blocked=False, overloaded=False):
        """Return an event kind and whether an action was reserved.

        A repair reservation is persisted by Runner before executing Docker.
        Recovery requires subsequent probes, not a successful restart command.
        """
        service["healthy"] = healthy
        service["last_probe"] = {"at": now, "healthy": healthy, "reason": reason}
        if overloaded:
            # A live process rejecting excess business work is not repaired by
            # restarting it. Preserve incident budget/lockout/cooldown, but do
            # not let 429 responses contribute to a later restart threshold.
            service["healthy"] = False
            service["last_probe"]["healthy"] = False
            service["consecutive_failures"] = 0
            service["consecutive_successes"] = 0
            service["phase"] = "locked_out" if service["locked_out"] else "overloaded"
            return None, False
        if healthy:
            service["consecutive_failures"] = 0
            service["consecutive_successes"] = min(
                self.successes, service["consecutive_successes"] + 1
            )
            if service["consecutive_successes"] < self.successes:
                service["phase"] = "locked_out" if service["locked_out"] else "verifying"
                return None, False
            recovered = (
                service["incident_started_at"] is not None and not service["recovery_observed"]
            )
            service["recovery_observed"] = True
            if service["locked_out"]:
                service["phase"] = "locked_out"
            else:
                # Retain the service-wide cooldown even across separate incidents.
                service["phase"] = "healthy"
                service["attempts"] = 0
            return "healing_recovery_verified" if recovered else None, False

        if service["recovery_observed"] and not service["locked_out"]:
            previous_action = service["last_action_at"]
            service.update(fresh_service(previous_action))
            service["healthy"] = False
            service["last_probe"] = {"at": now, "healthy": False, "reason": reason}
        service["recovery_observed"] = False
        service["consecutive_successes"] = 0
        if blocked:
            service["consecutive_failures"] = 0
            service["phase"] = "locked_out" if service["locked_out"] else "blocked_by_dependency"
            return None, False
        if service["incident_started_at"] is None:
            service["incident_started_at"] = now
            service["incident_id"] = uuid.uuid4().hex
        service["consecutive_failures"] = min(self.failures, service["consecutive_failures"] + 1)
        if service["locked_out"]:
            service["phase"] = "locked_out"
            return None, False
        if service["consecutive_failures"] < self.failures:
            service["phase"] = "suspect"
            return None, False
        if service["attempts"] >= self.max_attempts:
            service["locked_out"] = True
            service["phase"] = "locked_out"
            return "healing_budget_exhausted", False
        last = service["last_action_at"]
        if last is not None and now - last < self.cooldown:
            service["phase"] = "cooldown"
            return None, False
        service["attempts"] += 1
        service["last_action_at"] = now
        service["phase"] = "verifying"
        return "healing_action_requested", True


class Runner:
    def __init__(self, policy=None, state_path=None, clock=time.time):
        self.policy = policy or HealingPolicy()
        self.state_path = Path(state_path or common.ROOT / "runtime/healing.json")
        self.clock = clock

    def load(self):
        if not self.state_path.exists():
            return {
                "schema_version": 1,
                "api_identity": None,
                "services": {name: fresh_service() for name in ("redis", "api")},
            }
        data = json.loads(self.state_path.read_text())
        if data.get("schema_version") != 1 or set(data.get("services", {})) != {"redis", "api"}:
            raise RuntimeError(
                "Invalid healing state; repair the state file before enabling actions"
            )
        required = set(fresh_service())
        for service in data["services"].values():
            if not required.issubset(service):
                raise RuntimeError("Incomplete healing state; refusing to reset repair budgets")
        return data

    def save(self, data):
        data["updated_at"] = timestamp()
        data["configuration"] = {
            "failure_threshold": self.policy.failures,
            "cooldown_seconds": self.policy.cooldown,
            "max_attempts": self.policy.max_attempts,
            "success_threshold": self.policy.successes,
        }
        atomic_json(self.state_path, data)

    @staticmethod
    def probe_redis():
        try:
            result = common.compose(
                "exec",
                "-T",
                "redis",
                "redis-cli",
                "--raw",
                "PING",
                timeout=3,
                capture_output=True,
                text=True,
            )
            if result.stdout.strip() == "PONG":
                return True, "redis_ping_ok"
            return False, "redis_ping_unexpected"
        except (subprocess.SubprocessError, OSError, RuntimeError):
            return False, "redis_ping_failed"

    @staticmethod
    def probe_api(slot):
        base = common.slot_url(slot)
        for path, expected in (("/health/live", "status"), ("/products/1", "id")):
            try:
                with urlopen(base + path, timeout=2) as response:
                    payload = json.loads(response.read())
                    if response.status != 200 or not isinstance(payload, dict):
                        return False, "api_probe_unexpected"
                    if payload.get(expected) != ("ok" if expected == "status" else 1):
                        return False, "api_probe_unexpected"
            except HTTPError as exc:
                code = exc.code
                exc.close()
                if path == "/products/1" and code == 429:
                    return False, "api_overloaded"
                return False, "api_liveness_failed" if path.endswith(
                    "live"
                ) else "api_business_failed"
            except (URLError, OSError, ValueError):
                return False, "api_liveness_failed" if path.endswith(
                    "live"
                ) else "api_business_failed"
        return True, "api_business_ok"

    @staticmethod
    def restart(target):
        if target not in {"redis", "api-blue", "api-green"}:
            raise ValueError("Restart target is not allowlisted")
        common.compose("restart", target, timeout=20, capture_output=True, text=True)

    def tick(self):
        try:
            with common.operation_lock(blocking=False):
                # Read after acquiring the deployment lock: never repair an old active slot.
                release = common.read_state()
                slot = release.get("active")
                phase = release.get("phase")
                if not phase or (phase == "idle" and slot not in {"blue", "green"}):
                    return {"skipped": "deployment_not_initialized"}
                redis_only = phase != "idle"
                data = self.load()
                data["api_actions_suspended"] = redis_only
                data["deployment_phase"] = phase
                if not redis_only:
                    slot_info = release.get("slots", {}).get(slot, {})
                    identity = (
                        slot + ":" + str(slot_info.get("image_id", slot_info.get("image", "")))
                    )
                    if data.get("api_identity") != identity:
                        data["services"]["api"] = fresh_service(
                            data["services"]["api"]["last_action_at"]
                        )
                        data["api_identity"] = identity
                redis_health = self.probe_redis()
                observations = [("redis", redis_health)]
                if not redis_only:
                    observations.append(("api", self.probe_api(slot)))
                # With the operation lock free, a non-idle state represents an
                # unfinished release. Repair its shared dependency, but never
                # guess which API slot should be active or perform a rollback.
                for name, (healthy, reason) in observations:
                    service = data["services"][name]
                    old_incident = service["incident_id"]
                    old_attempts = service["attempts"]
                    now = self.clock()
                    kind, requested = self.policy.observe(
                        service,
                        healthy,
                        now,
                        reason,
                        blocked=name == "api"
                        and not redis_health[0]
                        and reason != "api_liveness_failed",
                        overloaded=name == "api" and reason == "api_overloaded",
                    )
                    self.save(data)
                    if service["incident_id"] and service["incident_id"] != old_incident:
                        common.event(
                            "healing_incident_detected",
                            service=name,
                            incident_id=service["incident_id"],
                            reason=reason,
                            mode="real",
                        )
                    if kind:
                        details = {
                            "service": name,
                            "incident_id": service["incident_id"],
                            "attempts": service["attempts"],
                            "reason": reason,
                            "mode": "real",
                            "locked_out": service["locked_out"],
                        }
                        if kind == "healing_recovery_verified":
                            details["attempts"] = old_attempts
                            details["elapsed_seconds"] = max(
                                0, now - service["incident_started_at"]
                            )
                        common.event(kind, **details)
                    if requested:
                        target = "redis" if name == "redis" else "api-" + slot
                        try:
                            self.restart(target)
                        except (subprocess.SubprocessError, OSError, RuntimeError):
                            common.event(
                                "healing_action_failed",
                                service=name,
                                target=target,
                                incident_id=service["incident_id"],
                                mode="real",
                            )
                        else:
                            common.event(
                                "healing_action_accepted",
                                service=name,
                                target=target,
                                incident_id=service["incident_id"],
                                recovered=False,
                                mode="real",
                            )
                        # At most one repair per tick. Verification occurs on later probes.
                        return {"action_target": target, "state": data}
                return {"state": data}
        except BlockingIOError:
            return {"skipped": "operation_in_progress"}

    def reset(self, name):
        if name not in {"api", "redis"}:
            raise ValueError("service must be api or redis")
        with common.operation_lock():
            data = self.load()
            # Operator re-arms the budget but cannot bypass the cooldown.
            data["services"][name] = fresh_service(data["services"][name]["last_action_at"])
            self.save(data)
            common.event("healing_operator_reset", service=name, mode="real")
            return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "status", "reset"))
    parser.add_argument("--service", choices=("api", "redis"))
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--failure-threshold", type=int, default=3)
    parser.add_argument("--cooldown", type=float, default=30)
    parser.add_argument("--max-attempts", type=int, default=2)
    args = parser.parse_args()
    if not math.isfinite(args.interval) or not 1 <= args.interval <= 300:
        parser.error("interval must be finite and in 1..300 seconds")
    try:
        runner = Runner(HealingPolicy(args.failure_threshold, args.cooldown, args.max_attempts))
    except ValueError as exc:
        parser.error(str(exc))
    if args.action == "status":
        print(json.dumps(runner.load(), ensure_ascii=False, indent=2))
        return
    if args.action == "reset":
        if args.service is None:
            parser.error("reset requires --service")
        print(json.dumps(runner.reset(args.service), ensure_ascii=False, indent=2))
        return
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    common.event("healing_daemon_started", mode="real", interval_seconds=args.interval)
    while not stop.is_set():
        try:
            result = runner.tick()
            print(
                json.dumps(
                    {"timestamp": timestamp(), "event": "healing_tick", "result": result},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except (
            OSError,
            ValueError,
            RuntimeError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ) as exc:
            # Keep corrupted/missing runtime state fail-closed; never silently reset it.
            common.event("healing_controller_error", error_type=type(exc).__name__, mode="real")
            print(
                json.dumps({"event": "healing_controller_error", "error_type": type(exc).__name__}),
                flush=True,
            )
        stop.wait(args.interval)
    common.event("healing_daemon_stopped", mode="real")


if __name__ == "__main__":
    main()
