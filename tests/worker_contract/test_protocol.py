"""Protocol-only tests; all dataset bytes and analysis results are inert."""

import ast
import builtins
from dataclasses import FrozenInstanceError
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from backend import worker_protocol as p

JOB = bytes(range(16))


def request(data=b"x", **changes):
    values = dict(schema_version="1.0", operation="numeric_outliers", input_format="parquet",
                  input_bytes=len(data), input_sha256=hashlib.sha256(data).hexdigest(),
                  column_index=0, max_rows=100000, max_decoded_bytes=67108864)
    values.update(changes)
    return p.Request(**values)


def wire(value):
    return json.dumps(value, separators=(",", ":")).encode()


def result(numeric=True, status=None):
    status = status or ("SCREENED" if numeric else "COUNTED")
    version = "tukey-linear-exact-v1" if numeric else "category-counts-v1"
    errors = ("INVALID_INPUT", "UNSUPPORTED_NUMERIC_RANGE", "CATEGORY_LIMIT_EXCEEDED")
    counts = {"total_count": 5, "null_count": 0, "non_null_count": 5}
    if status == "EMPTY":
        counts = dict.fromkeys(counts, 0)
    elif status == "ALL_NULL":
        counts.update(null_count=5, non_null_count=0)
    elif status == "INSUFFICIENT_SAMPLE":
        counts.update(total_count=3, non_null_count=3)
    elif status in errors:
        counts = dict.fromkeys(counts)
    metrics = ({"below_fence_count": 0, "above_fence_count": 1, "potential_outlier_count": 1}
               if numeric else {"distinct_count": 3, "singleton_category_count": 1})
    if status not in ("SCREENED", "COUNTED"):
        metrics = dict.fromkeys(metrics)
    evidence = {} if status in errors else dict(counts)
    if status in ("SCREENED", "COUNTED"):
        evidence.update(metrics)
    value = dict(schema_version="1.0", analysis="numeric_outliers" if numeric else "categorical_counts",
                 method_version=version, status=status, **counts, **metrics,
                 findings=[dict(code=status, method_version=version, evidence=evidence)])
    if numeric:
        value["method"] = dict(quartiles="linear interpolation at p*(n-1)", multiplier="3/2",
                               fence_rule="strictly outside", minimum_non_null_count=4)
    return dict(schema_version="1.0", outcome="ok", result=value)


def frame(kind, sequence, payload=b"", job=JOB):
    return p.Frame(kind, job, sequence, payload).to_bytes()


def start(data=b"x", **changes):
    decoder = p.ExchangeDecoder(JOB)
    req = request(data, **changes)
    decoder.feed("worker", frame(p.MessageType.READY, 0))
    decoder.feed("controller", frame(p.MessageType.REQUEST, 0, req.to_bytes()))
    return decoder, req


def send_input(decoder, data=b"x"):
    for index, offset in enumerate(range(0, len(data), p.DATA_LIMIT), 1):
        decoder.feed("controller", frame(p.MessageType.DATA, index, data[offset:offset+p.DATA_LIMIT]))
    decoder.feed("controller", frame(p.MessageType.END_INPUT, index + 1))


@pytest.mark.parametrize("size", [1, 65535, 65536, 65537, p.INPUT_LIMIT])
def test_input_boundaries(size):
    data = b"x" * size
    decoder, _ = start(data)
    send_input(decoder, data)
    decoder.feed("worker", frame(p.MessageType.RESPONSE, 1, wire(result())))
    assert decoder.finish().to_dict() == result()
    assert decoder._count == size
    assert not decoder._buffer
    assert not any(isinstance(v, bytes) and len(v) > p.RESPONSE_LIMIT for v in vars(decoder).values())


@pytest.mark.parametrize("step", [1, 7, 31, 32, 33, 67])
def test_fragmented_exchange(step):
    decoder = p.ExchangeDecoder(JOB)
    messages = [("worker", frame(p.MessageType.READY, 0)),
                ("controller", frame(p.MessageType.REQUEST, 0, request().to_bytes())),
                ("controller", frame(p.MessageType.DATA, 1, b"x")),
                ("controller", frame(p.MessageType.END_INPUT, 2)),
                ("worker", frame(p.MessageType.RESPONSE, 1, wire(result())))]
    for sender, message in messages:
        for offset in range(0, len(message), step):
            decoder.feed(sender, message[offset:offset+step])
    assert decoder.finish().to_dict() == result()


