"""Offline VMMS policy model; no discovery, execution, persistence or Windows I/O.

JournalAuthority owns current states and provider-job associations in memory.
One live authority owns each explicit provider domain in this process. All
authority transitions use one process-local lock and immutable replacement state.
This is not cross-process exclusion, cryptographic authenticity or restart safety.
The trusted adapter must supply canonical domain/job identities and authentic
observations. A readback describes its capture time, not subsequent external
state: the Windows adapter must scope/serialize final verification and provider
mutation. Untrusted Python code must not access controller internals.

Entry.instance_id=None is allowed ONLY in desired/expected additions. Observed
snapshots require actual instance identities. Confirmation binds the returned
identity; subsequent approved steps bind placeholders to that observed identity.
"""

from dataclasses import dataclass, replace, field
from enum import Enum, IntEnum
from functools import wraps
from threading import RLock
from types import MappingProxyType
import re


class PolicyError(ValueError):
    def __init__(self):
        super().__init__("Invalid or uncertain VMMS policy evidence.")


def require(condition):
    if not condition:
        raise PolicyError()


def guid(value):
    """Accept canonical hyphenation, optional paired braces, and ASCII case only."""
    require(type(value) is str)
    if len(value) == 38 and value[0] == "{" and value[-1] == "}":
        value = value[1:-1]
    require(re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value) is not None)
    value = value.lower()
    require(value != "00000000-0000-0000-0000-000000000000")
    return value


def identifier(value):
    require(type(value) is str and 0 < len(value) <= 256)
    require(all(32 <= ord(c) <= 126 for c in value))
    return value


class DefaultPolicy(IntEnum):
    ENABLED = 2
    DISABLED = 3


class ServicePolicy(IntEnum):
    ENABLED = 2
    DISABLED = 3
    DEFERRED = 8


def policy(value, cls):
    require(type(value) is int or type(value) is cls)
    try:
        return cls(value)
    except ValueError:
        raise PolicyError() from None


@dataclass(frozen=True, slots=True)
class Identity:
    vm_id: str
    configuration_id: str
    interface_id: str
    generation: str  # Trusted inventory incarnation, NOT VM generation 1/2.

    def __post_init__(self):
        object.__setattr__(self, "vm_id", guid(self.vm_id))
        for value in (self.configuration_id, self.interface_id, self.generation):
            identifier(value)
        require(self.configuration_id != self.interface_id)


@dataclass(frozen=True, slots=True)
class Metadata:
    identity: Identity
    configuration_vm_id: str
    interface_configuration_id: str
    realized: bool

    def __post_init__(self):
        require(type(self.identity) is Identity)
        object.__setattr__(self, "configuration_vm_id", guid(self.configuration_vm_id))
        identifier(self.interface_configuration_id)
        require(type(self.realized) is bool)


def select_identity(expected, candidates):
    """Input is the targeted association result, not a host-wide VM inventory."""
    require(type(expected) is Identity and type(candidates) is tuple and len(candidates) == 1)
    row = candidates[0]
    require(type(row) is Metadata and row.realized)
    require(row.identity == expected and row.configuration_vm_id == expected.vm_id)
    require(row.interface_configuration_id == expected.configuration_id)
    return row.identity


@dataclass(frozen=True, slots=True)
class Entry:
    service_id: str
    value: ServicePolicy
    instance_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "service_id", guid(self.service_id))
        object.__setattr__(self, "value", policy(self.value, ServicePolicy))
        if self.instance_id is not None:
            identifier(self.instance_id)


@dataclass(frozen=True, slots=True)
class Snapshot:
    identity: Identity
    default: DefaultPolicy
    enabled_state: DefaultPolicy
    entries: tuple[Entry, ...]

    def __post_init__(self):
        require(type(self.identity) is Identity and type(self.entries) is tuple)
        require(len(self.entries) <= 1024 and all(type(e) is Entry for e in self.entries))
        require(len({e.service_id for e in self.entries}) == len(self.entries))
        ids = tuple(e.instance_id for e in self.entries if e.instance_id is not None)
        require(len(ids) == len(set(ids)))
        object.__setattr__(self, "default", policy(self.default, DefaultPolicy))
        object.__setattr__(self, "enabled_state", policy(self.enabled_state, DefaultPolicy))
        object.__setattr__(self, "entries", tuple(sorted(self.entries, key=lambda e: e.service_id)))

    def entry(self, service_id):
        key = guid(service_id)
        return next((e.value for e in self.entries if e.service_id == key), None)

    def resource(self, service_id):
        key = guid(service_id)
        return next((e for e in self.entries if e.service_id == key), None)


def observed(snapshot):
    require(type(snapshot) is Snapshot)
    require(all(e.instance_id is not None for e in snapshot.entries))
    return snapshot


@dataclass(frozen=True, slots=True)
class Interpretation:
    explicit: ServicePolicy | None
    inherited: bool
    effective: DefaultPolicy


def interpret(snapshot, service_id):
    require(type(snapshot) is Snapshot)
    value = snapshot.entry(service_id)
    inherited = value is None or value is ServicePolicy.DEFERRED
    return Interpretation(value, inherited, snapshot.default if inherited else DefaultPolicy(value.value))


@dataclass(frozen=True, slots=True)
class Approval:
    experiment_id: str
    baseline: Snapshot
    services: tuple[str, ...]
    run_id: str
    allow_default_change: bool = False
    scope: str = "per-vm"

    def __post_init__(self):
        object.__setattr__(self, "experiment_id", guid(self.experiment_id))
        identifier(self.run_id)
        require(type(self.baseline) is Snapshot and type(self.services) is tuple)
        observed(self.baseline)
        require(type(self.allow_default_change) is bool)
        require(type(self.scope) is str and self.scope == "per-vm")
        keys = tuple(guid(s) for s in self.services)
        require(len(keys) <= 16 and len(set(keys)) == len(keys))
        object.__setattr__(self, "services", tuple(sorted(keys)))


