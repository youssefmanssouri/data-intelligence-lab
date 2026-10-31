"""Fake-worker contract tests. No dataset parsers or execution adapters."""

import ast
import json
from dataclasses import fields
from pathlib import Path

import pytest

from backend import worker_controller as c
from backend import worker_protocol as wire
from .fakes import FakeClock, FakeWorker, request_bytes


@pytest.fixture
def setup(tmp_path):
    ledger = c.AdmissionLedger(tmp_path / "gate.sqlite")
    ledger.initialize()
    clock = FakeClock()
    return ledger, clock


def run(setup, **kwargs):
    ledger, clock = setup
    worker = FakeWorker(clock, **kwargs)
    result = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    return result, worker


def test_successful_lifecycle_and_release_order(setup):
    ledger, clock = setup
    observed = []
    def observe(event):
        assert ledger.inspect().state == "HELD"
        with pytest.raises(c.ControllerError) as caught:
            ledger.acquire(b"b"*16)
        assert caught.value.code == c.ErrorCode.BUSY
        observed.append(event)
    outcome, worker = run(setup, observer=observe)
    assert outcome.state == c.State.SUCCEEDED
    assert outcome.error is None and outcome.errors == ()
    assert outcome.response.to_dict()["outcome"] == "ok"
    assert outcome.history == tuple(c.State(name) for name in
        "NEW VALIDATED ADMITTED STARTING READY SENDING WAITING VALIDATING STOPPING CLEANING SUCCEEDED".split())
    assert observed[-2:] == ["stop", "cleanup"]
    assert worker.stopped and worker.cleaned
    assert ledger.inspect().state == "IDLE"


@pytest.mark.parametrize("payload", [b"{}", b"invalid", None, b"[]"])
def test_invalid_request_never_starts(setup, payload):
    ledger, clock = setup
    worker = FakeWorker(clock)
    outcome = c.Controller(ledger, worker, clock).run(payload, [b"x"])
    assert outcome.error == c.ErrorCode.INVALID_REQUEST
    assert worker.calls == [] and ledger.inspect().state == "IDLE"


@pytest.mark.parametrize("quarantined", [False, True])
def test_busy_never_starts(setup, quarantined):
    ledger, _ = setup
    lease = ledger.acquire(b"b"*16)
    if quarantined:
        ledger.quarantine(lease)
    outcome, worker = run(setup)
    assert outcome.error == c.ErrorCode.BUSY and worker.calls == []
    assert ledger.inspect().owner_token == lease.owner_token


@pytest.mark.parametrize("fail,code", [("start",c.ErrorCode.WORKER_FAILED),
    ("ready",c.ErrorCode.WORKER_FAILED),("send",c.ErrorCode.WORKER_FAILED),
    ("response",c.ErrorCode.WORKER_FAILED),("premature",c.ErrorCode.WORKER_FAILED),
    ("protocol",c.ErrorCode.PROTOCOL_ERROR),
    ("extra",c.ErrorCode.PROTOCOL_ERROR)])
def test_failures_teardown_before_release(setup, fail, code):
    outcome, worker = run(setup, fail=fail)
    assert outcome.error == code and outcome.state == c.State.FAILED
    assert outcome.response is None
    assert outcome.worker_error_code is None
    assert worker.calls[-2:] == ["stop", "cleanup"]
    assert setup[0].inspect().state == "IDLE"


@pytest.mark.parametrize("event,seconds", [("start",15),("ready",15),("send",5),("response",30)])
def test_deadlines_at_boundary(setup, event, seconds):
    outcome, worker = run(setup, delays={event:seconds})
    assert outcome.error == c.ErrorCode.TIMEOUT
    assert outcome.state == c.State.FAILED and worker.stopped and worker.cleaned
    assert setup[0].inspect().state == "IDLE"


@pytest.mark.parametrize("stage", ["ready", "response"])
def test_phase_deadlines_despite_trickle_progress(setup, stage):
    ledger, clock = setup
    worker = FakeWorker(clock, delays={stage:1}, fragment=1)
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    assert outcome.error == c.ErrorCode.TIMEOUT
    assert clock.now == (15 if stage == "ready" else 30)
    assert worker.stopped and worker.cleaned