@pytest.mark.parametrize("numeric,status", [(True,s) for s in
    ("EMPTY", "ALL_NULL", "INSUFFICIENT_SAMPLE", "CONSTANT", "ZERO_IQR", "SCREENED",
     "INVALID_INPUT", "UNSUPPORTED_NUMERIC_RANGE")] + [(False,s) for s in
    ("EMPTY", "ALL_NULL", "COUNTED", "INVALID_INPUT", "CATEGORY_LIMIT_EXCEEDED")])
def test_success_schemas(numeric, status):
    req = request(operation="numeric_outliers" if numeric else "categorical_counts")
    value = result(numeric, status)
    response = p.Response(wire(value), req)
    assert response.to_dict() == value
    detached = response.to_dict()
    detached["result"].clear()
    assert response.to_dict() == value
    with pytest.raises(FrozenInstanceError):
        response.payload = b""


@pytest.mark.parametrize("code", ["INVALID_INPUT", "UNSUPPORTED_TYPE", "RESOURCE_LIMIT", "INTERNAL_ERROR"])
def test_failure_schema(code):
    value = dict(schema_version="1.0", outcome="error", error_code=code)
    assert p.Response(wire(value), request()).to_dict() == value


@pytest.mark.parametrize("field,values", [
    ("input_bytes", [0, -1, p.INPUT_LIMIT+1, True, 1.0]),
    ("column_index", [-1, 100, False, 0.0]),
    ("max_rows", [0, 100001, True]),
    ("max_decoded_bytes", [0, 67108865, True]),
    ("input_sha256", ["A"*64, "a"*63, "g"*64, "a"*65, "../data", None]),
    ("operation", ["exec", "SELECT 1", "C:\\secret", None]),
    ("input_format", ["csv", "file:///secret"]),
    ("schema_version", ["2.0", 1])])
def test_invalid_request_values(field, values):
    for value in values:
        with pytest.raises(p.ProtocolError):
            request(**{field:value})
        obj = json.loads(request().to_bytes())
        obj[field] = value
        with pytest.raises(p.ProtocolError):
            p.decode_request(wire(obj))


@pytest.mark.parametrize("changes", [dict(input_bytes=1, column_index=0, max_rows=1, max_decoded_bytes=1),
    dict(input_bytes=p.INPUT_LIMIT, column_index=99, max_rows=100000, max_decoded_bytes=67108864)])
def test_request_exact_boundaries(changes):
    req = request(**changes)
    assert p.decode_request(req.to_bytes()) == req
    with pytest.raises(FrozenInstanceError):
        req.input_bytes = 2


@pytest.mark.parametrize("key", ["path", "dataset_name", "credentials", "code", "sql", "environment", "command"])
def test_no_extra_instructions(key):
    obj = json.loads(request().to_bytes())
    obj[key] = "private-sentinel"
    with pytest.raises(p.ProtocolError, match="^Invalid worker protocol message\\.$"):
        p.decode_request(wire(obj))


@pytest.mark.parametrize("key", list(json.loads(request().to_bytes())))
def test_missing_fields(key):
    obj = json.loads(request().to_bytes())
    del obj[key]
    with pytest.raises(p.ProtocolError):
        p.decode_request(wire(obj))


@pytest.mark.parametrize("payload", [b"", b"{", b"[]", b"null", b"{}{}", b"\xff", b"\xef\xbb\xbf{}",
    b'{"x":1,"x":2}', b'{"x":1.0}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}',
    b'{"x":12345678901}', b'{"x":-12345678901}', b'{"x":"\\ud800"}',
    b'['*9+b'0'+b']'*9, b'{"x":[1,2]}', wire({"x":"a"*257}), wire({"x":"\u00e9"*129})])