@dataclass(frozen=True, slots=True)
class Mutation:
    experiment_id: str
    before: Snapshot
    after: Snapshot
    service_id: str | None  # None means ONLY DefaultEnabledStatePolicy.

    def __post_init__(self):
        object.__setattr__(self, "experiment_id", guid(self.experiment_id))
        require(type(self.before) is Snapshot and type(self.after) is Snapshot)
        require(self.before.identity == self.after.identity)
        require(self.before.enabled_state == self.after.enabled_state and self.before != self.after)
        if self.service_id is None:
            require(self.before.entries == self.after.entries)
        else:
            key = guid(self.service_id)
            object.__setattr__(self, "service_id", key)
            require(self.before.default == self.after.default)
            require(tuple(e for e in self.before.entries if e.service_id != key) ==
                    tuple(e for e in self.after.entries if e.service_id != key))
            old, new = self.before.resource(key), self.after.resource(key)
            if old is not None and new is not None:
                require(old.instance_id == new.instance_id)

    @property
    def property_name(self):
        return "DefaultEnabledStatePolicy" if self.service_id is None else "EnabledStatePolicy"

    @property
    def method(self):
        if self.service_id is None:
            return "ModifyResourceSettings"
        if self.before.entry(self.service_id) is None:
            return "AddGuestServiceSettings"
        if self.after.entry(self.service_id) is None:
            return "RemoveGuestServiceSettings"
        return "ModifyGuestServiceSettings"


def check_precondition(mutation, current):
    require(type(mutation) is Mutation)
    require(observed(current) == mutation.before)


def safe_transition(start, target, states):
    """Never enable a service disabled at BOTH endpoints of the plan.

    The default is checked separately for every unlisted service. Explicit and
    deferred entries are checked over the union of relevant service GUIDs.
    """
    keys = {e.service_id for s in (start, target) + states for e in s.entries}
    for state in states:
        if start.default == target.default == DefaultPolicy.DISABLED:
            require(state.default is DefaultPolicy.DISABLED)
        for key in keys:
            if (interpret(start, key).effective is DefaultPolicy.DISABLED and
                    interpret(target, key).effective is DefaultPolicy.DISABLED):
                require(interpret(state, key).effective is DefaultPolicy.DISABLED)


def plan(approval, current, desired_default, desired_entries, *, journal=None):
    """Only per-VM policy changes; additions leave instance IDs to observed binding.

    Deletion requires a current journal proving this experiment created the exact
    instance. Permission to modify a GUID never confers ownership of its resource.
    """
    require(type(approval) is Approval)
    observed(current)
    require(current.identity == approval.baseline.identity)
    if journal is None:
        require(current == approval.baseline)
    else:
        require(type(journal) is Journal and journal.approval == approval)
        require(not journal.quarantined and journal.active is None and journal.recovery is None)
        require(current == journal.current.snapshot)
    target = Snapshot(current.identity, desired_default, current.enabled_state, desired_entries)
    require(target.default == current.default or approval.allow_default_change)
    keys = {e.service_id for e in current.entries + target.entries}
    changed = sorted(k for k in keys if current.resource(k) != target.resource(k))
    require(set(changed) <= set(approval.services))
    for key in changed:
        old, new = current.resource(key), target.resource(key)
        if old is not None and new is not None:
            require(old.instance_id == new.instance_id)
        elif old is None:
            require(new.instance_id is None)  # Provider allocates, never predict it.
        else:
            require(journal is not None)
            verify_removal(journal, old, ownership(journal, old))
    start, steps = current, []

    def default_step():
        nonlocal current
        after = replace(current, default=target.default)
        steps.append(Mutation(approval.experiment_id, current, after, None))
        current = after

    # Tighten first; relax only after restrictive exceptions have been installed.
    if current.default != target.default and target.default is DefaultPolicy.DISABLED:
        default_step()
    for key in changed:
        entries = tuple(e for e in current.entries if e.service_id != key)
        new = target.resource(key)
        if new is not None:
            entries += (new,)
        after = replace(current, entries=entries)
        steps.append(Mutation(approval.experiment_id, current, after, key))
        current = after
    if current.default != target.default:
        default_step()
    safe_transition(start, target, tuple(s.after for s in steps))
    return tuple(steps)


def counter(value):
    require(type(value) is int and value >= 0)
    return value


@dataclass(frozen=True, slots=True)
class Readback:
    observation_id: str
    sequence: int
    snapshot: Snapshot
    operation_id: str | None = None
    recovery_id: str | None = None
    recovery_state_id: object | None = None

    def __post_init__(self):
        identifier(self.observation_id)
        counter(self.sequence)
        observed(self.snapshot)
        if self.operation_id is not None:
            identifier(self.operation_id)
        if self.recovery_id is not None:
            identifier(self.recovery_id)
            require(type(self.recovery_state_id) is object)
        else:
            require(self.recovery_state_id is None)


class SubmissionState(str, Enum):
    PREPARED = "PREPARED"
    SUBMITTED = "SUBMITTED"