def test_overall_deadline_dominates(setup):
    ledger, clock = setup
    worker = FakeWorker(clock, delays={"send":1})
    outcome = c.Controller(ledger, worker, clock, c.TimingPolicy(overall=2)).run(request_bytes(), [b"x"])
    assert outcome.error == c.ErrorCode.TIMEOUT
    assert worker.stopped and worker.cleaned


@pytest.mark.parametrize("event,code", [("stop",c.ErrorCode.TERMINATION_FAILED),
                                        ("cleanup",c.ErrorCode.CLEANUP_FAILED)])
@pytest.mark.parametrize("timed", [False,True])
def test_teardown_failure_quarantines(setup, event, code, timed):
    kwargs = {"delays":{event:5}} if timed else {"fail":event}
    outcome, worker = run(setup, **kwargs)
    assert outcome.error == code and outcome.state == c.State.QUARANTINED
    assert outcome.response is None
    assert setup[0].inspect().state == "QUARANTINED"
    if event == "stop":
        assert "cleanup" not in worker.calls


@pytest.mark.parametrize("value", [False, None, 1, "yes"])
def test_stop_confirmation_is_literal_true(setup, value):
    ledger, clock = setup
    worker = FakeWorker(clock)
    worker.stop = lambda deadline: value
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    assert outcome.error == c.ErrorCode.TERMINATION_FAILED
    assert outcome.state == c.State.QUARANTINED
    assert "cleanup" not in worker.calls


@pytest.mark.parametrize("value", [False, None, 1, "yes"])
def test_cleanup_confirmation_is_literal_true(setup, value):
    ledger, clock = setup
    worker = FakeWorker(clock)
    worker.cleanup = lambda deadline: value
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    assert outcome.error == c.ErrorCode.CLEANUP_FAILED
    assert outcome.state == c.State.QUARANTINED
    assert ledger.inspect().state == "QUARANTINED"


def test_release_failure_quarantines_and_preserves_error(setup, monkeypatch):
    ledger, clock = setup
    def fail(*args, **kwargs):
        raise RuntimeError("private-ledger-path")
    monkeypatch.setattr(ledger, "release", fail)
    outcome, worker = run(setup, fail="protocol")
    assert outcome.errors == (c.ErrorCode.PROTOCOL_ERROR, c.ErrorCode.ADMISSION_UNAVAILABLE)
    assert outcome.state == c.State.QUARANTINED and outcome.response is None
    assert worker.stopped and worker.cleaned
    assert ledger.inspect().state == "QUARANTINED"
    assert "private" not in repr(outcome)


def test_quarantine_write_failure_leaves_held(setup, monkeypatch):
    ledger, _ = setup
    def fail(*args):
        raise RuntimeError()
    monkeypatch.setattr(ledger, "quarantine", fail)
    outcome, _ = run(setup, fail="stop")
    assert outcome.errors == (c.ErrorCode.TERMINATION_FAILED, c.ErrorCode.ADMISSION_UNAVAILABLE)
    assert outcome.state == c.State.QUARANTINED and ledger.inspect().state == "HELD"


def test_uninitialized_gate_no_worker(tmp_path):
    clock = FakeClock()
    worker = FakeWorker(clock)
    outcome = c.Controller(c.AdmissionLedger(tmp_path / "missing"), worker, clock).run(request_bytes(), [b"x"])
    assert outcome.error == c.ErrorCode.ADMISSION_UNAVAILABLE and not worker.calls


@pytest.mark.parametrize("field", [f.name for f in fields(c.TimingPolicy)])
@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf"), -float("inf"), "5", None, 10**1000])
def test_policy_rejects_invalid(field, value):
    with pytest.raises(ValueError):
        c.TimingPolicy(**{field:value})


def test_policy_defaults_and_valid_overrides():
    policy = c.TimingPolicy()
    assert tuple(getattr(policy,f.name) for f in fields(policy)) == (15,30,90,5,5,5)
    assert c.TimingPolicy(idle=0.1, overall=1).idle == 0.1


def test_fragmented_response(setup):
    outcome, _ = run(setup, fragment=1)
    assert outcome.state == c.State.SUCCEEDED


