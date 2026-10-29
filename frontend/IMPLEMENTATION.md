# Frontend vertical slice

Reconnaissance baseline: clean `master`, `cd1b48d` (`chore: establish project baseline`).
Node 24.14.0; npm via `npm.cmd` because PowerShell script execution is disabled.
Read README, SECURITY, environment example, Python requirements, product/architecture/ingestion
guides, FastAPI factory/configuration/middleware/routes/errors, execution/storage adapters,
domain profiles and API/storage/profiling tests before editing. No AGENTS.md or existing
frontend tooling was found. Historical design documents contain future capabilities;
implementation and tests take precedence. All existing backend/core/tests/manifests are frozen.

Plan: (1) isolate Next App Router app and lossless contract client; (2) implement upload and
metadata dashboard; (3) test browser states plus real backend/proxy transport; (4) verify
lint/types/build/backend tests and final diff. No automatic commit.

Contract: POST `/api/v1/datasets/upload`, multipart field `file`, returns 201 with
dataset_id, filename, created_at, row_count, column_count, data_quality_score. GET
`/api/v1/datasets/{id}` returns stored metadata: those fields plus encoding, delimiter,
quality, columns, column_metadata and warnings. No Pydantic response model exists; the
serializer `build_dataset_metadata` is authoritative. GET `/health` returns
`{"status":"ok"}`; GET `/ready` returns `{"status":"ready"}` or 503 problem JSON.
Errors use RFC 7807 type/title/status/detail/instance/code. UI error copy is controlled
locally and never echoes arbitrary response text. The delete route is outside this UI.

Quality is supplied by the backend: 60% completeness (populated cells), 30% uniqueness
(distinct raw rows), 10% validity (constant-column penalty). This is not semantic validity.
Header-only files receive a backend score of 100 with a zero-row warning. `is_unique`
means every row has a distinct non-null column value; it does not classify identifiers.
Statistics are allowlisted by inferred type; raw rows and samples are never rendered.

Upload middleware caps the ENTIRE multipart body (framing included), by default
209715200 bytes; `MAX_UPLOAD_BYTES` is the actual environment variable, unlike the
historical root example's MAX_UPLOAD_SIZE_MB. It rejects excessive Content-Length or
received bytes with 413. Core ingestion uses a cooperative 30-second processing deadline.
Neither configuration nor mocked UI tests prove proxy streaming or resource bounds.
