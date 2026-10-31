"""Application-level bridge for one already decoded column; no I/O or runtime.

The trusted input-provider boundary supplies values directly; it is not implemented
here. It must verify Parquet bytes/column identity before calling this function and
must not mutate values while the bounded snapshot is created. Protocol validation owns request fields.
This module owns representation admission, then delegates semantics to the engine.

Decoded size means sys.getsizeof(tuple_snapshot) plus sys.getsizeof(value) for every
occurrence, including nulls and shared objects. Only exact built-in scalar types
are inspected. This conservative, interpreter-specific charge covers the supplied
Python representation, not decoder buffers, allocator overhead, engine temporaries
or a process memory ceiling. There is no sandbox or hard deadline enforcement.
"""

import json
import math
import sys

from backend import worker_protocol as wire
from core import statistical_analysis as engine
from core.types import ExecutionDeadline, ProcessingTimeoutError


def _response(request, value):
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"),
                         allow_nan=False).encode("ascii")
    return wire.Response(payload, request)


def _error(request, code):
    return _response(request, {"schema_version": "1.0", "outcome": "error", "error_code": code})


class _Rejected(Exception):
    def __init__(self, code):
        self.code = code


def analyze_column(request: wire.Request, values: list | tuple, *,
                   deadline: ExecutionDeadline | None = None) -> wire.Response:
    """Return a strict response for a validated request and provider-owned column.

    Invalid request representations raise fixed ProtocolError: there is no valid
    request to bind a Response to. The existing protocol validator is reused,
    including against an incompletely constructed or tampered Request instance.
    Invalid containers/deadlines map to INVALID_INPUT; unsupported scalar types
    to UNSUPPORTED_TYPE; row/byte limits, memory exhaustion and cooperative expiry
    to RESOURCE_LIMIT. Unexpected failures map to INTERNAL_ERROR. Engine result
    statuses are preserved inside outcome=ok, including its invalid-input statuses.
    Cancellation (BaseException) propagates rather than becoming a worker result.
    """
    if type(request) is not wire.Request:
        raise wire.ProtocolError()
    try:
        wire.Request.__post_init__(request)
    except (wire.ProtocolError, AttributeError):
        raise wire.ProtocolError() from None

    checked_deadline = None
    try:
        if deadline is not None:
            if type(deadline) is not ExecutionDeadline:
                raise _Rejected("INVALID_INPUT")
            if "check" in vars(deadline):
                raise _Rejected("INVALID_INPUT")
            for number in (deadline.timeout_seconds, deadline.start_time, deadline.deadline):
                kind = type(number)
                if (kind is not int and kind is not float) or not math.isfinite(number):
                    raise _Rejected("INVALID_INPUT")
            if deadline.timeout_seconds <= 0:
                raise _Rejected("INVALID_INPUT")
            checked_deadline = deadline
            deadline.check()
        kind = type(values)
        if kind is not list and kind is not tuple:
            raise _Rejected("INVALID_INPUT")
        if len(values) > request.max_rows:
            raise _Rejected("RESOURCE_LIMIT")
        # Bound even the temporary slice. Provider concurrency during this copy
        # is unsupported; subsequent changes to its list cannot affect execution.
        snapshot = tuple(values[:request.max_rows + 1])
        if len(snapshot) > request.max_rows:
            raise _Rejected("RESOURCE_LIMIT")
        size = sys.getsizeof(snapshot)
        if size > request.max_decoded_bytes:
            raise _Rejected("RESOURCE_LIMIT")
        for index, value in enumerate(snapshot):
            if deadline is not None and index % 500 == 0:
                deadline.check()
            kind = type(value)
            if (kind is not type(None) and kind is not bool and kind is not int
                    and kind is not float and kind is not str):
                raise _Rejected("UNSUPPORTED_TYPE")
            size += sys.getsizeof(value)
            if size > request.max_decoded_bytes:
                raise _Rejected("RESOURCE_LIMIT")
        if deadline is not None:
            deadline.check()
        if request.operation == "numeric_outliers":
            result = engine.screen_numeric(snapshot, deadline=deadline)
            expected = engine.NumericScreenResult
        else:  # The protocol admits exactly two operations.
            result = engine.summarize_categories(snapshot, deadline=deadline)
            expected = engine.CategorySummaryResult
        if type(result) is not expected:
            raise _Rejected("INTERNAL_ERROR")
        response = _response(request, {"schema_version": "1.0", "outcome": "ok",
                                       "result": result.to_dict()})
        if deadline is not None:
            deadline.check()
        return response
    except _Rejected as exc:
        code = exc.code
    except (ProcessingTimeoutError, MemoryError):
        code = "RESOURCE_LIMIT"
    except Exception:
        code = "INTERNAL_ERROR"
    # Construct fixed error bytes outside the exception handler; never serialize
    # exceptions or keep their text in a returned object.
    try:
        response = _error(request, code)
        if checked_deadline is not None:
            checked_deadline.check()
        return response
    except ProcessingTimeoutError:
        return _error(request, "RESOURCE_LIMIT")
    except MemoryError:
        return _error(request, "RESOURCE_LIMIT")
    except Exception:
        return _error(request, "INTERNAL_ERROR")
