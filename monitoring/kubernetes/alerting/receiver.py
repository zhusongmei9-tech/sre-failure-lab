"""Internal, memory-bounded Alertmanager webhook sink; Python standard library only."""

import json
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 65536
MAX_ALERTS = 20
MAX_EVENTS = 200
LABELS = {"alertname", "severity", "namespace", "pod", "deployment", "node", "instance",
          "synthetic", "run_id", "module"}
ANNOTATIONS = {"summary", "runbook_url"}


def limited_fields(value, allowed):
    if not isinstance(value, dict):
        raise TypeError("fields must be objects")
    return {key: item[:256] for key, item in value.items()
            if key in allowed and isinstance(item, str)}


def parse_alerts(payload):
    if not isinstance(payload, dict):
        raise TypeError("payload must be an object")
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not 1 <= len(alerts) <= MAX_ALERTS:
        raise ValueError("expected 1..20 alerts")
    result = []
    for alert in alerts:
        if (not isinstance(alert, dict) or not isinstance(alert.get("status"), str)
                or alert["status"] not in {"firing", "resolved"}):
            raise ValueError("invalid alert status")
        labels = limited_fields(alert.get("labels", {}), LABELS)
        if not labels.get("alertname"):
            raise ValueError("alertname is required")
        result.append({"status": alert["status"], "labels": labels,
                       "annotations": limited_fields(alert.get("annotations", {}), ANNOTATIONS)})
    return result


class EventStore:
    def __init__(self):
        self.instance_id = uuid.uuid4().hex
        self.events = deque(maxlen=MAX_EVENTS)
        self.sequence = 0
        self.received = {"firing": 0, "resolved": 0}
        self.last_received_at = 0.0

    def append(self, alerts):
        now = time.time()
        for alert in alerts:
            self.sequence += 1
            self.received[alert["status"]] += 1
            self.events.append({**alert, "sequence": self.sequence, "received_at": now})
        self.last_received_at = now

    def snapshot(self, after=0, run_id=None):
        return {
            "instance_id": self.instance_id,
            "capacity": MAX_EVENTS,
            "last_sequence": self.sequence,
            "oldest_sequence": self.events[0]["sequence"] if self.events else self.sequence + 1,
            "dropped_events": max(0, self.sequence - MAX_EVENTS),
            "events": [item for item in self.events if item["sequence"] > after
                       and (run_id is None or item["labels"].get("run_id") == run_id)],
        }

    def metrics(self):
        return (
            '# TYPE sre_lab_alert_receiver_received_total counter\n'
            f'sre_lab_alert_receiver_received_total{{status="firing"}} {self.received["firing"]}\n'
            f'sre_lab_alert_receiver_received_total{{status="resolved"}} {self.received["resolved"]}\n'
            '# TYPE sre_lab_alert_receiver_dropped_events_total counter\n'
            f'sre_lab_alert_receiver_dropped_events_total {max(0, self.sequence - MAX_EVENTS)}\n'
            '# TYPE sre_lab_alert_receiver_last_received_timestamp_seconds gauge\n'
            f'sre_lab_alert_receiver_last_received_timestamp_seconds {self.last_received_at}\n'
        )


class Receiver(HTTPServer):
    # One request at a time plus socket deadlines bounds connections/threads too.
    request_queue_size = 8

    def __init__(self, address):
        self.store = EventStore()
        super().__init__(address, Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server_version = "SRELabReceiver/1"

    def log_message(self, *_args):
        pass  # Do not echo payloads, URLs, or caller-controlled headers to logs.

    def respond(self, status, payload, content_type="application/json"):
        body = (json.dumps(payload, ensure_ascii=True) if content_type == "application/json"
                else payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/healthz":
            self.respond(200, {"status": "ok", "instance_id": self.server.store.instance_id})
        elif url.path == "/metrics":
            self.respond(200, self.server.store.metrics(), "text/plain; version=0.0.4")
        elif url.path == "/events":
            try:
                query = parse_qs(url.query, max_num_fields=4)
                after = int(query.get("after", ["0"])[0])
                run_id = query.get("run_id", [None])[0]
                if after < 0 or after > 2**63 - 1 or (run_id and len(run_id) > 64):
                    raise ValueError("invalid query")
            except ValueError:
                self.respond(400, {"error": "invalid query"})
                return
            self.respond(200, self.server.store.snapshot(after, run_id))
        else:
            self.respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/alerts":
            self.respond(404, {"error": "not found"})
            return
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            self.respond(411, {"error": "one Content-Length is required"})
            return
        try:
            length = int(self.headers["Content-Length"])
        except ValueError:
            self.respond(400, {"error": "invalid length"})
            return
        if not 0 < length <= MAX_BODY:
            self.respond(413, {"error": "body size limit"})
            return
        if self.headers.get_content_type() != "application/json":
            self.respond(415, {"error": "application/json required"})
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("incomplete body")
            alerts = parse_alerts(json.loads(body))
        except (ValueError, TypeError, UnicodeError, TimeoutError, RecursionError):
            self.respond(400, {"error": "invalid alert payload"})
            return
        self.server.store.append(alerts)
        self.respond(200, {"accepted": len(alerts)})


if __name__ == "__main__":
    Receiver(("0.0.0.0", 8080)).serve_forever()
