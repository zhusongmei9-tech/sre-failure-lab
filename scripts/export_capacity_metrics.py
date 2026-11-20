"""Export window statistics from the capacity dashboard via Prometheus query_range."""

import argparse
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener

DASHBOARD = Path(__file__).resolve().parents[1] / "monitoring/grafana/dashboards/k8s-capacity.json"
NAMESPACES = ("sre-capacity-lab", "sre-app-lab")
PUBLIC_LABELS = ("pod", "container", "command", "result", "mountpoint")


def instant(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))  # noqa: FURB162 -- Python 3.9
    if dt.utcoffset() is None:
        raise ValueError("Use an ISO timestamp with a timezone (Z or offset)")
    return dt.astimezone(timezone.utc)  # noqa: UP017 -- host Python 3.9


def query_range(url, expr, start, end, step):
    params = urlencode({"query": expr, "start": start, "end": end, "step": step})
    endpoint = url.rstrip("/") + "/api/v1/query_range?" + params
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(endpoint, timeout=20) as response:
            body = json.load(response)
    except HTTPError as exc:
        raise ValueError(f"Prometheus returned HTTP {exc.code}: {exc.reason}") from exc
    if not isinstance(body, dict):
        raise TypeError("Expected a Prometheus response object")
    if body.get("status") != "success":
        raise ValueError(f"Prometheus error: {body.get('errorType')}: {body.get('error')}")
    data = body["data"]
    if data["resultType"] != "matrix":
        raise ValueError("Expected a Prometheus range matrix")
    return data["result"], len(body.get("warnings", []))


def series_stats(raw):
    values = []
    for _, value in raw["values"]:
        if value is not None:
            number = float(value)
            if math.isfinite(number):
                values.append(number)
    labels = raw["metric"]
    return {
        "labels": {key: labels[key] for key in PUBLIC_LABELS if key in labels},
        "samples": len(raw["values"]),
        "finite_samples": len(values),
        "min": min(values, default=None),
        "mean": fmean(values) if values else None,
        "max": max(values, default=None),
    }


def collect(dashboard, fetch, namespace, start, end, step):
    queries = []
    for panel in dashboard["panels"]:
        for target in panel["targets"]:
            expr = target["expr"].replace("${namespace}", namespace)
            if "$" in expr:
                raise ValueError(f"Unresolved Grafana variable in {expr}")
            label = f"{panel['id']}/{target['refId']} {panel['title']}"
            try:
                rows, warnings = fetch(expr, start, end, step)
                series = [series_stats(row) for row in rows]
            except (OSError, TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"Query {label} failed: {exc}") from exc
            queries.append(
                {
                    "panel_id": panel["id"],
                    "title": panel["title"],
                    "ref_id": target["refId"],
                    "expr": expr,
                    "warning_count": warnings,
                    "finite_samples": sum(s["finite_samples"] for s in series),
                    "series": series,
                }
            )
    return {
        "dashboard_uid": dashboard["uid"],
        "namespace": namespace,
        "start": start,
        "end": end,
        "step_seconds": step,
        "queries": queries,
    }


def write_new(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent) as tmp:
        json.dump(report, tmp, indent=2, ensure_ascii=False, allow_nan=False)
        tmp.write("\n")
        tmp.flush()
        os.link(tmp.name, path)  # Publish a complete file without overwriting an earlier report.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prometheus-url", required=True, help="Prometheus base URL or route prefix"
    )
    parser.add_argument("--start", required=True, type=instant)
    parser.add_argument("--end", required=True, type=instant)
    parser.add_argument("--namespace", choices=NAMESPACES, default=NAMESPACES[0])
    parser.add_argument("--step", type=int, default=10, help="query step in seconds")
    parser.add_argument(
        "--output", type=Path, help="new JSON file; defaults to reports/capacity-metrics-<UTC>.json"
    )
    args = parser.parse_args(argv)
    parts = urlsplit(args.prometheus_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        parser.error(
            "Supply the HTTP(S) address of the lab Prometheus without credentials or query"
        )
    if args.start >= args.end or args.step <= 0:
        parser.error("--end must follow --start and --step must be positive")
    utc_now = datetime.now(timezone.utc)  # noqa: UP017 -- host Python 3.9
    output = args.output or Path(
        "reports", utc_now.strftime("capacity-metrics-%Y%m%dT%H%M%S%fZ.json")
    )
    if output.exists():
        parser.error("Report exists; select a new --output filename")

    def fetch(expr, start, end, step):
        return query_range(args.prometheus_url, expr, start, end, step)

    try:
        report = collect(
            json.loads(DASHBOARD.read_text(encoding="utf-8")),
            fetch,
            args.namespace,
            args.start.isoformat(),
            args.end.isoformat(),
            args.step,
        )
        write_new(output, report)
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{len(report['queries'])} queries: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
