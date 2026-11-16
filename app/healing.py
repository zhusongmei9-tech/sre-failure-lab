"""Simulation-only controlled recovery state machine. No shell or runtime access."""

import math
import time
from collections import deque
from dataclasses import asdict, dataclass


@dataclass
class ServiceState:
    phase: str = "unknown"
    healthy: bool | None = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    attempts: int = 0
    last_action_at: float | None = None
    incident_started_at: float | None = None
    recovery_observed: bool = False
    locked_out: bool = False


class HealingController:
    """Consume simulated probe results; never touches actual services.

    Action acceptance is not recovery. Recovery requires later successful probes.
    A locked-out incident stays disarmed even if healthy observations arrive.
    reset() is the explicit operator decision to re-arm it.
    """

    SERVICES = ("api", "redis")

    def __init__(
        self,
        *,
        enabled=False,
        failure_threshold=3,
        cooldown_seconds=30,
        max_attempts=2,
        success_threshold=2,
        clock=time.monotonic,
        on_event=None,
    ):
        for name, value in (
            ("failure_threshold", failure_threshold),
            ("max_attempts", max_attempts),
            ("success_threshold", success_threshold),
        ):
            if type(value) is not int or not 1 <= value <= 20:
                raise ValueError(f"{name} must be an integer between 1 and 20")
        if (
            isinstance(cooldown_seconds, bool)
            or not isinstance(cooldown_seconds, (int, float))
            or not math.isfinite(cooldown_seconds)
            or not 1 <= cooldown_seconds <= 3600
        ):
            raise ValueError("cooldown_seconds must be between 1 and 3600")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        self.enabled = enabled
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.max_attempts = max_attempts
        self.success_threshold = success_threshold
        self.clock = clock
        self.on_event = on_event or (lambda *args: None)
        self.services = {service: ServiceState() for service in self.SERVICES}
        self.events = deque(maxlen=200)
        self.sequence = 0

    def _service(self, service):
        if not isinstance(service, str) or service not in self.services:
            raise ValueError("service must be api or redis")
        return self.services[service]

    def _event(self, action, service=None, **details):
        self.sequence += 1
        event = {
            "sequence": self.sequence,
            "action": action,
            "service": service,
            "observed_monotonic": self.clock(),
            "simulation": True,
            **details,
        }
        self.events.append(event)
        self.on_event(action, dict(event))

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if self.enabled != enabled:
            self.enabled = enabled
            self._event("healing_simulation_enabled" if enabled else "healing_simulation_disabled")
        return self.status()

    def reset(self, service):
        self._service(service)
        self.services[service] = ServiceState(phase="unknown")
        self._event("healing_operator_reset", service)
        return self.status()

    def observe(self, service, healthy):
        state = self._service(service)
        if type(healthy) is not bool:
            raise ValueError("healthy must be a boolean")
        now = self.clock()
        state.healthy = healthy
        self._event("healing_probe", service, healthy=healthy)
        if healthy:
            state.consecutive_failures = 0
            state.consecutive_successes = min(
                self.success_threshold, state.consecutive_successes + 1
            )
            if state.incident_started_at is None:
                state.phase = "healthy"
                return self.status()
            if state.consecutive_successes < self.success_threshold:
                if not state.locked_out:
                    state.phase = "verifying"
                return self.status()
            if not state.recovery_observed:
                state.recovery_observed = True
                self._event(
                    "healing_recovery_verified",
                    service,
                    elapsed_seconds=max(0, now - state.incident_started_at),
                    attempts=state.attempts,
                    locked_out=state.locked_out,
                )
            if state.locked_out:
                # Do not silently re-arm after exhausting the repair budget.
                state.phase = "locked_out"
            else:
                self.services[service] = ServiceState(
                    phase="healthy",
                    healthy=True,
                    consecutive_successes=self.success_threshold,
                    recovery_observed=True,
                )
            return self.status()

        state.consecutive_successes = 0
        state.recovery_observed = False
        state.consecutive_failures = min(
            self.failure_threshold, state.consecutive_failures + 1
        )
        if state.incident_started_at is None:
            state.incident_started_at = now
            self._event("healing_incident_detected", service)
        if state.locked_out:
            state.phase = "locked_out"
            return self.status()
        if state.consecutive_failures < self.failure_threshold:
            state.phase = "suspect"
            return self.status()
        if not self.enabled:
            state.phase = "disabled"
            return self.status()
        if state.attempts >= self.max_attempts:
            state.locked_out = True
            state.phase = "locked_out"
            self._event("healing_budget_exhausted", service, attempts=state.attempts)
            return self.status()
        if (
            state.last_action_at is not None
            and now - state.last_action_at < self.cooldown_seconds
        ):
            state.phase = "cooldown"
            return self.status()
        state.attempts += 1
        state.last_action_at = now
        state.phase = "verifying"
        self._event(
            "healing_action_simulated",
            service,
            action_name="restart_api" if service == "api" else "restart_redis",
            attempt=state.attempts,
            accepted=True,
            recovered=False,
        )
        return self.status()

    def status(self):
        now = self.clock()
        states = {}
        for service, state in self.services.items():
            states[service] = {
                **asdict(state),
                "cooldown_remaining_seconds": (
                    max(0, self.cooldown_seconds - (now - state.last_action_at))
                    if state.last_action_at is not None else 0
                ),
            }
        return {
            "mode": "simulation",
            "real_actions_supported": False,
            "enabled": self.enabled,
            "configuration": {
                "failure_threshold": self.failure_threshold,
                "cooldown_seconds": self.cooldown_seconds,
                "max_attempts": self.max_attempts,
                "success_threshold": self.success_threshold,
            },
            "services": states,
            "events": [dict(event) for event in self.events],
            "persistent": False,
        }
