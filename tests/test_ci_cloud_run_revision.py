"""Cloud Run resolves a pushed commit tag to a digest in revision metadata."""

from __future__ import annotations

import json
import sys

import pytest

from scripts import verify_cloud_run_deployment as deployment


def test_ready_revision_matches_resolved_commit_digest(tmp_path, monkeypatch):
    image = "us-central1-docker.pkg.dev/project/repo/gateway:" + "a" * 40
    digest = "sha256:" + "b" * 64
    resolved = deployment.resolved_image(image, digest)
    revision = tmp_path / "revision.json"
    revision.write_text(json.dumps({
        "metadata": {"name": "gateway-00157"},
        "spec": {"containers": [{"image": resolved}]},
        "status": {"imageDigest": resolved,
                   "conditions": [{"type": "Ready", "status": "True"}]},
    }), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["verify", "--revision", str(revision),
                                     "--image", image, "--resolved-digest", digest,
                                     "--expected-revision", "gateway-00157"])
    assert deployment.main() == 0


def test_revision_rejects_mismatched_resolved_digest(tmp_path, monkeypatch):
    image = "us-central1-docker.pkg.dev/project/repo/gateway:" + "a" * 40
    digest = "sha256:" + "b" * 64
    revision = tmp_path / "revision.json"
    revision.write_text(json.dumps({
        "metadata": {"name": "gateway-00157"},
        "spec": {"containers": [{"image": deployment.resolved_image(image, digest)}]},
        "status": {"imageDigest": deployment.resolved_image(image, digest),
                   "conditions": [{"type": "Ready", "status": "True"}]},
    }), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["verify", "--revision", str(revision),
                                     "--image", image,
                                     "--resolved-digest", "sha256:" + "c" * 64,
                                     "--expected-revision", "gateway-00157"])
    with pytest.raises(SystemExit, match="expected commit image digest"):
        deployment.main()


def test_promotion_and_rollback_accept_tagged_100_percent_route(tmp_path, monkeypatch):
    prior = "gateway-00099"
    staged = "gateway-00163"
    preserved = {"revisionName": "gateway-00083", "tag": "n8n-canary", "url": "https://example.test/tag"}
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    before.write_text(json.dumps({"status": {"traffic": [
        preserved, {"revisionName": prior, "percent": 100}
    ]}}), encoding="utf-8")
    after.write_text(json.dumps({"status": {"traffic": [
        {**preserved, "latestRevision": False},
        {"revisionName": staged, "tag": "ci-v1", "percent": 100,
         "url": "https://example.test/ci"},
        {"revisionName": prior, "percent": 0},
    ]}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["verify", "--before", str(before),
                                     "--after", str(after),
                                     "--verify-promoted-revision", staged,
                                     "--ignore-tag", "ci-v1"])
    assert deployment.main() == 0

    after.write_text(json.dumps({"status": {"traffic": [
        {**preserved, "latestRevision": False},
        {"revisionName": prior, "percent": 100},
        {"revisionName": staged, "tag": "ci-v1"},
    ]}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["verify", "--before", str(before),
                                     "--after", str(after),
                                     "--verify-traffic-restored", "--ignore-tag", "ci-v1"])
    assert deployment.main() == 0
