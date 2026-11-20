"""Measure a lab's product capacity from a separate k6 load generator (Python 3.9+)."""

import argparse
import http.client
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SCRIPT = Path(__file__).resolve().parents[1] / "loadgen" / "capacity.js"
SCENARIOS = ("hot", "mixed", "cold-start")
SLO_SUCCESS = 0.999
SLO_P95_MS = 100
CAPACITY_SECONDS = 600


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")  # noqa: UP017 -- Python 3.9


def origin(value):
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Supply a lab HTTP(S) origin without credentials, path, query or fragment")
    if parts.port == 0:
        raise ValueError("Invalid port")
    return value.rstrip("/")


def get(base, path):
    parts = urlsplit(base)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = cls(parts.hostname, parts.port, timeout=3)
    try:
        conn.request("GET", path)
        res = conn.getresponse()
        if res.status != 200:
            raise ValueError(f"Lab preflight {path} returned HTTP {res.status}")
        body = json.loads(res.read(1 << 20))
        if not isinstance(body, dict):
            raise TypeError(f"Lab preflight {path} expected a JSON object")
        return body, res.getheader("X-Cache")
    finally:
        conn.close()


def identify(base):
    schema, _ = get(base, "/openapi.json")
    info, paths = schema.get("info"), schema.get("paths")
    endpoint = paths.get("/products/{product_id}") if isinstance(paths, dict) else None
    if (
        not isinstance(info, dict)
        or info.get("title") != "SRE Failure Lab"
        or not isinstance(info.get("version"), str)
        or not isinstance(endpoint, dict)
        or "get" not in endpoint
    ):
        raise ValueError("Target does not expose this lab's product API")
    ready, _ = get(base, "/health/ready")
    if ready.get("redis") != "ok":
        raise ValueError("Redis must be ready before benchmarking")
    return info["version"]


def prepare(base, scenario, cold_wait):
    if scenario == "cold-start":
        time.sleep(cold_wait)
        return
    for product_id in [1] if scenario == "hot" else [1, 2, 3]:
        for _ in range(2):
            body, cache = get(base, f"/products/{product_id}")
            if body.get("id") == product_id and cache == "HIT":
                break
        else:
            raise ValueError(f"Could not verify warm cache for product {product_id}")


def summarize(metrics, scenario, rate, seconds):
    def count(name):
        return int(metrics.get(name, {}).get("values", {}).get("count", 0))

    started, completed = count("cap_started"), count("cap_completed")
    dropped, valid = count("dropped_iterations"), count("cap_valid")
    expected = rate * seconds
    samples = metrics.get("cap_client_ms", {}).get("values", {})
    p95, p99 = samples.get("p(95)"), samples.get("p(99)")
    success = valid / started if started else None
    checks = {
        "all_requests_completed": started == completed == count("iterations") == count("http_reqs"),
        "no_dropped_iterations": dropped == 0,
        # The executor may schedule the arrival exactly on the closing boundary.
        "offered_target": expected <= started <= expected + 1,
        "valid_product_ratio": success is not None and success >= SLO_SUCCESS,
        "p95_ms": p95 is not None and p95 <= SLO_P95_MS,
    }
    if scenario == "cold-start":
        checks["verified_first_three_misses"] = count("cap_initial_misses") == 3
    status_2xx = count("cap_http_2xx")
    return {
        "scenario": scenario,
        "target_rps": rate,
        "seconds": seconds,
        "planned": expected,
        "started": started,
        "completed": completed,
        "actual_rps": round(started / seconds, 3),
        "dropped_iterations": dropped,
        "http_2xx": status_2xx,
        "valid_products": valid,
        "invalid_2xx": status_2xx - valid,
        "http_429": count("cap_http_429"),
        "http_5xx": count("cap_http_5xx"),
        "transport_errors": count("cap_transport_errors"),
        "timeouts": count("cap_timeouts"),
        "valid_product_ratio": success,
        "p95_ms": p95,
        "p99_ms": p99,
        "cache": {k: count("cap_cache_" + k.lower()) for k in ("HIT", "MISS", "BYPASS", "UNKNOWN")},
        "initial_misses": count("cap_initial_misses") if scenario == "cold-start" else None,
        "checks": checks,
        "passed": all(checks.values()),
    }


