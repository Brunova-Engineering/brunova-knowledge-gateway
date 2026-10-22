"""The private staged revision allows only its exact temporary CI hostname."""

from app.mcp_server import _mcp_allowed_hosts_with_ci_tag


def test_exact_ci_host_is_added_only_for_the_gateway_cloud_run_service():
    base = "brunova-knowledge-gateway-723qfc5aea-uc.a.run.app"
    hosts = [base, "localhost:*"]
    expected = [*hosts, f"ci-v1---{base}"]
    assert _mcp_allowed_hosts_with_ci_tag(hosts, "brunova-knowledge-gateway") == expected
    assert _mcp_allowed_hosts_with_ci_tag(hosts, None) == hosts
    assert _mcp_allowed_hosts_with_ci_tag(hosts, "another-service") == hosts


def test_ambiguous_or_missing_canonical_host_fails_closed():
    base = "brunova-knowledge-gateway-723qfc5aea-uc.a.run.app"
    other = "brunova-knowledge-gateway-abcdef1234-uc.a.run.app"
    assert _mcp_allowed_hosts_with_ci_tag([base, other], "brunova-knowledge-gateway") == [base, other]
    assert _mcp_allowed_hosts_with_ci_tag(["localhost:*"], "brunova-knowledge-gateway") == ["localhost:*"]