@dataclass(frozen=True, slots=True)
class Operation:
    operation_id: str
    mutation: Mutation
    journal_revision: int
    after_sequence: int
    run_id: str
    recovery_id: str | None = None
    submission_state: SubmissionState = SubmissionState.PREPARED
    plan_id: str | None = None
    plan_version: object | None = None
    step_index: int = 0
    provider_domain: str | None = None
    authority_id: object | None = None

    def __post_init__(self):
        identifier(self.operation_id)
        identifier(self.run_id)
        require(type(self.mutation) is Mutation)
        observed(self.mutation.before)
        counter(self.journal_revision)
        counter(self.after_sequence)
        require(type(self.submission_state) is SubmissionState)
        if self.recovery_id is not None:
            identifier(self.recovery_id)
        counter(self.step_index)
        if self.plan_id is not None:
            identifier(self.plan_id)
            require(type(self.plan_version) is object)
        else:
            require(self.plan_version is None)
        if self.provider_domain is not None:
            identifier(self.provider_domain)
            require(type(self.authority_id) is object)
        else:
            require(self.authority_id is None)

    @property
    def expected_readback(self):
        return self.mutation.after


class JobState(str, Enum):
    PREPARED = "PREPARED"
    SYNC_SUCCESS = "SYNC_SUCCESS"
    PENDING = "PENDING"
    COMPLETED_SUCCESS = "COMPLETED_SUCCESS"
    COMPLETED_FAILURE = "COMPLETED_FAILURE"
    MISSING_JOB = "MISSING_JOB"
    MALFORMED_JOB = "MALFORMED_JOB"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    LATE_COMPLETION = "LATE_COMPLETION"


SUCCESS = (JobState.SYNC_SUCCESS, JobState.COMPLETED_SUCCESS)
UNCERTAIN = (JobState.COMPLETED_FAILURE, JobState.MISSING_JOB, JobState.MALFORMED_JOB,
             JobState.TIMEOUT, JobState.CANCELLED, JobState.LATE_COMPLETION)


@dataclass(frozen=True, slots=True)
class Job:
    operation: Operation
    state: JobState
    job_id: str | None = None

    def __post_init__(self):
        require(type(self.operation) is Operation and type(self.state) is JobState)
        require((self.state is JobState.PREPARED) ==
                (self.operation.submission_state is SubmissionState.PREPARED))
        if self.job_id is not None:
            identifier(self.job_id)
        if self.state in (JobState.PENDING, JobState.COMPLETED_SUCCESS):
            require(self.job_id is not None)
        if self.state in (JobState.PREPARED, JobState.SYNC_SUCCESS):
            require(self.job_id is None)


def submission(operation, return_code, job_id=None):
    require(type(operation) is Operation and operation.submission_state is SubmissionState.PREPARED)
    operation = replace(operation, submission_state=SubmissionState.SUBMITTED)
    if type(return_code) is not int or not 0 <= return_code <= 4294967295:
        return Job(operation, JobState.MALFORMED_JOB)
    if return_code == 4096:
        if job_id is None:
            return Job(operation, JobState.MISSING_JOB)
        try:
            identifier(job_id)
        except PolicyError:
            return Job(operation, JobState.MALFORMED_JOB)
        return Job(operation, JobState.PENDING, job_id)
    if job_id is not None:
        return Job(operation, JobState.MALFORMED_JOB)
    return Job(operation, JobState.SYNC_SUCCESS if return_code == 0 else JobState.COMPLETED_FAILURE)


def observe(job, operation, state, job_id=None):
    """An observation carries the full submitted operation, not just a copied ID."""
    require(type(job) is Job and type(operation) is Operation and operation == job.operation)
    require(type(state) is JobState)
    require(job.state not in (JobState.PREPARED, *SUCCESS, JobState.COMPLETED_FAILURE,
                             JobState.LATE_COMPLETION))
    if state in (JobState.TIMEOUT, JobState.CANCELLED, JobState.MISSING_JOB, JobState.MALFORMED_JOB):
        return Job(operation, state, job.job_id)
    require(state in (JobState.PENDING, JobState.COMPLETED_SUCCESS, JobState.COMPLETED_FAILURE))
    if type(job_id) is not str or job.job_id is None or job_id != job.job_id:
        return Job(operation, JobState.MALFORMED_JOB, job.job_id)
    if job.state is not JobState.PENDING:
        if state is JobState.PENDING:
            return job
        return Job(operation, JobState.LATE_COMPLETION, job.job_id)
    return Job(operation, state, job.job_id)


def matches_expected(mutation, snapshot):
    """Only a newly added instance may differ from the expected template."""
    observed(snapshot)
    expected = mutation.after
    require(snapshot.identity == expected.identity and snapshot.default == expected.default)
    require(snapshot.enabled_state == expected.enabled_state)
    require(len(snapshot.entries) == len(expected.entries))
    for actual, wanted in zip(snapshot.entries, expected.entries):
        require(actual.service_id == wanted.service_id and actual.value == wanted.value)
        if wanted.instance_id is None:
            require(mutation.before.resource(wanted.service_id) is None and
                    mutation.service_id == wanted.service_id)
        else:
            require(actual.instance_id == wanted.instance_id)


@dataclass(frozen=True, slots=True)
class Receipt:
    job: Job
    readback: Readback

    def __post_init__(self):
        require(type(self.job) is Job and self.job.state in SUCCESS)
        require(type(self.readback) is Readback)
        op = self.job.operation
        require(self.readback.operation_id == op.operation_id)
        require(self.readback.sequence > op.after_sequence)
        matches_expected(op.mutation, self.readback.snapshot)

    @property
    def mutation(self):
        op = self.job.operation
        return Mutation(op.mutation.experiment_id, op.mutation.before,
                        self.readback.snapshot, op.mutation.service_id)


class RecoveryState(str, Enum):
    PLANNED = "PLANNED"
    PARTIAL = "PARTIAL"
    RESTORED = "RESTORED"
    REPEATED = "REPEATED"
    QUARANTINED = "QUARANTINED"


