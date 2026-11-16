"""Small paced worker load generator. Standard library only."""

import argparse
import json
import math
import signal
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def run(base_url, rps, workers, duration, timeout, report_interval):
    stop = threading.Event()
    lock = threading.Lock()
    counts = Counter()
    latencies = []
    started = time.monotonic()
    previous = started
    total = Counter()

    def worker(index):
        interval = workers / rps
        stop.wait(index / rps)
        iteration = index
        while not stop.is_set():
            tick = time.monotonic()
            try:
                with urlopen(
                    f"{base_url.rstrip('/')}/products/{iteration % 3 + 1}", timeout=timeout
                ) as response:
                    response.read()
                    status = str(response.status)
            except HTTPError as exc:
                status = str(exc.code)
                exc.close()
            except (OSError, URLError):
                status = "transport_error"
            elapsed = time.monotonic() - tick
            with lock:
                counts[status] += 1
                latencies.append(elapsed)
            iteration += 1
            stop.wait(max(0, interval - elapsed))

    def report():
        nonlocal previous
        now = time.monotonic()
        with lock:
            snapshot = dict(counts)
            samples = sorted(latencies)
            counts.clear()
            latencies.clear()
        total.update(snapshot)
        size = len(samples)
        print(
            json.dumps(
                {
                    "event": "load_window",
                    "elapsed_seconds": round(now - started, 2),
                    "completed": size,
                    "actual_rps": round(size / max(now - previous, 0.001), 2),
                    "status_counts": snapshot,
                    "success_ratio": sum(n for code, n in snapshot.items() if code.startswith("2"))
                    / size
                    if size
                    else None,
                    "p95_ms": round(samples[math.ceil(size * 0.95) - 1] * 1000, 2)
                    if size
                    else None,
                }
            ),
            flush=True,
        )
        previous = now

    def shutdown(*_):
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(
        json.dumps(
            {
                "event": "load_started",
                "target": base_url,
                "target_rps": rps,
                "workers": workers,
                "duration_seconds": duration,
            }
        ),
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, i) for i in range(workers)]
        while not stop.is_set():
            remaining = duration - (time.monotonic() - started) if duration else report_interval
            if remaining <= 0:
                break
            stop.wait(min(report_interval, remaining))
            report()
        stop.set()
        for future in futures:
            future.result()
    report()
    print(json.dumps({"event": "load_finished", "status_counts": dict(total)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://api:8000")
    parser.add_argument("--rps", type=float, default=10)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--duration", type=float, default=0, help="Seconds; 0 runs until stopped")
    parser.add_argument("--timeout", type=float, default=2)
    parser.add_argument("--report-interval", type=float, default=5)
    args = parser.parse_args()
    if (
        not math.isfinite(args.rps)
        or not 0 < args.rps <= 1000
        or not 1 <= args.workers <= 100
        or not math.isfinite(args.duration)
        or args.duration < 0
        or not math.isfinite(args.timeout)
        or not 0 < args.timeout <= 30
        or not math.isfinite(args.report_interval)
        or not 0 < args.report_interval <= 60
    ):
        parser.error(
            "rps: (0,1000], workers: [1,100], duration: >=0, timeout: (0,30], report-interval: (0,60]"
        )
    run(args.base_url, args.rps, args.workers, args.duration, args.timeout, args.report_interval)
