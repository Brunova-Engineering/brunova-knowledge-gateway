# Known-Errors

- **Accepted gap:** Native Google Sheets image insertion and replacement are unsupported under ADR 0001.
- **OpenWA approval propagation: fixed and deployed per 2026-09-07 audit; live message trial unverified:** The former argument-only versus metadata-only contract mismatch caused `openwa_approval_required` despite a supplied reference.
- **Known limitation:** Sheets mutation reports `concurrency_control: none`; validation inspection is a read-time observation, not an atomic write precondition.

## Evidence

- `docs/adr/0001-google-sheets-native-images.md`
- `docs/openwa-approval-propagation.md`
- `app/spreadsheet_production.py`
- `docs/sheets-validation-production-proof.md`