def reverse_steps(experiment_id, current, baseline):
    """Restore baseline semantics/identities without recreating deleted instances.

    Creation followed by owned deletion already restores absence: do not reverse
    that pair by inventing the deleted instance identity. Preexisting instances
    can never be deleted, so every baseline instance must still be present.
    """
    require(current.identity == baseline.identity and current.enabled_state == baseline.enabled_state)
    for entry in baseline.entries:
        now = current.resource(entry.service_id)
        require(now is not None and now.instance_id == entry.instance_id)
    start, steps = current, []
    if current.default != baseline.default and baseline.default is DefaultPolicy.DISABLED:
        after = replace(current, default=baseline.default)
        steps.append(Mutation(experiment_id, current, after, None))
        current = after
    for key in sorted({e.service_id for e in current.entries + baseline.entries}):
        if current.resource(key) == baseline.resource(key):
            continue
        entries = tuple(e for e in current.entries if e.service_id != key)
        original = baseline.resource(key)
        if original is not None:
            entries += (original,)
        after = replace(current, entries=entries)
        steps.append(Mutation(experiment_id, current, after, key))
        current = after
    if current.default != baseline.default:
        after = replace(current, default=baseline.default)
        steps.append(Mutation(experiment_id, current, after, None))
    safe_transition(start, baseline, tuple(s.after for s in steps))
    return tuple(steps)


@dataclass(frozen=True, slots=True)
class Recovery:
    recovery_id: str
    experiment_id: str
    baseline: Snapshot
    source_revision: int
    source_receipts: tuple[Receipt, ...]
    start: Readback
    steps: tuple[Mutation, ...]
    run_id: str

    def __post_init__(self):
        identifier(self.recovery_id)
        identifier(self.run_id)
        object.__setattr__(self, "experiment_id", guid(self.experiment_id))
        observed(self.baseline)
        counter(self.source_revision)
        require(type(self.source_receipts) is tuple and len(self.source_receipts) > 0)
        require(all(type(r) is Receipt for r in self.source_receipts))
        require(type(self.steps) is tuple and all(type(s) is Mutation for s in self.steps))
        require(type(self.start) is Readback and self.start.operation_id is None)
        cursor = self.baseline
        for receipt in self.source_receipts:
            op = receipt.job.operation
            require(op.run_id == self.run_id)
            require(op.recovery_id is None and op.mutation.experiment_id == self.experiment_id)
            require(op.mutation.before == cursor)
            cursor = receipt.readback.snapshot
        require(self.start.snapshot == cursor)
        require(self.start.sequence > self.source_receipts[-1].readback.sequence)
        expected = reverse_steps(self.experiment_id, cursor, self.baseline)
        require(self.steps == expected)
        safe_transition(cursor, self.baseline, tuple(s.after for s in self.steps))


@dataclass(frozen=True, slots=True)
class Journal:
    """Immutable view of an authority-owned head; copies confer no authority.

    The in-memory authority rejects old entire journals during its lifetime.
    Durable restart protection still requires a trusted compare-and-swap store.
    """
    approval: Approval
    current: Readback
    revision: int = 0
    active: Job | None = None
    receipts: tuple[Receipt, ...] = ()
    operation_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    recovery: Recovery | None = None
    completed_recoveries: tuple[str, ...] = ()
    quarantined: bool = False
    journal_id: object | None = None
    state_id: object | None = None
    forward: 'ForwardPlan | None' = None
    step_index: int = 0
    provider_domain: str | None = None
    authority_id: object | None = None

    def __post_init__(self):
        require(type(self.approval) is Approval and type(self.current) is Readback)
        require(self.current.snapshot.identity == self.approval.baseline.identity)
        counter(self.revision)
        require(type(self.quarantined) is bool)
        require(self.journal_id is None or type(self.journal_id) is object)
        require(self.state_id is None or type(self.state_id) is object)
        if self.provider_domain is not None:
            identifier(self.provider_domain)
            require(type(self.authority_id) is object)
        else:
            require(self.authority_id is None)
        counter(self.step_index)
        if self.forward is not None:
            require(type(self.forward) is ForwardPlan and self.forward.approval == self.approval)
            require(self.step_index <= len(self.forward.steps))
        require(self.active is None or type(self.active) is Job)
        require(self.recovery is None or type(self.recovery) is Recovery)
        require(type(self.receipts) is tuple and all(type(r) is Receipt for r in self.receipts))
        require(all(r.job.operation.run_id == self.approval.run_id for r in self.receipts))
        if self.recovery is not None:
            require(self.recovery.run_id == self.approval.run_id)
            require(self.recovery.experiment_id == self.approval.experiment_id)
            require(self.recovery.baseline == self.approval.baseline)
            require(self.recovery.source_revision < self.revision)
            require(self.receipts[:len(self.recovery.source_receipts)] ==
                    self.recovery.source_receipts)
        for values in (self.operation_ids, self.observation_ids, self.completed_recoveries):
            require(type(values) is tuple)
            for value in values:
                identifier(value)
            require(len(values) == len(set(values)))
        require(self.current.observation_id in self.observation_ids)
        require(all(r.job.operation.operation_id in self.operation_ids for r in self.receipts))
        require(len({r.job.operation.operation_id for r in self.receipts}) == len(self.receipts))
        if self.active is not None:
            require(self.active.operation.run_id == self.approval.run_id)
            require(self.active.operation.operation_id in self.operation_ids)
            require(all(r.job.operation.operation_id != self.active.operation.operation_id for r in self.receipts))


def _begin(approval, baseline_readback):
    require(type(approval) is Approval and type(baseline_readback) is Readback)
    require(baseline_readback.snapshot == approval.baseline and baseline_readback.operation_id is None)
    return Journal(approval, baseline_readback, observation_ids=(baseline_readback.observation_id,))


