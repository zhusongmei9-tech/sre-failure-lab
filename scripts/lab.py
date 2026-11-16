"""Linux/macOS host CLI; execute from the project root. No third-party packages."""

import argparse
import json
import math
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def compose(*args, **kwargs):
    kwargs.setdefault("timeout", 30)
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, **kwargs)


def api(path, method="GET", payload=None):
    program = """import json,os,urllib.request
headers={'Content-Type':'application/json'}
if PATH.startswith('/lab/'):
 token=os.environ.get('LAB_TOKEN','')
 if not token:
  raise SystemExit('LAB_TOKEN is missing in the API container; configure it before using lab controls')
 headers['Authorization']='Bearer '+token
req=urllib.request.Request('http://127.0.0.1:8000'+PATH, method=METHOD,
 data=json.dumps(PAYLOAD).encode() if PAYLOAD is not None else None,
 headers=headers)
with urllib.request.urlopen(req,timeout=10) as response:
 print(response.read().decode())
"""
    prefix = f"PATH={path!r}\nMETHOD={method!r}\nPAYLOAD={payload!r}\n"
    result = compose(
        "exec", "-T", "api", "python", "-", input=prefix + program, text=True, capture_output=True
    )
    return json.loads(result.stdout)


def redis_pause(seconds):
    compose("exec", "-T", "redis", "redis-cli", "CLIENT", "PAUSE", str(int(seconds * 1000)), "ALL")


def recover():
    # Authenticate before any Redis mutation; a missing token must not restart a service.
    api("/lab/status")
    # Also recover a container paused manually during earlier lessons.
    subprocess.run(
        ["docker", "compose", "unpause", "redis"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=30,
    )
    try:
        compose(
            "exec", "-T", "redis", "redis-cli", "CLIENT", "UNPAUSE", timeout=1, capture_output=True
        )
    except subprocess.TimeoutExpired:
        # Some Redis versions queue UNPAUSE behind PAUSE ALL too.
        # Restart only the ephemeral cache when early recovery is requested.
        compose("restart", "redis")
        for _ in range(20):
            try:
                result = compose(
                    "exec",
                    "-T",
                    "redis",
                    "redis-cli",
                    "ping",
                    timeout=1,
                    text=True,
                    capture_output=True,
                )
                if result.stdout.strip() == "PONG":
                    break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("Redis failed to recover")
    return api("/lab/faults", "DELETE")


def compare(seconds, rps):
    running = compose("ps", "--status", "running", "--services", text=True, capture_output=True)
    if "loadgen" in running.stdout.splitlines():
        raise RuntimeError("Stop background traffic first: make traffic-stop")
    original = api("/lab/status")
    if original["fault"] is not None:
        raise RuntimeError("Clear the active application fault before comparing")
    results = []
    # Execute requests within the API container; its network is independent of host port settings.
    sample_program = """import concurrent.futures,json,math,time,urllib.request,urllib.error
started=time.monotonic()
def request(i):
    target=started+i/RPS
    time.sleep(max(0,target-time.monotonic()))
    begin=time.monotonic()
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/products/'+str(i%3+1),timeout=3) as response:
            response.read(); status=response.status
    except urllib.error.HTTPError as exc:
        status=exc.code; exc.close()
    except OSError:
        status=0
    return status,time.monotonic()-begin
with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
    rows=list(pool.map(request,range(int(SECONDS*RPS))))
elapsed=time.monotonic()-started
durations=sorted(row[1] for row in rows)
print(json.dumps({'requests':len(rows),'success_rate':sum(200<=r[0]<300 for r in rows)/len(rows),
 'actual_rps':len(rows)/elapsed,'p95_ms':durations[math.ceil(.95*len(rows))-1]*1000,
 'status_counts':{str(code):sum(r[0]==code for r in rows) for code in set(r[0] for r in rows)}}))
"""

    def counter(name):
        program = f"""import urllib.request
text=urllib.request.urlopen('http://127.0.0.1:8000/metrics',timeout=5).read().decode()
print(next(float(line.split()[1]) for line in text.splitlines() if line.startswith({(name + " ")!r})))
"""
        return float(
            compose(
                "exec", "-T", "api", "python", "-", input=program, text=True, capture_output=True
            ).stdout
        )

    try:
        for fallback in (False, True):
            recover()
            api("/lab/config", "PATCH", {"fallback_enabled": fallback})
            before = counter("lab_source_reads_total")
            redis_pause(seconds + 15)
            start = datetime.now(UTC).isoformat()
            result = compose(
                "exec",
                "-T",
                "api",
                "python",
                "-",
                input=f"SECONDS={seconds!r}\nRPS={rps!r}\n" + sample_program,
                timeout=seconds * 3 + 60,
                text=True,
                capture_output=True,
            )
            row = json.loads(result.stdout)
            row.update(
                mode="fallback" if fallback else "baseline",
                started_at=start,
                source_reads=counter("lab_source_reads_total") - before,
            )
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            recover()
    finally:
        try:
            recover()
        finally:
            api("/lab/config", "PATCH", {"fallback_enabled": original["fallback_enabled"]})
    directory = ROOT / "reports"
    directory.mkdir(exist_ok=True)
    path = directory / (datetime.now(UTC).strftime("compare-%Y%m%dT%H%M%S%fZ") + ".json")
    path.write_text(
        json.dumps(
            {"seconds_per_mode": seconds, "target_rps": rps, "results": results},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(f"Report: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "status",
            "latency",
            "errors",
            "cpu",
            "redis",
            "recover",
            "fallback-on",
            "fallback-off",
            "compare",
        ],
    )
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--delay-ms", type=int, default=800)
    parser.add_argument("--error-rate", type=float, default=0.3)
    parser.add_argument("--cpu-iterations", type=int, default=100_000)
    parser.add_argument("--rps", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300 or not 1 <= args.rps <= 20:
        parser.error("seconds must be 1..300, rps must be 1..20")
    # Validate every submitted field before recover() can alter Redis or fault state.
    if not 0 <= args.delay_ms <= 5000:
        parser.error("delay-ms must be 0..5000")
    if not math.isfinite(args.error_rate) or not 0 <= args.error_rate <= 1:
        parser.error("error-rate must be a finite number in 0..1")
    if not 1 <= args.cpu_iterations <= 5_000_000:
        parser.error("cpu-iterations must be 1..5000000")
    if args.action in ("latency", "errors", "cpu"):
        # One fault type at a time when using the CLI.
        recover()
        result = api(
            "/lab/faults",
            "POST",
            {
                "kind": args.action,
                "duration_seconds": args.seconds,
                "delay_ms": args.delay_ms,
                "error_rate": args.error_rate,
                "cpu_iterations": args.cpu_iterations,
            },
        )
    elif args.action == "redis":
        recover()
        redis_pause(args.seconds)
        result = {"redis_commands_paused_seconds": args.seconds, "automatic_resume": True}
    elif args.action == "recover":
        result = recover()
    elif args.action == "compare":
        compare(args.seconds, args.rps)
        return
    elif args.action.startswith("fallback-"):
        result = api("/lab/config", "PATCH", {"fallback_enabled": args.action == "fallback-on"})
    else:
        result = api("/lab/status")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
