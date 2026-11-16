import json
import subprocess
import sys
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def test_load_generator_counts_http_errors_and_stops():
    received = Counter()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            code = 503 if self.path.endswith("/2") else 200
            received[str(code)] += 1
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        process = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parents[1] / "loadgen/main.py"),
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                "--rps",
                "20",
                "--workers",
                "2",
                "--duration",
                "1",
                "--report-interval",
                "0.5",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        events = [json.loads(line) for line in process.stdout.splitlines()]
        assert events[-1]["event"] == "load_finished"
        assert events[-1]["status_counts"] == dict(received)
        assert received["200"] > 0 and received["503"] > 0
        windows = [event for event in events if event["event"] == "load_window"]
        assert sum(event["completed"] for event in windows) == sum(received.values())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