def fresh(journal, readback):
    require(type(readback) is Readback)
    require(readback.sequence > journal.current.sequence)
    require(readback.observation_id not in journal.observation_ids)


@dataclass(frozen=True, slots=True)
class Ownership:
    experiment_id: str
    identity: Identity
    entry: Entry
    creation_operation_id: str
    run_id: str

    def __post_init__(self):
        object.__setattr__(self, "experiment_id", guid(self.experiment_id))
        require(type(self.identity) is Identity and type(self.entry) is Entry)
        require(self.entry.instance_id is not None)
        identifier(self.creation_operation_id)
        identifier(self.run_id)


def ownership(journal, entry):
    require(type(journal) is Journal and type(entry) is Entry)
    require(journal.approval.baseline.resource(entry.service_id) is None)
    require(journal.current.snapshot.resource(entry.service_id) == entry)
    owner = None
    for receipt in journal.receipts:
        m = receipt.mutation
        if m.service_id != entry.service_id:
            continue
        old, new = m.before.resource(entry.service_id), m.after.resource(entry.service_id)
        if old is None and new is not None:
            owner = Ownership(m.experiment_id, m.after.identity, new,
                              receipt.job.operation.operation_id, receipt.job.operation.run_id)
        elif new is None:
            owner = None
        elif owner is not None:
            owner = replace(owner, entry=new)
    require(owner is not None and owner.entry == entry)
    return owner


def verify_removal(journal, entry, claimed):
    require(type(claimed) is Ownership)
    require(ownership(journal, entry) == claimed)
    return entry


def _prepare(journal, operation_id, mutation):
    require(type(journal) is Journal and type(mutation) is Mutation)
    require(not journal.quarantined and journal.active is None and not journal.completed_recoveries)
    identifier(operation_id)
    require(operation_id not in journal.operation_ids)
    require(mutation.experiment_id == journal.approval.experiment_id)
    check_precondition(mutation, journal.current.snapshot)
    key = mutation.service_id
    require(key in journal.approval.services if key is not None else journal.approval.allow_default_change)
    recovery_id = None
    if journal.recovery is not None:
        completed = tuple(r for r in journal.receipts
                          if r.job.operation.recovery_id == journal.recovery.recovery_id)
        require(len(completed) < len(journal.recovery.steps))
        require(mutation == journal.recovery.steps[len(completed)])
        recovery_id = journal.recovery.recovery_id
    if key is not None:
        old, new = mutation.before.resource(key), mutation.after.resource(key)
        if old is not None and new is None:
            verify_removal(journal, old, ownership(journal, old))
        if old is None:
            require(new.instance_id is None)  # Rollback never recreates preexisting entries.
    op = Operation(operation_id, mutation, journal.revision, journal.current.sequence,
                   journal.approval.run_id, recovery_id)
    return replace(journal, revision=journal.revision + 1, active=Job(op, JobState.PREPARED),
                   operation_ids=journal.operation_ids + (operation_id,))


def _record_job(journal, job):
    require(type(journal) is Journal and type(job) is Job and journal.active is not None)
    old = journal.active
    if old.state is JobState.PREPARED:
        require(job.operation == replace(old.operation, submission_state=SubmissionState.SUBMITTED))
        require(job.state in (JobState.SYNC_SUCCESS, JobState.PENDING,
                              JobState.COMPLETED_FAILURE, JobState.MISSING_JOB, JobState.MALFORMED_JOB))
    else:
        require(job.operation == old.operation)
        # Reconstruct the legal transition rather than accepting an arbitrary Job.
        if job.state is JobState.LATE_COMPLETION:
            require(old.state in (JobState.TIMEOUT, JobState.CANCELLED,
                                  JobState.MISSING_JOB, JobState.MALFORMED_JOB))
            require(old.job_id is not None and old.job_id == job.job_id)
        else:
            require(job == observe(old, old.operation, job.state, job.job_id))
    return replace(journal, revision=journal.revision + 1, active=job,
                   quarantined=journal.quarantined or job.state in UNCERTAIN)


def _confirm(journal, job, readback):
    require(type(journal) is Journal and not journal.quarantined)
    require(type(job) is Job and journal.active == job and job.state in SUCCESS)
    fresh(journal, readback)
    receipt = Receipt(job, readback)
    require(job.operation.mutation.before == journal.current.snapshot)
    return replace(journal, revision=journal.revision + 1, current=readback, active=None,
                   receipts=journal.receipts + (receipt,),
                   observation_ids=journal.observation_ids + (readback.observation_id,))


def _rollback(journal, current_readback, recovery_id):
    """Begin a recovery only from the complete, quiescent trusted journal."""
    require(type(journal) is Journal)
    identifier(recovery_id)
    require(not journal.quarantined and journal.active is None and journal.recovery is None)
    require(not journal.completed_recoveries and len(journal.receipts) > 0)
    fresh(journal, current_readback)
    require(current_readback.operation_id is None and current_readback.snapshot == journal.current.snapshot)
    steps = reverse_steps(journal.approval.experiment_id, journal.current.snapshot,
                          journal.approval.baseline)
    recovery = Recovery(recovery_id, journal.approval.experiment_id, journal.approval.baseline,
                        journal.revision, journal.receipts, current_readback, steps,
                        journal.approval.run_id)
    return replace(journal, revision=journal.revision + 1, current=current_readback, recovery=recovery,
                   observation_ids=journal.observation_ids + (current_readback.observation_id,))


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    state: RecoveryState
    journal: Journal

    def __post_init__(self):
        require(type(self.state) is RecoveryState and type(self.journal) is Journal)


