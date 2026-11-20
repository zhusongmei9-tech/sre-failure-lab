"""Refresh the public source asset from the passing main branch; keep Git tags fixed."""

import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

REPO = "zhusongmei9-tech/sre-failure-lab"
TAG = "v0.6.0"
TAG_COMMIT = "b6c416a2cb3cee62c4bfa35c7e813f1bf9995113"
NAME = f"sre-faliure-lab-{TAG}.tar.gz"
STAGE = "sre-main-source.staging.tar.gz"
BACKUP = "sre-main-source.previous.tar.gz"
LABEL = "Managed main source refresh"
PREFIX = f"sre-failure-lab-{TAG}/"
MARKER = "<!-- rolling-main-source -->"
TITLE = "SRE 故障演练实验室 · 源码与复现教程"
TUTORIAL = "docs/from-zero/README.md"
GUIDES = {
    TUTORIAL,
    "docs/from-zero/01-linux-compose.md",
    "docs/from-zero/02-k3s-monitoring.md",
    "docs/from-zero/03-product-rollout-hpa.md",
    "docs/from-zero/04-operations.md",
    "docs/from-zero/05-capacity.md",
}
BASE = f"repos/{REPO}"
SOURCE_DIRS = {
    ".github",
    "ansible",
    "app",
    "docs",
    "gitops",
    "loadgen",
    "monitoring",
    "ops",
    "scripts",
    "tests",
}
SOURCE_FILES = {
    ".dockerignore",
    ".env.example",
    ".gitignore",
    "Dockerfile",
    "Makefile",
    "compose.yaml",
    "compose.resources.yaml",
    "compose.runtime.yaml",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
}
PRIVATE_PATTERN = re.compile(
    rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|"
    rb"github_pat_[a-zA-Z0-9_]{30,}|gh[pousr]_[a-zA-Z0-9]{30,}"
)


def run(*args):
    return subprocess.check_output(args)


def api(path, *args):
    output = run("gh", "api", path, *args)
    return json.loads(output) if output.strip() else None


def pack(source):
    prefix = PREFIX.encode()
    tree = run("git", "ls-tree", "-rz", source).split(b"\0")
    files = set()
    for entry in filter(None, tree):
        mode_type_sha, raw_name = entry.split(b"\t", 1)
        path = Path(raw_name.decode())
        if not (
            path.parts[0] in SOURCE_DIRS
            or str(path) in SOURCE_FILES
            or (len(path.parts) == 1 and path.suffix == ".md")
        ):
            raise ValueError(f"Review new source path before packaging: {path}")
        if (
            mode_type_sha.split()[0] not in {b"100644", b"100755"}
            or set(path.parts) & {"private", "runtime", "reports", "dist", ".venv", ".ssh"}
            or (path.name.startswith(".env") and str(path) != ".env.example")
            or path.name.endswith((".pem", ".key", ".kubeconfig", ".local.yml", ".tar.gz", ".zip"))
        ):
            raise ValueError(f"Disallowed source path: {path}")
        files.add((prefix + raw_name).decode())
    tar = run("git", "archive", "--format=tar", f"--prefix={PREFIX}", source)
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        members = [m for m in archive if not m.isdir()]
        if {m.name for m in members} != files or not {PREFIX + guide for guide in GUIDES} <= files:
            raise ValueError("Archive differs from tracked source or lacks the tutorial")
        for member in members:
            if not member.isfile() or PRIVATE_PATTERN.search(archive.extractfile(member).read()):
                raise ValueError(f"Disallowed archive content: {member.name}")
    return gzip.compress(tar, mtime=0)


def current_head():
    return api(f"{BASE}/git/ref/heads/main")["object"]["sha"]


def check_authority(source):
    if (
        os.environ["GITHUB_REPOSITORY"] != REPO
        or os.environ["GITHUB_REF"] != "refs/heads/main"
        or not re.fullmatch(r"[0-9a-f]{40}", source)
        or run("git", "rev-parse", "HEAD").decode().strip() != source
    ):
        raise RuntimeError("Only the checked-out main source in the project repository may publish")
    run("git", "merge-base", "--is-ancestor", TAG_COMMIT, source)
    tag = api(f"{BASE}/git/ref/tags/{TAG}")["object"]
    if tag["type"] != "commit" or tag["sha"] != TAG_COMMIT:
        raise RuntimeError("Historical tag changed")
    runs = api(
        f"{BASE}/actions/workflows/checks.yml/runs?event=push&branch=main"
        f"&head_sha={source}&per_page=1"
    )["workflow_runs"]
    if not runs or not (
        runs[0]["event"] == "push"
        and runs[0]["head_branch"] == "main"
        and runs[0]["head_sha"] == source
        and runs[0]["head_repository"]["full_name"] == REPO
        and runs[0]["path"] == ".github/workflows/checks.yml"
        and runs[0]["conclusion"] == "success"
    ):
        raise RuntimeError("Expected successful main push CI for this exact source commit")


