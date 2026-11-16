from scripts.simulate import run


def test_rehearsal_runs_without_deploying():
    report, markdown, summary = run()
    assert summary["real_actions_executed"] is False
    assert report["slo"]["success_ratio"] == 4 / 6
    assert "component" in markdown
    assert report["events"]