def _restoration(journal, recovery, final_readback):
    """Consume fresh final evidence; never infer completion from tuple lengths."""
    require(type(journal) is Journal and type(recovery) is Recovery)
    if recovery.recovery_id in journal.completed_recoveries:
        return RecoveryResult(RecoveryState.REPEATED, journal)
    try:
        require(not journal.quarantined and journal.recovery == recovery and journal.active is None)
        require(journal.receipts[:len(recovery.source_receipts)] == recovery.source_receipts)
        completed = journal.receipts[len(recovery.source_receipts):]
        require(len(completed) <= len(recovery.steps))
        for receipt, step in zip(completed, recovery.steps):
            require(receipt.job.operation.recovery_id == recovery.recovery_id)
            require(receipt.mutation == step)
        fresh(journal, final_readback)
        require(final_readback.operation_id is None and final_readback.snapshot == journal.current.snapshot)
        if len(completed) < len(recovery.steps):
            state = RecoveryState.PARTIAL if completed else RecoveryState.PLANNED
            return RecoveryResult(state, journal)
        require(final_readback.snapshot == recovery.baseline)
        result = replace(journal, revision=journal.revision + 1, current=final_readback, recovery=None,
                         completed_recoveries=journal.completed_recoveries + (recovery.recovery_id,),
                         observation_ids=journal.observation_ids + (final_readback.observation_id,))
        return RecoveryResult(RecoveryState.RESTORED, result)
    except PolicyError:
        return RecoveryResult(RecoveryState.QUARANTINED,
                              replace(journal, revision=journal.revision + 1, quarantined=True))


@dataclass(frozen=True, slots=True)
class ForwardPlan:
    plan_id: str
    version: object
    approval: Approval
    start: Snapshot
    target: Snapshot
    steps: tuple[Mutation, ...]

    def __post_init__(self):
        identifier(self.plan_id)
        require(type(self.version) is object and type(self.approval) is Approval)
        observed(self.start)
        require(type(self.target) is Snapshot and type(self.steps) is tuple)
        require(self.start.identity == self.approval.baseline.identity == self.target.identity)
        cursor = self.start
        for step in self.steps:
            require(type(step) is Mutation and step.before == cursor)
            require(step.experiment_id == self.approval.experiment_id)
            cursor = step.after
        require(cursor == self.target)
        safe_transition(self.start, self.target, tuple(s.after for s in self.steps))


def _checkpoint(stage):
    """No-op fault/scheduling seam; tests may replace it only in memory."""


_authority_lock = RLock()


def _serialized(function):
    @wraps(function)
    def call(*args, **kwargs):
        _checkpoint('before_lock')
        with _authority_lock:
            return function(*args, **kwargs)
    return call


def _put(mapping, key, value):
    return MappingProxyType({**mapping, key: value})


class AuthorityLifecycle(str, Enum):
    ACTIVE = 'ACTIVE'
    CLOSED = 'CLOSED'
    FENCED = 'FENCED'


@dataclass(frozen=True, slots=True)
class _DomainState:
    owner: object | None
    authority: 'JournalAuthority | None'
    heads: object = field(default_factory=lambda: MappingProxyType({}))
    jobs: object = field(default_factory=lambda: MappingProxyType({}))
    consumed: frozenset = frozenset()
    operations: frozenset = frozenset()
    runs: frozenset = frozenset()
    lifecycle: AuthorityLifecycle = AuthorityLifecycle.ACTIVE


# Closed domains retain replay tombstones, never live ownership registrations.
# Only this module's trusted entry points access this registry under the lock.
_domains = MappingProxyType({})


def _publish(domain, state):
    global _domains
    _checkpoint('state_prepared')
    replacement = _put(_domains, domain, state)
    _checkpoint('before_publish')
    _domains = replacement  # Sole publication; no allocating work follows it.


class JournalAuthority:
    """Explicit process-local provider-domain lease, closed only via close().

    Registration is strong: dropping the handle never silently releases a live
    provider domain. close() releases ownership but retains replay tombstones.
    Closing unresolved/quarantined work permanently fences that domain for this
    process; a future durable coordinator must reconcile it. A cleanly closed
    domain may reopen with a fresh owner token and retained replay history.
    Canonical domain identity and authentic provider responses remain trusted
    adapter responsibilities; this is not a sandbox against hostile Python.
    """
    def __init__(self, provider_domain):
        identifier(provider_domain)
        _checkpoint('before_lock')
        with _authority_lock:
            require(not hasattr(self, '_owner'))
            previous = _domains.get(provider_domain)
            require(previous is None or (previous.owner is None and
                                         previous.lifecycle is AuthorityLifecycle.CLOSED))
            self._provider_domain = provider_domain
            self._owner = object()
            state = (_DomainState(self._owner, self) if previous is None else
                     replace(previous, owner=self._owner, authority=self, heads=MappingProxyType({}),
                             lifecycle=AuthorityLifecycle.ACTIVE))
            _checkpoint('construction_prepared')
            _publish(provider_domain, state)

    @property
    def provider_domain(self):
        return self._provider_domain

    @property
    @_serialized
    def closed(self):
        state = _domains.get(self._provider_domain)
        return state is None or state.owner is not self._owner

    @property
    @_serialized
    def lifecycle(self):
        state = _domains.get(self._provider_domain)
        if state is None or (state.owner is not None and state.owner is not self._owner):
            return AuthorityLifecycle.CLOSED
        return state.lifecycle

    def _state(self):
        state = _domains.get(self._provider_domain)
        require(state is not None and state.lifecycle is AuthorityLifecycle.ACTIVE and
                state.owner is self._owner and state.authority is self)
        return state

    def _check(self, journal):
        state = self._state()
        require(type(journal) is Journal)
        require(journal.provider_domain == self._provider_domain and journal.authority_id is self._owner)
        require(state.heads.get(journal.journal_id) is journal)

    def _advance(self, old, new, *, job=None, consumed=None, operation=None, result_state=None):
        self._check(old)
        state = self._state()
        require(new.revision == old.revision + 1)
        require(new.journal_id is old.journal_id and new.approval == old.approval)
        new = replace(new, state_id=object())
        _checkpoint('journal_prepared')
        jobs = state.jobs if job is None else _put(state.jobs, job.job_id, (old.journal_id, job.operation))
        updated = replace(state, heads=_put(state.heads, old.journal_id, new), jobs=jobs,
                          consumed=state.consumed if consumed is None else state.consumed | {consumed},
                          operations=state.operations if operation is None else state.operations | {operation})
        result = new if result_state is None else RecoveryResult(result_state, new)
        _publish(self._provider_domain, updated)
        return result

    @_serialized
    def close(self):
        if self.closed:
            return
        state = self._state()
        clean = all(not j.quarantined and j.active is None and j.recovery is None and
                    j.current.snapshot == j.approval.baseline for j in state.heads.values())
        updated = replace(state, owner=None, authority=None, heads=MappingProxyType({}),
                          lifecycle=AuthorityLifecycle.CLOSED if clean else AuthorityLifecycle.FENCED)
        _checkpoint('closure_prepared')
        _publish(self._provider_domain, updated)