def stage(args, scenario, rate):
    prepare(args.base_url, scenario, args.cold_wait)
    with tempfile.TemporaryDirectory() as tmp:
        summary_file = Path(tmp, "summary.json")
        env = os.environ.copy()
        # The load route must match the direct connection used for identity checks.
        env.update(
            NO_PROXY="*",
            no_proxy="*",
            K6_NO_USAGE_REPORT="true",
            CAP_SUMMARY=str(summary_file),
            CAP_SCENARIO=scenario,
            CAP_BASE=args.base_url,
            CAP_RATE=str(rate),
            CAP_SECONDS=str(args.seconds),
            CAP_VUS=str(args.vus),
        )
        started = utc()
        subprocess.run(
            [args.k6, "run", "--quiet", "--address", "127.0.0.1:0", str(SCRIPT)],
            env=env,
            check=True,
            timeout=args.seconds + 35,
        )
        row = summarize(json.loads(summary_file.read_text()), scenario, rate, args.seconds)
        return dict(row, started_at=started, ended_at=utc())


def highest(rows, scenarios, min_seconds):
    return {
        s: max(
            (
                r["target_rps"]
                for r in rows
                if r["scenario"] == s and r["seconds"] >= min_seconds and r["passed"]
            ),
            default=None,
        )
        for s in scenarios
    }


def save(path, report):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
        temp = Path(tmp.name)
        try:
            json.dump(report, tmp, indent=2, allow_nan=False)
            tmp.write("\n")
            tmp.flush()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", type=origin, default="http://127.0.0.1:18080")
    p.add_argument("--run", action="store_true", help="send requests; otherwise print the plan")
    p.add_argument("--rates", default="40,80,120")
    p.add_argument("--scenarios", default=",".join(SCENARIOS))
    p.add_argument("--seconds", type=int, default=600, help="duration per scenario and rate")
    p.add_argument("--cold-wait", type=int, default=65, help="quiet seconds before each cold start")
    p.add_argument("--vus", type=int, default=32, help="preallocated k6 virtual users")
    p.add_argument("--k6", default="k6")
    p.add_argument("--output", type=Path, default=Path("reports/capacity.json"))
    args = p.parse_args(argv)
    try:
        rates = [int(r) for r in args.rates.split(",")]
    except ValueError:
        p.error("--rates expects comma-separated integers")
    scenarios = args.scenarios.split(",")
    if not (rates == sorted(set(rates)) and 1 <= rates[0] <= rates[-1] <= 500):
        p.error("--rates must contain increasing integers between 1 and 500")
    if len(set(scenarios)) != len(scenarios) or not set(scenarios) <= set(SCENARIOS):
        p.error("--scenarios must be a comma-separated selection from " + ",".join(SCENARIOS))
    if not (1 <= args.seconds <= 3600 and 1 <= args.vus <= 256 and 0 <= args.cold_wait <= 3600):
        p.error("seconds 1..3600, vus 1..256, cold-wait 0..3600")
    report = {
        "base_url": args.base_url,
        "rates": rates,
        "scenarios": scenarios,
        "seconds_per_stage": args.seconds,
        "preallocated_vus": args.vus,
        "cold_wait_seconds": args.cold_wait,
        "success_target": SLO_SUCCESS,
        "p95_target_ms": SLO_P95_MS,
        "minimum_capacity_seconds": CAPACITY_SECONDS,
    }
    if not args.run:
        print(json.dumps(report, indent=2))
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.output.open("x"):
            pass
    except FileExistsError:
        p.error("Report exists; select a fresh --output filename")
    report.update(
        status="running",
        started_at=utc(),
        load_generator=platform.machine(),
        stages=[],
        highest_passed_rps=dict.fromkeys(scenarios),
        capacity_rps=dict.fromkeys(scenarios),
    )
    save(args.output, report)
    try:
        report["k6_version"] = subprocess.check_output([args.k6, "version"], text=True).strip()
        report["app_version"] = identify(args.base_url)
        for scenario in scenarios:
            for rate in rates:
                print(f"{scenario}: {rate} requests/s for {args.seconds}s", flush=True)
                row = stage(args, scenario, rate)
                report["stages"].append(row)
                report["highest_passed_rps"] = highest(report["stages"], scenarios, 0)
                report["capacity_rps"] = highest(report["stages"], scenarios, CAPACITY_SECONDS)
                save(args.output, report)
        report["status"] = "complete"
    except (
        TypeError,
        ValueError,
        OSError,
        http.client.HTTPException,
        subprocess.SubprocessError,
    ) as exc:
        report.update(status="error", error=str(exc))
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    finally:
        report["ended_at"] = utc()
        save(args.output, report)
    print(f"{report['status']}: {args.output}", flush=True)
    if report["status"] != "complete":
        return 2
    return 0 if all(r["passed"] for r in report["stages"]) else 1


if __name__ == "__main__":
    sys.exit(main())
