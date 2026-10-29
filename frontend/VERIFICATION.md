# Vertical slice verification — 29 October 2026

## Security remediation verification — 29 October 2026

The results below this section record the original vertical slice verification. The
subsequent audit remediation adds inbound authority checks, bounded metadata reading and
presentation, downstream-aware proxy deadlines, and restricted upstream responses.
No dependencies or backend files were changed for this remediation.

| Command | Remediation result |
| --- | --- |
| `node --test tests/security.test.mjs tests/json-body.test.mjs` | 26 passed |
| `node --test --test-name-pattern "A:\|unknown API" tests/proxy.test.mjs` | 3 passed |
| `npm.cmd run test:proxy` | 35 passed, 0 failed, 122183.0006 ms |
| `npm.cmd test` | 35 passed, 41.1 s |
| `npm.cmd run test:real` | 6 passed, 16.6 s |
| `npm.cmd run lint` | exit 0 |
| `npm.cmd run typecheck` | exit 0 |
| `npm.cmd run build` | exit 0 |
| `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` | 395 passed, 1 existing warning, 19.18 s |

One earlier proxy run had 34 passes and one harness failure because Node's HTTP client
refused a Host array before sending it. The corrected test sends duplicate Host lines over
a real socket; the full rerun above passed. Real tests exercise actual declared and
chunked 200 MiB rejection. Proxy tests exercise upload streaming, disconnects, a paused
reader, and the real 120-second upstream timeout. Downstream deadline expiry and timer
cleanup use deterministic simulated streams, not a 120-second slow-socket test. A synthetic
1,000-column response padded to exactly 8 MiB rendered in 1,627 ms in the recorded browser
run, with 25 profile elements and 14 detail cells after one expansion; this is not a
performance or memory guarantee.

The 8 MiB response budget limits bytes accumulated by the client, not peak process memory.
The local header policy accepts requests missing both browser headers for command-line
clients and does not eliminate DNS rebinding or authenticate local callers. A downstream
finish is not a browser delivery acknowledgement, and cancellation does not guarantee the
backend stopped work. Manual screen-reader testing, a browser DNS-rebinding exploit, and a
long-lived slow-socket deadline test were not performed. Overall project progress remains
approximately 50%; this work hardens the existing milestone rather than adding a new one.

Outcome: CSV upload → real FastAPI ingestion/storage → metadata retrieval → Data Health
Dashboard works through the production frontend proxy. Refresh retrieves metadata without
another upload. No existing tracked repository file was modified. No commit was made.

## Environment and scope