def _authority(journal):
    require(type(journal) is Journal)
    state = _domains.get(journal.provider_domain)
    require(state is not None and state.authority is not None)
    state.authority._check(journal)
    return state.authority


@_serialized
def begin(approval, baseline_readback, *, authority):
    require(type(authority) is JournalAuthority)
    state = authority._state()
    initial = _begin(approval, baseline_readback)
    run = (approval.experiment_id, approval.run_id, approval.baseline.identity)
    require(run not in state.runs)
    journal = replace(initial, journal_id=object(), state_id=object(),
                      provider_domain=authority.provider_domain, authority_id=authority._owner)
    _checkpoint('journal_prepared')
    updated = replace(state, runs=state.runs | {run}, heads=_put(state.heads, journal.journal_id, journal))
    _publish(authority.provider_domain, updated)
    return journal


@_serialized
def authorize_plan(journal, plan_id, desired_default, desired_entries):
    """Trusted intent approval. An unfinished plan cannot be silently replaced."""
    authority = _authority(journal)
    identifier(plan_id)
    require(not journal.completed_recoveries)
    require(journal.forward is None or journal.step_index == len(journal.forward.steps))
    steps = plan(journal.approval, journal.current.snapshot, desired_default,
                 desired_entries, journal=journal)
    target = Snapshot(journal.current.snapshot.identity, desired_default,
                      journal.current.snapshot.enabled_state, desired_entries)
    forward = ForwardPlan(plan_id, object(), journal.approval,
                          journal.current.snapshot, target, steps)
    return authority._advance(journal, replace(journal, revision=journal.revision + 1,
                                             forward=forward, step_index=0))


@_serialized
def next_mutation(journal):
    _authority(journal)
    forward = journal.forward
    require(forward is not None and journal.step_index < len(forward.steps))
    template = forward.steps[journal.step_index]

    def bind(snapshot):
        entries = tuple(replace(e, instance_id=actual.instance_id)
                        if e.instance_id is None and
                        (actual := journal.current.snapshot.resource(e.service_id)) is not None
                        else e for e in snapshot.entries)
        return replace(snapshot, entries=entries)

    return replace(template, before=bind(template.before), after=bind(template.after))


@_serialized
def prepare(journal, operation_id, mutation, *, plan_version=None):
    authority = _authority(journal)
    identifier(operation_id)
    require(operation_id not in authority._state().operations)
    if journal.recovery is None:
        require(journal.forward is not None)
        require(plan_version is journal.forward.version)
        require(mutation == next_mutation(journal))
    prepared = _prepare(journal, operation_id, mutation)
    op = replace(prepared.active.operation, provider_domain=authority.provider_domain,
                 authority_id=authority._owner)
    if journal.recovery is None:
        op = replace(op, plan_id=journal.forward.plan_id,
                     plan_version=journal.forward.version, step_index=journal.step_index)
    prepared = replace(prepared, active=Job(op, JobState.PREPARED))
    return authority._advance(journal, prepared, operation=operation_id)


@_serialized
def record_job(journal, job):
    authority = _authority(journal)
    updated = _record_job(journal, job)
    state = authority._state()
    if job.job_id is not None:
        association = (journal.journal_id, job.operation)
        if journal.active.state is JobState.PREPARED:
            require(job.job_id not in state.jobs)
        else:
            require(state.jobs.get(job.job_id) == association)
            require(job.job_id not in state.consumed)
    return authority._advance(journal, updated, job=job if job.job_id is not None else None)


@_serialized
def confirm(journal, job, readback):
    authority = _authority(journal)
    require(type(job) is Job)
    state = authority._state()
    if job.job_id is not None:
        require(state.jobs.get(job.job_id) == (journal.journal_id, job.operation))
        require(job.job_id not in state.consumed)
    updated = _confirm(journal, job, readback)
    if journal.recovery is None:
        updated = replace(updated, step_index=journal.step_index + 1)
    return authority._advance(journal, updated, consumed=job.job_id)


@_serialized
def rollback(journal, current_readback, recovery_id):
    authority = _authority(journal)
    updated = _rollback(journal, current_readback, recovery_id)
    return authority._advance(journal, replace(updated, forward=None, step_index=0))


