"""Real engine compatibility plus admission, dispatch and privacy regressions."""

import ast
import builtins
from dataclasses import replace
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from backend import worker_adapter as adapter
from backend import worker_protocol as wire
from core import statistical_analysis as engine
from core import types as core_types
from backend import worker_controller as controller
from .fakes import FakeClock, FakeWorker
from core.types import ExecutionDeadline, ProcessingTimeoutError


def request(operation="numeric_outliers", **changes):
    return replace(wire.Request("1.0", operation, "parquet", 1,
                               hashlib.sha256(b"x").hexdigest(), 0, 100000, 67108864), **changes)


def assert_error(response, code):
    assert type(response) is wire.Response
    assert response.to_dict() == {"schema_version": "1.0", "outcome": "error", "error_code": code}
    assert len(response.payload) <= wire.RESPONSE_LIMIT
    assert b"private-sentinel" not in response.payload


@pytest.mark.parametrize("operation,values,status", [
    ("numeric_outliers", [], "EMPTY"),
    ("numeric_outliers", [None], "ALL_NULL"),
    ("numeric_outliers", [1, 2, None], "INSUFFICIENT_SAMPLE"),
    ("numeric_outliers", [5, 5, 5, 5], "CONSTANT"),
    ("numeric_outliers", [0, 0, 0, 0, 1], "ZERO_IQR"),
    ("numeric_outliers", [1, 2, 3, 4, 100], "SCREENED"),
    ("numeric_outliers", [True], "INVALID_INPUT"),
    ("numeric_outliers", [2**63], "UNSUPPORTED_NUMERIC_RANGE"),
    ("categorical_counts", [], "EMPTY"),
    ("categorical_counts", [None], "ALL_NULL"),
    ("categorical_counts", ["private-sentinel", "other", "other", None], "COUNTED"),
    ("categorical_counts", [True, False, True], "COUNTED"),
    ("categorical_counts", [True, "private-sentinel"], "INVALID_INPUT"),
    ("categorical_counts", [str(i) for i in range(10001)], "CATEGORY_LIMIT_EXCEEDED"),
])
@pytest.mark.parametrize("container", [list, tuple])
def test_all_engine_statuses_preserved_exactly(operation, values, status, container):
    values = container(values)
    before = values[:]
    fn = engine.screen_numeric if operation == "numeric_outliers" else engine.summarize_categories
    expected = fn(values).to_dict()
    response = adapter.analyze_column(request(operation), values)
    assert type(response) is wire.Response
    assert response.to_dict() == {"schema_version": "1.0", "outcome": "ok", "result": expected}
    assert expected["status"] == status
    assert values == before
    assert len(response.payload) <= wire.RESPONSE_LIMIT
    assert b"private-sentinel" not in response.payload and b'"other"' not in response.payload


@pytest.mark.parametrize("operation,values", [
    ("numeric_outliers", [float("nan")]), ("numeric_outliers", [float("inf")]),
    ("numeric_outliers", ["private-sentinel"]),
    ("categorical_counts", ["\ud800"]), ("categorical_counts", ["x" * 32769]),
    ("categorical_counts", [1]),
])
def test_native_invalid_values_keep_engine_semantics(operation, values):
    result = adapter.analyze_column(request(operation), values).to_dict()
    assert result["outcome"] == "ok" and result["result"]["status"] == "INVALID_INPUT"
    assert result["result"]["findings"][0]["evidence"] == {}


class Hostile:
    def forbidden(self, *args):
        raise AssertionError("Conversion or inspection must not run")
    __str__ = __repr__ = __float__ = __int__ = __iter__ = __len__ = __sizeof__ = forbidden


class HostileList(list):
    __len__ = __iter__ = __sizeof__ = Hostile.forbidden


class HostileInt(int):
    __sizeof__ = __int__ = Hostile.forbidden


class HostileString(str):
    __sizeof__ = encode = Hostile.forbidden


@pytest.mark.parametrize("bad", [None, {}, b"x", "path", Hostile(), 1],
                         ids=["none", "dict", "bytes", "path", "custom", "int"])
def test_invalid_request_representation(bad, monkeypatch):
    monkeypatch.setattr(engine, "screen_numeric", Hostile().forbidden)
    with pytest.raises(wire.ProtocolError, match="^Invalid worker protocol message\\.$"):
        adapter.analyze_column(bad, [1])


@pytest.mark.parametrize("field,value", [("operation", "exec"), ("operation", "SELECT 1"),
    ("schema_version", "2.0"), ("input_format", "csv"), ("max_rows", True),
    ("max_decoded_bytes", 67108865)])
def test_tampered_request_uses_protocol_validation(field, value, monkeypatch):
    req = request()
    object.__setattr__(req, field, value)
    monkeypatch.setattr(engine, "screen_numeric", Hostile().forbidden)
    with pytest.raises(wire.ProtocolError):
        adapter.analyze_column(req, [1])


