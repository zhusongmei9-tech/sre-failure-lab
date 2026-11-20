"""Verify the routing drill's live Kubernetes objects and print the Service UID."""

import hashlib
import json
import sys
import uuid

LABELS = {
    "app.kubernetes.io/part-of": "sre-failure-lab",
    "app.kubernetes.io/managed-by": "sre-failure-lab",
    "sre-lab-role": "demo",
}
# SHA256 of each sorted, compact JSON object from v0.4.0:monitoring/kubernetes/demo.yaml.
LEGACY = {
    "Namespace": "cc8a3f6c6d951c7b512cf612a56c262f0055be791c669810d49028629381f1aa",
    "Deployment": "e698a891b3fd4d5cdb018a1b1a1c05cf29b9e0e26ba7a94c08fa68da53b5b479",
    "Service": "c2f953fed88a7fc61300c2183ccae98b1a567493558b6f1113bec5b4431b9f00",
}


def contains_applied(live, desired):
    """Ignore additional API-defaulted keys, but compare every declared list and scalar."""
    if isinstance(desired, dict):
        return isinstance(live, dict) and all(
            key in live and contains_applied(live[key], value) for key, value in desired.items()
        )
    if isinstance(desired, list):
        return (isinstance(live, list) and len(live) == len(desired)
                and all(contains_applied(a, b) for a, b in zip(live, desired)))
    return type(live) is type(desired) and live == desired


def matches_legacy(obj):
    meta = obj["metadata"]
    if LABELS.keys() & meta.get("labels", {}).keys():
        return False
    applied = json.loads(meta.get("annotations", {}).get(
        "kubectl.kubernetes.io/last-applied-configuration", "null"))
    if not isinstance(applied, dict):
        return False
    if applied["metadata"].get("annotations") == {}:
        del applied["metadata"]["annotations"]  # inserted by kubectl apply
    encoded = json.dumps(applied, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest() == LEGACY[obj["kind"]] and contains_applied(
        obj, applied
    )


def service_identity(items):
    objects = {x["kind"]: x for x in items}
    ns, deploy, svc = (objects[k] for k in ("Namespace", "Deployment", "Service"))
    if any(x["metadata"].get("deletionTimestamp") or x["metadata"].get("ownerReferences")
           for x in (ns, deploy, svc)):
        raise ValueError("A demo object is terminating or has a controller owner")
    labelled = all(LABELS.items() <= x["metadata"].get("labels", {}).items() for x in (ns, deploy))
    if not labelled and not all(matches_legacy(x) for x in (ns, deploy, svc)):
        raise ValueError("Expected labelled lab objects or the verified v0.4.0 configuration")
    if (deploy["spec"]["replicas"] != 1 or deploy.get("status", {}).get("readyReplicas") != 1
            or deploy["spec"]["selector"] != {"matchLabels": {"app": "demo-web"}}):
        raise ValueError("Expected the Ready single-replica demo Deployment")
    spec = svc["spec"]
    if (spec["type"] != "ClusterIP" or spec.get("externalIPs")
            or spec.get("clusterIP") in ("None", None)
            or spec.get("selector") != {"app": "demo-web"} or len(spec["ports"]) != 1
            or spec["ports"][0].get("port") != 8080 or spec["ports"][0].get("targetPort") != "http"):
        raise ValueError("Unexpected demo Service selector, exposure or ports")
    return str(uuid.UUID(svc["metadata"]["uid"]))


if __name__ == "__main__":
    try:
        print(service_identity(json.load(sys.stdin)["items"]))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        sys.exit(f"Routing drill preflight failed: {exc}")
