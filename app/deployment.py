"""In-memory blue/green release simulator. Never starts containers or changes traffic."""

import math
import re
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from datetime import UTC, datetime


class DeploymentController:
    """Manual probe evidence drives a serialized, simulation-only state machine.

    An action acknowledgement is not health evidence. A candidate must pass both
    readiness and a business smoke check before the simulated pointer switches.
    The observation period needs a healthy observation at/after its deadline.
    """

    def __init__(self, clock=time.monotonic, observation_seconds=30, on_event=None):
        if (
            type(observation_seconds) not in (int, float)
            or not math.isfinite(observation_seconds)
            or not 1 <= observation_seconds <= 3600
        ):
            raise ValueError("observation_seconds must be finite and between 1 and 3600")
        self.clock = clock
        self.observation_seconds = observation_seconds
        self.on_event = on_event or (lambda *_: None)
        self._lock = threading.RLock()
        self._events = deque(maxlen=200)
        self._phase = "idle"
        self._active = None
        self._previous = None
        self._candidate = None
        self._before_switch = None
        self._deadline = None
        self._release_id = None
        self._last_outcome = "not_deployed"

    def _event(self, action, **details):
        event = {
            "event_id": uuid.uuid4().hex,
            "timestamp": datetime.now(UTC).isoformat(),
            "action": action,
            "release_id": self._release_id,
            "mode": "simulation",
            "details": details,
        }
        self._events.append(event)
        self.on_event(action, deepcopy(event))

    def _finish(self, outcome):
        self._phase = "idle"
        self._candidate = None
        self._before_switch = None
        self._deadline = None
        self._last_outcome = outcome

    @staticmethod
    def _boolean(value):
        if type(value) is not bool:
            raise ValueError("Probe evidence must be boolean")

    def _require_release(self, release_id):
        """Fence delayed evidence/actions; caller must already hold self._lock."""
        if not isinstance(release_id, str) or not release_id or release_id != self._release_id:
            raise ValueError("release_id does not match the current release")

    def begin(self, version):
        """Stage into the inactive slot, replacing any rollback copy occupying it."""
        with self._lock:
            if not isinstance(version, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version
            ):
                raise ValueError(
                    "version must be 1..64 letters, digits, dots, underscores or hyphens"
                )
            if self._phase != "idle":
                raise ValueError("Another release is still validating or observing")
            if self._active and self._active["version"] == version:
                raise ValueError("That version is already active")
            self._release_id = uuid.uuid4().hex
            slot = "green" if self._active and self._active["slot"] == "blue" else "blue"
            if self._previous and self._previous["slot"] == slot:
                replaced = deepcopy(self._previous)
                self._previous = None
                self._event("deployment_previous_replaced", previous=replaced, slot=slot)
            self._candidate = {"slot": slot, "version": version}
            self._phase = "validating"
            self._last_outcome = "candidate_pending"
            self._event("deployment_started", candidate=deepcopy(self._candidate))
            return self.status()

    def check_candidate(self, ready, smoke_ok, *, release_id):
        """Apply explicitly supplied simulated readiness and business evidence."""
        with self._lock:
            self._require_release(release_id)
            self._boolean(ready)
            self._boolean(smoke_ok)
            if self._phase != "validating":
                raise ValueError("No candidate is waiting for validation")
            self._event("deployment_candidate_checked", ready=ready, smoke_ok=smoke_ok)
            if not ready or not smoke_ok:
                self._event("deployment_rejected", candidate=deepcopy(self._candidate))
                self._finish("rejected_before_switch")
                return self.status()
            self._before_switch = (deepcopy(self._active), deepcopy(self._previous))
            self._previous = deepcopy(self._active)
            self._active = deepcopy(self._candidate)
            self._phase = "observing"
            self._deadline = self.clock() + self.observation_seconds
            self._last_outcome = "observing"
            self._event("deployment_switched", active=deepcopy(self._active))
            return self.status()

    def observe(self, healthy, *, release_id):
        """A failed observation aborts even if its time window already elapsed."""
        with self._lock:
            self._require_release(release_id)
            self._boolean(healthy)
            if self._phase != "observing":
                raise ValueError("No release is in its observation period")
            self._event("deployment_observed", healthy=healthy)
            if not healthy:
                self._restore("unhealthy_observation")
            elif self.clock() >= self._deadline:
                self._event("deployment_completed", active=deepcopy(self._active))
                self._finish("completed")
            return self.status()

    def _restore(self, reason):
        rejected = deepcopy(self._active)
        self._active, self._previous = deepcopy(self._before_switch)
        if self._active is None:
            self._event("deployment_failed_no_previous", reason=reason, rejected=rejected)
            self._finish("failed_no_previous")
        else:
            self._event(
                "deployment_rolled_back",
                reason=reason,
                rejected=rejected,
                active=deepcopy(self._active),
            )
            self._finish("rolled_back")

    def rollback(self, *, release_id):
        """Restore the retained accepted release; never claim a nonexistent rollback."""
        with self._lock:
            self._require_release(release_id)
            if self._phase == "validating":
                raise ValueError("Cancel the pending candidate before manual rollback")
            if self._previous is None:
                raise ValueError("No previous release is available for rollback")
            if self._phase == "observing":
                self._restore("manual_rollback")
            else:
                self._active, self._previous = self._previous, self._active
                self._event(
                    "deployment_rolled_back",
                    reason="manual_rollback",
                    active=deepcopy(self._active),
                )
                self._last_outcome = "rolled_back"
            return self.status()

    def cancel(self, *, release_id):
        """Discard a not-yet-active candidate. Observing releases require rollback."""
        with self._lock:
            self._require_release(release_id)
            if self._phase != "validating":
                raise ValueError("Only a validating candidate can be canceled")
            self._event("deployment_canceled", candidate=deepcopy(self._candidate))
            self._finish("canceled")
            return self.status()

    def status(self):
        with self._lock:
            return deepcopy(
                {
                    "mode": "simulation",
                    "real_actions_enabled": False,
                    "phase": self._phase,
                    "locked": self._phase != "idle",
                    "release_id": self._release_id,
                    "active": self._active,
                    "previous": self._previous,
                    "candidate": self._candidate,
                    "last_outcome": self._last_outcome,
                    "observation_seconds": self.observation_seconds,
                    "remaining_seconds": max(0, self._deadline - self.clock())
                    if self._deadline is not None
                    else None,
                    "events": list(reversed(self._events)),
                    "persistent": False,
                }
            )