def test_uninitialized_request_is_rejected():
    with pytest.raises(wire.ProtocolError):
        adapter.analyze_column(object.__new__(wire.Request), [])


@pytest.mark.parametrize("values", [None, "private-sentinel", b"x", {}, iter([1]), Hostile(), HostileList([1])],
                         ids=["none", "string", "bytes", "dict", "iterator", "custom", "subclass"])
def test_invalid_containers_do_not_dispatch(values, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "screen_numeric", lambda *a, **k: calls.append(True))
    assert_error(adapter.analyze_column(request(), values), "INVALID_INPUT")
    assert calls == []


@pytest.mark.parametrize("value", [b"x", Decimal("1"), [1], {"x": 1}, Hostile(), HostileInt(1), HostileString("x")],
                         ids=["bytes", "decimal", "list", "dict", "custom", "int-subclass", "str-subclass"])
def test_unsupported_values_never_coerced_or_dispatched(value, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "screen_numeric", lambda *a, **k: calls.append(True))
    assert_error(adapter.analyze_column(request(), [value]), "UNSUPPORTED_TYPE")
    assert calls == []


def test_row_limit_exact_and_one_over(monkeypatch):
    req = request(max_rows=4)
    assert adapter.analyze_column(req, [1, 2, 3, 4]).to_dict()["outcome"] == "ok"
    calls = []
    monkeypatch.setattr(engine, "screen_numeric", lambda *a, **k: calls.append(True))
    assert_error(adapter.analyze_column(req, [1, 2, 3, 4, 5]), "RESOURCE_LIMIT")
    assert_error(adapter.analyze_column(request(), [None] * 100001), "RESOURCE_LIMIT")
    assert calls == []


@pytest.mark.parametrize("values", [[], (), [1, 2.0, None], (True, False), ["é", "😀"], [2**1000]])
def test_decoded_size_exact_and_one_under(values, monkeypatch):
    size = sys.getsizeof(tuple(values)) + sum(sys.getsizeof(v) for v in values)
    req = request(max_decoded_bytes=size)
    assert adapter.analyze_column(req, values).to_dict()["outcome"] == "ok"
    calls = []
    monkeypatch.setattr(engine, "screen_numeric", lambda *a, **k: calls.append(True))
    assert_error(adapter.analyze_column(replace(req, max_decoded_bytes=size - 1), values), "RESOURCE_LIMIT")
    assert calls == []


def test_shared_values_charged_per_occurrence():
    value = "é" * 100
    values = [value, value]
    one_charge = sys.getsizeof(tuple(values)) + sys.getsizeof(value)
    assert_error(adapter.analyze_column(request(max_decoded_bytes=one_charge), values), "RESOURCE_LIMIT")


@pytest.mark.parametrize("operation,name,values", [
    ("numeric_outliers", "screen_numeric", [1, 2, 3, 4]),
    ("categorical_counts", "summarize_categories", ["a", "b"]),
])
def test_dispatch_and_deadline_passthrough(operation, name, values, monkeypatch):
    deadline = ExecutionDeadline(60)
    original = getattr(engine, name)
    calls = []
    def spy(supplied, *, deadline):
        calls.append((supplied, deadline))
        return original(supplied, deadline=deadline)
    monkeypatch.setattr(engine, name, spy)
    other = "summarize_categories" if name == "screen_numeric" else "screen_numeric"
    monkeypatch.setattr(engine, other, Hostile().forbidden)
    assert adapter.analyze_column(request(operation), values, deadline=deadline).to_dict()["outcome"] == "ok"
    assert len(calls) == 1 and type(calls[0][0]) is tuple
    assert calls[0][0] == tuple(values) and calls[0][1] is deadline


@pytest.mark.parametrize("exception,code", [
    (MemoryError("private-sentinel"), "RESOURCE_LIMIT"),
    (ProcessingTimeoutError(2, 1), "RESOURCE_LIMIT"),
    (RuntimeError("private-sentinel"), "INTERNAL_ERROR"),
    (ValueError("private-sentinel"), "INTERNAL_ERROR"),
    (TypeError("private-sentinel"), "INTERNAL_ERROR"),
])
def test_engine_exceptions_are_fixed_errors(exception, code, monkeypatch):
    def fail(*args, **kwargs):
        raise exception
    monkeypatch.setattr(engine, "screen_numeric", fail)
    assert_error(adapter.analyze_column(request(), [1]), code)


