import pytest

from policy_backend_v2.repository_lifecycle import (
    UNIVERSAL_GITHUB_LOGIN,
    RepositoryGrant,
    plan_repository_lifecycle,
)


def node(repository, *, lifecycle="ACTIVE", mode="authoritative", generation=1,
         node_id=None):
    return {
        "node_id": node_id,
        "lifecycle": lifecycle,
        "backing_bindings": [{
            "mode": mode,
            "repository": repository,
            "store_id": "github",
            "generation": generation,
            "local_path": f"domains/{repository.split('/')[-1]}",
        }],
    }


def topology(**nodes):
    return {"nodes": {key: {**value, "node_id": key} for key, value in nodes.items()}}


def test_plans_new_repositories_for_one_universal_login():
    before = topology(core=node("Pancracio-s/core"))
    after = topology(core=node("Pancracio-s/core"), hq=node("Pancracio-s/hq"))

    plan = plan_repository_lifecycle(before, after)

    assert plan.active_repositories == ("Pancracio-s/core", "Pancracio-s/hq")
    assert plan.grants == (RepositoryGrant("Pancracio-s/hq"),)
    assert {grant.login for grant in plan.grants} == {UNIVERSAL_GITHUB_LOGIN}


def test_rejects_repository_with_wrong_owner():
    with pytest.raises(ValueError, match="owner is invalid"):
        plan_repository_lifecycle(topology(), topology(dev=node("someone-else/dev")))


def test_archived_node_does_not_enter_active_repository_set():
    plan = plan_repository_lifecycle(
        topology(), topology(retired=node("Pancracio-s/retired", lifecycle="ARCHIVED"))
    )

    assert plan.active_repositories == ()
    assert plan.grants == ()


@pytest.mark.parametrize("bindings", [[], [node("Pancracio-s/a")["backing_bindings"][0],
                                               node("Pancracio-s/b")["backing_bindings"][0]]])
def test_requires_exactly_one_authoritative_or_generation_binding(bindings):
    invalid = {"nodes": {"dev": {"backing_bindings": bindings}}}
    with pytest.raises(ValueError, match="one backing"):
        plan_repository_lifecycle(topology(), invalid)


def test_rejects_invalid_backing_marker():
    bad = node("Pancracio-s/dev", mode="mirror")
    with pytest.raises(ValueError, match="backing mode"):
        plan_repository_lifecycle(topology(), topology(dev=bad))


@pytest.mark.parametrize("repository", [
    "Pancracio-s/..", "Pancracio-s/name with space", "Pancracio-s/name;touch",
    "Pancracio-s/name\\nnext", "Pancracio-s/name/extra",
])
def test_rejects_unsafe_repository_names(repository):
    with pytest.raises(ValueError, match="repository owner is invalid"):
        plan_repository_lifecycle(topology(), topology(dev=node(repository)))


def test_rejects_duplicate_active_repository_bindings():
    duplicate = topology(first=node("Pancracio-s/shared"), second=node("Pancracio-s/shared"))
    with pytest.raises(ValueError, match="multiple active nodes"):
        plan_repository_lifecycle(topology(), duplicate)


def test_rejects_unknown_lifecycle_value():
    with pytest.raises(ValueError, match="lifecycle is invalid"):
        plan_repository_lifecycle(topology(), topology(dev=node("Pancracio-s/dev",
                                                               lifecycle="retired")))


def test_unchanged_repository_set_is_an_idempotent_noop():
    current = topology(core=node("Pancracio-s/core"), hq=node("Pancracio-s/hq"))

    plan = plan_repository_lifecycle(current, current)

    assert plan.active_repositories == ("Pancracio-s/core", "Pancracio-s/hq")
    assert plan.grants == ()
