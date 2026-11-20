"""Render the isolated GitOps exercise from the canonical Kubernetes demo."""

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

GITOPS = Path(__file__).resolve().parent
NAMESPACE = "sre-k8s-demo"


def workload():
    import yaml

    source = GITOPS.parent / "monitoring/kubernetes/demo.yaml"
    with source.open() as manifests:
        resources = [
            doc
            for doc in yaml.safe_load_all(manifests)
            if doc["kind"] in ("Deployment", "Service")
            and doc["metadata"].get("namespace") == NAMESPACE
            and doc["metadata"]["name"] == "demo-web"
        ]
    if sorted((r["apiVersion"], r["kind"]) for r in resources) != [
        ("apps/v1", "Deployment"),
        ("v1", "Service"),
    ]:
        raise ValueError("Expected exactly the demo-web Deployment and Service")
    return yaml.safe_dump_all(resources, sort_keys=False)


def application(repo_url):
    parsed = urlsplit(repo_url)
    if (
        parsed.scheme not in {"https", "ssh", "http"}
        or not parsed.hostname
        or not parsed.path.strip("/")
        or parsed.password is not None
        or (parsed.scheme != "ssh" and parsed.username is not None)
        or parsed.query
        or parsed.fragment
        or any(c.isspace() or c in "*?[]!" for c in repo_url)
    ):
        raise ValueError("Use an exact HTTPS, SSH or HTTP repository URL without embedded secrets")
    template = (GITOPS / "application.yaml.in").read_text()
    return template.replace("LAB_REPOSITORY_URL", json.dumps(repo_url))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("workload", help="write workload/demo.yaml to stdout (requires PyYAML)")
    modes.add_parser(
        "application", help="write the AppProject and Application to stdout"
    ).add_argument("repo_url")
    args = parser.parse_args()
    try:
        sys.stdout.write(workload() if args.mode == "workload" else application(args.repo_url))
    except (ImportError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
