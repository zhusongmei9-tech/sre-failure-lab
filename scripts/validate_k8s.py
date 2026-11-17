"""Render and check Kubernetes configurations without contacting a cluster.

Requires the locked dependencies, Helm 4.3.2, promtool 3.16.0, amtool 0.35.0 and
an unmodified kube-prometheus-stack 93.3.1 chart archive. Never runs kubectl.
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART_SHA = "a7fbf983aadc20548c167c40ba2e78fecbf03753830911130419a16064c91aff"


def run(command, env, cwd=ROOT):
    result = subprocess.run(
        [str(arg) for arg in command], capture_output=True, text=True,
        timeout=180, env=env, cwd=cwd, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed: {(result.stdout + result.stderr)[-6000:]}")
    return result.stdout


def require(condition, message):
    if not condition:
        raise ValueError(message)


def only(documents, kind):
    items = [item for item in documents if item.get("kind") == kind]
    require(len(items) == 1, f"Expected exactly one {kind}, got {len(items)}")
    return items[0]


def check_profile(documents, alerting):
    prometheus = only(documents, "Prometheus")
    spec = prometheus["spec"]
    require(spec["replicas"] == 1, "Unexpected Prometheus replica count")
    require(spec["ruleSelector"]["matchLabels"]["release"] == "sre-k8s",
            "Prometheus would not select the lab rules")
    require(spec["serviceMonitorSelector"]["matchLabels"]["release"] == "sre-k8s",
            "Prometheus would not select lab ServiceMonitors")
    for obj in documents:
        if obj.get("kind") == "Service":
            require(obj.get("spec", {}).get("type", "ClusterIP") == "ClusterIP",
                    f"Unexpected externally exposed Service: {obj['metadata']['name']}")
    managers = [obj for obj in documents if obj.get("kind") == "Alertmanager"]
    require(len(managers) == int(alerting), "Alertmanager must only exist in the optional profile")
    if alerting:
        require(managers[0]["spec"]["replicas"] == 1, "Unexpected Alertmanager replicas")
        endpoints = spec.get("alerting", {}).get("alertmanagers", [])
        require(any(x.get("namespace") == "sre-monitoring" for x in endpoints),
                "Prometheus does not route alerts to the monitoring namespace")


def validate(args, report):
    chart = Path(args.chart).resolve()
    require(hashlib.sha256(chart.read_bytes()).hexdigest() == CHART_SHA,
            "Chart SHA256 does not match the reviewed version 93.3.1")
    with tempfile.TemporaryDirectory(prefix="sre-k8s-config-") as folder:
        temporary = Path(folder)
        # Ignore any ambient credentials; all Helm operations are client-side.
        kubeconfig = temporary / "empty-kubeconfig"
        kubeconfig.write_text("apiVersion: v1\nkind: Config\nclusters: []\ncontexts: []\nusers: []\n")
        kubeconfig.chmod(0o600)
        env = dict(os.environ, KUBECONFIG=str(kubeconfig))
        helm_version = run([args.helm, "version", "--template", "{{.Version}}"], env).strip()
        require(helm_version == "v4.3.2", "Expected Helm v4.3.2")
        promtool_version = run([args.promtool, "--version"], env).splitlines()[0]
        require("version 3.16.0 " in promtool_version, "Expected promtool 3.16.0")
        amtool_version = run([args.amtool, "--version"], env).splitlines()[0]
        require("version 0.35.0 " in amtool_version, "Expected amtool 0.35.0")
        report["tools"] = {"amtool": amtool_version, "helm": helm_version, "promtool": promtool_version,
                           "chart": "93.3.1", "chart_sha256": CHART_SHA}
        base = ROOT / "monitoring/kubernetes/values.yaml"
        overlay = ROOT / "monitoring/kubernetes/alerting/values.yaml"
        for profile, values in (("base", [base]), ("alerting", [base, overlay])):
            flags = [arg for value in values for arg in ("--values", str(value))]
            run([args.helm, "lint", chart, "--kube-version", "1.37.2", *flags], env)
            rendered = run([args.helm, "template", "sre-k8s", chart,
                            "--namespace", "sre-monitoring", "--kube-version", "1.37.2",
                            *flags], env)
            documents = [d for d in yaml.safe_load_all(rendered) if d]
            check_profile(documents, profile == "alerting")
            if profile == "alerting":
                secrets = [obj for obj in documents if obj.get("kind") == "Secret"
                           and "alertmanager.yaml" in obj.get("data", {})]
                require(len(secrets) == 1, "Expected one rendered Alertmanager config")
                config_path = temporary / "alertmanager.yaml"
                config_path.write_bytes(base64.b64decode(secrets[0]["data"]["alertmanager.yaml"]))
                run([args.amtool, "check-config", config_path], env)
                for labels, receiver in ((["module=sre-lab-optional"], "lab-webhook"),
                                         (["alertname=UnrelatedAlert"], "discard")):
                    run([args.amtool, "config", "routes", "test",
                         "--config.file", config_path, "--verify.receivers", receiver, *labels], env)
                report["alertmanager_config_and_routes"] = "passed"
            groups = [group for obj in documents if obj.get("kind") == "PrometheusRule"
                      for group in obj["spec"]["groups"]]
            rules_path = temporary / f"{profile}-rules.json"
            rules_path.write_text(json.dumps({"groups": groups}))
            run([args.promtool, "check", "rules", rules_path], env)
            report["profiles"][profile] = {"rendered_objects": len(documents),
                                            "rule_groups": len(groups), "status": "passed"}
        # The optional rules are applied separately; verify their selector and exact source.
        alert_dir = ROOT / "monitoring/kubernetes/alerting"
        rule = yaml.safe_load(run([sys.executable, alert_dir / "render_rules.py"], env))
        require(rule["kind"] == "PrometheusRule", "Unexpected custom rule resource kind")
        require(rule["metadata"]["namespace"] == "sre-monitoring", "Wrong rule namespace")
        require(rule["metadata"]["labels"]["release"] == "sre-k8s", "Wrong rule release label")
        require(rule["spec"] == json.loads((alert_dir / "rules.json").read_text()),
                "Rendered custom rules differ from the tested rules")
        run([args.promtool, "check", "rules", alert_dir / "rules.json"], env)
        output = run([args.promtool, "test", "rules", "rules.test.yml"], env, cwd=alert_dir)
        report["custom_rules"] = {"status": "passed", "output": output.strip()}
        # Keep existing business alert/SLO tests in the same no-cluster gate.
        for filename in ("rules.test.yml", "slo.test.yml"):
            run([args.promtool, "test", "rules", filename], env,
                cwd=ROOT / "monitoring/prometheus")
        report["existing_business_rules"] = "passed"
        for filename in (ROOT / "monitoring/kubernetes/demo.yaml", alert_dir / "receiver.yaml"):
            objects = [d for d in yaml.safe_load_all(filename.read_text()) if d]
            require(all(obj.get("kind") and obj.get("apiVersion") for obj in objects),
                    f"Missing Kubernetes object metadata in {filename.name}")
            report["manifests"][str(filename.relative_to(ROOT))] = len(objects)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", required=True, help="Verified local chart .tgz")
    parser.add_argument("--helm", default="helm")
    parser.add_argument("--promtool", default="promtool")
    parser.add_argument("--amtool", default="amtool")
    parser.add_argument("--output", default="reports/kubernetes-config-validation.json")
    args = parser.parse_args(argv)
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "configuration_only", "live_cluster": "not_tested", "status": "failed",
        "profiles": {}, "manifests": {},
    }
    try:
        validate(args, report)
        report["status"] = "passed"
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError,
            subprocess.TimeoutExpired, yaml.YAMLError) as exc:
        report["error"] = str(exc)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