def test_cancellation_propagates(monkeypatch):
    def cancel(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(engine, "screen_numeric", cancel)
    with pytest.raises(KeyboardInterrupt):
        adapter.analyze_column(request(), [])


@pytest.mark.parametrize("bad", [Hostile(), 30, ExecutionDeadline(float("nan")), ExecutionDeadline(-1)],
                         ids=["custom", "seconds", "nan", "negative"])
def test_invalid_deadline(bad):
    assert_error(adapter.analyze_column(request(), [], deadline=bad), "INVALID_INPUT")


def test_expired_deadline_prevents_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "screen_numeric", lambda *a, **k: calls.append(True))
    assert_error(adapter.analyze_column(request(), [], deadline=ExecutionDeadline(1, start_time=-10000)),
                 "RESOURCE_LIMIT")
    assert calls == []


def test_deadline_polled_during_admission_and_after_engine(monkeypatch):
    deadline = ExecutionDeadline(60)
    checks = []
    def check(self):
        checks.append(1)
        if len(checks) == 3:
            raise ProcessingTimeoutError(2, 1)
    monkeypatch.setattr(ExecutionDeadline, "check", check)
    calls = []
    monkeypatch.setattr(engine, "screen_numeric", lambda *a, **k: calls.append(True))
    assert_error(adapter.analyze_column(request(), [1] * 1000, deadline=deadline), "RESOURCE_LIMIT")
    assert calls == []
    checks.clear()
    # Empty values: initial check, pre-engine check, post-serialization check.
    monkeypatch.setattr(engine, "screen_numeric", lambda *a, **k: engine.NumericScreenResult("EMPTY", 0, 0, 0))
    assert_error(adapter.analyze_column(request(), [], deadline=deadline), "RESOURCE_LIMIT")


def test_unexpected_result_object_not_inspected(monkeypatch):
    monkeypatch.setattr(engine, "screen_numeric", lambda *a, **k: Hostile())
    assert_error(adapter.analyze_column(request(), []), "INTERNAL_ERROR")


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(observations=["private-sentinel"]),
    lambda d: d.update(total_count=float("nan")),
    lambda d: d.update(status="x" * (wire.RESPONSE_LIMIT + 1)),
])
def test_invalid_engine_serialization_fails_closed(monkeypatch, mutation):
    original = engine.NumericScreenResult.to_dict
    def changed(self):
        result = original(self)
        mutation(result)
        return result
    monkeypatch.setattr(engine.NumericScreenResult, "to_dict", changed)
    assert_error(adapter.analyze_column(request(), []), "INTERNAL_ERROR")


@pytest.mark.parametrize("operation,values", [
    ("numeric_outliers", [917263, 1, 2, 3, 4]),
    ("categorical_counts", ["private-sentinel", "秘密", "private-sentinel", None]),
])
def test_determinism_privacy_and_detached_output(operation, values):
    req = request(operation)
    response = adapter.analyze_column(req, values)
    assert response.payload == adapter.analyze_column(req, values).payload
    assert response.payload == adapter.analyze_column(req, list(reversed(values))).payload
    assert b"917263" not in response.payload and b"private-sentinel" not in response.payload
    assert b"observations" not in response.payload and b"category_labels" not in response.payload
    response.to_dict()["result"].clear()
    assert response.to_dict()["result"]
    assert json.loads(response.payload)["outcome"] == "ok"


def test_no_io_or_runtime_dependencies(monkeypatch):
    source = Path(adapter.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imports == {"json", "math", "sys", "backend", "core", "core.types"}
    req = request()
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected I/O")
    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", forbidden)
        guard.setattr(io, "open", forbidden)
        guard.setattr(socket, "socket", forbidden)
        guard.setattr(socket, "create_connection", forbidden)
        guard.setattr(subprocess, "Popen", forbidden)
        # Module initialization and execution both run under the I/O guards.
        namespace = {"__name__": "_adapter_probe"}
        exec(compile(source, adapter.__file__, "exec"), namespace)
        assert namespace["analyze_column"](req, [1, 2, 3, 4]).to_dict()["outcome"] == "ok"
        assert namespace["analyze_column"](replace(req, operation="categorical_counts"),
                                           ["a", "b"]).to_dict()["outcome"] == "ok"


@pytest.mark.parametrize("location", ["container", "scalar", "deadline-number", "deadline"])
def test_hostile_metaclass_equality_never_runs(location):
    calls = []
    class LyingMeta(type):
        def __eq__(cls, other):
            calls.append("equality")
            return True
    class Pretender(metaclass=LyingMeta):
        def __len__(self):
            calls.append("len")
            return 0
        def __sizeof__(self):
            calls.append("sizeof")
            return 1
        def __float__(self):
            calls.append("float")
            return 1.0
    values, deadline = [], None
    if location == "container":
        values = Pretender()
    elif location == "scalar":
        values = [Pretender()]
    elif location == "deadline":
        deadline = Pretender()
    else:
        deadline = ExecutionDeadline(60)
        deadline.timeout_seconds = Pretender()
    response = adapter.analyze_column(request(), values, deadline=deadline)
    assert_error(response, "UNSUPPORTED_TYPE" if location == "scalar" else "INVALID_INPUT")
    assert calls == []


def test_snapshot_measured_and_executed_despite_original_mutation(monkeypatch):
    values = [1, 2, 3, 4, 100]
    expected = engine.screen_numeric(values).to_dict()
    measured = []
    original_sizeof = sys.getsizeof
    original_engine = engine.screen_numeric
    def measure(value):
        if type(value) is tuple:
            measured.append(value)
            values[:] = ["private-sentinel"] * 10
        return original_sizeof(value)
    def execute(snapshot, *, deadline):
        assert snapshot is measured[0]
        assert snapshot == (1, 2, 3, 4, 100)
        return original_engine(snapshot, deadline=deadline)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "getsizeof", measure)
        patch.setattr(engine, "screen_numeric", execute)
        response = adapter.analyze_column(request(max_rows=5), values)
    assert response.to_dict()["result"] == expected
    assert values == ["private-sentinel"] * 10


