# Data Intelligence Lab frontend

Next.js App Router, React, TypeScript, CSS Modules, native fetch/FormData, lossless-json,
and Playwright. The slice covers CSV upload → stored metadata → Data Health Dashboard.
All dataset metrics come from the existing backend. There are no row previews or samples.

## Run locally (PowerShell, two terminals)

Backend, from the repository root:

```powershell
Set-Location "$HOME\Desktop\Youssef Manssouri\Data Intelligence Lab"
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Frontend:

```powershell
Set-Location "$HOME\Desktop\Youssef Manssouri\Data Intelligence Lab\frontend"
npm.cmd ci
npm.cmd run dev
```

Open <http://127.0.0.1:3000>. Node 22.14+ is required; verified with Node 24.14.0.
Using `npm.cmd` avoids the machine's PowerShell restriction on `npm.ps1`.
The existing backend requires its Python 3.12 virtual environment and requirements.

For a production frontend, replace `npm.cmd run dev` with:

```powershell
npm.cmd run build
npm.cmd start
```

The supplied dev/start commands run a custom Next server with the streaming API proxy.
They must be used to serve this app; a bare `next start` does not provide its API proxy.
The custom server is a local Node deployment, not a static export or serverless handler.

## Configuration and transport

The default backend origin is `http://127.0.0.1:8000`. To change it, copy the frontend
`.env.example` to `.env.local`, set `DIL_BACKEND_ORIGIN`, and restart the frontend.
This server-only variable accepts HTTP IPv4 loopback literals or `[::1]` with an explicit
port. Hostnames, remote IPs, credentials, paths, fragments, queries and alternate schemes
are rejected. Neither browser input nor request parameters select the destination.

The proxy handles only the fixed health/readiness/upload/metadata paths and GET/POST
methods. Deletion is not exposed. Requests and responses use Node streams with
backpressure. The proxy does not accumulate CSV bytes or parse multipart data. It flushes
headers immediately so FastAPI can reject oversized Content-Length before file data.
Unknown API paths do not enter Next's body-handling code.

Next 16.4.3's built-in external rewrite implementation calls `cloneBodyStream`, retaining
a second body copy and imposing a default 10 MiB clone limit. The custom server avoids
that code path. See the installed `next/dist/server/body-streams.js` and
`next/dist/server/lib/router-server.js`, and the official
[body-cloning documentation](https://nextjs.org/docs/app/api-reference/config/next-config-js/proxyClientMaxBodySize).

FastAPI remains authoritative for the entire multipart-body budget: **209715200 bytes
(200 MiB) by default**, including framing. Its actual environment variable is
`MAX_UPLOAD_BYTES` (the root historical example uses a different name). The browser's
200 MiB preflight check is a usability check; the backend may have a lower configured cap.
No backend limits or defaults were changed.

The browser and proxy each use a 120-second absolute request deadline. Proxy transport
failures use safe plain-text 502/504 responses, separately from unchanged FastAPI problem
JSON. The UI never renders arbitrary error-body text. A deadline or browser cancellation
does not prove that backend processing stopped; an upload may already be saved. There
are no automatic upload retries. The core's existing processing deadline is cooperative.

Metadata requests use `cache: 'no-store'`; proxy responses carry `Cache-Control: no-store`.
The application does not write dataset metadata or CSV contents to browser storage.
Next telemetry is disabled by the supplied launch/build scripts. System fonts are local.

The local server accepts only `127.0.0.1:<frontend port>` and `localhost:<frontend port>`
as Host authorities. Upload Origin, when present, must match the corresponding local
origin; cross-site Fetch Metadata is rejected. An upload with neither Origin nor Fetch
Metadata is accepted for non-browser clients. If Fetch Metadata is present without Origin,
the upload is rejected. Health checks need neither header. These checks are not
authentication and do not eliminate DNS rebinding or all browser-to-localhost attacks.

The client limits each JSON response to 8 MiB of received bytes, including when the
Content-Length is absent or misleading. Larger responses produce a recoverable error;
some unusually large valid profiles may exceed this frontend limit. Decoding, parsing,
individual network chunks, and React rendering require additional memory, so this is not
a strict process-memory ceiling. Column profiles display 25 at a time, with details rendered
on expansion; long text is shortened for display while statistics retain their values.

The proxy accepts expected JSON successes and backend problem JSON errors, rejects
redirects and unexpected content types, and does not forward upstream cookies, redirects,
or security policies. Its API responses use frontend-controlled security headers. The
deadline stays active until the downstream response finishes or closes. After headers have
been sent, a timeout closes the response instead of changing its status to 504; completion
at the server does not prove delivery to the browser or cancellation of backend work.

## Verification

Run the backend suite from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

From `frontend/`:

```powershell
npx.cmd playwright install chromium
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run build
npm.cmd test
npm.cmd run test:proxy
npm.cmd run test:real
```

Run proxy and real suites sequentially. Both require port 8000 free; the suites own their
loopback fixtures. They do not use or modify your normal dataset storage. The real suite
starts the unchanged backend with temporary storage and its default upload/processing
limits. Its chunked rejection test sends just over 200 MiB, without ending the multipart
body. The proxy timeout test intentionally takes two minutes.

- UI tests intercept API calls to exercise supported UI states. They do not prove transport.
- Proxy tests use the production frontend and a controlled loopback upstream to measure
  early byte forwarding, >10 MiB integrity, fixed destination, and actual timeout.
- Real tests use the production frontend, real FastAPI/core/storage and Chromium to check
  upload/navigation/refresh, edge cases, numeric fidelity and declared/chunked size limits.

Screenshots from synthetic UI fixtures are written under ignored `test-results/` for
desktop/mobile inspection. Traces, video and automatic screenshots are disabled.
See [VERIFICATION.md](VERIFICATION.md) for measured results and limitations and
[IMPLEMENTATION.md](IMPLEMENTATION.md) for reconnaissance and contract decisions.

## Added files

- App: `src/app/{layout,page,error,not-found}.tsx`, `src/app/globals.css`,
  `src/app/datasets/[datasetId]/{page,loading}.tsx`.
- UI/client: `src/components/{UploadPanel,Dashboard}.tsx`,
  `src/components/workspace.module.css`, `src/services/api.ts`.
- Proxy/runtime: `config/proxy.mjs`, `scripts/{server,api-proxy,next}.mjs`, `next.config.mjs`.
- Tooling: `package.json`, `package-lock.json`, `tsconfig.json`, `next-env.d.ts`,
  `eslint.config.mjs`, `.gitignore`, `.env.example`.
- Tests: `playwright{,.real}.config.ts`, `tests/{ui,real}.spec.ts`,
  `tests/{proxy,security,json-body}.test.mjs`, `tests/fixture.ts`, `tests/backend_server.py`.
- Documentation: `README.md`, `IMPLEMENTATION.md`, `VERIFICATION.md`.

All additions are under `frontend/`. Existing repository files and commits are preserved.
