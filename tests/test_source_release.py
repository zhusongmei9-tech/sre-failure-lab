"""Exercise release preparation and simulate interrupted GitHub asset mutations locally."""

import gzip
import hashlib
import io
import subprocess
import tarfile

import pytest

from scripts import publish_source_release as pub


class MemoryRelease:
    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.mutations = 0
        self.next_id = 1
        self.store = {}
        self.body = ""
        self.add(pub.NAME, b"historical archive")

    def add(self, name, data):
        obj = {
            "id": self.next_id,
            "name": name,
            "label": None if name == pub.NAME else pub.LABEL,
            "state": "uploaded",
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        }
        self.store[obj["id"]] = obj, data
        self.next_id += 1
        return obj.copy()

    def mutation(self):
        assert any(data for _, data in self.store.values())
        self.mutations += 1
        if self.mutations == self.fail_at:
            raise RuntimeError("lost response")

    def assets(self):
        return {a["name"]: a.copy() for a, _ in self.store.values()}

    def verified_digest(self, asset):
        return hashlib.sha256(self.store[asset["id"]][1]).hexdigest()

    def rename(self, asset, name):
        obj = self.store[asset["id"]][0]
        assert name not in self.assets()
        obj.update(name=name, label=None if name == pub.NAME else pub.LABEL)
        self.mutation()
        return obj.copy()

    def upload(self, archive):
        assert pub.STAGE not in self.assets()
        stage = self.add(pub.STAGE, archive)
        self.mutation()
        return stage

    def delete(self, asset):
        assert asset["name"] in (pub.STAGE, pub.BACKUP)
        del self.store[asset["id"]]
        self.mutation()

    def notes(self, body):
        self.body = body
        self.mutation()


@pytest.mark.parametrize("interruption", [1, 2, 3, 4, 5])
def test_retry_after_any_write_can_finish_even_with_a_newer_head(interruption):
    release = MemoryRelease(fail_at=interruption)
    with pytest.raises(RuntimeError, match="lost response"):
        pub.sync(release, b"first", "a" * 40, "d1", lambda: "a" * 40)
    assert pub.sync(release, b"second", "b" * 40, "d2", lambda: "b" * 40)
    assert list(release.assets()) == [pub.NAME]
    asset = release.assets()[pub.NAME]
    assert release.verified_digest(asset) == hashlib.sha256(b"second").hexdigest()
    assert "b" * 40 in release.body and pub.TAG_COMMIT in release.body


def test_recover_interruption_between_renames_and_skip_superseded_source():
    release = MemoryRelease()
    release.rename(release.assets()[pub.NAME], pub.BACKUP)
    release.upload(b"first")
    assert not pub.sync(release, b"first", "old", "d1", lambda: "new")
    assert list(release.assets()) == [pub.NAME]
    assert (
        release.verified_digest(release.assets()[pub.NAME])
        == hashlib.sha256(b"historical archive").hexdigest()
    )


def test_swap_resumes_when_canonical_is_already_current():
    release = MemoryRelease()
    old = release.assets()[pub.NAME]
    release.rename(old, pub.BACKUP)
    release.rename(release.upload(b"target"), pub.NAME)
    assert pub.sync(release, b"target", "a" * 40, "today", lambda: "a" * 40)
    assert list(release.assets()) == [pub.NAME]
    assert hashlib.sha256(b"target").hexdigest() in release.body


def test_unknown_attachment_and_immutable_release_refused(monkeypatch):
    monkeypatch.setattr(
        pub,
        "api",
        lambda *args: {
            "tag_name": pub.TAG,
            "draft": False,
            "prerelease": False,
            "immutable": True,
            "body": pub.MARKER,
        },
    )
    with pytest.raises(RuntimeError, match="mutable published"):
        pub.Release()
    rel = object.__new__(pub.Release)
    rel.path = "test"
    monkeypatch.setattr(pub, "api", lambda *args: [{"name": pub.STAGE, "label": "operator data"}])
    with pytest.raises(RuntimeError, match="Unexpected release attachment"):
        rel.assets()


def test_uses_exact_git_tree_and_blocks_private_payload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Demo")
    git("config", "user.email", "release@example.invalid")
    for name in sorted(pub.GUIDES):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("tracked")
    git("add", ".")
    git("commit", "-qm", "source")
    commit = git("rev-parse", "HEAD")
    (tmp_path / pub.TUTORIAL).write_text("local-only")
    packed = pub.pack(commit)
    assert packed == pub.pack(commit)
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(packed))) as tar:
        assert tar.extractfile(pub.PREFIX + pub.TUTORIAL).read() == b"tracked"
    (tmp_path / "docs" / "key.txt").write_text("-----BEGIN " + "OPENSSH PRIVATE KEY-----")
    git("add", "docs/key.txt")
    git("commit", "-qm", "unwanted")
    with pytest.raises(ValueError, match="Disallowed archive content"):
        pub.pack(git("rev-parse", "HEAD"))
    git("rm", "docs/key.txt")
    (tmp_path / ".env").write_text("do not publish")
    git("add", ".env")
    git("commit", "-qm", "unwanted path")
    with pytest.raises(ValueError):
        pub.pack(git("rev-parse", "HEAD"))
