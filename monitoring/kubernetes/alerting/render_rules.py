"""Render the canonical rule JSON as a release-selected PrometheusRule."""

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def manifest(run_id=None):
    if run_id is not None and not re.fullmatch(r"[a-z0-9]{1,32}", run_id):
        raise ValueError("run ID must contain 1..32 lowercase letters or digits")
    source = "synthetic.rules.json" if run_id else "rules.json"
    rules = json.loads((ROOT / source).read_text())
    if run_id:
        for group in rules["groups"]:
            for rule in group["rules"]:
                rule["labels"]["run_id"] = run_id
    return {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
        "metadata": {
            "name": "sre-lab-synthetic-" + run_id if run_id else "sre-k8s-optional-alerts",
            "namespace": "sre-monitoring",
            "labels": {"release": "sre-k8s", "app.kubernetes.io/part-of": "sre-lab-alerting"},
        },
        "spec": rules,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-run", help="explicitly render an ALWAYS FIRING test rule")
    args = parser.parse_args()
    print(json.dumps(manifest(args.synthetic_run), indent=2))
