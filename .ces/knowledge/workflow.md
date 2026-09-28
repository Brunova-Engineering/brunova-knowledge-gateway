# Workflow

The Dockerfile installs Python dependencies and runs `uvicorn app.main:app` on port 8080. Deterministic tests cover policy and error behavior. Dated production proof for Sheets validation records read-only structure and value checks, a deployment smoke check, and zero Registry writes. Current deployment and test status at repository HEAD: **UNKNOWN**.

## Evidence

- `Dockerfile`
- `requirements.txt`
- `tests/test_signal_lifecycle_policy.py`
- `tests/test_errors.py`
- `docs/sheets-validation-production-proof.md`
