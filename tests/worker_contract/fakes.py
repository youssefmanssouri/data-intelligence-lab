"""Inert scripted test doubles. Never used by backend application wiring."""

import hashlib
import json

from backend import worker_protocol as wire


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def request_bytes(data=b"x"):
    return wire.Request("1.0", "numeric_outliers", "parquet", len(data),
                        hashlib.sha256(data).hexdigest(), 0, 100000, 67108864).to_bytes()


def success_payload():
    # Inert exact-schema fixture, not calculated from input.
    counts = dict(total_count=0, null_count=0, non_null_count=0)
    result = dict(schema_version="1.0", analysis="numeric_outliers",
                  method_version="tukey-linear-exact-v1", status="EMPTY", **counts,
                  below_fence_count=None, above_fence_count=None, potential_outlier_count=None,
                  method=dict(quartiles="linear interpolation at p*(n-1)", multiplier="3/2",
                              fence_rule="strictly outside", minimum_non_null_count=4),
                  findings=[dict(code="EMPTY", method_version="tukey-linear-exact-v1", evidence=counts)])
    return json.dumps(dict(schema_version="1.0", outcome="ok", result=result)).encode()


class FakeWorker:
    """Only wire-shaped byte exchange, scripted failures, and simulated clock time."""

    def __init__(self, clock, *, fail=None, delays=None, fragment=65568, observer=None):
        self.clock = clock
        self.fail = fail
        self.delays = delays or {}
        self.fragment = fragment
        self.observer = observer
        self.calls = []
        self.pending = bytearray()
        self.phase = "ready"
        self.job = None
        self.stopped = False
        self.cleaned = False

    def _event(self, name, deadline):
        self.calls.append(name)
        if self.observer:
            self.observer(name)
        self.clock.advance(self.delays.get(name, 0))
        if self.clock() >= deadline:
            raise TimeoutError("private-timeout")
        if self.fail == name:
            raise RuntimeError("private-worker-diagnostic")

    def start(self, job_id, deadline):
        self._event("start", deadline)
        self.job = job_id
        self.pending.extend(wire.Frame(wire.MessageType.READY, job_id, 0, b"").to_bytes())

    def send(self, data, deadline):
        self._event("send", deadline)
        # Only inspect the fixed message discriminator, never dataset contents.
        if data[5] == wire.MessageType.END_INPUT:
            self.phase = "response"
            payload = success_payload()
            if self.fail == "reported_error":
                payload = b'{"schema_version":"1.0","outcome":"error","error_code":"INTERNAL_ERROR"}'
            self.pending.extend(wire.Frame(wire.MessageType.RESPONSE, self.job, 1, payload).to_bytes())
            if self.fail == "extra":
                self.pending.extend(b"x")

    def receive(self, max_bytes, deadline):
        self._event(self.phase, deadline)
        if self.fail == "premature" and self.phase == "response":
            return b""
        count = min(max_bytes, self.fragment, len(self.pending))
        chunk = bytes(self.pending[:count])
        del self.pending[:count]
        if self.fail == "protocol" and self.phase == "ready":
            return b"FAIL" + chunk[4:]
        return chunk

    def stop(self, deadline):
        self._event("stop", deadline)
        self.stopped = True
        return True

    def cleanup(self, deadline):
        self._event("cleanup", deadline)
        assert self.stopped
        self.cleaned = True
        return True


def contend(path, job, barrier, results):
    """Reviewed benign multiprocessing target: ledger access only, no worker."""
    from backend.worker_controller import AdmissionLedger, ControllerError
    barrier.wait(timeout=15)
    try:
        lease = AdmissionLedger(path).acquire(job)
        results.put(("HELD", lease.job_id))
    except ControllerError as exc:
        results.put((exc.code.value, job))