@_serialized
def restoration(journal, recovery, final_readback):
    authority = _authority(journal)
    require(type(recovery) is Recovery)
    if recovery.recovery_id in journal.completed_recoveries:
        return RecoveryResult(RecoveryState.REPEATED, journal)
    require(type(final_readback) is Readback)
    require(final_readback.recovery_id == recovery.recovery_id)
    require(final_readback.recovery_state_id is journal.state_id)
    result = _restoration(journal, recovery, final_readback)
    updated = result.journal
    if updated is journal:
        # Consume even a partial observation: it cannot be replayed as fresh.
        updated = replace(journal, revision=journal.revision + 1, current=final_readback,
                          observation_ids=journal.observation_ids + (final_readback.observation_id,))
    return authority._advance(journal, updated, result_state=result.state)


class EvidenceResult(str, Enum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    INCONCLUSIVE = "INCONCLUSIVE"


class Attempt(str, Enum):
    EXCHANGE = "EXCHANGE"
    POLICY_REJECTION = "POLICY_REJECTION"
    TIMEOUT = "TIMEOUT"
    REFUSED = "REFUSED"
    MISSING_LISTENER = "MISSING_LISTENER"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"


@dataclass(frozen=True, slots=True)
class Endpoint:
    experiment_id: str
    run_id: str
    identity: Identity
    service_id: str
    listener_id: str

    def __post_init__(self):
        object.__setattr__(self, "experiment_id", guid(self.experiment_id))
        object.__setattr__(self, "service_id", guid(self.service_id))
        require(type(self.identity) is Identity)
        identifier(self.run_id)
        identifier(self.listener_id)


@dataclass(frozen=True, slots=True)
class Observation:
    endpoint: Endpoint
    readback: Readback
    attempt: Attempt
    sequence: int
    policy_event_id: str | None = None

    def __post_init__(self):
        require(type(self.endpoint) is Endpoint and type(self.readback) is Readback)
        require(type(self.attempt) is Attempt)
        counter(self.sequence)
        require(self.sequence > self.readback.sequence)
        if self.policy_event_id is not None:
            identifier(self.policy_event_id)


@dataclass(frozen=True, slots=True)
class PolicyRejection:
    """Trusted provider diagnostic correlated to a particular attempt/readback.

    The host must establish diagnostic authenticity and policy-specific meaning.
    A generic socket error must never be converted into this record.
    """
    event_id: str
    endpoint: Endpoint
    observation_sequence: int
    readback_id: str

    def __post_init__(self):
        identifier(self.event_id)
        identifier(self.readback_id)
        require(type(self.endpoint) is Endpoint)
        counter(self.observation_sequence)


@dataclass(frozen=True, slots=True)
class Evidence:
    before: Observation | None
    test: Observation | None
    after: Observation | None
    rejection: PolicyRejection | None = None
    intended_endpoint: Endpoint | None = None
    intended_policy: Snapshot | None = None  # Trusted planned state with bound instance IDs.

    def __post_init__(self):
        for value in (self.before, self.test, self.after):
            require(value is None or type(value) is Observation)
        require(self.rejection is None or type(self.rejection) is PolicyRejection)
        require(self.intended_endpoint is None or type(self.intended_endpoint) is Endpoint)
        if self.intended_policy is not None:
            observed(self.intended_policy)


def classify(evidence):
    require(type(evidence) is Evidence)
    before, test, after = evidence.before, evidence.test, evidence.after
    if before is None or test is None or after is None:
        return EvidenceResult.INCONCLUSIVE
    endpoint = test.endpoint
    if evidence.intended_endpoint is None or endpoint != evidence.intended_endpoint:
        return EvidenceResult.INCONCLUSIVE
    if evidence.intended_policy is None or test.readback.snapshot != evidence.intended_policy:
        return EvidenceResult.INCONCLUSIVE
    if not (before.endpoint == endpoint == after.endpoint):
        return EvidenceResult.INCONCLUSIVE
    if not (before.sequence < test.readback.sequence < test.sequence <
            after.readback.sequence < after.sequence):
        return EvidenceResult.INCONCLUSIVE
    if len({o.readback.observation_id for o in (before, test, after)}) != 3:
        return EvidenceResult.INCONCLUSIVE
    if any(o.readback.snapshot.identity != endpoint.identity for o in (before, test, after)):
        return EvidenceResult.INCONCLUSIVE
    if before.attempt is not Attempt.EXCHANGE or after.attempt is not Attempt.EXCHANGE:
        return EvidenceResult.INCONCLUSIVE
    if test.attempt is Attempt.EXCHANGE and evidence.rejection is None:
        return EvidenceResult.ALLOWED
    rejection = evidence.rejection
    if test.attempt is not Attempt.POLICY_REJECTION or rejection is None:
        return EvidenceResult.INCONCLUSIVE
    if not (rejection.endpoint == endpoint and rejection.event_id == test.policy_event_id and
            rejection.observation_sequence == test.sequence and
            rejection.readback_id == test.readback.observation_id):
        return EvidenceResult.INCONCLUSIVE
    # Controls restore the same policy; unrelated configuration changes invalidate
    # correlation. Only the intended service or its inherited default may differ.
    control = before.readback.snapshot
    negative = test.readback.snapshot
    if after.readback.snapshot != control or control.enabled_state != negative.enabled_state:
        return EvidenceResult.INCONCLUSIVE
    if tuple(e for e in control.entries if e.service_id != endpoint.service_id) != tuple(
            e for e in negative.entries if e.service_id != endpoint.service_id):
        return EvidenceResult.INCONCLUSIVE
    if (interpret(control, endpoint.service_id).effective is not DefaultPolicy.ENABLED or
            interpret(negative, endpoint.service_id).effective is not DefaultPolicy.DISABLED):
        return EvidenceResult.INCONCLUSIVE
    return EvidenceResult.DENIED
