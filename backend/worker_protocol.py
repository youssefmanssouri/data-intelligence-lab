"""Bounded worker wire contract. No transport, execution, or isolation facilities.

Call ExchangeDecoder.feed with bounded chunks in wire order, then finish at EOF.
Returned DATA frames may be consumed immediately; the decoder retains only a
single frame, byte count and digest. A protocol error permanently poisons it.
"""

from dataclasses import dataclass, fields
from enum import IntEnum
import hashlib
import json
import re
import struct

REQUEST_LIMIT = 4096
DATA_LIMIT = 65536
INPUT_LIMIT = 16777216
RESPONSE_LIMIT = 16384
DEPTH_LIMIT = 8
STRING_LIMIT = 256
INTEGER_DIGITS = 10
FRAME_LIMIT = 256
HEADER = struct.Struct("!4sBBH16sII")


class ProtocolError(ValueError):
    """Fixed diagnostic: never contains peer-controlled content."""

    def __init__(self):
        super().__init__("Invalid worker protocol message.")


def _require(condition):
    if not condition:
        raise ProtocolError()


def _integer(value, low, high):
    _require(type(value) is int and low <= value <= high)


def _keys(value, expected):
    _require(type(value) is dict and value.keys() == set(expected))


def _reject(_):
    raise ProtocolError()


