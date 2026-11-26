"""Temporary SQLite gate tests; child processes only contend for admission."""

from contextlib import closing
from dataclasses import replace
import gc
import multiprocessing
import queue
import sqlite3
import time

import pytest

from backend.worker_controller import AdmissionLedger, ControllerError, ErrorCode, Lease
from .fakes import contend

JOB = b"a" * 16


@pytest.fixture
def ledger(tmp_path):
    gate = AdmissionLedger(tmp_path / "admission.sqlite")
    gate.initialize()
    return gate


def test_acquire_commit_release(ledger):
    lease = ledger.acquire(JOB)
    assert len(lease.owner_token) == 32
    assert ledger.inspect().state == "HELD"
    # Separate connection can obtain a writer transaction: none remains open.
    with closing(ledger._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        assert db.execute("SELECT job_id FROM admission").fetchone()[0] == JOB
        db.rollback()
    ledger.release(lease, stopped=True, cleaned=True)
    assert ledger.inspect().state == "IDLE"
    assert ledger.acquire(JOB).owner_token != lease.owner_token


def test_busy_and_abandonment(ledger):
    lease = ledger.acquire(JOB)
    del lease
    for _ in range(3):
        with pytest.raises(ControllerError) as caught:
            ledger.acquire(b"b" * 16)
        assert caught.value.code == ErrorCode.BUSY
    assert ledger.inspect().state == "HELD"


@pytest.mark.parametrize("wrong", ["job", "token"])
def test_wrong_owner(ledger, wrong):
    lease = ledger.acquire(JOB)
    forged = replace(lease, **({"job_id": b"b"*16} if wrong == "job" else {"owner_token": b"z"*32}))
    for operation in (lambda: ledger.release(forged, stopped=True, cleaned=True),
                      lambda: ledger.quarantine(forged)):
        with pytest.raises(ControllerError):
            operation()
    assert ledger.inspect().owner_token == lease.owner_token
    assert ledger.inspect().state == "HELD"


@pytest.mark.parametrize("stopped,cleaned", [(False,False),(False,True),(True,False),(1,True),(True,1),(None,True)])
def test_release_requires_confirmations(ledger, stopped, cleaned):
    lease = ledger.acquire(JOB)
    with pytest.raises(ControllerError):
        ledger.release(lease, stopped=stopped, cleaned=cleaned)
    assert ledger.inspect().state == "HELD"


def test_quarantine_cannot_be_released_or_stolen(ledger):
    lease = ledger.acquire(JOB)
    ledger.quarantine(lease)
    ledger.quarantine(lease)  # Idempotent, but no recovery transition.
    with pytest.raises(ControllerError) as caught:
        ledger.acquire(JOB)
    assert caught.value.code == ErrorCode.BUSY
    with pytest.raises(ControllerError):
        ledger.release(lease, stopped=True, cleaned=True)
    assert ledger.inspect().state == "QUARANTINED"


def test_explicit_initialization_only(tmp_path):
    path = tmp_path / "new.sqlite"
    gate = AdmissionLedger(path)
    for operation in (gate.inspect, lambda: gate.acquire(JOB)):
        with pytest.raises(ControllerError) as caught:
            operation()
        assert caught.value.code == ErrorCode.ADMISSION_UNAVAILABLE
        assert not path.exists()
    gate.initialize()
    lease = gate.acquire(JOB)
    with pytest.raises(ControllerError):
        gate.initialize()
    assert gate.inspect().owner_token == lease.owner_token


@pytest.mark.parametrize("case", ["corrupt", "empty", "missing_row"])
def test_unusable_database_is_not_repaired(tmp_path, case):
    path = tmp_path / "gate.sqlite"
    gate = AdmissionLedger(path)
    if case == "corrupt":
        path.write_bytes(b"not a database")
    elif case == "empty":
        path.touch()
    else:
        gate.initialize()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("DELETE FROM admission")
    before = path.read_bytes()
    with pytest.raises(ControllerError) as caught:
        gate.acquire(JOB)
    assert caught.value.code == ErrorCode.ADMISSION_UNAVAILABLE
    assert path.read_bytes() == before


def test_locked_database_immediately_busy(ledger):
    db = ledger._connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ControllerError) as caught:
            ledger.acquire(JOB)
        assert caught.value.code == ErrorCode.BUSY
    finally:
        db.rollback()
        db.close()


def test_deleted_ledger_is_never_recreated(tmp_path):
    path = tmp_path / "deleted.sqlite"
    gate = AdmissionLedger(path)
    gate.initialize()
    gate.acquire(JOB)
    path.unlink()
    with pytest.raises(ControllerError) as caught:
        gate.acquire(JOB)
    assert caught.value.code == ErrorCode.ADMISSION_UNAVAILABLE
    assert not path.exists()


def test_reopened_gate_has_no_expiry_or_pid_ownership(tmp_path, monkeypatch):
    import time
    path = tmp_path / "abandoned.sqlite"
    gate = AdmissionLedger(path)
    gate.initialize()
    lease = gate.acquire(JOB)
    del gate
    monkeypatch.setattr(time, "time", lambda: 10**15)
    monkeypatch.setattr(time, "monotonic", lambda: 10**15)
    reopened = AdmissionLedger(path)
    with pytest.raises(ControllerError) as caught:
        reopened.acquire(b"b"*16)
    assert caught.value.code == ErrorCode.BUSY
    assert reopened.inspect().owner_token == lease.owner_token


