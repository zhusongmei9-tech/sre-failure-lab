"""Bounded in-memory operation history; JSON logs are the durable export path."""

import logging
import uuid
from collections import deque
from datetime import UTC, datetime


class EventStore:
    def __init__(self, capacity=200):
        self.events = deque(maxlen=capacity)

    def append(self, action, details=None):
        event = {
            "event_id": uuid.uuid4().hex,
            "timestamp": datetime.now(UTC).isoformat(),
            "action": action,
            "details": details or {},
        }
        self.events.append(event)
        logging.getLogger("lab").info("lab_event", extra={"fields": event})
        return event

    def latest(self, limit=50):
        return list(reversed(self.events))[:limit]