def test_hostile_json(payload):
    with pytest.raises(p.ProtocolError) as caught:
        p.decode_request(payload)
    assert str(caught.value) == "Invalid worker protocol message."
    assert caught.value.__context__ is None


def test_json_lexical_exact_limits():
    assert p._json(b'['*8+b'0'+b']'*8, 100) is not None
    assert p._json(wire({"x":"a"*256}), 4096)["x"] == "a"*256
    assert p._json(wire({"x":"\u00e9"*128}), 4096)["x"] == "\u00e9"*128
    assert p._json(b'9999999999', 100) == 9999999999
    assert p._json(wire({"x":'[{"\\'}), 100)["x"] == '[{"\\'


@pytest.mark.parametrize("response", [False, True])
def test_json_payload_exact_size(response):
    payload = wire(result()) if response else request().to_bytes()
    cap = p.RESPONSE_LIMIT if response else p.REQUEST_LIMIT
    decode = (lambda b:p.Response(b, request())) if response else p.decode_request
    decode(payload + b" "*(cap-len(payload)))
    with pytest.raises(p.ProtocolError):
        decode(payload + b" "*(cap-len(payload)+1))


@pytest.mark.parametrize("mutation", [
    lambda r:r.update(observations=[1]), lambda r:r.update(total_count=True),
    lambda r:r.update(total_count=6), lambda r:r.update(status="UNKNOWN"),
    lambda r:r.update(potential_outlier_count=2), lambda r:r.update(above_fence_count=6),
    lambda r:r.update(method_version="other"), lambda r:r.update(analysis="categorical_counts"),
    lambda r:r["method"].update(minimum_non_null_count=True),
    lambda r:r["findings"][0]["evidence"].update(above_fence_count=True),
    lambda r:r["findings"][0].update(code="EMPTY"),
    lambda r:r["findings"][0].update(raw_values=[1]),
    lambda r:r.update(findings=[]), lambda r:r.update(total_count=100001)])
def test_invalid_results(mutation):
    value = result()
    mutation(value["result"])
    with pytest.raises(p.ProtocolError):
        p.Response(wire(value), request())


@pytest.mark.parametrize("value", [dict(schema_version="1.0",outcome="error",error_code="OTHER"),
    dict(schema_version="1.0",outcome="error",error_code="INVALID_INPUT",message="secret"),
    dict(schema_version="2.0",outcome="error",error_code="INVALID_INPUT"), {}, []])
def test_invalid_envelope(value):
    with pytest.raises(p.ProtocolError):
        p.Response(wire(value), request())


@pytest.mark.parametrize("offset,value", [(0,0),(4,2),(5,0),(5,6),(6,1),(7,1),(8,255),(27,1),(31,1)])
def test_invalid_ready_header(offset,value):
    decoder = p.ExchangeDecoder(JOB)
    header = bytearray(frame(p.MessageType.READY,0))
    header[offset] = value
    with pytest.raises(p.ProtocolError):
        decoder.feed("worker",bytes(header))
    with pytest.raises(p.ProtocolError):
        decoder.feed("worker",frame(p.MessageType.READY,0))


@pytest.mark.parametrize("kind,size", [(p.MessageType.REQUEST,4097),(p.MessageType.REQUEST,0),
    (p.MessageType.DATA,65537),(p.MessageType.DATA,0),(p.MessageType.RESPONSE,16385),
    (p.MessageType.RESPONSE,0),(p.MessageType.END_INPUT,1)])
def test_reject_length_at_header_before_payload(kind,size):
    decoder, _ = start()
    if kind == p.MessageType.REQUEST:
        decoder = p.ExchangeDecoder(JOB)
        decoder.feed("worker",frame(p.MessageType.READY,0))
    elif kind == p.MessageType.RESPONSE:
        send_input(decoder)
    header = p.HEADER.pack(b"DILW",1,kind,0,JOB,1 if kind != p.MessageType.REQUEST else 0,size)
    with pytest.raises(p.ProtocolError):
        decoder.feed("worker" if kind == p.MessageType.RESPONSE else "controller",header)
    assert not decoder._buffer and decoder._pending is None


