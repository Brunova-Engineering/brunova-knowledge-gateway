# Workflow

The Dockerfile installs Python dependencies and runs `uvicorn app.main:app` on port 8080. Deterministic tests cover policy and error behavior. Dated production proof for Sheets validation records read-only structure and value checks, a deployment smoke check, and zero Registry writes. The production GitHub Actions workflow tests the triggering `main` commit and uses Workload Identity Federation to build and deploy its SHA-tagged image. It first verifies a tagged no-traffic revision, runtime configuration, health endpoint, and authenticated MCP catalog, then promotes that revision to 100% production traffic and repeats health/catalog checks. If post-promotion checks fail, it restores the captured prior active revision percentages; the unique temporary tag is removed after the run. Local suite and workflow checks passed; the workflow has not been run in GitHub and no deployment was performed during that change.

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
