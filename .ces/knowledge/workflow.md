# Workflow

The Dockerfile installs Python dependencies and runs `uvicorn app.main:app` on port 8080. Deterministic tests cover policy and error behavior. Dated production proof for Sheets validation records read-only structure and value checks, a deployment smoke check, and zero Registry writes. The production GitHub Actions workflow tests the triggering `main` commit and uses Workload Identity Federation to build and deploy its SHA-tagged image. It first verifies a no-traffic `ci-v1` revision, runtime configuration, resolved image digest, health endpoint, and authenticated read-only MCP catalog, then promotes that revision to 100% production traffic and repeats health/catalog checks. If post-promotion checks fail, it restores the captured prior active revision percentages; the exact temporary tag is removed after the run. The first successful GitHub run on 2026-10-22 promoted commit `0a3fe42f11daf6dfcd853383295847de65b7bcd4` to revision `brunova-knowledge-gateway-00167-noh`; readback showed 100% traffic and no remaining `ci-v1` tag. Local Gateway suite passed 342 tests before that release.

## Evidence

- `Dockerfile`
- `.github/workflows/production-cloud-run.yml`
- `docs/github-actions-cloud-run.md`
- `scripts/verify_cloud_run_deployment.py`
- `scripts/verify_mcp_catalog.py`
- `requirements.txt`
- `tests/test_signal_lifecycle_policy.py`
- `tests/test_errors.py`
- `docs/sheets-validation-production-proof.md`
