import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FaultSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["latency", "errors", "cpu"]
    duration_seconds: float = Field(default=120, ge=1, le=600)
    delay_ms: int = Field(default=800, ge=0, le=5000)
    cpu_iterations: int = Field(default=100_000, ge=1, le=5_000_000)
    error_rate: float = Field(default=0.3, ge=0, le=1)


class LabConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fallback_enabled: bool


class FaultState:
    def __init__(self, clock=time.monotonic, on_event=None):
        self.clock = clock
        self.on_event = on_event or (lambda *args: None)
        self.spec = None
        self.deadline = 0

    def set(self, spec):
        if self.active() is not None:
            raise ValueError("Clear the active fault before starting another")
        self.spec = spec
        self.deadline = self.clock() + spec.duration_seconds
        self.on_event("fault_started", spec.model_dump())

    def clear(self, reason="fault_cleared"):
        previous = self.spec
        self.spec = None
        self.deadline = 0
        if previous is not None:
            self.on_event(reason, {"kind": previous.kind})

    def active(self):
        if self.clock() >= self.deadline:
            self.clear("fault_expired")
        return self.spec

    def status(self):
        spec = self.active()
        return {
            "fault": spec.model_dump() if spec else None,
            "remaining_seconds": max(0, self.deadline - self.clock()),
        }
