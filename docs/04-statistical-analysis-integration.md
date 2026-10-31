# Statistical Analysis Engine v1: backend integration design

**Status:** The statistical engine, Phase 1A wire protocol, and Phase 1B
controller/admission contracts exist. Contract reconciliation preserves worker
errors and distinguishes computation from communication stalls. The worker-side
analysis adapter now exists as a pure application-level bridge. A production
worker runtime, isolation, and API integration do not exist. Existing upload,
metadata, storage, and frontend contracts remain
unchanged. The standalone engine is in `core/statistical_analysis.py`.

## Worker boundary and result handling

The intended architecture is:

```text
controller
    ↓
worker request
    ↓
future isolated worker
    ↓
statistical analysis
    ↓
validated aggregate response
    ↓
controller
```

The controller sends a versioned request followed by bounded Parquet DATA frames
and END_INPUT. The request contains only the allowlisted operation
(`numeric_outliers` or `categorical_counts`), input format, byte length, SHA-256,
column index, row limit, decoded-byte budget, and schema version. Dataset resolution
belongs to a future trusted host-side boundary. The worker receives bytes, never
arbitrary paths or dataset names to resolve. Requests cannot contain commands,
Python code, SQL, credentials, or user-controlled environment variables.

The worker returns READY, then one bounded RESPONSE and EOF. The controller
independently validates framing, job identity, ordering, input integrity, exact
response schemas and limits, including the 16 KiB response ceiling. Extra output
invalidates the exchange. The worker remains untrusted; schema validation does
not prove that its computation is correct. A future driver must also bound
transport buffers and diagnostic output and prevent free-text diagnostic leakage.

An `outcome: ok` response contains the engine's complete, unchanged `to_dict()`
result: versions, status, aggregate counts, numeric method description when
applicable, and one finding with aggregate evidence. No raw observations, category
labels, frequency maps, quartile values, or fence values are returned. Engine
statuses such as INVALID_INPUT remain part of this result and are not rewritten
as transport errors.

A valid `outcome: error` response preserves exactly one of INVALID_INPUT,
UNSUPPORTED_TYPE, RESOURCE_LIMIT, or INTERNAL_ERROR. After validated EOF, the
controller exposes it as `Outcome.worker_error_code`. With successful teardown,
the outcome is FAILED, the validated response remains available, and lifecycle
`error` is None with an empty `errors` tuple. SUCCEEDED requires an `ok` response
and successful lifecycle completion.

Controller lifecycle errors remain separate: INVALID_REQUEST, BUSY,
PROTOCOL_ERROR, TIMEOUT, WORKER_FAILED, TERMINATION_FAILED, CLEANUP_FAILED, and
ADMISSION_UNAVAILABLE. Malformed responses are protocol failures; missing responses
or unexpected driver/worker failure are WORKER_FAILED; deadline expiration is
TIMEOUT. No valid worker error is relabeled WORKER_FAILED. A response followed by
extra bytes or missing EOF does not publish a worker error. If teardown fails
after validated EOF, the worker error code survives alongside the lifecycle error,
but the response is withheld. Admission is released only after confirmed stop and
cleanup; uncertainty quarantines or leaves the durable slot held, never reusable.

## Worker-side analysis adapter

`backend/worker_adapter.py` exposes `analyze_column(request, values, *, deadline=None)`
and returns the existing strict `worker_protocol.Response`. An injected provider
boundary supplies an already decoded list or tuple directly; the adapter does not
implement or call a provider. The adapter creates an immutable tuple snapshot and
measures, validates and processes that same snapshot. Later changes to the caller's
list cannot affect execution. The provider must not mutate the original container
concurrently while the snapshot is being created; this is a trusted-provider
assumption, not protection against an untrusted concurrent producer. The adapter
does not mutate caller input or coerce its values.

Responsibilities are separated as follows:

- The protocol owns request versions, allowlisted operations, fields, ranges,
  framing and response schemas. The adapter accepts only an exact Request and
  reuses its validator. Invalid or tampered requests raise the fixed ProtocolError;
  no response can be bound to an invalid request, and no substitute request is made.
- The controller owns lifecycle, admission, transport deadlines and independent
  validation of untrusted responses. The adapter adds no controller fallback.