- Baseline and final branch: `master`; HEAD `cd1b48d chore: establish project baseline`.
- Initial working tree: clean. Final status: `?? frontend/`; no staged changes.
- Node 24.14.0, npm 11.9.0, existing Python 3.12.10 virtual environment.
- Next 16.4.3, React/React DOM 19.3.1, lossless-json 4.3.3, Playwright 1.64.1.
- TypeScript 5.9.3, ESLint 9.39.5, matching eslint-config-next 16.4.3; all exact pins.
- At the original verification, all 32 new source/config/test/documentation files were in `frontend/`; the complete
  inventory is in [README.md](README.md#added-files). Node modules, build output, browser
  screenshots and test reports are ignored. `src/services/` avoids the root's frozen
  `lib/` ignore pattern.

The Python runtime initially appeared unavailable inside the sandbox; the existing venv
worked with approved execution outside it. PowerShell blocks `npm.ps1`, so all npm
commands use `npm.cmd`. Chromium binaries were installed successfully. No required check
remains blocked by the environment.

## Final commands and exact results

Backend command was run at the repository root; npm commands were run in `frontend/`.

| Command | Result |
| --- | --- |
| `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` | **395 passed, 1 warning in 16.84s** |
| `npm.cmd ci` | Installed 352 packages; audited 353; **0 reported vulnerabilities**; exit 0 |
| `npm.cmd run lint` | ESLint completed, exit 0 |
| `npm.cmd run typecheck` | `tsc --noEmit` completed, exit 0 |
| `npm.cmd run build` | Production webpack build completed, exit 0; `/`, `/_not-found`, `/datasets/[datasetId]` |
| `npm.cmd test` | **33 passed (19.7s)**, Chromium, 2 workers, 0 retries |
| `npm.cmd run test:proxy` | **5 passed, 0 failed**, 122202.6992 ms total |
| `npm.cmd run test:real` | **6 passed (15.5s)**, real backend, Chromium, 0 retries |
| `git diff --exit-code HEAD -- .` | Exit 0: no tracked changes |
| `git diff --cached --name-only` | Empty: nothing staged |
| Per-new-file `git diff --no-index --check -- NUL <file>` | No whitespace errors (Git emitted local LF→CRLF normalization notices) |

The single backend warning is the existing Starlette TestClient/httpx deprecation.
Browser runs emit a terminal NO_COLOR/FORCE_COLOR warning. npm emits an ESLint 9
deprecation notice; that version satisfies the installed React lint plugin's peer range
(which excludes ESLint 10). These notices are not test failures or security certification.
The clean npm install's audit result is a point-in-time registry result.

Other setup/review commands included `git status --short`, `git branch --show-current`,
`git log -1`, `rg` and file reads; `node --version`, `npm.cmd --version`, Python version
checks; npm registry version queries and exact-version installs; `npx.cmd playwright
install chromium`; and temporary `npm.cmd exec --yes --package=prettier@3.6.2 -- prettier
--write ...` formatting. Prettier was not added to the project dependencies.

Initial verification found and corrected a test type cast, selectors that also matched
Next's route announcer, a test that expanded columns before metadata arrived, and a
frontend proxy header-flush bug. The initial real suite had 5 passes and one 502 failure
on a headers-only oversized request. Calling `upstream.flushHeaders()` corrected it;
both subsequent full real suites passed, including explicit cap-value assertions in the
last run. The backend was never changed to make tests pass.

## API contract checked against source and tests

- `GET /health`: 200, `{"status":"ok"}`. Real proxy request passed.
- `GET /ready`: 200, `{"status":"ready"}`; existing backend also defines 503 problem
  JSON when storage is unavailable. Real writable-storage response passed.
- `POST /api/v1/datasets/upload`: `multipart/form-data`, **field `file`**, 201 with
  `dataset_id`, `filename`, `created_at`, `row_count`, `column_count`, `data_quality_score`.
- `GET /api/v1/datasets/{dataset_id}`: 200 with those stored fields plus `encoding`,
  `delimiter`, `quality`, `columns`, `column_metadata`, `warnings`.
- `quality`: overall/completeness/uniqueness/validity scores, total cells, missing cells,
  duplicate rows and warnings. Columns contain counts/ratios, uniqueness, inferred type,
  inference reason/policy version and type-specific `stats`.
- FastAPI errors retain `type`, `title`, `status`, `detail`, `instance`, `code` and
  `application/problem+json`. The proxy forwards backend errors. Its own 502/504
  transport failures are safe plain text. UI copy is application-controlled.

Numeric statistics are min/max/mean/median/q25/q75/iqr/std_dev/variance. Strings expose
length statistics and empty-string count; booleans expose true/false counts; dates and
datetimes expose extrema. The UI selects only those fields. Missing stats show
"Not available"; an empty stats object gets a type-appropriate empty explanation.

Backend scoring is 60% populated-cell completeness, 30% distinct raw-row uniqueness,
10% constant-column penalty (called `validity_score`). No frontend score is calculated.
The UI qualifies the backend's 100 score for header-only datasets and never describes
this score as semantic validity. `is_unique` is displayed as an observation, not an
identifier classification.

## Transport evidence

| Property | Evidence and scope |
| --- | --- |
| Early streaming | Actual production proxy delivered a multipart prefix to an instrumented loopback HTTP upstream while the client stream was still open. The upstream observed its first data event before client EOF. |
| No 10 MiB truncation | The same proxy delivered exactly all bytes of a body with 12 MiB payload plus multipart framing. This is a transport fixture, not a CSV profiling benchmark. |
| Real upload | Chromium submitted a CSV through the production proxy to unmodified FastAPI/core/local storage. The dashboard then GET-fetched the stored profile. Reload left the POST count at one. |
| Declared oversized body | With Content-Length 209715201 and no CSV bytes sent, real FastAPI returned 413 through the proxy before client EOF. The response identified `ERR_FILE_SIZE_LIMIT_EXCEEDED` and `configured limit (209,715,200 bytes)`. |
| Chunked oversized body | With no Content-Length, a multipart stream crossed the default 209715200-byte cap and received the same 413 before closing the stream or sending a multipart terminator. The response's exact cap was asserted. |
| Timeout | Actual proxy plus deliberately stalled loopback HTTP upstream returned safe plain-text 504 at the configured 120-second absolute deadline. Timeout test duration: 120012.7768 ms. This did not force the real core's processing deadline. |
| Fixed destination | Remote/ambiguous origins are rejected by configuration tests. Browser-supplied `url` and `destination` query parameters still reached the configured loopback fixture. No request value changes host or port. |
| Cache policy | Browser fetch initialization uses `no-store`; an actual metadata response through the production proxy included `Cache-Control: no-store`. |

These tests prove observed forwarding and rejection behavior. They do **not** establish
a hard OS/process RAM ceiling, preemptive core cancellation, or performance guarantees
for all valid 200 MiB CSVs. The core still materializes datasets under its accepted model.

## UI, accessibility and privacy evidence

The 33 browser tests cover upload/navigation/refresh, invalid/empty/multiple files,
immediate duplicate-submit locking, loading, recovery, network errors, HTTP 400/404/413/
422/500/504 on upload and dashboard, non-JSON/malformed/missing-field responses, timeout,
warning deduplication, header-only/all-missing profiles, all six inferred types, large
integers, negative zero, zero, false, null and absent statistics, ID validation and cache
settings. Additional unknown response fields containing rows/samples/raw CSV are ignored.

Real tests confirm `9223372036854775807` and `-0.0` display fidelity. No synthetic private
cell strings appeared in the DOM. The real happy-path browser recorded no page errors or
requests outside the frontend origin; localStorage and sessionStorage remained empty.
This observation is not a system-wide network-egress audit.

Keyboard tests exercise tab order, the skip link, the file input, Enter-operated column
disclosures and visible focus. Dashboard widths 375, 768 and 1440 pixels and the 375-pixel
upload screen had no horizontal overflow. Desktop/mobile landing and dashboard screenshots
were visually inspected; the footer alignment was corrected and the final UI suite rerun.
Controls have labels, semantic landmarks and focused error messages. System fonts are used.

Manual screen-reader evaluation and Firefox/WebKit coverage were not performed. No full
accessibility certification, penetration test, CSP audit or OWASP compliance claim is made.
The local application has no authentication, as scoped. Existing backend diagnostic logs
remain governed by its accepted logging behavior; this frontend adds no dataset logging.
An upload may finish storing after a client disconnect/deadline; the UI explains this and
does not retry automatically. Backend-origin IPv6 validation is tested; live transport was
verified with IPv4 loopback only. The real backend's cooperative 30-second processing timeout
and unhandled 500 were not deliberately induced here; UI handling is mocked, and the actual
proxy 504 path was exercised with a controlled stalled upstream.

## Final disposition and progress

Required environment-blocked items: **none**. Full manual accessibility, other browsers,
resource ceilings and broad CSV performance testing remain outside the verified claims.

No backend/core implementation, existing backend tests, frozen manifests, root documentation
or historical commits changed. `git diff HEAD` and staged-diff inspection were empty; the
only status entry was the untracked `frontend/` directory. All source additions are ready
for review, including `package-lock.json`; no automatic commit was made.

Overall project estimate: **approximately 50%**, up from approximately 42%. The increase
reflects completion and automated verification of the first frontend upload/metadata/health
milestone, including actual streaming, default-size rejection and proxy timeout behavior.
It does not include SQL, inferential statistics, AI insights, exports, authentication or other excluded
roadmap work. The frozen historical README's 42% baseline was preserved.