@pytest.mark.parametrize("chunks", [[], [b"y"], [b"xx"], [b"x",b"x"]])
def test_input_integrity_uses_phase1a(setup, chunks):
    ledger, clock = setup
    worker = FakeWorker(clock)
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), chunks)
    assert outcome.error == c.ErrorCode.PROTOCOL_ERROR and worker.cleaned
    assert ledger.inspect().state == "IDLE"


def test_cancellation_tears_down_before_propagating(setup):
    ledger, clock = setup
    worker = FakeWorker(clock)
    def interrupted():
        raise KeyboardInterrupt()
        yield b"x"
    with pytest.raises(KeyboardInterrupt):
        c.Controller(ledger, worker, clock).run(request_bytes(), interrupted())
    assert worker.stopped and worker.cleaned and ledger.inspect().state == "IDLE"


def test_import_dependencies_are_narrow():
    tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))
    imports = {n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)}
    imports |= {a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names}
    assert imports == {"contextlib","dataclasses","enum","math","pathlib","secrets","sqlite3","typing","backend"}
    from_backend = [n for n in ast.walk(tree) if isinstance(n,ast.ImportFrom) and n.module == "backend"]
    assert [[a.name for a in n.names] for n in from_backend] == [["worker_protocol"]]


def test_import_does_not_open_files_database_or_generate_tokens(monkeypatch):
    import builtins
    import importlib.util
    import sys
    source = Path(c.__file__).read_text(encoding="utf-8")
    code = compile(source, c.__file__, "exec")
    spec = importlib.util.spec_from_loader("_controller_import_probe", loader=None)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected import-time side effect")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(c.sqlite3, "connect", forbidden)
    monkeypatch.setattr(c.secrets, "token_bytes", forbidden)
    exec(code, module.__dict__)


class ScriptedWorker(FakeWorker):
    """Inert responses and per-read delays; never executes analysis or input."""

    def __init__(self, clock, *, payload=None, read_delays=None, suffix=b"", **kwargs):
        super().__init__(clock, **kwargs)
        self.payload = payload
        self.read_delays = {phase: iter(delays) for phase, delays in (read_delays or {}).items()}
        self.suffix = suffix

    def send(self, data, deadline):
        super().send(data, deadline)
        if data[5] == wire.MessageType.END_INPUT and self.payload is not None:
            self.pending[:] = wire.Frame(wire.MessageType.RESPONSE, self.job, 1,
                                         self.payload).to_bytes() + self.suffix

    def receive(self, max_bytes, deadline):
        self.delays[self.phase] = next(self.read_delays.get(self.phase, iter(())), 0)
        return super().receive(max_bytes, deadline)


def worker_error_payload(code):
    return json.dumps(dict(schema_version="1.0", outcome="error", error_code=code)).encode()


@pytest.mark.parametrize("code", ["INVALID_INPUT", "UNSUPPORTED_TYPE", "RESOURCE_LIMIT", "INTERNAL_ERROR"])
def test_worker_error_preserved_separately_from_lifecycle(setup, code):
    ledger, clock = setup
    payload = worker_error_payload(code)
    worker = ScriptedWorker(clock, payload=payload, fragment=1)
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    assert outcome.state == c.State.FAILED
    assert outcome.error is None and outcome.errors == ()
    assert outcome.worker_error_code == code
    assert outcome.response.to_dict() == json.loads(payload)
    assert worker.stopped and worker.cleaned and ledger.inspect().state == "IDLE"


def test_existing_fake_reported_error_is_preserved(setup):
    outcome, worker = run(setup, fail="reported_error")
    assert outcome.state == c.State.FAILED
    assert outcome.worker_error_code == "INTERNAL_ERROR"
    assert outcome.error is None and outcome.errors == ()
    assert outcome.response.to_dict()["error_code"] == "INTERNAL_ERROR"
    assert worker.stopped and worker.cleaned


@pytest.mark.parametrize("payload", [b"{", worker_error_payload("UNKNOWN"),
    b'{"schema_version":"1.0","outcome":"error","error_code":"RESOURCE_LIMIT","message":"private-sentinel"}'])
def test_invalid_worker_error_is_protocol_failure(setup, payload):
    ledger, clock = setup
    worker = ScriptedWorker(clock, payload=payload)
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    assert outcome.error == c.ErrorCode.PROTOCOL_ERROR
    assert outcome.worker_error_code is None and outcome.response is None
    assert "private-sentinel" not in repr(outcome)
    assert worker.stopped and worker.cleaned and ledger.inspect().state == "IDLE"


