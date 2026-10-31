"""Controller/admission contracts, not a production execution implementation.

Drivers and input iterators must honor bounded-call contracts. Checking an
injected clock before/after calls cannot interrupt a blocked call. Coordination
tokens are not authentication. SQLite paths belong to the trusted controller;
there is no dataset filesystem access, worker launch, or automatic recovery.
"""

from contextlib import closing
from dataclasses import dataclass, fields
from enum import Enum
import math
from pathlib import Path
import secrets
import sqlite3
from typing import Callable, Iterable, Protocol

from backend import worker_protocol as wire


class ErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    BUSY = "BUSY"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    TIMEOUT = "TIMEOUT"
    WORKER_FAILED = "WORKER_FAILED"
    TERMINATION_FAILED = "TERMINATION_FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    ADMISSION_UNAVAILABLE = "ADMISSION_UNAVAILABLE"


class ControllerError(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code.value)


class State(str, Enum):
    NEW = "NEW"
    VALIDATED = "VALIDATED"
    ADMITTED = "ADMITTED"
    STARTING = "STARTING"
    READY = "READY"
    SENDING = "SENDING"
    WAITING = "WAITING"
    VALIDATING = "VALIDATING"
    STOPPING = "STOPPING"
    CLEANING = "CLEANING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class TimingPolicy:
    """Phase ceilings plus communication-stall limits, in seconds.

    Readiness includes start and waiting for the first READY byte. Execution
    starts after END_INPUT is sent and includes computation, response and EOF.
    Idle bounds each send and gaps after the first byte of READY or RESPONSE,
    including the final EOF wait; it never bounds silent startup/computation.
    Overall caps these phases. Teardown has separate bounded grace periods.
    """
    readiness: float = 15.0
    execution: float = 30.0
    overall: float = 90.0
    idle: float = 5.0
    termination: float = 5.0
    cleanup: float = 5.0

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and value > 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("Timing values must be finite positive numbers.")


