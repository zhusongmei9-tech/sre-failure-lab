import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import export_capacity_metrics as export


def test_summarizes_finite_samples_only_and_preserves_zero_and_public_labels():
    raw = {
        "metric": {
            "pod": "product-api-x",
            "container": "api",
            "command": "get",
            "instance": "10.1.2.3:9100",
            "node": "private",
            "hostname": "private",
        },
        "values": [[1, "1"], [2, "NaN"], [3, "3"], [4, "+Inf"], [5, None], [6, "0"]],
    }
    result = export.series_stats(raw)
    assert result == {
        "labels": {"pod": "product-api-x", "container": "api", "command": "get"},
        "samples": 6,
        "finite_samples": 3,
        "min": 0,
        "mean": 4 / 3,
        "max": 3,
    }
    raw["values"] = [[1, "NaN"], [2, "-Inf"]]
    missing = export.series_stats(raw)
    assert missing["finite_samples"] == 0
    assert missing["min"] is missing["max"] is missing["mean"] is None


def test_real_dashboard_queries_every_target_and_preserves_missing_data(tmp_path, monkeypatch):
    dashboard = json.loads(export.DASHBOARD.read_text())
    seen = []

    def respond(url, expr, start, end, step):
        seen.append((expr, start, end, step))
        return [], 0

    monkeypatch.setattr(export, "query_range", respond)
    target = tmp_path / "nested" / "windows.json"
    args = [
        "--prometheus-url",
        "http://127.0.0.1:19090",
        "--namespace",
        "sre-app-lab",
        "--start",
        "2026-11-20T23:00:00+08:00",
        "--end",
        "2026-11-20T15:10:00Z",
        "--output",
        str(target),
    ]
    assert export.main(args) == 0
    report = json.loads(target.read_text())
    expected = [
        (panel["id"], target["refId"], target["expr"].replace("${namespace}", "sre-app-lab"))
        for panel in dashboard["panels"]
        for target in panel["targets"]
    ]
    assert [(q["panel_id"], q["ref_id"], q["expr"]) for q in report["queries"]] == expected
    assert [r[0] for r in seen] == [r[2] for r in expected]
    assert all(
        r[1:] == ("2026-11-20T15:00:00+00:00", "2026-11-20T15:10:00+00:00", 10) for r in seen
    )
    assert all(q["series"] == [] and q["finite_samples"] == 0 for q in report["queries"])
    with pytest.raises(SystemExit):
        export.main(args)
    assert json.loads(target.read_text()) == report


def test_api_transport_encodes_promql_and_rejects_application_errors(monkeypatch):
    replies = [
        {"status": "success", "data": {"resultType": "matrix", "result": []}, "warnings": ["w"]},
        {"status": "error", "error": "bad query", "errorType": "bad_data"},
        {"status": "success", "data": {"resultType": "vector", "result": []}},
    ]
    expr = 'sum by (pod) (example{foo="1+2"})'

    class Opener:
        def open(self, url, timeout):
            assert urlsplit(url).path == "/prom/api/v1/query_range"
            assert parse_qs(urlsplit(url).query) == {
                "query": [expr],
                "start": ["s"],
                "end": ["e"],
                "step": ["10"],
            }
            return io.BytesIO(json.dumps(replies.pop(0)).encode())

    monkeypatch.setattr(export, "build_opener", lambda *args: Opener())
    args = ("http://localhost/prom/", expr, "s", "e", 10)
    assert export.query_range(*args) == ([], 1)
    with pytest.raises(ValueError, match="bad_data.*bad query"):
        export.query_range(*args)
    with pytest.raises(ValueError, match="matrix"):
        export.query_range(*args)


def test_failing_query_exits_with_panel_identity_and_no_partial_file(monkeypatch, tmp_path, capsys):
    out = tmp_path / "error.json"

    def fail(*args):
        raise ValueError("timeout")

    monkeypatch.setattr(export, "query_range", fail)
    assert (
        export.main(
            [
                "--prometheus-url",
                "http://127.0.0.1:19090",
                "--start",
                "2026-11-20T15:00:00Z",
                "--end",
                "2026-11-20T15:10:00Z",
                "--output",
                str(out),
            ]
        )
        == 1
    )
    assert "Query 1/A" in capsys.readouterr().err and not out.exists()


@pytest.mark.parametrize(
    "flags",
    [
        ["--namespace", "other"],
        ["--start", "2026-11-20T15:00:00"],
        ["--end", "2026-11-20T14:00:00Z"],
        ["--step", "0"],
    ],
)
def test_invalid_window_and_namespace_rejected(flags):
    with pytest.raises(SystemExit):
        export.main(
            [
                "--prometheus-url",
                "http://localhost",
                "--start",
                "2026-11-20T15:00:00Z",
                "--end",
                "2026-11-20T15:10:00Z",
                *flags,
            ]
        )