def test_instance_deadline_override_is_rejected_without_calling_it():
    deadline = ExecutionDeadline(60)
    calls = []
    deadline.check = lambda: calls.append(True)
    assert_error(adapter.analyze_column(request(), [], deadline=deadline), "INVALID_INPUT")
    assert calls == []


@pytest.mark.parametrize("now,expired", [(10.0, False), (10.0001, True)])
def test_engine_deadline_equality_semantics_preserved(monkeypatch, now, expired):
    deadline = ExecutionDeadline(10, start_time=0)
    monkeypatch.setattr(core_types.time, "monotonic", lambda: now)
    response = adapter.analyze_column(request(), [], deadline=deadline)
    if expired:
        assert_error(response, "RESOURCE_LIMIT")
    else:
        assert response.to_dict()["result"]["status"] == "EMPTY"


def test_expiry_during_real_engine_processing(monkeypatch):
    now = [0.0]
    deadline = ExecutionDeadline(10, start_time=0)
    monkeypatch.setattr(core_types.time, "monotonic", lambda: now[0])
    original = engine.screen_numeric
    def execute(values, *, deadline):
        now[0] = 11.0
        return original(values, deadline=deadline)
    monkeypatch.setattr(engine, "screen_numeric", execute)
    assert_error(adapter.analyze_column(request(), [1, 2, 3, 4], deadline=deadline), "RESOURCE_LIMIT")


@pytest.mark.parametrize("failure", ["admission", "engine"])
def test_error_response_expiry_becomes_resource_limit(monkeypatch, failure):
    now = [0.0]
    deadline = ExecutionDeadline(10, start_time=0)
    monkeypatch.setattr(core_types.time, "monotonic", lambda: now[0])
    original = adapter._error
    def error(req, code):
        response = original(req, code)
        now[0] = 11.0
        return response
    monkeypatch.setattr(adapter, "_error", error)
    def fail(*args, **kwargs):
        raise RuntimeError("private-sentinel")
    monkeypatch.setattr(engine, "screen_numeric", fail)
    values = None if failure == "admission" else []
    assert_error(adapter.analyze_column(request(), values, deadline=deadline), "RESOURCE_LIMIT")


@pytest.mark.parametrize("operation,values", [
    ("numeric_outliers", [1, 2, 3, 4, 100]),
    ("categorical_counts", ["private-sentinel", "b", "b"]),
    ("numeric_outliers", [b"unsupported"]),
])
def test_adapter_payload_through_framing_eof_and_controller(tmp_path, operation, values):
    req = request(operation)
    response = adapter.analyze_column(req, values)
    class AdapterReplyWorker(FakeWorker):
        def send(self, data, deadline):
            super().send(data, deadline)
            if data[5] == wire.MessageType.END_INPUT:
                self.pending[:] = wire.Frame(wire.MessageType.RESPONSE, self.job, 1,
                                             response.payload).to_bytes()
    clock = FakeClock()
    worker = AdapterReplyWorker(clock, fragment=1)
    ledger = controller.AdmissionLedger(tmp_path / "gate.sqlite")
    ledger.initialize()
    outcome = controller.Controller(ledger, worker, clock).run(req.to_bytes(), [b"x"])
    # Controller's ExchangeDecoder validates all fragmented frames and finish()
    # validates EOF before exposing the response; no result conversion is used.
    assert outcome.response.payload == response.payload
    assert outcome.response.to_dict() == response.to_dict()
    assert outcome.error is None and outcome.errors == ()
    assert outcome.worker_error_code == response.to_dict().get("error_code")
    assert outcome.state == (controller.State.FAILED if outcome.worker_error_code
                             else controller.State.SUCCEEDED)
    assert b"private-sentinel" not in outcome.response.payload
    assert worker.stopped and worker.cleaned and ledger.inspect().state == "IDLE"