def test_singleton_constraint_and_stale_release(ledger):
    db = ledger._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO admission VALUES(2,'IDLE',NULL,NULL)")
    finally:
        db.close()
    old = ledger.acquire(JOB)
    ledger.release(old, stopped=True, cleaned=True)
    new = ledger.acquire(JOB)
    with pytest.raises(ControllerError):
        ledger.release(old, stopped=True, cleaned=True)
    assert ledger.inspect().owner_token == new.owner_token


# Hang guard for spawned children; success never waits on it. Must exceed the
# 15 s barrier timeout in fakes.contend so a missing sibling surfaces as a
# child failure rather than a parent timeout.
CHILD_HANG_GUARD_S = 20


@pytest.fixture
def no_cyclic_gc():
    """Keep the cyclic GC out of multiprocessing's resource tracker.

    These tests leave Barrier/Queue SemLocks in reference cycles (for example
    test frame -> pytest.raises ExceptionInfo -> traceback -> frame). If a later
    collection frees them while this process is inside the resource tracker
    (registering a new SemLock or spawning a child), the SemLock finalizer
    re-enters it. CPython 3.12 reports that as an unraisable UserWarning
    ("ResourceTracker called reentrantly"), which filterwarnings=error turns
    into a failure of whichever test is running; 3.13+ queue the message instead.
    Collecting at safe points and pausing automatic collection in between makes
    finalization deterministic. It changes no assertion.
    """
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()
        gc.collect()


def _next_result(results, children):
    """Return the next child result, failing fast and descriptively.

    A child that exits non-zero before reporting fails immediately with its
    exit code instead of an opaque queue.Empty after the full guard period.
    """
    deadline = time.monotonic() + CHILD_HANG_GUARD_S
    while True:
        try:
            return results.get(timeout=0.1)
        except queue.Empty:
            pass
        crashed = [(c.name, c.exitcode) for c in children if c.exitcode not in (None, 0)]
        assert not crashed, f"child exited before reporting a result: {crashed}"
        assert time.monotonic() < deadline, (
            f"no child result within {CHILD_HANG_GUARD_S}s; "
            f"alive={[(c.name, c.is_alive()) for c in children]}")


def _join_ok(child):
    child.join(timeout=CHILD_HANG_GUARD_S)
    assert child.exitcode == 0, f"{child.name} exitcode={child.exitcode}"


def test_cross_process_contention_and_owner_exit(tmp_path, no_cyclic_gc):
    path = tmp_path / "shared.sqlite"
    gate = AdmissionLedger(path)
    gate.initialize()
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    children = [context.Process(target=contend, args=(path, bytes([i])*16, barrier, results))
                for i in (1,2)]
    try:
        for child in children:
            child.start()
        outcomes = [_next_result(results, children) for _ in children]
        for child in children:
            _join_ok(child)
        states = [x[0] for x in outcomes]
        assert set(states) <= {"BUSY", "HELD"}
        assert states.count("HELD") <= 1
        # With timeout=0, a competing transient reader can prevent the first
        # writer's commit too. Both BUSY is safe, and must leave IDLE. Exercise
        # an uncontended child separately in that case; this is not a retry
        # policy in the ledger or controller.
        if "HELD" not in states:
            assert gate.inspect().state == "IDLE"
            # Keep a parent-side reference: Process.start() drops its args, and
            # with "spawn" a SemLock is unlinked when the parent object is freed,
            # so an inline Barrier makes the child fail to reopen it (ENOENT).
            owner_barrier = context.Barrier(1)
            owner = context.Process(target=contend,
                args=(path, b"c"*16, owner_barrier, results))
            children.append(owner)
            owner.start()
            assert _next_result(results, [owner])[0] == "HELD"
            _join_ok(owner)
        assert gate.inspect().state == "HELD"  # Owner has exited, no recovery.
        with pytest.raises(ControllerError) as caught:
            gate.acquire(JOB)
        assert caught.value.code == ErrorCode.BUSY
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
        results.close()
        results.join_thread()


def test_forced_both_busy_then_uncontended_owner_exit(tmp_path, no_cyclic_gc):
    """Deterministically cover the rare both-BUSY branch of the test above.

    The parent holds the writer lock while both children contend, so both must
    observe BUSY and leave the slot IDLE; an uncontended owner then acquires,
    exits, and the slot stays HELD with no recovery.
    """
    path = tmp_path / "shared.sqlite"
    gate = AdmissionLedger(path)
    gate.initialize()
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    owner_barrier = context.Barrier(1)  # Parent reference outlives the child.
    results = context.Queue()
    children = [context.Process(target=contend, args=(path, bytes([i])*16, barrier, results))
                for i in (1, 2)]
    try:
        lock = gate._connect()
        try:
            lock.execute("BEGIN IMMEDIATE")
            for child in children:
                child.start()
            outcomes = [_next_result(results, children) for _ in children]
            for child in children:
                _join_ok(child)
        finally:
            lock.rollback()
            lock.close()
        assert sorted(outcomes) == [("BUSY", bytes([1])*16), ("BUSY", bytes([2])*16)]
        assert gate.inspect().state == "IDLE"
        owner = context.Process(target=contend, args=(path, b"c"*16, owner_barrier, results))
        children.append(owner)
        owner.start()
        assert _next_result(results, [owner]) == ("HELD", b"c"*16)
        _join_ok(owner)
        record = gate.inspect()
        assert (record.state, record.job_id) == ("HELD", b"c"*16)  # Owner exited, no recovery.
        with pytest.raises(ControllerError) as caught:
            gate.acquire(JOB)
        assert caught.value.code == ErrorCode.BUSY
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
        results.close()
        results.join_thread()