class Release:
    def __init__(self):
        item = api(f"{BASE}/releases/tags/{TAG}")
        body = item["body"] or ""
        if (
            item["tag_name"] != TAG
            or item["draft"]
            or item["prerelease"]
            or item["immutable"]
            or not (MARKER in body or f"<!-- source-release: {TAG_COMMIT} -->" in body)
        ):
            raise RuntimeError("Expected the mutable published project release")
        self.path = f"{BASE}/releases/{item['id']}"

    def assets(self):
        items = api(f"{self.path}/assets?per_page=100")
        for a in items:
            if a["name"] != NAME and (a["name"] not in {STAGE, BACKUP} or a["label"] != LABEL):
                raise RuntimeError("Unexpected release attachment; leaving it untouched")
        return {a["name"]: a for a in items}

    def verified_digest(self, asset):
        if asset["state"] != "uploaded" or asset["size"] <= 0:
            raise RuntimeError("Release asset is incomplete")
        content = run(
            "gh",
            "api",
            f"{BASE}/releases/assets/{asset['id']}",
            "-H",
            "Accept: application/octet-stream",
        )
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != asset["size"] or asset["digest"] != "sha256:" + digest:
            raise RuntimeError("Downloaded asset differs from GitHub's metadata")
        return digest

    def rename(self, asset, name):
        return api(
            f"{BASE}/releases/assets/{asset['id']}",
            "-X",
            "PATCH",
            "-f",
            f"name={name}",
            "-f",
            "label=" if name == NAME else f"label={LABEL}",
        )

    def upload(self, archive):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp, NAME)
            file.write_bytes(archive)
            return api(
                f"https://uploads.github.com/{self.path}/assets?name={STAGE}"
                "&label=Managed%20main%20source%20refresh",
                "-X",
                "POST",
                "-H",
                "Content-Type: application/gzip",
                "--input",
                str(file),
            )

    def delete(self, asset):
        if asset["name"] not in {STAGE, BACKUP} or asset["label"] != LABEL:
            raise ValueError("Only temporary managed assets can be deleted")
        api(f"{BASE}/releases/assets/{asset['id']}", "-X", "DELETE")

    def notes(self, body):
        item = api(self.path)
        if item["body"] != body or item["name"] != TITLE:
            result = api(self.path, "-X", "PATCH", "-f", f"name={TITLE}", "-f", f"body={body}")
            if result["body"] != body or result["name"] != TITLE:
                raise RuntimeError("Release notes update failed verification")


def sync(release, archive, source, date, head=current_head):
    """Keep one verified copy throughout a swap. A later run can resume any partial swap."""
    digest = hashlib.sha256(archive).hexdigest()
    items = release.assets()
    current = items.get(NAME)
    if current is None:
        # The previous run stopped between moving the old asset aside and promoting the new one.
        if BACKUP not in items:
            raise RuntimeError("The canonical asset and its managed backup are both missing")
        previous = items[BACKUP]
        release.verified_digest(previous)
        current = release.rename(previous, NAME)
        items.pop(BACKUP)
    old_digest = release.verified_digest(current)
    if old_digest != digest:
        if BACKUP in items:
            release.delete(items.pop(BACKUP))  # canonical asset was just verified
        stage = items.get(STAGE)
        if stage and (stage["state"] != "uploaded" or stage["digest"] != "sha256:" + digest):
            release.delete(stage)
            stage = None
        if not stage:
            stage = release.upload(archive)
        if release.verified_digest(stage) != digest:
            raise RuntimeError("Staged archive differs from local build")
        if head() != source:
            release.delete(stage)
            return False
        try:
            release.rename(current, BACKUP)
            current = release.rename(stage, NAME)
        except Exception:
            # PATCH may have succeeded even if the response was lost. Inspect actual state.
            actual = release.assets()
            if NAME not in actual and BACKUP in actual:
                release.rename(actual[BACKUP], NAME)
            raise
    if release.verified_digest(current) != digest:
        raise RuntimeError("Canonical asset verification failed; backup retained")
    url = f"https://github.com/{REPO}"
    body = (
        f"{MARKER}\nSRE 故障演练实验室的源码附件随 `main` 的成功 CI 更新。\n\n"
        f"**[下载 {NAME}]({url}/releases/download/{TAG}/{NAME})**\n\n"
        f"源码提交 [{source[:12]}]({url}/commit/{source})，提交时间 {date}。\n\n"
        f"[从零搭建教程]({url}/blob/{source}/{TUTORIAL}) · [项目说明]({url}/blob/{source}/README.md)\n\n"
        f"SHA-256 为 `{digest}`。下载后可用 `sha256sum {NAME}` 核对。\n\n"
        f"GitHub 自动生成的 Source code (zip/tar.gz) 对应 {TAG} 的历史提交 "
        f"[`{TAG_COMMIT[:12]}`]({url}/commit/{TAG_COMMIT})。"
    )
    release.notes(body)
    remaining = release.assets()
    if NAME not in remaining or remaining[NAME]["id"] != current["id"]:
        raise RuntimeError("Canonical asset changed before cleanup; backup retained")
    for item in remaining.values():
        if item["name"] != NAME:
            release.delete(item)
    final = release.assets()
    if set(final) != {NAME} or release.verified_digest(final[NAME]) != digest:
        raise RuntimeError("Expected exactly one verified source attachment")
    return True


def main():
    source = os.environ["SOURCE_SHA"]
    if current_head() != source:
        print("Superseded main commit; skipping.")
        return
    check_authority(source)
    date = run("git", "show", "-s", "--format=%cI", source).decode().strip()
    archive = pack(source)
    release = Release()
    if current_head() == source and sync(release, archive, source, date):
        print(f"Published {source} to {TAG}: {NAME} sha256:{hashlib.sha256(archive).hexdigest()}")


if __name__ == "__main__":
    main()