- The adapter checks exact built-in list/tuple containers, the request row limit,
  and a conservative decoded-Python-size charge before engine dispatch. Only exact
  None, bool, int, float and str values can be measured; subclasses, nested objects,
  custom scalars and lazy iterables are rejected without conversion or inspection
  hooks. All exact-type admission checks use identity comparisons, never equality
  or type membership, so custom metaclass equality cannot influence admission.
  Native scalar semantic errors remain the engine's responsibility.
- Future Parquet decoding must verify the encoded bytes, digest, column selection,
  schema and decoder-buffer limits before supplying values. The declared input
  format remains `parquet`; it does not imply that this adapter parses Parquet or
  can verify the provenance of an already decoded column.
- A future isolated execution runtime must enforce process resources and hard
  deadlines. This adapter provides neither process isolation nor a security
  boundary against native-code execution.

Decoded-size admission charges `sys.getsizeof(tuple_snapshot)` plus
`sys.getsizeof(value)` for every occurrence, including nulls and shared values.
This is an interpreter-specific, conservative charge of the supplied Python
representation: repeated references are charged repeatedly. It uses constant-time
size queries on exact built-ins, never encodes strings to measure them, and stops
as soon as the budget is exceeded. It does not measure decoder buffers, allocator
overhead, engine temporaries or total process memory, nor prevent allocations
already made by the provider. Future decoding must enforce its own buffer budget.
The adapter checks the original row count, takes a slice bounded to `max_rows + 1`,
converts that slice to an exact tuple, and rechecks the snapshot row count. Snapshot
creation can temporarily retain the original container, a bounded slice and the
tuple. The tuple is included in admission accounting; the original container and
temporary slice are not. Additional reference storage is O(max_rows), bounded by
the protocol's row ceiling, but may be allocated before byte admission completes.
Admission scans the snapshot in linear time without copying scalar payloads.

The adapter dispatches only `screen_numeric` or `summarize_categories`, preserving
the engine's entire `to_dict()` result inside `outcome: ok`, including EMPTY,
ALL_NULL and invalid-input/limit statuses returned by the engine. Serialization
uses fixed JSON formatting, rejects non-finite JSON numbers, and passes through the
existing strict response validator and size ceiling. No raw values, labels or
exception text are serialized.

| Adapter failure | Outcome |
| --- | --- |
| Invalid container or deadline representation | INVALID_INPUT |
| Unsupported scalar representation | UNSUPPORTED_TYPE |
| Row/decoded-size budget, MemoryError, cooperative deadline expiry | RESOURCE_LIMIT |
| Unexpected engine exception, wrong result type or invalid serialized result | INTERNAL_ERROR |

An optional trusted, exact `ExecutionDeadline` is checked during admission and passed
unchanged to the engine. Instance-level `check` overrides are rejected without
calling them. Its numeric fields are admitted by identity checks. The trusted
caller must not mutate the deadline or its class during the call. Successful
results and error responses with a validated deadline receive a final cooperative
check; expiry replaces an error response with RESOURCE_LIMIT. Invalid deadline
objects are rejected without invoking their methods. The fixed expiry response
itself is not recursively deadline-checked. There is no
wire timeout code, so cooperative expiry is RESOURCE_LIMIT; this does not replace
the controller's independent TIMEOUT lifecycle error. Cancellation propagates.
Unexpected TypeError/ValueError from the engine after admission is INTERNAL_ERROR,
not inferred to be caller fault. Persistent memory exhaustion may still prevent
construction of even a small error response and requires future runtime handling.
The engine deadline expires at `now > deadline`, so equality remains valid. The
controller's independent `now >= deadline` rule is unchanged. Request validation,
snapshot allocation, sorting, hashing, encoding and other individual native
operations cannot be forcibly interrupted by cooperative checks. These checks
do not guarantee a hard execution ceiling or provide process isolation.

## Timeout semantics

The existing defaults remain unchanged:

| Budget | Meaning |
| --- | --- |
| Readiness: 15 seconds | Start through receipt of complete READY; silent startup may use this budget. |
| Execution: 30 seconds | From completion of END_INPUT send through computation, complete response, and EOF. |
| Overall: 90 seconds | Outer bound on admission/start, input transmission, and response exchange; phase deadlines cannot extend it. |
| Communication idle: 5 seconds | Each bounded send, and gaps between reads after the first byte of READY or RESPONSE, including waiting for EOF. |
| Termination / cleanup: 5 seconds each | Separate teardown grace periods, attempted even after the overall deadline. |

