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
