"""SLI calculations and deliberately allowlisted, secret-free report exports."""

import html
import math
import re
from datetime import UTC, datetime


def _finite(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")  # noqa: TRY004
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return float(value)


def summarize_slo(
    status_counts, target=0.99, duration_seconds=None, p95_seconds=None,
    p95_method="not_available",
):
    """Request-based availability: every completed product request is in the denominator.

    429, 4xx and 5xx are failures. Network failures outside the API are not measured.
    Counts must cover the same interval; the caller owns window/counter-reset handling.
    """
    target = _finite(target, "target")
    if not 0 < target < 1:
        raise ValueError("target must be strictly between 0 and 1")
    if not isinstance(status_counts, dict):
        raise ValueError("status_counts must be a mapping of HTTP status to counts")  # noqa: TRY004
    counts = {}
    for status, count in status_counts.items():
        code = str(status)
        if not re.fullmatch(r"[1-5][0-9]{2}", code):
            raise ValueError("status must be a three-digit HTTP status code")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("counts must be nonnegative integers")
        if code in counts:
            raise ValueError("duplicate normalized status code")
        counts[code] = count
    if duration_seconds is not None:
        duration_seconds = _finite(duration_seconds, "duration_seconds")
        if not duration_seconds:
            raise ValueError("duration_seconds must be positive")
    if p95_seconds is None:
        p95_method = "not_available"
    else:
        p95_seconds = _finite(p95_seconds, "p95_seconds")
        if p95_method not in {
            "histogram_estimate", "prometheus_histogram_estimate", "client_nearest_rank", "exact",
        }:
            raise ValueError("p95_method must identify the measurement method")
    total = sum(counts.values())
    successes = sum(count for status, count in counts.items() if status.startswith("2"))
    failures = total - successes
    allowance = total * (1 - target) if total else None
    spent = failures / allowance if allowance is not None else None
    if spent is not None and math.isclose(spent, 1.0, rel_tol=1e-12):
        spent = 1.0
    ratio = successes / total if total else None
    return {
        "definition": "2xx / all completed product HTTP requests; includes 429 as failure",
        "target": target, "status_counts": dict(sorted(counts.items())),
        "total_requests": total, "successful_requests": successes, "failed_requests": failures,
        "success_ratio": ratio, "target_met": ratio >= target if total else None,
        "allowed_failed_requests": allowance,
        "error_budget_consumed_ratio": spent,
        "error_budget_remaining_ratio": max(0.0, 1 - spent) if total else None,
        "error_budget_overrun_ratio": max(0.0, spent - 1) if total else None,
        "duration_seconds": duration_seconds,
        "actual_rps": total / duration_seconds if total and duration_seconds else None,
        "p95_seconds": p95_seconds if total else None,
        "p95_method": p95_method if total else "not_available",
        "has_data": bool(total),
    }


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be ISO-8601 text with timezone")  # noqa: TRY004
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return parsed.astimezone(UTC).isoformat()


def _safe_events(events):
    """Only structured IDs/actions/time; never export raw details, errors or credentials."""
    if not isinstance(events, (list, tuple)):
        raise ValueError("events must be a list")  # noqa: TRY004
    output = []
    for event in events[-200:]:
        if not isinstance(event, dict):
            continue
        action = event.get("action", "unknown")
        if not isinstance(action, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", action):
            action = "unrecognized"
        if any(word in action for word in ("token", "password", "secret", "authorization")):
            action = "redacted"
        row = {"action": action}
        event_id = event.get("event_id")
        if isinstance(event_id, str) and re.fullmatch(r"[a-fA-F0-9-]{8,64}", event_id):
            row["event_id"] = event_id
        try:
            row["timestamp"] = _timestamp(event.get("timestamp"))
        except ValueError:
            row["timestamp"] = None
        output.append(row)
    return output


def build_report(snapshot, events, scope="simulation", target=0.99):
    """Export only allowlisted values. No raw state, environment, headers or free text.

    Snapshot keys: status_counts, duration_seconds, p95_seconds, p95_method,
    window_start, window_end. Without both boundaries, scope is process-lifetime.
    """
    if scope not in {"simulation", "component", "full_stack"}:
        raise ValueError("scope must be simulation, component, or full_stack")
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be a mapping")  # noqa: TRY004
    start = snapshot.get("window_start", snapshot.get("started_at"))
    end = snapshot.get("window_end", snapshot.get("ended_at"))
    window = {"kind": "process_lifetime", "start": None, "end": None}
    if start is not None or end is not None:
        start, end = _timestamp(start), _timestamp(end)
        interval = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        if interval <= 0:
            raise ValueError("window_end must be after window_start")
        window = {"kind": "provided_interval", "start": start, "end": end}
        duration = snapshot.get("duration_seconds", interval)
        if not math.isclose(_finite(duration, "duration_seconds"), interval, abs_tol=0.1):
            raise ValueError("duration_seconds disagrees with window boundaries")
    else:
        duration = snapshot.get("duration_seconds")
    return {
        "schema_version": 1, "generated_at": datetime.now(UTC).isoformat(),
        "validation_scope": scope, "window": window,
        "slo": summarize_slo(
            snapshot.get("status_counts", {}), target, duration,
            snapshot.get("p95_seconds"), snapshot.get("p95_method", "not_available"),
        ),
        "events": _safe_events(events),
        "limitations": [
            "Counts measure completed API requests, not client network failures.",
            "A process restart resets lifetime counters; do not compare different processes.",
            "Events are bounded in-memory observations, not a persistent audit database.",
            "Scope is caller-declared; simulation is not proof of real recovery or deployment.",
        ],
    }


def _cell(value):
    # Always escape HTML, Markdown table delimiters and newlines, including hand-built reports.
    return html.escape(str(value), quote=True).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")


def render_markdown(report):
    """Render only the known report fields, never arbitrary dictionary contents."""
    slo = report["slo"]
    ratio = slo["success_ratio"]
    budget = slo["error_budget_remaining_ratio"]
    p95 = slo["p95_seconds"]
    rows = [
        "# SRE 演练报告", "",
        f"验证范围：{_cell(report['validation_scope'])}。模拟结果不等于真实部署或自愈。",
        f"生成时间：{_cell(report['generated_at'])}",
        f"统计范围：{_cell(report['window']['kind'])}", "",
        "| 指标 | 结果 |", "| --- | --- |",
        f"| 完成请求数 | {slo['total_requests']} |",
        f"| 成功请求数（2xx） | {slo['successful_requests']} |",
        f"| 失败请求数（含 429） | {slo['failed_requests']} |",
        f"| 目标 | {slo['target']:.2%} |",
        f"| 成功率 | {'无数据' if ratio is None else format(ratio, '.2%')} |",
        f"| 错误预算剩余 | {'无数据' if budget is None else format(budget, '.2%')} |",
        f"| P95（秒） | {'未测量' if p95 is None else p95} |",
        f"| P95 口径 | {_cell(slo['p95_method'])} |", "",
        "## 操作时间线（最近最多 200 条）", "",
        "| 时间 | 动作 | 事件 ID |", "| --- | --- | --- |",
    ]
    for event in report["events"]:
        rows.append("| " + " | ".join(_cell(event.get(key) or "未知") for key in (
            "timestamp", "action", "event_id",
        )) + " |")
    rows.extend(["", "## 边界", *[f"- {_cell(item)}" for item in report["limitations"]]])
    return "\n".join(rows) + "\n"
