"""A Core publication waits for POC repository transport readback."""

import pytest

from policy_backend_v2 import repository_publication as publication


def _node(repository, node_id, *, mode="authoritative"):
    return {"lifecycle": "ACTIVE", "owner_class": "organization",
            "owner_id": "brunova", "backing_bindings": [{
                "repository": repository, "store_id": node_id + "_durable",
                "generation": 1, "local_path": "domains/nodes/" + node_id,
                "mode": mode,
            }]}


def _topology(new=False):
    nodes = {"ctx_brunova": _node("Pancracio-s/brunova-knowledge", "ctx_brunova")}
    if new:
        nodes["ctx_new"] = _node("Pancracio-s/pancracio-node-new", "ctx_new")
    return {"nodes": nodes}


class _Client:
    def __init__(self, **kwargs):
        self.options = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_no_new_backing_needs_no_publisher_credential(monkeypatch):
    monkeypatch.setattr(publication, "_previous_topology", lambda _: _topology())
    monkeypatch.delenv("PANKY_REPOSITORY_GITHUB_TOKEN", raising=False)
    assert publication.provision_new_repositories("unused", _topology()) == {
        "created_or_verified": [], "status": "no_change"}


def test_new_backing_requires_publisher_credential(monkeypatch):
    monkeypatch.setattr(publication, "_previous_topology", lambda _: _topology())
    monkeypatch.delenv("PANKY_REPOSITORY_GITHUB_TOKEN", raising=False)
    with pytest.raises(publication.RepositoryPublicationUnavailable,
                       match="credential is unavailable"):
        publication.provision_new_repositories("unused", _topology(new=True))


def test_new_backing_is_verified_before_publication(monkeypatch):
    monkeypatch.setattr(publication, "_previous_topology", lambda _: _topology())
    monkeypatch.setenv("PANKY_REPOSITORY_GITHUB_TOKEN", "fixture-only")
    monkeypatch.setattr(publication.httpx, "Client", _Client)
    observed = []
    monkeypatch.setattr(publication, "ensure_private_repository",
                        lambda client, **kw: observed.append(("create", kw)))
    monkeypatch.setattr(publication, "verify_effective_access",
                        lambda client, **kw: observed.append(("verify", kw)))
    receipt = publication.provision_new_repositories("unused", _topology(new=True))
    assert receipt == {"created_or_verified": ["Pancracio-s/pancracio-node-new"],
                       "status": "verified"}
    assert [entry[0] for entry in observed] == ["create", "verify"]
    assert observed[0][1]["marker_path"] == "node.yaml"
    assert observed[0][1]["marker_data"]["node_id"] == "ctx_new"
    assert observed[1][1] == {"repository": "Pancracio-s/pancracio-node-new",
                              "login": "jorgeaveraf", "permission": "write"}


def test_failed_github_readback_keeps_release_unpublished(monkeypatch):
    monkeypatch.setattr(publication, "_previous_topology", lambda _: _topology())
    monkeypatch.setenv("PANKY_REPOSITORY_GITHUB_TOKEN", "fixture-only")
    monkeypatch.setattr(publication.httpx, "Client", _Client)
    monkeypatch.setattr(publication, "ensure_private_repository",
                        lambda *args, **kwargs: None)
    def fail(*args, **kwargs):
        raise RuntimeError("private GitHub detail")
    monkeypatch.setattr(publication, "verify_effective_access", fail)
    with pytest.raises(publication.RepositoryPublicationUnavailable,
                       match="provisioning is incomplete") as exc:
        publication.provision_new_repositories("unused", _topology(new=True))
    assert "private GitHub detail" not in str(exc.value)