@dataclass(frozen=True, slots=True)
class Lease:
    job_id: bytes
    owner_token: bytes

    def __post_init__(self):
        if (type(self.job_id) is not bytes or len(self.job_id) != 16 or
                type(self.owner_token) is not bytes or len(self.owner_token) != 32):
            raise ControllerError(ErrorCode.ADMISSION_UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class AdmissionRecord:
    state: str
    job_id: bytes | None
    owner_token: bytes | None


class AdmissionLedger:
    """Single durable slot on local SQLite storage; no expiry or recovery API.

    Every participant must use the same trusted database. Release confirmation
    flags are assertions from the controller, not proof of real containment.
    Missing/corrupt/schema-incompatible stores fail closed. An uncertain commit
    can leave HELD; this is intentionally not repaired automatically.
    """

    def __init__(self, path: str | Path):
        self._uri = Path(path).absolute().as_uri()

    def _connect(self, create=False):
        connection = sqlite3.connect(self._uri + ("?mode=rwc" if create else "?mode=rw"),
                                     uri=True, timeout=0, isolation_level=None)
        return connection

    @staticmethod
    def _error(exc):
        code = getattr(exc, "sqlite_errorcode", None)
        return ControllerError(ErrorCode.BUSY if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                               else ErrorCode.ADMISSION_UNAVAILABLE)

    def initialize(self):
        """Explicit first initialization only; never resets an existing slot."""
        try:
            with closing(self._connect(create=True)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""CREATE TABLE admission (
                    slot INTEGER PRIMARY KEY CHECK(slot=1),
                    state TEXT NOT NULL CHECK(state IN ('IDLE','HELD','QUARANTINED')),
                    job_id BLOB, owner_token BLOB,
                    CHECK((state='IDLE' AND job_id IS NULL AND owner_token IS NULL) OR
                          (state!='IDLE' AND typeof(job_id)='blob' AND length(job_id)=16
                           AND typeof(owner_token)='blob' AND length(owner_token)=32)))""")
                db.execute("INSERT INTO admission VALUES (1,'IDLE',NULL,NULL)")
        except sqlite3.Error as exc:
            raise self._error(exc) from None

    @staticmethod
    def _record(db):
        rows = db.execute("SELECT slot,state,job_id,owner_token FROM admission").fetchmany(2)
        if len(rows) != 1 or rows[0][0] != 1:
            raise ControllerError(ErrorCode.ADMISSION_UNAVAILABLE)
        _, state, job, token = rows[0]
        if state == "IDLE":
            if job is not None or token is not None:
                raise ControllerError(ErrorCode.ADMISSION_UNAVAILABLE)
        elif state in ("HELD", "QUARANTINED"):
            Lease(job, token)
        else:
            raise ControllerError(ErrorCode.ADMISSION_UNAVAILABLE)
        return AdmissionRecord(state, job, token)

    def inspect(self) -> AdmissionRecord:
        try:
            with closing(self._connect()) as db:
                return self._record(db)
        except sqlite3.Error as exc:
            raise self._error(exc) from None

    def acquire(self, job_id: bytes) -> Lease:
        lease = Lease(job_id, secrets.token_bytes(32))
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if self._record(db).state != "IDLE":
                    raise ControllerError(ErrorCode.BUSY)
                db.execute("UPDATE admission SET state='HELD',job_id=?,owner_token=? WHERE slot=1",
                           (lease.job_id, lease.owner_token))
            return lease  # Context manager has committed, and connection is closed.
        except sqlite3.Error as exc:
            raise self._error(exc) from None

    def _change_owned(self, lease, target):
        if type(lease) is not Lease:
            raise ControllerError(ErrorCode.ADMISSION_UNAVAILABLE)
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                record = self._record(db)
                if (record.job_id != lease.job_id or record.owner_token != lease.owner_token or
                        record.state not in (("HELD",) if target == "IDLE" else ("HELD", "QUARANTINED"))):
                    raise ControllerError(ErrorCode.ADMISSION_UNAVAILABLE)
                if target == "IDLE":
                    db.execute("UPDATE admission SET state='IDLE',job_id=NULL,owner_token=NULL WHERE slot=1")
                else:
                    db.execute("UPDATE admission SET state='QUARANTINED' WHERE slot=1")
        except sqlite3.Error as exc:
            raise self._error(exc) from None

    def release(self, lease: Lease, *, stopped: bool, cleaned: bool) -> None:
        if stopped is not True or cleaned is not True:
            raise ControllerError(ErrorCode.ADMISSION_UNAVAILABLE)
        self._change_owned(lease, "IDLE")

    def quarantine(self, lease: Lease) -> None:
        self._change_owned(lease, "QUARANTINED")


class WorkerDriver(Protocol):
    """Injected driver only; methods must honor absolute clock-domain deadlines.

    send accepts a complete bounded frame. receive returns 1..max_bytes bytes,
    or b'' for EOF; raise TimeoutError on a deadline, not an empty idle read.
    stop returns literal True only after stopped state is confirmed, even after
    a partial start failure. cleanup is called only after that confirmation.
    """

    def start(self, job_id: bytes, deadline: float) -> None: ...
    def send(self, data: bytes, deadline: float) -> None: ...
    def receive(self, max_bytes: int, deadline: float) -> bytes: ...
    def stop(self, deadline: float) -> bool: ...
    def cleanup(self, deadline: float) -> bool: ...


@dataclass(frozen=True, slots=True)
class Outcome:
    """Lifecycle errors and validated worker result errors are separate.

    worker_error_code is set only after a complete response and EOF. It survives
    teardown failures, but response is withheld whenever lifecycle errors occur.
    A worker error yields FAILED with no lifecycle error if teardown succeeds.
    """
    state: State
    error: ErrorCode | None
    errors: tuple[ErrorCode, ...]
    response: wire.Response | None
    history: tuple[State, ...]
    worker_error_code: str | None = None


class Controller:
    """Synchronous contract runner. No default driver and no execution fallback.

    Input is an iterable of already bounded, full DATA payloads (last may be
    short), never a path. Driver calls and iteration are cooperative. Real
    watchdog enforcement is explicitly outside this module.
    """

    def __init__(self, ledger: AdmissionLedger, driver: WorkerDriver,
                 clock: Callable[[], float], policy: TimingPolicy | None = None):
        self.ledger = ledger
        self.driver = driver
        self.clock = clock
        self.policy = policy if policy is not None else TimingPolicy()
        if type(self.policy) is not TimingPolicy:
            raise ValueError("Invalid timing policy.")

    def _call(self, deadline, operation, *args):
        if self.clock() >= deadline:
            raise TimeoutError()
        result = operation(*args, deadline)
        if self.clock() >= deadline:
            raise TimeoutError()
        return result

    def run(self, request_payload: bytes, chunks: Iterable[bytes]) -> Outcome:
        history = [State.NEW]
        errors = []
        response = None
        worker_error_code = None
        lease = None
        interrupted = None

        def outcome(state):
            history.append(state)
            return Outcome(state, errors[-1] if errors else None, tuple(errors),
                           response if not errors else None, tuple(history), worker_error_code)

        try:
            request = wire.decode_request(request_payload)
        except wire.ProtocolError:
            errors.append(ErrorCode.INVALID_REQUEST)
            return outcome(State.FAILED)
        history.append(State.VALIDATED)
        overall = self.clock() + self.policy.overall
        job = secrets.token_bytes(16)
        try:
            lease = self.ledger.acquire(job)
        except ControllerError as exc:
            errors.append(exc.code)
            return outcome(State.FAILED)
        history.append(State.ADMITTED)
        decoder = wire.ExchangeDecoder(job)
        try:
            history.append(State.STARTING)
            ready_deadline = min(overall, self.clock() + self.policy.readiness)
            self._call(ready_deadline, self.driver.start, job)
            ready = False
            ready_started = False
            while not ready:
                deadline = (min(ready_deadline, self.clock() + self.policy.idle)
                            if ready_started else ready_deadline)
                data = self._call(deadline,
                                  self.driver.receive, wire.HEADER.size + wire.DATA_LIMIT)
                if data == b"":
                    raise RuntimeError()
                frames = decoder.feed("worker", data)
                ready_started = True
                ready = any(frame.kind == wire.MessageType.READY for frame in frames)
            history.extend((State.READY, State.SENDING))

            def send(kind, sequence, payload=b""):
                packet = wire.Frame(kind, job, sequence, payload).to_bytes()
                decoder.feed("controller", packet)
                self._call(min(overall, self.clock() + self.policy.idle), self.driver.send, packet)

            send(wire.MessageType.REQUEST, 0, request.to_bytes())
            sequence = 1
            for chunk in chunks:
                if self.clock() >= overall:
                    raise TimeoutError()
                send(wire.MessageType.DATA, sequence, chunk)
                sequence += 1
            send(wire.MessageType.END_INPUT, sequence)
            execution_deadline = min(overall, self.clock() + self.policy.execution)
            history.append(State.WAITING)
            validating = False
            while True:
                deadline = (min(execution_deadline, self.clock() + self.policy.idle)
                            if validating else execution_deadline)
                data = self._call(deadline,
                                  self.driver.receive, wire.HEADER.size + wire.DATA_LIMIT)
                if data == b"":
                    if not validating:
                        raise RuntimeError()
                    response = decoder.finish()
                    break
                if not validating:
                    history.append(State.VALIDATING)
                    validating = True
                decoder.feed("worker", data)
            worker_error_code = response.to_dict().get("error_code")
        except wire.ProtocolError:
            errors.append(ErrorCode.PROTOCOL_ERROR)
        except TimeoutError:
            errors.append(ErrorCode.TIMEOUT)
        except Exception:
            errors.append(ErrorCode.WORKER_FAILED)
        except BaseException as exc:
            # Cancellation must not release a slot without teardown. Re-raise
            # only after attempting the same cleanup as ordinary failures.
            interrupted = exc
            errors.append(ErrorCode.WORKER_FAILED)

        history.append(State.STOPPING)
        stopped = cleaned = False
        try:
            stopped = self._call(self.clock() + self.policy.termination, self.driver.stop) is True
        except Exception:
            pass
        if not stopped:
            errors.append(ErrorCode.TERMINATION_FAILED)
        else:
            history.append(State.CLEANING)
            try:
                cleaned = self._call(self.clock() + self.policy.cleanup, self.driver.cleanup) is True
            except Exception:
                pass
            if not cleaned:
                errors.append(ErrorCode.CLEANUP_FAILED)
        quarantined = not (stopped and cleaned)
        if not quarantined:
            try:
                self.ledger.release(lease, stopped=True, cleaned=True)
            except Exception:
                errors.append(ErrorCode.ADMISSION_UNAVAILABLE)
                quarantined = True
        if quarantined:
            try:
                self.ledger.quarantine(lease)
            except Exception:
                # A failed quarantine write leaves HELD or uncertain durable
                # state. Never clear it or report successful release.
                errors.append(ErrorCode.ADMISSION_UNAVAILABLE)
        final = outcome(State.QUARANTINED if quarantined else
                        (State.FAILED if errors or worker_error_code else State.SUCCEEDED))
        if interrupted is not None:
            raise interrupted
        return final