def test_valid_error_followed_by_extra_bytes_is_not_published(setup):
    ledger, clock = setup
    worker = ScriptedWorker(clock, payload=worker_error_payload("RESOURCE_LIMIT"), suffix=b"x", fragment=1)
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    assert outcome.error == c.ErrorCode.PROTOCOL_ERROR
    assert outcome.worker_error_code is None and outcome.response is None


@pytest.mark.parametrize("failure,expected", [("stop", c.ErrorCode.TERMINATION_FAILED),
                                             ("cleanup", c.ErrorCode.CLEANUP_FAILED)])
def test_worker_error_survives_teardown_failure_without_releasing_slot(setup, failure, expected):
    ledger, clock = setup
    worker = ScriptedWorker(clock, payload=worker_error_payload("RESOURCE_LIMIT"), fail=failure)
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    assert outcome.state == c.State.QUARANTINED
    assert outcome.worker_error_code == "RESOURCE_LIMIT"
    assert outcome.error == expected and outcome.errors == (expected,)
    assert outcome.response is None and ledger.inspect().state == "QUARANTINED"


@pytest.mark.parametrize("seconds", [5, 10, 29.9])
def test_silent_computation_can_exceed_idle_interval(setup, seconds):
    ledger, clock = setup
    worker = ScriptedWorker(clock, read_delays={"response": [seconds]})
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    assert outcome.state == c.State.SUCCEEDED and outcome.worker_error_code is None
    assert clock.now == seconds
    assert worker.stopped and worker.cleaned and ledger.inspect().state == "IDLE"


def test_silent_startup_uses_readiness_budget(setup):
    ledger, clock = setup
    worker = ScriptedWorker(clock, read_delays={"ready": [14]})
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    assert outcome.state == c.State.SUCCEEDED and clock.now == 14


@pytest.mark.parametrize("phase", ["ready", "response"])
@pytest.mark.parametrize("gap", [5, 6])
def test_partial_message_stall_uses_idle_timeout(setup, phase, gap):
    ledger, clock = setup
    worker = ScriptedWorker(clock, fragment=1, read_delays={phase: [0, gap]})
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    assert outcome.error == c.ErrorCode.TIMEOUT and clock.now == gap
    assert outcome.response is None and outcome.worker_error_code is None
    assert worker.stopped and worker.cleaned and ledger.inspect().state == "IDLE"


@pytest.mark.parametrize("gap", [4, 5, 6])
def test_error_response_requires_timely_eof(setup, gap):
    ledger, clock = setup
    worker = ScriptedWorker(clock, payload=worker_error_payload("INVALID_INPUT"),
                            read_delays={"response": [0, gap]})
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    assert outcome.state == c.State.FAILED
    if gap < 5:
        assert outcome.error is None and outcome.worker_error_code == "INVALID_INPUT"
        assert outcome.response is not None
    else:
        assert outcome.error == c.ErrorCode.TIMEOUT
        assert outcome.worker_error_code is None and outcome.response is None


def test_execution_deadline_is_not_reset_when_response_begins(setup):
    ledger, clock = setup
    worker = ScriptedWorker(clock, read_delays={"response": [29, 1]})
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    assert outcome.error == c.ErrorCode.TIMEOUT and clock.now == 30
    assert outcome.response is None and outcome.worker_error_code is None


def test_overall_deadline_caps_silent_computation(setup):
    ledger, clock = setup
    worker = ScriptedWorker(clock, read_delays={"ready": [10], "response": [10]})
    outcome = c.Controller(ledger, worker, clock, c.TimingPolicy(overall=20)).run(request_bytes(), [b"x"])
    assert outcome.error == c.ErrorCode.TIMEOUT and clock.now == 20
    assert worker.stopped and worker.cleaned


def test_execution_budget_begins_after_input_transmission(setup):
    ledger, clock = setup
    worker = ScriptedWorker(clock, delays={"send": 4}, read_delays={"response": [29]})
    outcome = c.Controller(ledger, worker, clock).run(request_bytes(), [b"x"])
    assert outcome.state == c.State.SUCCEEDED and clock.now == 41