@pytest.mark.parametrize("sender,kind,sequence", [("worker",p.MessageType.DATA,1),
    ("controller",p.MessageType.DATA,0),("controller",p.MessageType.DATA,2),
    ("controller",p.MessageType.REQUEST,0),("worker",p.MessageType.RESPONSE,1)])
def test_wrong_direction_or_sequence(sender,kind,sequence):
    decoder,_ = start()
    with pytest.raises(p.ProtocolError):
        decoder.feed(sender,frame(kind,sequence,b"x"))


@pytest.mark.parametrize("stage", ["none","header","request","payload","data","end","response"])
def test_premature_eof(stage):
    decoder = p.ExchangeDecoder(JOB)
    if stage == "header":
        decoder.feed("worker",frame(p.MessageType.READY,0)[:31])
    elif stage != "none":
        decoder.feed("worker",frame(p.MessageType.READY,0))
        if stage == "payload":
            decoder.feed("controller",frame(p.MessageType.REQUEST,0,request().to_bytes())[:-1])
        elif stage != "request":
            decoder.feed("controller",frame(p.MessageType.REQUEST,0,request().to_bytes()))
            if stage in ("end","response"):
                decoder.feed("controller",frame(p.MessageType.DATA,1,b"x"))
            if stage == "response":
                decoder.feed("controller",frame(p.MessageType.END_INPUT,2))
                decoder.feed("worker",frame(p.MessageType.RESPONSE,1,wire(result()))[:-1])
    with pytest.raises(p.ProtocolError):
        decoder.finish()


@pytest.mark.parametrize("case", ["short","long","digest","zero","early_end","extra"])
def test_input_integrity(case):
    decoder,_ = start()
    with pytest.raises(p.ProtocolError):
        if case == "short":
            decoder.feed("controller",p.HEADER.pack(b"DILW",1,3,0,JOB,1,1))
            decoder.finish()
        elif case == "long":
            decoder.feed("controller",frame(p.MessageType.DATA,1,b"xx"))
        elif case == "digest":
            send_input(decoder,b"y")
        elif case == "zero":
            decoder.feed("controller",p.HEADER.pack(b"DILW",1,3,0,JOB,1,0))
        elif case == "early_end":
            decoder.feed("controller",frame(p.MessageType.END_INPUT,1))
        else:
            decoder.feed("controller",frame(p.MessageType.DATA,1,b"x"))
            decoder.feed("controller",frame(p.MessageType.DATA,2,b"x"))


def test_257th_data_frame_rejected():
    block = b"x"*65536
    digest = hashlib.sha256()
    for _ in range(256):
        digest.update(block)
    decoder,_ = start(input_bytes=p.INPUT_LIMIT,input_sha256=digest.hexdigest())
    for sequence in range(1,257):
        decoder.feed("controller",frame(p.MessageType.DATA,sequence,block))
    with pytest.raises(p.ProtocolError):
        decoder.feed("controller",p.HEADER.pack(b"DILW",1,3,0,JOB,257,1))


def test_extra_frame_and_sender_change_poison():
    decoder,_ = start()
    send_input(decoder)
    decoder.feed("worker",frame(p.MessageType.RESPONSE,1,wire(result())))
    with pytest.raises(p.ProtocolError):
        decoder.feed("worker",b"x")
    assert decoder.response is None
    with pytest.raises(p.ProtocolError):
        decoder.finish()
    decoder = p.ExchangeDecoder(JOB)
    decoder.feed("worker",b"D")
    with pytest.raises(p.ProtocolError):
        decoder.feed("controller",b"I")


def test_concatenated_frames_and_chunk_ceiling():
    decoder,_ = start()
    decoder.feed("controller",frame(p.MessageType.DATA,1,b"x")+frame(p.MessageType.END_INPUT,2))
    decoder.feed("worker",frame(p.MessageType.RESPONSE,1,wire(result())))
    decoder.finish()
    with pytest.raises(p.ProtocolError):
        p.ExchangeDecoder(JOB).feed("worker",b"x"*(65536+33))