Waiting for the first response byte after END_INPUT is computation time, not a
communication stall. A silent computation may exceed five seconds but must finish
within execution and overall deadlines. Once response bytes arrive, communication
has begun and an idle gap is a stall. Neither byte progress nor partial messages
reset the readiness, execution, or overall deadline. There are no retries,
heartbeats, or increased limits. Exact deadline equality counts as expiration.

These are cooperative driver contracts with an injected clock. Checks before and
after a call cannot interrupt blocked calls or input iteration. No OS timers,
process interruption, or hard execution enforcement are implemented.

## Operation and result

Propose `GET /api/v1/datasets/{dataset_id}/columns/{column_index}/analysis`.
Validate `dataset_id` with the existing storage validator; require a canonical,
non-negative bounded integer index and resolve it against validated stored column
metadata and Parquet schema. Do not accept paths, column names, filters, arbitrary
methods, or raw values from the caller. Check that the artifact and metadata agree.
Analyze integer/float columns numerically and string/boolean columns categorically;
report other types as unsupported. Stored nulls and literal strings retain their
existing meanings. No row samples or category labels are returned.

On success, return `200 application/json` with the engine's versioned aggregate
result and `Cache-Control: no-store`. Return no observations, labels, quartiles,
fences, sample values, or input-derived error text. HTTP mappings remain proposals;
the worker/controller contracts above are defined. Use controlled
`application/problem+json` for malformed
identifiers/indices (400), absent datasets or columns (404), unsupported types
(422), admission limits (429 or 503, to be chosen consistently), corrupt artifacts
(500), and analysis deadlines (504). The existing `ProcessingTimeoutError` public
message says ingestion timed out; add an analysis-specific mapping without changing
the existing upload response. Do not log cell values, labels, exception-supplied
paths, or frame locals. Review existing diagnostic logging at this boundary.

## Resource policy and isolation

Do not use `LocalDatasetStorage.read_parquet_table` as a memory guard: it reads the
whole table. A future worker-side selected-column reader must
check metadata, row counts, schema, and bounded record batches before converting to
Python values. The wire contract permits at most **100,000 rows**, a **64 MiB
decoded-column budget**, and **16 MiB encoded input**. Admission provides one durable
slot when all controllers use the same trusted ledger. These contract limits are
not proof of runtime memory enforcement; deployment sizing still needs benchmarks.
Reject rather than truncate. Count actual decoded buffers and account for
variable-length data; verify pre-read metadata, but do not trust it as the sole
limit. Arrow decompression, Python object conversion, sorting and rational arithmetic
can allocate beyond a decoded-byte budget, including within a single batch. Measure
peak process memory and fail closed when safe admission cannot be established.

The engine checks a cooperative deadline, but cannot interrupt sorting, a blocked
read, or native Arrow work. A **separate worker process with enforced wall-clock
termination and OS resource limits** is necessary for a hard execution deadline;
the parent should cap concurrency across all server workers, not merely inside one
process. Specify worker cleanup, cancellation, queue bounds, recovery, and behavior
if resource enforcement is unavailable before exposing the endpoint. Neither a byte
budget nor worker isolation by itself proves a strict overall memory ceiling.

Subprocess, Hyper-V, AppContainer/LPAC, and network isolation are not implemented.
Parquet parsing is not currently sandboxed. Hyper-V remains deferred. No production
driver or worker runtime is wired into the application.

## Delivery sequence

1. Phase 1C now provides the worker-side analysis adapter and compatibility tests
   described above. It does not parse Parquet or create a runtime. The engine's
   one-million-entry ceiling does not override the smaller request limit.
2. Separately implement reviewed worker-side selected-column decoding, type fidelity,
   decoded-buffer accounting, trusted input sourcing, and an enforcing driver and
   isolation boundary. The adapter cannot prove these guarantees from a Python
   list alone. Define worker-local deadline policy; the wire request carries no
   execution deadline. Approve deployment resource values and HTTP mappings before
   API exposure, retaining frozen behavior and storage artifacts.
3. Add backend tests for selected-column-only reads, corrupted files, null/type
   fidelity, exact limits and one-over, no partial results, concurrent admission,
   deadline/worker cleanup, no leakage, and unchanged upload/metadata responses.
4. Only after backend validation, extend the frontend proxy allowlist and client;
   review same-origin controls for this expensive read operation, no-cache headers,
   cancellation, and accessible progressive UI.

No route, decoder, execution runtime, storage, or frontend integration is implemented
by this phase. Parquet decoding and process isolation remain unimplemented.
