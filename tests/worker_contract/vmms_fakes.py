"""Deterministic in-memory metadata/job providers; never call Windows APIs."""

from dataclasses import replace
from itertools import count
from backend import vmms_policy as p

VM = '11111111-1111-4111-8111-111111111111'
OTHER_VM = '22222222-2222-4222-8222-222222222222'
EXPERIMENT = '33333333-3333-4333-8333-333333333333'
A = '00001234-facb-11e6-bd58-64006a7986d3'
B = '00001235-facb-11e6-bd58-64006a7986d3'
IDENTITY = p.Identity(VM, 'configuration-1', 'interface-1', 'incarnation-1')
_domains = count()


def new_authority():
    return p.JournalAuthority('fake-provider-' + str(next(_domains)))


class MetadataProvider:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else (
            p.Metadata(IDENTITY, VM, IDENTITY.configuration_id, True),)
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.rows


class JobProvider:
    def __init__(self, events):
        self.events = iter(events)

    def next(self, job):
        state, reference = next(self.events)
        return p.observe(job, job.operation, state, reference)


def snapshot(value=None, default=2):
    return p.Snapshot(IDENTITY, default, 3, () if value is None else
                      (p.Entry(A, value, 'baseline-A'),))


def approval(base=None):
    return p.Approval(EXPERIMENT, snapshot() if base is None else base, (A, B), 'run-1', True)


class PolicyProvider:
    """Apply only the indicated property to independent in-memory state.

    Instance IDs are allocated here, not copied from an expected after snapshot.
    Readback sequence and observation IDs model a trusted monotonic collector.
    """
    def __init__(self, state):
        self.state = state
        self.sequence = 0
        self.created = 0
        self.jobs = {}
        self.finished = set()
        self.completions = {}
        self.readbacks = []

    def read(self, operation_id=None, *, recovery=None):
        self.sequence += 1
        readback = p.Readback('read-' + str(self.sequence), self.sequence, self.state, operation_id,
                             recovery.recovery.recovery_id if recovery is not None else None,
                             recovery.state_id if recovery is not None else None)
        self.readbacks.append(readback)
        return readback

    def submit(self, operation):
        job_id = 'provider-job-' + str(len(self.jobs))
        job = p.Job(replace(operation, submission_state=p.SubmissionState.SUBMITTED),
                    p.JobState.PENDING, job_id)
        self.jobs[job_id] = job
        return job

    def finish(self, job_id):
        assert job_id not in self.finished
        pending = self.jobs[job_id]
        readback = self.apply(pending.operation)
        self.finished.add(job_id)
        completion = p.Job(pending.operation, p.JobState.COMPLETED_SUCCESS, job_id)
        self.completions[job_id] = (completion, readback)
        return self.completions[job_id]

    def apply(self, operation):
        m = operation.mutation
        p.check_precondition(m, self.state)
        if m.service_id is None:
            self.state = replace(self.state, default=m.after.default)
        else:
            key = m.service_id
            old = self.state.resource(key)
            new = m.after.resource(key)
            entries = tuple(e for e in self.state.entries if e.service_id != key)
            if new is not None:
                if old is None:
                    self.created += 1
                    instance_id = 'allocated-' + str(self.created)
                else:
                    instance_id = old.instance_id
                entries += (p.Entry(key, new.value, instance_id),)
            self.state = replace(self.state, entries=entries)
        return self.read(operation.operation_id)


def start(base=None, *, authority=None, app=None):
    app = approval(base) if app is None else app
    provider = PolicyProvider(app.baseline)
    return p.begin(app, provider.read(), authority=authority or new_authority()), provider


def complete(journal, provider, mutation, operation_id=None):
    operation_id = operation_id or 'op-' + str(len(journal.operation_ids))
    journal = p.prepare(journal, operation_id, mutation,
                        plan_version=journal.forward.version if journal.forward else None)
    job = provider.submit(journal.active.operation)
    journal = p.record_job(journal, job)
    job, readback = provider.finish(job.job_id)
    journal = p.record_job(journal, job)
    return p.confirm(journal, job, readback)


def execute_target(journal, provider, default, desired_entries):
    desired = tuple(replace(e, instance_id=journal.current.snapshot.resource(e.service_id).instance_id)
                    if e.instance_id is None and journal.current.snapshot.resource(e.service_id)
                    else e for e in desired_entries)
    journal = p.authorize_plan(journal, 'plan-' + str(journal.revision), default, desired)
    while journal.step_index < len(journal.forward.steps):
        journal = complete(journal, provider, p.next_mutation(journal))
    return journal


def finish_recovery(journal, provider):
    recovery = journal.recovery
    for step in recovery.steps:
        journal = complete(journal, provider, step)
    return p.restoration(journal, recovery, provider.read(recovery=journal))