def _parse_int(token):
    _require(len(token.lstrip("-")) <= INTEGER_DIGITS)
    return int(token)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _json(payload, ceiling):
    _require(type(payload) is bytes and 0 < len(payload) <= ceiling)
    # Scan before json.loads so deeply nested input never reaches its recursion.
    try:
        text = payload.decode("utf-8", errors="strict")
        _require(not text.startswith("\ufeff"))
        depth = 0
        quoted = escaped = False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                _require(depth <= DEPTH_LIMIT)
            elif char in "]}":
                depth -= 1
                _require(depth >= 0)
        value = json.loads(text, object_pairs_hook=_pairs, parse_int=_parse_int,
                           parse_float=_reject, parse_constant=_reject)
        _tree(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        pass
    raise ProtocolError() from None


def _tree(value):
    if type(value) is str:
        _require(len(value.encode("utf-8")) <= STRING_LIMIT)
    elif type(value) is list:
        _require(len(value) <= 1)
        for child in value:
            _tree(child)
    elif type(value) is dict:
        for key, child in value.items():
            _tree(key)
            _tree(child)


@dataclass(frozen=True, slots=True)
class Request:
    schema_version: str
    operation: str
    input_format: str
    input_bytes: int
    input_sha256: str
    column_index: int
    max_rows: int
    max_decoded_bytes: int

    def __post_init__(self):
        _require(type(self.schema_version) is str and self.schema_version == "1.0")
        _require(type(self.operation) is str and self.operation in
                 ("numeric_outliers", "categorical_counts"))
        _require(type(self.input_format) is str and self.input_format == "parquet")
        _require(type(self.input_sha256) is str and
                 re.fullmatch("[0-9a-f]{64}", self.input_sha256) is not None)
        _integer(self.input_bytes, 1, INPUT_LIMIT)
        _integer(self.column_index, 0, 99)
        _integer(self.max_rows, 1, 100000)
        _integer(self.max_decoded_bytes, 1, 67108864)

    def to_bytes(self):
        return json.dumps({f.name: getattr(self, f.name) for f in fields(self)},
                          separators=(",", ":"), allow_nan=False).encode("ascii")


def decode_request(payload):
    value = _json(payload, REQUEST_LIMIT)
    _keys(value, (f.name for f in fields(Request)))
    return Request(**value)


def _result(value, request):
    numeric = request.operation == "numeric_outliers"
    common = ("total_count", "null_count", "non_null_count")
    specific = (("below_fence_count", "above_fence_count", "potential_outlier_count")
                if numeric else ("distinct_count", "singleton_category_count"))
    _keys(value, ("schema_version", "analysis", "method_version", "status",
                  "findings", *common, *specific, *(("method",) if numeric else ())))
    version = "tukey-linear-exact-v1" if numeric else "category-counts-v1"
    _require(value["schema_version"] == "1.0" and
             value["analysis"] == request.operation and value["method_version"] == version)
    status = value["status"]
    errors = (("INVALID_INPUT", "UNSUPPORTED_NUMERIC_RANGE") if numeric else
              ("INVALID_INPUT", "CATEGORY_LIMIT_EXCEEDED"))
    completed = "SCREENED" if numeric else "COUNTED"
    statuses = ("EMPTY", "ALL_NULL", *errors, completed,
                *(("INSUFFICIENT_SAMPLE", "CONSTANT", "ZERO_IQR") if numeric else ()))
    _require(type(status) is str and status in statuses)
    if numeric:
        method = value["method"]
        _keys(method, ("quartiles", "multiplier", "fence_rule", "minimum_non_null_count"))
        _require(method["quartiles"] == "linear interpolation at p*(n-1)" and
                 method["multiplier"] == "3/2" and method["fence_rule"] == "strictly outside")
        _integer(method["minimum_non_null_count"], 4, 4)
    evidence = {}
    if status in errors:
        _require(all(value[key] is None for key in (*common, *specific)))
    else:
        for key in common:
            _integer(value[key], 0, request.max_rows)
        total, nulls, non_null = (value[key] for key in common)
        _require(total == nulls + non_null)
        if status == "EMPTY":
            _require(total == 0)
        elif status == "ALL_NULL":
            _require(total > 0 and non_null == 0)
        elif status == "INSUFFICIENT_SAMPLE":
            _require(1 <= non_null <= 3)
        else:
            _require(non_null >= (4 if numeric else 1))
        evidence.update((key, value[key]) for key in common)
        if status == completed:
            for key in specific:
                _integer(value[key], 0, non_null)
            if numeric:
                _require(value[specific[0]] + value[specific[1]] == value[specific[2]])
            else:
                distinct, singleton = (value[key] for key in specific)
                _require(1 <= distinct <= min(10000, non_null) and singleton <= distinct)
                _require(non_null >= singleton + 2 * (distinct - singleton))
                _require(singleton != distinct or non_null == distinct)
            evidence.update((key, value[key]) for key in specific)
        else:
            _require(all(value[key] is None for key in specific))
    findings = value["findings"]
    _require(type(findings) is list and len(findings) == 1)
    finding = findings[0]
    _keys(finding, ("code", "method_version", "evidence"))
    _require(finding["code"] == status and finding["method_version"] == version)
    _keys(finding["evidence"], evidence)
    for key, count in evidence.items():
        _integer(finding["evidence"][key], count, count)


@dataclass(frozen=True, slots=True)
class Response:
    """Validated immutable wire bytes; to_dict returns a fresh independent tree."""

    payload: bytes
    request: Request

    def __post_init__(self):
        _require(type(self.request) is Request)
        value = _json(self.payload, RESPONSE_LIMIT)
        _require(type(value) is dict)
        if value.get("outcome") == "ok":
            _keys(value, ("schema_version", "outcome", "result"))
            _result(value["result"], self.request)
        else:
            _keys(value, ("schema_version", "outcome", "error_code"))
            _require(value["outcome"] == "error" and type(value["error_code"]) is str and
                     value["error_code"] in ("INVALID_INPUT", "UNSUPPORTED_TYPE",
                                             "RESOURCE_LIMIT", "INTERNAL_ERROR"))
        _require(value["schema_version"] == "1.0")

    def to_dict(self):
        return _json(self.payload, RESPONSE_LIMIT)


class MessageType(IntEnum):
    READY = 1
    REQUEST = 2
    DATA = 3
    END_INPUT = 4
    RESPONSE = 5


def _payload_limit(kind):
    return {MessageType.READY: 0, MessageType.REQUEST: REQUEST_LIMIT,
            MessageType.DATA: DATA_LIMIT, MessageType.END_INPUT: 0,
            MessageType.RESPONSE: RESPONSE_LIMIT}[kind]


@dataclass(frozen=True, slots=True)
class Frame:
    kind: MessageType
    job_id: bytes
    sequence: int
    payload: bytes

    def __post_init__(self):
        _require(type(self.kind) is MessageType)
        _require(type(self.job_id) is bytes and len(self.job_id) == 16)
        _integer(self.sequence, 0, 4294967295)
        _require(type(self.payload) is bytes and len(self.payload) <= _payload_limit(self.kind))
        if self.kind in (MessageType.REQUEST, MessageType.DATA, MessageType.RESPONSE):
            _require(len(self.payload) > 0)

    def to_bytes(self):
        return HEADER.pack(b"DILW", 1, self.kind, 0, self.job_id,
                           self.sequence, len(self.payload)) + self.payload


class ExchangeDecoder:
    """One bidirectional exchange; feed uses sender 'worker' or 'controller'.

    Each feed is at most HEADER.size + DATA_LIMIT bytes. EOF is explicit via
    finish. No reads, callbacks, threads, or external resources are used.
    """

    def __init__(self, job_id):
        _require(type(job_id) is bytes and len(job_id) == 16)
        self.job_id = job_id
        self.request = None
        self.response = None
        self._state = "ready"
        self._buffer = bytearray()
        self._pending = None
        self._sender = None
        self._count = 0
        self._frames = 0
        self._digest = hashlib.sha256()
        self._failed = False
        self._finished = False

    def _header(self):
        magic, version, kind, reserved, job, sequence, size = HEADER.unpack(self._buffer)
        _require(magic == b"DILW" and version == 1 and reserved == 0 and job == self.job_id)
        _require(kind in (1, 2, 3, 4, 5))
        kind = MessageType(kind)
        _require(size <= _payload_limit(kind))
        if kind in (MessageType.REQUEST, MessageType.DATA, MessageType.RESPONSE):
            _require(size > 0)
        expected = {"ready": ("worker", MessageType.READY, 0),
                    "request": ("controller", MessageType.REQUEST, 0),
                    "response": ("worker", MessageType.RESPONSE, 1)}
        if self._state in expected:
            _require((self._sender, kind, sequence) == expected[self._state])
        else:
            _require(self._state == "data" and self._sender == "controller")
            _require(sequence == self._frames + 1)
            if kind == MessageType.DATA:
                _require(self._frames < FRAME_LIMIT)
                remaining = self.request.input_bytes - self._count
                _require(remaining > 0 and size == min(DATA_LIMIT, remaining))
            else:
                _require(kind == MessageType.END_INPUT and self._count == self.request.input_bytes)
        return kind, sequence, size

    def _complete(self, kind, sequence):
        frame = Frame(kind, self.job_id, sequence, bytes(self._buffer))
        if kind == MessageType.READY:
            self._state = "request"
        elif kind == MessageType.REQUEST:
            self.request = decode_request(frame.payload)
            self._state = "data"
        elif kind == MessageType.DATA:
            self._count += len(frame.payload)
            self._frames += 1
            self._digest.update(frame.payload)
        elif kind == MessageType.END_INPUT:
            _require(self._digest.hexdigest() == self.request.input_sha256)
            self._state = "response"
        else:
            self.response = Response(frame.payload, self.request)
            self._state = "done"
        self._buffer.clear()
        self._pending = None
        self._sender = None
        return frame

    def feed(self, sender, chunk):
        """Return completed frames. Failure poisons the entire exchange."""
        try:
            _require(not self._failed and not self._finished)
            _require(sender in ("worker", "controller") and type(sender) is str)
            _require(type(chunk) is bytes and len(chunk) <= HEADER.size + DATA_LIMIT)
            frames = []
            offset = 0
            while offset < len(chunk):
                _require(self._state != "done")
                if self._sender is None:
                    self._sender = sender
                _require(self._sender == sender)
                target = HEADER.size if self._pending is None else self._pending[2]
                amount = min(target - len(self._buffer), len(chunk) - offset)
                self._buffer.extend(memoryview(chunk)[offset:offset + amount])
                offset += amount
                if len(self._buffer) < target:
                    break
                if self._pending is None:
                    self._pending = self._header()
                    self._buffer.clear()
                    if self._pending[2]:
                        continue
                kind, sequence, _ = self._pending
                frames.append(self._complete(kind, sequence))
            return tuple(frames)
        except ProtocolError:
            self._failed = True
            self.response = None
            self._buffer.clear()
            self._pending = None
            raise

    def finish(self):
        """Confirm EOF and return the response; never accept an incomplete exchange."""
        if self._failed or self._finished or self._state != "done" or self._buffer or self._pending:
            self._failed = True
            self.response = None
            raise ProtocolError()
        self._finished = True
        return self.response
