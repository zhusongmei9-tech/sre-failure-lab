"""Exercise installer retry and ownership boundaries without Kubernetes."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
K3S = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
state = Path(os.environ["STATE"])
current = json.loads(state.read_text())
desired = json.loads(Path(os.environ["DESIRED"]).read_text())
if args[0] == "ctr":
    print("docker.io/library/sre-lab-api:0.3.0\ndocker.io/library/sre-lab-api:0.3.1")
elif "--raw=/apis/metrics.k8s.io/v1beta1/nodes" in args:
    print(json.dumps({"items": [1]}))
elif "--dry-run=client" in args:
    for doc in desired:
        if doc["kind"] != "ServiceMonitor" or any(s.endswith("product-monitor.yaml") for s in args):
            print(json.dumps(doc))
elif "--ignore-not-found" in args:
    docs = [d for d in current if d["kind"] != "ServiceMonitor" or any(s.endswith("product-monitor.yaml") for s in args)]
    print(json.dumps({"kind":"List","items":docs}))
elif "--save-config" in args:
    docs = json.loads(Path(args[args.index("-f")+1]).read_text())["items"]
    for doc in docs:
        assert not any((d["kind"],d["metadata"]["name"]) == (doc["kind"],doc["metadata"]["name"]) for d in current)
        current.append(doc)
    state.write_text(json.dumps(current))
elif "service/product-api" in args:
    print(json.dumps({"spec":{"type":"ClusterIP","selector":{"app":"product-api"},"clusterIP":"10.43.0.12"}}))
elif "namespace/sre-monitoring" not in args and "rollout" not in args:
    sys.exit("unexpected test call "+str(args))
"""
CURL = r"""#!/usr/bin/env python3
import sys
urls = {"health/ready": '{"status":"ready","redis":"ok"}', "products/1": '{"id":1}', "version": '{"version":"0.3.1"}'}
print(urls[sys.argv[-1].split("http://10.43.0.12:8000/")[1]])
"""


@pytest.fixture
def cluster(tmp_path):
    for name, source in {
        "k3s": K3S,
        "curl": CURL,
        "flock": "#!/bin/sh\nexit 0\n",
        "timeout": '#!/bin/sh\nshift\nexec "$@"\n',
    }.items():
        target = tmp_path / name
        target.write_text(source)
        target.chmod(0o755)
    base = ROOT / "monitoring/kubernetes"
    files = [
        *(base / "app-lab").glob("*.yaml"),
        base / "app-lab-observability/product-monitor.yaml",
    ]
    desired = [obj for path in files for obj in yaml.safe_load_all(path.read_text())]
    (tmp_path / "desired.json").write_text(json.dumps(desired))
    state = tmp_path / "state.json"
    state.write_text("[]")
    env = dict(
        os.environ,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        STATE=str(state),
        DESIRED=str(tmp_path / "desired.json"),
    )
    return state, env


def install(env, *args):
    return subprocess.run(
        ["bash", str(ROOT / "scripts/install_k8s_app_lab.sh"), *args],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def test_retry_preserves_active_release_and_hpa_replica_count(cluster):
    state, env = cluster
    first = install(env)
    assert first.returncode == 0, first.stderr
    objects = json.loads(state.read_text())
    assert not any(o["kind"] == "ServiceMonitor" for o in objects)
    live = next(
        o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "product-api"
    )
    live["spec"]["replicas"] = 2
    live["spec"]["template"]["spec"]["containers"][0]["image"] = "sre-lab-api:0.3.1"
    state.write_text(json.dumps(objects))
    repeat = install(env, "--monitoring")
    assert repeat.returncode == 0, repeat.stderr
    after = json.loads(state.read_text())
    assert [o for o in after if o["kind"] != "ServiceMonitor"] == objects
    assert sum(o["kind"] == "ServiceMonitor" for o in after) == 1


def test_foreign_namespace_prevents_all_creates(cluster):
    state, env = cluster
    foreign = [{"kind": "Namespace", "metadata": {"name": "sre-app-lab"}}]
    state.write_text(json.dumps(foreign))
    result = install(env)
    assert result.returncode != 0 and "Refusing to adopt" in result.stderr
    assert json.loads(state.read_text()) == foreign