def test_hostile_length_never_grows_payload_buffer(monkeypatch):
    sizes = []
    class ObservedBuffer(bytearray):
        def extend(self, value):
            sizes.append(len(self) + len(value))
            assert sizes[-1] <= p.HEADER.size
            super().extend(value)
    monkeypatch.setattr(p, "bytearray", ObservedBuffer, raising=False)
    decoder = p.ExchangeDecoder(JOB)
    decoder.feed("worker", frame(p.MessageType.READY, 0))
    header = p.HEADER.pack(b"DILW", 1, 2, 0, JOB, 0, 4294967295)
    with pytest.raises(p.ProtocolError):
        decoder.feed("controller", header)
    assert max(sizes) == 32


@pytest.mark.parametrize("payload", [b'{', b'{"x":1,"x":2}', b'{}'])
def test_invalid_request_payload_poisons_exchange(payload):
    decoder = p.ExchangeDecoder(JOB)
    decoder.feed("worker", frame(p.MessageType.READY, 0))
    with pytest.raises(p.ProtocolError):
        decoder.feed("controller", frame(p.MessageType.REQUEST, 0, payload))
    with pytest.raises(p.ProtocolError):
        decoder.feed("controller", frame(p.MessageType.REQUEST, 0, request().to_bytes()))


@pytest.mark.parametrize("status,field,value", [
    ("EMPTY", "total_count", 1), ("ALL_NULL", "non_null_count", 1),
    ("INSUFFICIENT_SAMPLE", "non_null_count", 4),
    ("CONSTANT", "below_fence_count", 0), ("ZERO_IQR", "total_count", 0),
    ("INVALID_INPUT", "total_count", 0), ("UNSUPPORTED_NUMERIC_RANGE", "null_count", False)])
def test_status_specific_count_rejection(status, field, value):
    obj = result(True, status)
    obj["result"][field] = value
    with pytest.raises(p.ProtocolError):
        p.Response(wire(obj), request())


@pytest.mark.parametrize("distinct,singleton", [(0,0),(6,0),(3,4),(3,True),(3,3),(10001,0)])
def test_category_count_rejection(distinct, singleton):
    obj = result(False)
    obj["result"].update(distinct_count=distinct, singleton_category_count=singleton)
    obj["result"]["findings"][0]["evidence"].update(
        distinct_count=distinct, singleton_category_count=singleton)
    with pytest.raises(p.ProtocolError):
        p.Response(wire(obj), request(operation="categorical_counts"))


def test_response_row_budget_and_operation_are_bound_to_request():
    with pytest.raises(p.ProtocolError):
        p.Response(wire(result()), request(max_rows=4))
    with pytest.raises(p.ProtocolError):
        p.Response(wire(result()), request(operation="categorical_counts"))


def test_unknown_nested_keys_and_duplicate_response_keys():
    payload = wire(result()).replace(b'"outcome":"ok"', b'"outcome":"ok","outcome":"ok"')
    with pytest.raises(p.ProtocolError):
        p.Response(payload, request())
    obj = result()
    obj["result"]["method"]["code"] = "private-sentinel"
    with pytest.raises(p.ProtocolError):
        p.Response(wire(obj), request())


def test_import_has_only_stdlib_dependencies_and_no_io(monkeypatch):
    path = Path(p.__file__)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed = {"dataclasses","enum","hashlib","json","re","struct"}
    imports = {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)}
    imports |= {alias.name.split(".")[0] for n in ast.walk(tree) if isinstance(n,ast.Import) for alias in n.names}
    assert imports <= allowed
    # Read/compile before guards: source loading by import machinery is not module I/O.
    code = compile(source,str(path),"exec")
    spec = importlib.util.spec_from_loader("_protocol_import_probe",loader=None)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules,spec.name,module)
    original_import = builtins.__import__
    def guarded_import(name,*args,**kwargs):
        assert name.split(".")[0] in allowed
        return original_import(name,*args,**kwargs)
    def forbidden(*args,**kwargs):
        raise AssertionError("Import-time I/O")
    monkeypatch.setattr(builtins,"open",forbidden)
    monkeypatch.setattr(builtins,"__import__",guarded_import)
    exec(code,module.__dict__)
