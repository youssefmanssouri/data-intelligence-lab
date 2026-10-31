"""Offline policy contracts; no test proves Windows policy enforcement."""

import ast
import inspect
import sys
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from backend import vmms_policy as p
from .vmms_fakes import (A, B, VM, OTHER_VM, EXPERIMENT, IDENTITY,
                         MetadataProvider, JobProvider, PolicyProvider, snapshot, approval,
                         start, complete, execute_target, finish_recovery, new_authority)


@pytest.mark.parametrize('value', [VM, VM.upper(), '{' + VM + '}'])
def test_guid_normalization(value):
    assert p.guid(value) == VM


@pytest.mark.parametrize('bad', ['', None, True, 2, b'x'*16, 'VM display name',
    VM.replace('-', ''), ' ' + VM, VM + '\n', '{' + VM, VM + '}',
    '00000000-0000-0000-0000-000000000000', 'g' + VM[1:], 'urn:uuid:' + VM])
def test_malformed_guid(bad):
    with pytest.raises(p.PolicyError):
        p.guid(bad)


def test_identity_uses_exact_association_and_incarnation():
    provider = MetadataProvider()
    assert p.select_identity(IDENTITY, provider.read()) == IDENTITY
    assert provider.reads == 1


@pytest.mark.parametrize('case', ['missing', 'duplicate', 'wrong_vm', 'stale',
                                  'wrong_config', 'snapshot', 'wrong_interface', 'list'])
def test_identity_rejects_ambiguity_and_stale_references(case):
    row = MetadataProvider().rows[0]
    rows = (row,)
    if case == 'missing': rows = ()
    if case == 'duplicate': rows = (row, row)
    if case == 'wrong_vm': rows = (replace(row, configuration_vm_id=OTHER_VM),)
    if case == 'stale': rows = (replace(row, identity=replace(IDENTITY, generation='old')),)
    if case == 'wrong_config': rows = (replace(row, interface_configuration_id='other'),)
    if case == 'snapshot': rows = (replace(row, realized=False),)
    if case == 'wrong_interface': rows = (replace(row, identity=replace(IDENTITY, interface_id='other')),)
    if case == 'list': rows = [row]
    with pytest.raises(p.PolicyError):
        p.select_identity(IDENTITY, rows)


@pytest.mark.parametrize('default', [2, 3])
@pytest.mark.parametrize('value', [None, 2, 3, 8])
def test_policy_truth_table_preserves_absence_and_deferred(default, value):
    result = p.interpret(snapshot(value, default), A)
    assert result.explicit == value
    assert result.inherited is (value is None or value == 8)
    assert result.effective == (default if value is None or value == 8 else value)


@pytest.mark.parametrize('bad', [True, False, 0, 1, 4, 9, '2', 2.0, None])
def test_unsupported_policy_values(bad):
    with pytest.raises(p.PolicyError): p.Entry(A, bad)
    with pytest.raises(p.PolicyError): snapshot(default=bad)


def test_default_cannot_be_deferred_or_service_enum():
    for bad in (8, p.ServicePolicy.ENABLED):
        with pytest.raises(p.PolicyError): snapshot(default=bad)
    with pytest.raises(p.PolicyError): p.Entry(A, p.DefaultPolicy.ENABLED)


def test_duplicate_guids_and_canonical_order():
    with pytest.raises(p.PolicyError):
        replace(snapshot(), entries=(p.Entry(A, 2), p.Entry('{' + A.upper() + '}', 3)))
    entries = (p.Entry(B, 8), p.Entry(A, 2))
    assert replace(snapshot(), entries=entries).entries == tuple(reversed(entries))


def test_plan_exact_targets_and_immutable_determinism():
    app = approval()
    steps = p.plan(app, app.baseline, 3, (p.Entry(B, 3), p.Entry(A, 2)))
    assert steps == p.plan(app, app.baseline, 3, (p.Entry(A, 2), p.Entry(B, 3)))
    assert [s.method for s in steps] == ['ModifyResourceSettings', 'AddGuestServiceSettings',
                                        'AddGuestServiceSettings']
    assert [s.property_name for s in steps] == ['DefaultEnabledStatePolicy',
                                               'EnabledStatePolicy', 'EnabledStatePolicy']
    cursor = app.baseline
    for step in steps:
        assert step.before == cursor  # Templates; observed execution is tested below.
        assert step.before.identity == IDENTITY and step.after.enabled_state == 3
        cursor = step.after
    with pytest.raises(FrozenInstanceError): steps[0].service_id = A
    assert p.plan(app, app.baseline, 2, ()) == ()


@pytest.mark.parametrize('scope', ['host', 'registry', 'Addresses', 'WildcardDescriptors', None])
def test_host_wide_plans_rejected(scope):
    with pytest.raises(p.PolicyError): replace(approval(), scope=scope)


def test_approval_limits_and_drift():
    app = replace(approval(), services=(A,), allow_default_change=False)
    for current, default, entries in ((app.baseline, 3, ()),
        (app.baseline, 2, (p.Entry(B, 2),)), (snapshot(default=3), 2, ())):
        with pytest.raises(p.PolicyError): p.plan(app, current, default, entries)
    with pytest.raises(p.PolicyError): replace(app, services=(A, A.upper()))
    step = p.plan(approval(), snapshot(), 3, ())[0]
    with pytest.raises(p.PolicyError): p.check_precondition(step, snapshot(2))
    with pytest.raises(p.PolicyError): replace(step, after=replace(step.after, enabled_state=2))



def prepared(base=None):
    journal, provider = start(base)
    journal = p.authorize_plan(journal, 'plan', 3, journal.current.snapshot.entries)
    return p.prepare(journal, 'op', p.next_mutation(journal), plan_version=journal.forward.version), provider


@pytest.mark.parametrize('code,ref,state', [
    (0, None, p.JobState.SYNC_SUCCESS), (4096, 'job-1', p.JobState.PENDING),
    (4096, None, p.JobState.MISSING_JOB), (4096, '', p.JobState.MALFORMED_JOB),
    (4096, object(), p.JobState.MALFORMED_JOB), (2, None, p.JobState.COMPLETED_FAILURE),
    (0, 'unexpected', p.JobState.MALFORMED_JOB), (True, None, p.JobState.MALFORMED_JOB),
    (-1, None, p.JobState.MALFORMED_JOB), ('0', None, p.JobState.MALFORMED_JOB)])
def test_submission_states(code, ref, state):
    journal, _ = prepared()
    assert p.submission(journal.active.operation, code, ref).state is state


@pytest.mark.parametrize('state', [p.JobState.COMPLETED_SUCCESS, p.JobState.COMPLETED_FAILURE,
    p.JobState.TIMEOUT, p.JobState.CANCELLED, p.JobState.MISSING_JOB, p.JobState.MALFORMED_JOB])
def test_job_provider_outcomes(state):
    journal, provider = prepared()
    job = p.submission(journal.active.operation, 4096, 'job')
    journal = p.record_job(journal, job)
    observed = JobProvider(((state, 'job'),)).next(job)
    journal = p.record_job(journal, observed)
    if state is p.JobState.COMPLETED_SUCCESS:
        journal = p.confirm(journal, observed, provider.apply(observed.operation))
        assert journal.active is None and len(journal.receipts) == 1
    else:
        with pytest.raises(p.PolicyError):
            p.confirm(journal, observed, provider.read('op'))


def test_pending_wrong_job_and_readback_never_confirm():
    journal, provider = prepared()
    job = p.submission(journal.active.operation, 4096, 'job')
    journal = p.record_job(journal, job)
    with pytest.raises(p.PolicyError): p.confirm(journal, job, provider.read('op'))
    wrong = p.observe(job, job.operation, p.JobState.COMPLETED_SUCCESS, 'other')
    assert wrong.state is p.JobState.MALFORMED_JOB
    done = p.observe(job, job.operation, p.JobState.COMPLETED_SUCCESS, 'job')
    journal = p.record_job(journal, done)
    with pytest.raises(p.PolicyError): p.confirm(journal, done, provider.read('op'))
    readback = provider.apply(done.operation)
    with pytest.raises(p.PolicyError): p.confirm(journal, done, replace(readback, operation_id='other'))
    with pytest.raises(p.PolicyError):
        p.observe(done, done.operation, p.JobState.PENDING, 'job')
    assert p.confirm(journal, done, readback).current == readback


@pytest.mark.parametrize('state', [p.JobState.TIMEOUT, p.JobState.CANCELLED])
def test_pending_observation_after_timeout_remains_uncertain(state):
    journal, _ = prepared()
    job = p.submission(journal.active.operation, 4096, 'job')
    journal = p.record_job(journal, job)
    failed = p.observe(job, job.operation, state)
    journal = p.record_job(journal, failed)
    assert p.observe(failed, failed.operation, p.JobState.PENDING, 'job') == failed
    assert journal.quarantined and journal.active == failed


@pytest.mark.parametrize('state', [p.JobState.TIMEOUT, p.JobState.CANCELLED])
@pytest.mark.parametrize('late', [p.JobState.COMPLETED_SUCCESS, p.JobState.COMPLETED_FAILURE])
def test_late_completion_does_not_clear_uncertainty(state, late):
    journal, provider = start()
    journal = execute_target(journal, provider, 3, (p.Entry(A, 2),))
    journal = p.rollback(journal, provider.read(), 'recovery')
    recovery = journal.recovery
    journal = p.prepare(journal, 'undo', recovery.steps[0])
    job = p.submission(journal.active.operation, 4096, 'job')
    journal = p.record_job(journal, job)
    job = p.observe(job, job.operation, state)
    journal = p.record_job(journal, job)
    job = p.observe(job, job.operation, late, 'job')
    journal = p.record_job(journal, job)
    assert job.state is p.JobState.LATE_COMPLETION and journal.quarantined
    with pytest.raises(p.PolicyError):
        p.confirm(journal, job, provider.apply(job.operation))
    assert p.restoration(journal, recovery, provider.read(recovery=journal)).state is p.RecoveryState.QUARANTINED


@pytest.mark.parametrize('field', ['vm', 'service', 'before', 'experiment', 'id', 'revision', 'recovery'])
def test_a_one_successful_job_cannot_confirm_another_operation(field):
    journal, provider = prepared()
    op = journal.active.operation
    mutation = op.mutation
    if field == 'vm':
        identity = replace(IDENTITY, vm_id=OTHER_VM)
        mutation = replace(mutation, before=replace(mutation.before, identity=identity),
                           after=replace(mutation.after, identity=identity))
    if field == 'service':
        mutation = p.Mutation(EXPERIMENT, mutation.before,
                              replace(mutation.before, entries=(p.Entry(A, 2),)), A)
    if field == 'before':
        mutation = replace(mutation, before=replace(mutation.before, enabled_state=2),
                           after=replace(mutation.after, enabled_state=2))
    if field == 'experiment': mutation = replace(mutation, experiment_id=OTHER_VM)
    other = replace(op, mutation=mutation)
    if field == 'id': other = replace(other, operation_id='other')
    if field == 'revision': other = replace(other, journal_revision=999)
    if field == 'recovery': other = replace(other, recovery_id='forged')
    wrong = p.submission(other, 0)
    with pytest.raises(p.PolicyError): p.record_job(journal, wrong)
    good = p.submission(op, 0)
    journal = p.record_job(journal, good)
    with pytest.raises(p.PolicyError): p.confirm(journal, wrong, provider.apply(good.operation))


def test_a_replay_after_confirmation_and_operation_id_reuse_rejected():
    journal, provider = prepared()
    job = p.submission(journal.active.operation, 0)
    journal = p.record_job(journal, job)
    readback = provider.apply(job.operation)
    journal = p.confirm(journal, job, readback)
    with pytest.raises(p.PolicyError): p.confirm(journal, job, readback)
    journal = p.authorize_plan(journal, 'second-plan', 3, (p.Entry(A, 2),))
    step = p.next_mutation(journal)
    with pytest.raises(p.PolicyError): p.prepare(journal, 'op', step)
    journal = p.prepare(journal, 'next', step, plan_version=journal.forward.version)
    with pytest.raises(p.PolicyError): p.record_job(journal, job)


def test_a_async_observation_cannot_substitute_operation_with_copied_id():
    journal, _ = prepared()
    job = p.submission(journal.active.operation, 4096, 'job')
    other = replace(job.operation, mutation=replace(job.operation.mutation, experiment_id=OTHER_VM))
    with pytest.raises(p.PolicyError):
        p.observe(job, other, p.JobState.COMPLETED_SUCCESS, 'job')


def test_a_cross_run_job_rejected_despite_identical_operation_and_job_ids():
    journal, _ = prepared()
    op = journal.active.operation
    other = replace(op, run_id='run-2')
    wrong = p.submission(other, 0)
    with pytest.raises(p.PolicyError):
        p.record_job(journal, wrong)
    pending = p.submission(op, 4096, 'same-job')
    with pytest.raises(p.PolicyError):
        p.observe(pending, replace(pending.operation, run_id='run-2'),
                  p.JobState.COMPLETED_SUCCESS, 'same-job')


def test_run_identity_cannot_be_boolean_or_relabel_existing_history():
    with pytest.raises(p.PolicyError):
        replace(approval(), run_id=True)
    journal, provider = start()
    journal = execute_target(journal, provider, 2, (p.Entry(A, 2),))
    entry = journal.current.snapshot.resource(A)
    claim = p.ownership(journal, entry)
    with pytest.raises(p.PolicyError):
        p.verify_removal(journal, entry, replace(claim, run_id='run-2'))
    with pytest.raises(p.PolicyError):
        replace(journal, approval=replace(journal.approval, run_id='run-2'))
    recovering = p.rollback(journal, provider.read(), 'recovery')
    assert recovering.recovery.run_id == journal.approval.run_id
    with pytest.raises(p.PolicyError):
        replace(recovering.recovery, run_id='run-2')


def recovery_ready(original=None, target=2):
    journal, provider = start(snapshot(original))
    desired = () if target is None else (p.Entry(A, target,
        'baseline-A' if original is not None else None),)
    journal = execute_target(journal, provider, 3, desired)
    return p.rollback(journal, provider.read(), 'recover'), provider


@pytest.mark.parametrize('original', [None, 2, 3, 8])
@pytest.mark.parametrize('target', [None, 2, 3, 8])
def test_exact_rollback_including_absence_and_deferred(original, target):
    if original is not None and target is None:
        # Stronger contract: baseline resources cannot be removed, even if approved.
        journal, _ = start(snapshot(original))
        with pytest.raises(p.PolicyError):
            p.plan(journal.approval, journal.current.snapshot, 3, (), journal=journal)
        return
    journal, provider = recovery_ready(original, target)
    baseline = journal.approval.baseline
    result = finish_recovery(journal, provider)
    assert result.state is p.RecoveryState.RESTORED
    assert provider.state == baseline and result.journal.current.snapshot == baseline


def test_b_fabricated_empty_or_mutable_recovery_rejected():
    journal, _ = recovery_ready()
    recovery = journal.recovery
    with pytest.raises(TypeError): p.Recovery()
    with pytest.raises(p.PolicyError): replace(recovery, source_receipts=(), steps=())
    with pytest.raises(p.PolicyError): replace(recovery, steps=list(recovery.steps))
    with pytest.raises(p.PolicyError): replace(recovery, source_receipts=list(recovery.source_receipts))
    with pytest.raises(FrozenInstanceError): recovery.steps = ()
    with pytest.raises(FrozenInstanceError): recovery.steps[0].before.default = 2


def test_b_mutable_nested_recovery_evidence_rejected():
    journal, _ = recovery_ready()
    recovery = journal.recovery
    receipt = recovery.source_receipts[0]
    with pytest.raises(TypeError):
        p.Recovery(p.RecoveryState.PLANNED)
    with pytest.raises(p.PolicyError):
        replace(recovery, source_receipts=())
    with pytest.raises(p.PolicyError):
        replace(recovery, steps=([],))
    with pytest.raises(p.PolicyError):
        replace(recovery, source_receipts=([receipt],))
    with pytest.raises(p.PolicyError):
        replace(recovery.baseline, entries=list(recovery.baseline.entries))
    with pytest.raises(p.PolicyError):
        replace(receipt, readback=[receipt.readback])
    with pytest.raises(p.PolicyError):
        replace(receipt.job, operation=[receipt.job.operation])
    with pytest.raises(p.PolicyError):
        replace(recovery.start, snapshot=[recovery.start.snapshot])


@pytest.mark.parametrize('field', ['baseline', 'experiment_id'])
def test_b_recovery_cannot_be_attached_to_different_original_approval(field):
    journal, _ = recovery_ready()
    changes = ({'baseline': replace(journal.approval.baseline, default=3)}
               if field == 'baseline' else {'experiment_id': OTHER_VM})
    with pytest.raises(p.PolicyError):
        replace(journal, approval=replace(journal.approval, **changes))


def test_b_recovery_requires_source_revision_and_exact_receipt_prefix():
    journal, _ = recovery_ready()
    with pytest.raises(p.PolicyError):
        replace(journal, revision=journal.recovery.source_revision)
    with pytest.raises(p.PolicyError):
        replace(journal, recovery=replace(journal.recovery,
                                         source_revision=journal.revision + 1))
    with pytest.raises(p.PolicyError):
        replace(journal, receipts=journal.receipts[1:])
    receipt = journal.receipts[0]
    substituted = replace(receipt, readback=replace(receipt.readback,
                                                  observation_id='substituted-readback'))
    with pytest.raises(p.PolicyError):
        replace(journal, receipts=(substituted,) + journal.receipts[1:])


@pytest.mark.parametrize('change', ['revision', 'id', 'baseline', 'experiment'])
def test_b_recovery_must_match_journal_provenance(change):
    journal, provider = recovery_ready()
    recovery = journal.recovery
    if change == 'revision': altered = replace(recovery, source_revision=recovery.source_revision + 1)
    elif change == 'id': altered = replace(recovery, recovery_id='copied')
    else:
        with pytest.raises(p.PolicyError):
            replace(recovery, **({'baseline': replace(recovery.baseline, default=3)}
                                 if change == 'baseline' else {'experiment_id': OTHER_VM}))
        return
    if change == 'id':
        with pytest.raises(p.PolicyError):
            p.restoration(journal, altered, provider.read(recovery=journal))
    else:
        assert p.restoration(journal, altered, provider.read(recovery=journal)).state is p.RecoveryState.QUARANTINED


def test_b_partial_repeated_and_fresh_final_readback():
    journal, provider = recovery_ready()
    recovery = journal.recovery
    partial = p.restoration(journal, recovery, provider.read(recovery=journal))
    assert partial.state is p.RecoveryState.PLANNED
    journal = partial.journal
    journal = complete(journal, provider, recovery.steps[0])
    if len(recovery.steps) > 1:
        partial = p.restoration(journal, recovery, provider.read(recovery=journal))
        assert partial.state is p.RecoveryState.PARTIAL
        journal = partial.journal
    for step in recovery.steps[1:]:
        journal = complete(journal, provider, step)
    # Last mutation readback cannot stand in for a fresh final observation.
    with pytest.raises(p.PolicyError):
        p.restoration(journal, recovery, journal.current)
    final = provider.read(recovery=journal)
    result = p.restoration(journal, recovery, final)
    assert result.state is p.RecoveryState.RESTORED
    replay = p.restoration(result.journal, recovery, final)
    assert replay.state is p.RecoveryState.REPEATED
    with pytest.raises(p.PolicyError):
        p.rollback(result.journal, provider.read(), 'another')


def test_b_replayed_forward_job_cannot_complete_rollback():
    journal, provider = recovery_ready()
    old_job = journal.receipts[0].job
    journal = p.prepare(journal, 'undo', journal.recovery.steps[0])
    with pytest.raises(p.PolicyError): p.record_job(journal, old_job)
    with pytest.raises(p.PolicyError):
        p.confirm(journal, old_job, provider.read(old_job.operation.operation_id))


def test_b_matching_receipt_count_and_baseline_do_not_prove_restoration():
    journal, provider = recovery_ready()
    recovery = journal.recovery
    for step in recovery.steps:
        journal = complete(journal, provider, step)
    assert len(journal.receipts) - len(recovery.source_receipts) == len(recovery.steps)
    assert provider.state == recovery.baseline
    receipt = journal.receipts[-1]
    wrong_operation = replace(receipt.job.operation, recovery_id='unrelated-recovery')
    wrong_receipt = replace(receipt, job=replace(receipt.job, operation=wrong_operation))
    substituted = replace(journal, receipts=journal.receipts[:-1] + (wrong_receipt,))
    with pytest.raises(p.PolicyError):
        p.restoration(substituted, recovery, provider.read(recovery=journal))


def test_b_drift_after_recovery_planning_and_before_final_readback():
    journal, provider = recovery_ready()
    recovery = journal.recovery
    provider.state = replace(provider.state, entries=provider.state.entries + (p.Entry(B, 2, 'other'),))
    with pytest.raises(p.PolicyError):
        p.check_precondition(recovery.steps[0], provider.state)
    assert p.restoration(journal, recovery, provider.read(recovery=journal)).state is p.RecoveryState.QUARANTINED
    # A quarantined head cannot be bypassed by retaining its old snapshot.
    with pytest.raises(p.PolicyError):
        complete(journal, provider, recovery.steps[0])
    journal, provider = recovery_ready()
    recovery = journal.recovery
    for step in recovery.steps:
        journal = complete(journal, provider, step)
    provider.state = replace(provider.state, default=3)
    assert p.restoration(journal, recovery, provider.read(recovery=journal)).state is p.RecoveryState.QUARANTINED


@pytest.mark.parametrize('case', ['drift', 'pending', 'failed', 'missing_job', 'cancelled'])
def test_rollback_requires_quiescent_complete_journal(case):
    journal, provider = start()
    journal = execute_target(journal, provider, 3, ())
    if case == 'drift':
        provider.state = replace(provider.state, entries=(p.Entry(B, 2, 'other'),))
    else:
        journal = p.authorize_plan(journal, 'pending-plan', 3, (p.Entry(A, 2),))
        journal = p.prepare(journal, 'active', p.next_mutation(journal), plan_version=journal.forward.version)
        job = p.submission(journal.active.operation, 2 if case == 'failed' else 4096,
                           None if case in ('failed', 'missing_job') else 'job')
        journal = p.record_job(journal, job)
        if case == 'cancelled':
            journal = p.record_job(journal, p.observe(job, job.operation, p.JobState.CANCELLED))
    with pytest.raises(p.PolicyError): p.rollback(journal, provider.read(), 'recover')


def test_d_preexisting_removal_cannot_be_authorized_by_guid():
    journal, _ = start(snapshot(2))
    base = journal.current.snapshot
    with pytest.raises(p.PolicyError): p.plan(journal.approval, base, 2, (), journal=journal)
    removal = p.Mutation(EXPERIMENT, base, replace(base, entries=()), A)
    with pytest.raises(p.PolicyError): p.prepare(journal, 'remove', removal)
    with pytest.raises(p.PolicyError): p.ownership(journal, base.resource(A))


@pytest.mark.parametrize('case', ['experiment', 'vm', 'creation', 'instance', 'value'])
def test_d_ownership_forgery_and_changed_resources(case):
    journal, provider = start()
    journal = execute_target(journal, provider, 2, (p.Entry(A, 2),))
    entry = journal.current.snapshot.resource(A)
    claim = p.ownership(journal, entry)
    assert p.verify_removal(journal, entry, claim) == entry
    if case == 'experiment': claim = replace(claim, experiment_id=OTHER_VM)
    if case == 'vm': claim = replace(claim, identity=replace(IDENTITY, vm_id=OTHER_VM))
    if case == 'creation': claim = replace(claim, creation_operation_id='forged')
    if case == 'instance': entry = replace(entry, instance_id='replacement')
    if case == 'value': entry = replace(entry, value=3)
    with pytest.raises(p.PolicyError): p.verify_removal(journal, entry, claim)


def test_d_owned_deletion_consumes_evidence_and_recreation_cannot_replay_it():
    journal, provider = start()
    journal = execute_target(journal, provider, 2, (p.Entry(A, 2),))
    old_entry = journal.current.snapshot.resource(A)
    old_claim = p.ownership(journal, old_entry)
    journal = execute_target(journal, provider, 2, ())
    with pytest.raises(p.PolicyError): p.verify_removal(journal, old_entry, old_claim)
    journal = execute_target(journal, provider, 2, (p.Entry(A, 2),))
    new_entry = journal.current.snapshot.resource(A)
    assert new_entry.instance_id != old_entry.instance_id
    with pytest.raises(p.PolicyError): p.verify_removal(journal, new_entry, old_claim)
    assert p.ownership(journal, new_entry).creation_operation_id != old_claim.creation_operation_id
    journal = execute_target(journal, provider, 2, ())
    # Already restored absence still requires a provenance-bound recovery and fresh readback.
    recovering = p.rollback(journal, provider.read(), 'empty-but-proven')
    assert recovering.recovery.steps == ()
    assert finish_recovery(recovering, provider).state is p.RecoveryState.RESTORED


def test_e_replacement_same_guid_value_rejected_and_additions_bind_readback():
    journal, provider = start(snapshot(2))
    step = p.plan(journal.approval, journal.current.snapshot, 3, journal.current.snapshot.entries)[0]
    replacement = replace(journal.current.snapshot, entries=(p.Entry(A, 2, 'replacement'),))
    with pytest.raises(p.PolicyError): p.check_precondition(step, replacement)
    with pytest.raises(p.PolicyError): p.plan(journal.approval, replacement, 3, replacement.entries)
    journal, provider = start()
    planned = p.plan(journal.approval, journal.current.snapshot, 3, (p.Entry(A, 2), p.Entry(B, 3)))
    journal = execute_target(journal, provider, 3, (p.Entry(A, 2), p.Entry(B, 3)))
    assert all(e.instance_id is not None for e in journal.current.snapshot.entries)
    assert planned[-1].before.resource(A).instance_id is None
    # A stale template cannot be executed instead of replanning with actual identities.
    with pytest.raises(p.PolicyError): p.prepare(journal, 'stale-template', planned[-1])
    assert len({e.instance_id for e in journal.current.snapshot.entries}) == 2


def test_e_duplicate_instances_and_prediction_rejected():
    with pytest.raises(p.PolicyError):
        replace(snapshot(), entries=(p.Entry(A, 2, 'same'), p.Entry(B, 3, 'same')))
    with pytest.raises(p.PolicyError): approval(replace(snapshot(), entries=(p.Entry(A, 2),)))
    with pytest.raises(p.PolicyError): p.plan(approval(), snapshot(), 2, (p.Entry(A, 2, 'predicted'),))


@pytest.mark.parametrize('before_default,after_default', [(2, 3), (3, 2)])
@pytest.mark.parametrize('before_value', [None, 2, 3, 8])
@pytest.mark.parametrize('after_value', [2, 3, 8])
def test_f_intermediate_states_never_enable_service_disabled_at_both_ends(
        before_default, after_default, before_value, after_value):
    base = snapshot(before_value, before_default)
    desired = (p.Entry(A, after_value, 'baseline-A' if before_value is not None else None),)
    steps = p.plan(approval(base), base, after_default, desired)
    final = steps[-1].after
    if (p.interpret(base, A).effective is p.DefaultPolicy.DISABLED and
            p.interpret(final, A).effective is p.DefaultPolicy.DISABLED):
        assert all(p.interpret(s.after, A).effective is p.DefaultPolicy.DISABLED for s in steps)


def test_f_restrictive_exception_precedes_relaxed_default():
    base = snapshot(default=3)
    steps = p.plan(approval(base), base, 2, (p.Entry(B, 3),))
    assert steps[0].service_id == B and steps[-1].service_id is None
    assert all(p.interpret(s.after, B).effective is p.DefaultPolicy.DISABLED for s in steps)
    journal, provider = start(base)
    journal = execute_target(journal, provider, 2, (p.Entry(B, 3),))
    journal = p.rollback(journal, provider.read(), 'recover')
    assert journal.recovery.steps[0].service_id is None  # Tighten before removing exception.
    assert finish_recovery(journal, provider).state is p.RecoveryState.RESTORED


def evidence(attempt=p.Attempt.POLICY_REJECTION):
    endpoint = p.Endpoint(EXPERIMENT, 'run', IDENTITY, A, 'listener')
    control = snapshot(2)
    negative = snapshot(3)
    before = p.Observation(endpoint, p.Readback('before', 1, control), p.Attempt.EXCHANGE, 2)
    test = p.Observation(endpoint, p.Readback('negative', 3, negative), attempt, 4, 'event')
    after = p.Observation(endpoint, p.Readback('after', 5, control), p.Attempt.EXCHANGE, 6)
    diagnostic = p.PolicyRejection('event', endpoint, 4, 'negative')
    return p.Evidence(before, test, after,
                      diagnostic if attempt is p.Attempt.POLICY_REJECTION else None, endpoint, negative)


def test_c_missing_intended_policy_is_inconclusive_and_unbound_instances_rejected():
    ev = evidence()
    assert p.classify(replace(ev, intended_policy=None)) is p.EvidenceResult.INCONCLUSIVE
    with pytest.raises(p.PolicyError):
        replace(ev, intended_policy=True)
    with pytest.raises(p.PolicyError):
        replace(ev, intended_policy=replace(ev.intended_policy, entries=(p.Entry(A, 3),)))


@pytest.mark.parametrize('field', ['missing', 'vm', 'service', 'listener', 'run'])
def test_c_consistent_observations_must_match_intended_endpoint(field):
    ev = evidence()
    intended = ev.intended_endpoint
    if field == 'missing': intended = None
    if field == 'vm': intended = replace(intended, identity=replace(IDENTITY, vm_id=OTHER_VM))
    if field == 'service': intended = replace(intended, service_id=B)
    if field == 'listener': intended = replace(intended, listener_id='intended-listener')
    if field == 'run': intended = replace(intended, run_id='intended-run')
    assert p.classify(replace(ev, intended_endpoint=intended)) is p.EvidenceResult.INCONCLUSIVE


@pytest.mark.parametrize('attempt', list(p.Attempt))
@pytest.mark.parametrize('positive', [True, False])
def test_evidence_never_confuses_failure_with_denial(attempt, positive):
    ev = evidence(attempt)
    if not positive: ev = replace(ev, before=replace(ev.before, attempt=p.Attempt.TIMEOUT))
    expected = p.EvidenceResult.INCONCLUSIVE
    if positive and attempt is p.Attempt.EXCHANGE: expected = p.EvidenceResult.ALLOWED
    if positive and attempt is p.Attempt.POLICY_REJECTION: expected = p.EvidenceResult.DENIED
    assert p.classify(ev) is expected


@pytest.mark.parametrize('change', ['instance', 'default'])
def test_c_observed_disabled_policy_must_be_the_intended_exact_state(change):
    ev = evidence()
    actual = ev.test.readback.snapshot
    if change == 'instance':
        actual = replace(actual, entries=(replace(actual.resource(A), instance_id='recreated-A'),))
    else:
        actual = replace(actual, default=3)
    # Effective denial alone cannot establish that the planned state was applied.
    assert p.interpret(actual, A).effective is p.DefaultPolicy.DISABLED
    altered = replace(ev, test=replace(ev.test, readback=replace(ev.test.readback, snapshot=actual)))
    assert p.classify(altered) is p.EvidenceResult.INCONCLUSIVE


@pytest.mark.parametrize('attempt', [p.Attempt.TIMEOUT, p.Attempt.REFUSED, p.Attempt.MISSING_LISTENER,
                                    p.Attempt.TRANSPORT_FAILURE, p.Attempt.MALFORMED_RESPONSE])
def test_generic_failure_cannot_be_relabelled_by_correlation_record(attempt):
    ev = evidence()
    assert p.classify(replace(ev, test=replace(ev.test, attempt=attempt))) is p.EvidenceResult.INCONCLUSIVE


@pytest.mark.parametrize('which', ['before', 'test', 'after'])
@pytest.mark.parametrize('field', ['vm', 'incarnation', 'service', 'listener', 'run', 'experiment',
                                   'readback_vm', 'readback_id', 'missing'])
def test_c_evidence_identity_substitution_is_inconclusive(which, field):
    ev = evidence()
    obs = getattr(ev, which)
    endpoint = obs.endpoint
    if field == 'vm': endpoint = replace(endpoint, identity=replace(IDENTITY, vm_id=OTHER_VM))
    if field == 'incarnation': endpoint = replace(endpoint, identity=replace(IDENTITY, generation='old'))
    if field == 'service': endpoint = replace(endpoint, service_id=B)
    if field == 'listener': endpoint = replace(endpoint, listener_id='other')
    if field == 'run': endpoint = replace(endpoint, run_id='other')
    if field == 'experiment': endpoint = replace(endpoint, experiment_id=OTHER_VM)
    obs = replace(obs, endpoint=endpoint)
    if field == 'readback_vm':
        obs = replace(obs, readback=replace(obs.readback,
            snapshot=replace(obs.readback.snapshot, identity=replace(IDENTITY, vm_id=OTHER_VM))))
    if field == 'readback_id':
        # Replay another observation's policy version.
        obs = replace(obs, readback=replace(obs.readback,
            observation_id='negative' if which != 'test' else 'before'))
    if field == 'missing': obs = None
    assert p.classify(replace(ev, **{which: obs})) is p.EvidenceResult.INCONCLUSIVE


@pytest.mark.parametrize('field', ['event_id', 'endpoint', 'observation_sequence', 'readback_id'])
def test_c_policy_specific_diagnostic_must_match_attempt(field):
    ev = evidence()
    value = {'event_id': 'other', 'endpoint': replace(ev.test.endpoint, service_id=B),
             'observation_sequence': 99, 'readback_id': 'other'}[field]
    assert p.classify(replace(ev, rejection=replace(ev.rejection, **{field: value}))) is p.EvidenceResult.INCONCLUSIVE


def test_c_unrelated_policy_drift_and_failed_after_control_inconclusive():
    ev = evidence()
    changed = replace(ev.test.readback.snapshot, entries=ev.test.readback.snapshot.entries + (p.Entry(B, 2, 'B'),))
    assert p.classify(replace(ev, test=replace(ev.test,
        readback=replace(ev.test.readback, snapshot=changed)))) is p.EvidenceResult.INCONCLUSIVE
    assert p.classify(replace(ev, after=replace(ev.after, attempt=p.Attempt.REFUSED))) is p.EvidenceResult.INCONCLUSIVE
    assert p.classify(replace(ev, rejection=None)) is p.EvidenceResult.INCONCLUSIVE


def test_exact_types_do_not_invoke_hostile_equality():
    class Meta(type):
        def __eq__(cls, other): raise AssertionError('equality invoked')
    class Hostile(metaclass=Meta): pass
    for operation in (lambda: p.guid(Hostile()), lambda: p.policy(Hostile(), p.ServicePolicy),
                      lambda: p.select_identity(Hostile(), ())):
        with pytest.raises(p.PolicyError): operation()


def test_f_unsafe_intermediate_state_is_rejected():
    base = snapshot(default=3)
    target = replace(base, default=2, entries=(p.Entry(B, 3, 'observed-B'),))
    unsafe = replace(base, default=2)
    with pytest.raises(p.PolicyError):
        p.safe_transition(base, target, (unsafe, target))
    p.safe_transition(base, target, (replace(base, entries=target.entries), target))


def test_c_boolean_correlation_is_not_identity_evidence():
    with pytest.raises(p.PolicyError):
        p.Evidence(True, True, True, True)
    assert p.classify(p.Evidence(None, None, None)) is p.EvidenceResult.INCONCLUSIVE


def test_d_ownership_receipt_cannot_substitute_service():
    journal, provider = start()
    journal = execute_target(journal, provider, 2, (p.Entry(A, 2), p.Entry(B, 2)))
    entry_a = journal.current.snapshot.resource(A)
    entry_b = journal.current.snapshot.resource(B)
    claim = p.ownership(journal, entry_a)
    with pytest.raises(p.PolicyError):
        p.verify_removal(journal, entry_b, claim)
    with pytest.raises(p.PolicyError):
        p.verify_removal(journal, entry_b, replace(claim, entry=entry_b))


def test_dry_run_has_no_external_dependencies_or_calls(monkeypatch):
    source = Path(p.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imports == {'dataclasses', 'enum', 're', 'functools', 'threading', 'types'}
    import builtins
    import io
    import os
    import socket
    import subprocess
    import sys
    import types
    code = compile(source, p.__file__, 'exec')
    module = types.ModuleType('_offline_vmms_import_probe')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    def forbidden(*args, **kwargs): raise AssertionError('external call')
    journal, provider = start()
    with monkeypatch.context() as guard:
        for mod, name in ((builtins, 'open'), (io, 'open'), (os, 'open'), (os, 'system'),
                          (socket, 'socket'), (subprocess, 'Popen')):
            guard.setattr(mod, name, forbidden)
        exec(code, module.__dict__)  # Import-time body also runs under guards.
        journal = execute_target(journal, provider, 3, (p.Entry(A, 2),))
        journal = p.rollback(journal, provider.read(), 'dry-run')
        assert finish_recovery(journal, provider).state is p.RecoveryState.RESTORED


def approved_add(journal, service, operation_id):
    journal = p.authorize_plan(journal, 'plan-' + operation_id, journal.current.snapshot.default,
                               journal.current.snapshot.entries + (p.Entry(service, 2),))
    return p.prepare(journal, operation_id, p.next_mutation(journal),
                     plan_version=journal.forward.version)


def finish_provider_job(journal, provider, job):
    journal = p.record_job(journal, job)
    done, readback = provider.finish(job.job_id)
    journal = p.record_job(journal, done)
    return p.confirm(journal, done, readback), done, readback


def test_provider_job_id_cannot_be_reused_for_two_services():
    journal, provider = start()
    journal = approved_add(journal, A, 'a')
    first = provider.submit(journal.active.operation)
    journal, old_completion, old_readback = finish_provider_job(journal, provider, first)
    journal = approved_add(journal, B, 'b')
    second = provider.submit(journal.active.operation)
    # Simulate a provider/adapter recycling a previously consumed identity.
    with pytest.raises(p.PolicyError):
        p.record_job(journal, replace(second, job_id=first.job_id))
    journal, _, _ = finish_provider_job(journal, provider, second)
    assert provider.state.resource(A) and provider.state.resource(B)
    assert len({r.job.job_id for r in journal.receipts}) == 2
    with pytest.raises(p.PolicyError): p.record_job(journal, old_completion)
    with pytest.raises(p.PolicyError): p.confirm(journal, old_completion, old_readback)
    journal = p.authorize_plan(journal, 'third', 3, journal.current.snapshot.entries)
    journal = p.prepare(journal, 'third', p.next_mutation(journal), plan_version=journal.forward.version)
    third = provider.submit(journal.active.operation)
    journal = p.record_job(journal, third)
    # Replay after B completed, while a different operation is actually pending.
    with pytest.raises(p.PolicyError): p.record_job(journal, old_completion)
    with pytest.raises(p.PolicyError): p.confirm(journal, old_completion, old_readback)


@pytest.mark.parametrize('dimension', ['vm', 'incarnation', 'experiment', 'run'])
def test_provider_job_reuse_across_journals_is_rejected(dimension):
    authority = new_authority()
    first, provider = start(authority=authority)
    first = approved_add(first, A, 'first')
    pending = provider.submit(first.active.operation)
    first, _, _ = finish_provider_job(first, provider, pending)
    app = approval()
    if dimension == 'vm': app = replace(app, baseline=replace(app.baseline, identity=replace(IDENTITY, vm_id=OTHER_VM)))
    if dimension == 'incarnation': app = replace(app, baseline=replace(app.baseline, identity=replace(IDENTITY, generation='new')))
    if dimension == 'experiment': app = replace(app, experiment_id=OTHER_VM)
    if dimension == 'run': app = replace(app, run_id='second-run')
    second, other_provider = start(authority=authority, app=app)
    second = approved_add(second, B, 'second')
    # Separate provider instance deliberately emits the same job ID for a new request.
    reused = other_provider.submit(second.active.operation)
    assert reused.job_id == pending.job_id
    with pytest.raises(p.PolicyError): p.record_job(second, reused)


@pytest.mark.parametrize('field', ['operation_id', 'mutation', 'run_id'])
def test_provider_completion_must_match_retained_submission(field):
    journal, provider = start()
    journal = approved_add(journal, A, 'a')
    pending = provider.submit(journal.active.operation)
    journal = p.record_job(journal, pending)
    done, readback = provider.finish(pending.job_id)
    op = done.operation
    if field == 'operation_id': op = replace(op, operation_id='other')
    if field == 'run_id': op = replace(op, run_id='other-run')
    if field == 'mutation':
        op = replace(op, mutation=p.Mutation(EXPERIMENT, op.mutation.before,
                     replace(op.mutation.before, entries=(p.Entry(B, 2),)), B))
    with pytest.raises(p.PolicyError): p.record_job(journal, replace(done, operation=op))
    journal = p.record_job(journal, done)
    assert p.confirm(journal, done, readback).current.snapshot == provider.state


def test_actual_provider_state_must_match_expected_state():
    journal, provider = start()
    journal = approved_add(journal, A, 'a')
    pending = provider.submit(journal.active.operation)
    journal = p.record_job(journal, pending)
    done, _ = provider.finish(pending.job_id)
    # External drift after the provider completed the requested mutation.
    provider.state = replace(provider.state, entries=(replace(provider.state.resource(A), value=3),))
    actual = provider.read(done.operation.operation_id)
    journal = p.record_job(journal, done)
    with pytest.raises(p.PolicyError): p.confirm(journal, done, actual)


def recovered_head():
    journal, provider = recovery_ready()
    recovery = journal.recovery
    for step in recovery.steps:
        journal = complete(journal, provider, step)
    return journal, provider, recovery


def test_authority_rejects_relabelled_source_and_copied_current_head():
    journal, provider, recovery = recovered_head()
    final = provider.read(recovery=journal)
    altered = replace(recovery, source_revision=0)
    with pytest.raises(p.PolicyError):
        p.restoration(replace(journal, recovery=altered), altered, final)
    with pytest.raises(p.PolicyError):
        p.restoration(replace(journal), recovery, final)
    result = p.restoration(journal, recovery, final)
    assert result.state is p.RecoveryState.RESTORED
    assert result.journal.journal_id is journal.journal_id
    assert result.journal.state_id is not journal.state_id
    assert result.journal.revision == journal.revision + 1
    with pytest.raises(p.PolicyError): p.restoration(journal, recovery, final)
    with pytest.raises(p.PolicyError):
        p.restoration(replace(journal, revision=result.journal.revision), recovery, final)
    assert p.restoration(result.journal, recovery, final).state is p.RecoveryState.REPEATED


@pytest.mark.parametrize('field', ['experiment', 'run', 'baseline', 'recovery_id', 'rollback_id'])
def test_authority_rejects_modified_history(field):
    journal, provider, recovery = recovered_head()
    with pytest.raises(p.PolicyError):
        altered = journal
        if field == 'experiment': altered = replace(journal, approval=replace(journal.approval, experiment_id=OTHER_VM))
        if field == 'run': altered = replace(journal, approval=replace(journal.approval, run_id='wrong'))
        if field == 'baseline': altered = replace(journal, approval=replace(journal.approval, baseline=replace(journal.approval.baseline, default=3)))
        if field == 'recovery_id': altered = replace(journal, recovery=replace(recovery, recovery_id='wrong'))
        if field == 'rollback_id':
            last = journal.receipts[-1]
            op = replace(last.job.operation, recovery_id='wrong')
            altered = replace(journal, receipts=journal.receipts[:-1] + (replace(last, job=replace(last.job, operation=op)),))
        p.restoration(altered, altered.recovery, provider.read(recovery=journal))


def test_same_revision_in_another_journal_does_not_authorize_final_readback():
    first, provider, recovery = recovered_head()
    other, other_provider, other_recovery = recovered_head()
    assert first.revision == other.revision
    assert first.journal_id is not other.journal_id
    foreign = other_provider.read(recovery=other)
    with pytest.raises(p.PolicyError): p.restoration(first, recovery, foreign)
    assert p.restoration(first, recovery, provider.read(recovery=first)).state is p.RecoveryState.RESTORED
    assert p.restoration(other, other_recovery, foreign).state is p.RecoveryState.RESTORED


@pytest.mark.parametrize('start_default,target_default,value', [(3, 2, 3), (2, 3, 2)])
def test_prepare_enforces_both_transition_orders(start_default, target_default, value):
    journal, provider = start(snapshot(default=start_default))
    journal = p.authorize_plan(journal, 'ordered', target_default, (p.Entry(B, value),))
    version = journal.forward.version
    with pytest.raises(p.PolicyError):
        p.authorize_plan(journal, 'bypass-target', target_default, ())
    wrong = (p.Mutation(EXPERIMENT, journal.current.snapshot,
                        replace(journal.current.snapshot, default=target_default), None)
             if start_default == 3 else
             p.Mutation(EXPERIMENT, journal.current.snapshot,
                        replace(journal.current.snapshot, entries=(p.Entry(B, value),)), B))
    with pytest.raises(p.PolicyError): p.prepare(journal, 'skip', wrong, plan_version=version)
    with pytest.raises(p.PolicyError):
        p.prepare(journal, 'skip', journal.forward.steps[1], plan_version=version)
    first = p.next_mutation(journal)
    assert first.service_id == (B if start_default == 3 else None)
    journal = complete(journal, provider, first)
    assert provider.state.default is p.DefaultPolicy.DISABLED
    journal = complete(journal, provider, p.next_mutation(journal))
    assert provider.state.default == target_default and provider.state.entry(B) == value


@pytest.mark.parametrize('field', ['unapproved', 'experiment', 'vm', 'baseline', 'version'])
def test_prepare_rejects_unapproved_or_substituted_plan_step(field):
    journal, _ = start(snapshot(default=3))
    if field == 'unapproved':
        mutation = p.Mutation(EXPERIMENT, journal.current.snapshot,
                              replace(journal.current.snapshot, default=2), None)
        with pytest.raises(p.PolicyError): p.prepare(journal, 'raw', mutation)
        return
    journal = p.authorize_plan(journal, 'approved', 2, (p.Entry(B, 3),))
    mutation = p.next_mutation(journal)
    version = journal.forward.version
    if field == 'experiment': mutation = replace(mutation, experiment_id=OTHER_VM)
    if field == 'vm': mutation = replace(mutation,
        before=replace(mutation.before, identity=replace(IDENTITY, vm_id=OTHER_VM)),
        after=replace(mutation.after, identity=replace(IDENTITY, vm_id=OTHER_VM)))
    if field == 'baseline': mutation = replace(mutation,
        before=replace(mutation.before, enabled_state=2), after=replace(mutation.after, enabled_state=2))
    if field == 'version': version = object()
    with pytest.raises(p.PolicyError): p.prepare(journal, 'bad', mutation, plan_version=version)


def test_prepare_rejects_cross_run_plan_version_and_stale_plan():
    journal, provider = start()
    journal = p.authorize_plan(journal, 'one', 3, ())
    old_version = journal.forward.version
    old_head = journal
    journal = complete(journal, provider, p.next_mutation(journal))
    journal = p.authorize_plan(journal, 'two', 2, ())
    with pytest.raises(p.PolicyError):
        p.prepare(journal, 'stale', p.next_mutation(journal), plan_version=old_version)
    with pytest.raises(p.PolicyError): p.next_mutation(old_head)
    other, _ = start(app=replace(approval(), run_id='different-run'))
    other = p.authorize_plan(other, 'two', 3, ())
    with pytest.raises(p.PolicyError):
        p.prepare(journal, 'cross-run', p.next_mutation(journal), plan_version=other.forward.version)
    journal = complete(journal, provider, p.next_mutation(journal))
    assert provider.state.default is p.DefaultPolicy.ENABLED


def test_rollback_cannot_rebind_a_consumed_provider_job():
    journal, provider = recovery_ready()
    old = journal.receipts[0].job
    journal = p.prepare(journal, 'rollback-job', journal.recovery.steps[0])
    pending = provider.submit(journal.active.operation)
    with pytest.raises(p.PolicyError):
        p.record_job(journal, replace(pending, job_id=old.job_id))
    journal, _, _ = finish_provider_job(journal, provider, pending)
    for step in journal.recovery.steps[1:]:
        journal = complete(journal, provider, step)
    assert p.restoration(journal, journal.recovery, provider.read(recovery=journal)).state is p.RecoveryState.RESTORED


def test_partial_recovery_consumes_observation_and_original_head():
    journal, provider = recovery_ready()
    recovery = journal.recovery
    observation = provider.read(recovery=journal)
    result = p.restoration(journal, recovery, observation)
    assert result.state is p.RecoveryState.PLANNED
    with pytest.raises(p.PolicyError): p.restoration(journal, recovery, observation)
    with pytest.raises(p.PolicyError): p.restoration(result.journal, recovery, observation)
    assert finish_recovery(result.journal, provider).state is p.RecoveryState.RESTORED


def race_at_entry(monkeypatch, calls):
    """Both threads reach the validation/lock boundary before either proceeds."""
    gate = threading.Barrier(len(calls), timeout=5)
    local = threading.local()
    results = [None] * len(calls)

    def checkpoint(stage):
        if stage == 'before_lock' and not getattr(local, 'entered', False):
            local.entered = True
            gate.wait()

    def worker(index, call):
        try:
            results[index] = call()
        except Exception as exc:
            results[index] = exc

    with monkeypatch.context() as patch:
        patch.setattr(p, '_checkpoint', checkpoint)
        threads = [threading.Thread(target=worker, args=(i, call), daemon=True)
                   for i, call in enumerate(calls)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        assert all(not thread.is_alive() for thread in threads)
    assert all(not isinstance(result, threading.BrokenBarrierError) for result in results)
    return results


def test_concurrent_provider_job_registration_exactly_one_succeeds(monkeypatch):
    authority = new_authority()
    first, a = start(authority=authority)
    other, b = start(authority=authority, app=replace(approval(), run_id='other'))
    first = approved_add(first, A, 'a')
    other = approved_add(other, B, 'b')
    job_a = a.submit(first.active.operation)
    job_b = b.submit(other.active.operation)
    assert job_a.job_id == job_b.job_id
    results = race_at_entry(monkeypatch, [lambda: p.record_job(first, job_a),
                                        lambda: p.record_job(other, job_b)])
    assert sum(type(result) is p.Journal for result in results) == 1
    assert sum(type(result) is p.PolicyError for result in results) == 1
    winner = next(result for result in results if type(result) is p.Journal)
    state = authority._state()
    assert state.jobs[job_a.job_id] == (winner.journal_id, winner.active.operation)
    assert state.heads[winner.journal_id] is winner


def completion_ready():
    journal, provider = prepared()
    pending = provider.submit(journal.active.operation)
    journal = p.record_job(journal, pending)
    done, readback = provider.finish(pending.job_id)
    return p.record_job(journal, done), provider, done, readback


def test_concurrent_completion_consumed_exactly_once(monkeypatch):
    journal, _, done, readback = completion_ready()
    results = race_at_entry(monkeypatch, [lambda: p.confirm(journal, done, readback)] * 2)
    assert sum(type(result) is p.Journal for result in results) == 1
    assert sum(type(result) is p.PolicyError for result in results) == 1
    winner = next(result for result in results if type(result) is p.Journal)
    assert len(winner.receipts) == 1 and winner.revision == journal.revision + 1
    assert done.job_id in p._authority(winner)._state().consumed


def test_concurrent_recovery_restored_at_most_once(monkeypatch):
    journal, provider, recovery = recovered_head()
    final = provider.read(recovery=journal)
    results = race_at_entry(monkeypatch, [lambda: p.restoration(journal, recovery, final)] * 2)
    restored = [result for result in results if type(result) is p.RecoveryResult]
    assert len(restored) == 1 and restored[0].state is p.RecoveryState.RESTORED
    assert sum(type(result) is p.PolicyError for result in results) == 1
    assert p.restoration(restored[0].journal, recovery, final).state is p.RecoveryState.REPEATED


def test_confirm_check_publish_interleaving_regression(monkeypatch):
    """Old code: contender commits while first pauses after validation, then first
    commits again. New code: contender waits for the same lock and is rejected.
    No sleeps; the trace changes scheduling only, never validation or state.
    """
    journal, _, done, readback = completion_ready()
    validated = threading.Event()
    contender_at_boundary = threading.Event()
    actual_lock = threading.RLock()
    results = [None, None]
    source, first_line = inspect.getsourcelines(p.JournalAuthority._advance)
    boundary = first_line + next(i for i, line in enumerate(source)
                                 if 'require(new.revision' in line)

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == 'contender':
                contender_at_boundary.set()
            actual_lock.acquire()
            return self

        def __exit__(self, *exc):
            actual_lock.release()

    def trace(frame, event, arg):
        if (event == 'line' and frame.f_code is p.JournalAuthority._advance.__code__ and
                frame.f_lineno == boundary):
            validated.set()
            assert contender_at_boundary.wait(5)
        return trace

    def worker(index):
        if index == 0: sys.settrace(trace)
        try:
            results[index] = p.confirm(journal, done, readback)
        except Exception as exc:
            results[index] = exc
        finally:
            sys.settrace(None)
            if index == 1: contender_at_boundary.set()

    with monkeypatch.context() as patch:
        # raising=False also lets this scheduling test run on the old unlocked code.
        patch.setattr(p, '_authority_lock', ObservedLock(), raising=False)
        first = threading.Thread(target=worker, args=(0,), daemon=True)
        second = threading.Thread(target=worker, args=(1,), name='contender', daemon=True)
        first.start()
        assert validated.wait(5)
        second.start()
        first.join(10)
        second.join(10)
        assert not first.is_alive() and not second.is_alive()
    assert type(results[0]) is p.Journal
    assert type(results[1]) is p.PolicyError


@pytest.mark.parametrize('error', [MemoryError, RuntimeError])
@pytest.mark.parametrize('stage', ['journal_prepared', 'state_prepared', 'before_publish'])
@pytest.mark.parametrize('transition', ['begin', 'plan', 'prepare', 'submission', 'completion',
                                       'confirm', 'rollback', 'restoration'])
def test_atomic_transition_faults_leave_every_registry_unchanged(monkeypatch, error, stage, transition):
    authority = new_authority()
    journal, provider = start(authority=authority)
    if transition == 'begin':
        app = replace(approval(), run_id='second')
        baseline = provider.read()
        call = lambda: p.begin(app, baseline, authority=authority)
    elif transition == 'plan':
        call = lambda: p.authorize_plan(journal, 'plan', 3, ())
    elif transition == 'prepare':
        journal = p.authorize_plan(journal, 'plan', 3, ())
        mutation = p.next_mutation(journal)
        call = lambda: p.prepare(journal, 'op', mutation, plan_version=journal.forward.version)
    elif transition in ('submission', 'completion', 'confirm'):
        journal = approved_add(journal, A, 'op')
        pending = provider.submit(journal.active.operation)
        if transition == 'submission':
            call = lambda: p.record_job(journal, pending)
        else:
            journal = p.record_job(journal, pending)
            done, readback = provider.finish(pending.job_id)
            if transition == 'completion':
                call = lambda: p.record_job(journal, done)
            else:
                journal = p.record_job(journal, done)
                call = lambda: p.confirm(journal, done, readback)
    else:
        journal = execute_target(journal, provider, 3, ())
        if transition == 'rollback':
            observation = provider.read()
            call = lambda: p.rollback(journal, observation, 'recover')
        else:
            journal = p.rollback(journal, provider.read(), 'recover')
            recovery = journal.recovery
            for step in recovery.steps: journal = complete(journal, provider, step)
            final = provider.read(recovery=journal)
            call = lambda: p.restoration(journal, recovery, final)
    registry = p._domains
    prior = authority._state()
    seen = []

    def fail(label):
        if label == stage:
            seen.append(label)
            raise error('injected ' + label)

    with monkeypatch.context() as patch:
        patch.setattr(p, '_checkpoint', fail)
        with pytest.raises(error): call()
    assert seen == [stage]
    assert p._domains is registry and authority._state() is prior
    assert prior.heads[journal.journal_id] is journal
    # Retrying the exact recorded provider outcome is safe; no provider re-execution.
    result = call()
    assert result is not None and authority._state() is not prior


def test_duplicate_domain_and_closed_authority_lifecycle():
    authority = new_authority()
    domain = authority.provider_domain
    journal, provider = start(authority=authority)
    with pytest.raises(p.PolicyError): p.JournalAuthority(domain)
    authority.close()
    assert authority.closed
    assert authority.lifecycle is p.AuthorityLifecycle.CLOSED
    authority.close()  # Idempotent shutdown.
    with pytest.raises(p.PolicyError): p.authorize_plan(journal, 'late', 3, ())
    with pytest.raises(p.PolicyError): p.begin(approval(), provider.read(), authority=authority)
    reopened = p.JournalAuthority(domain)
    assert not reopened.closed
    assert reopened._owner is not authority._owner
    with pytest.raises(p.PolicyError): p.begin(approval(), provider.read(), authority=reopened)
    fresh, _ = start(authority=reopened, app=replace(approval(), run_id='fresh'))
    assert fresh.authority_id is reopened._owner
    with pytest.raises(p.PolicyError): p.authorize_plan(journal, 'old-owner', 3, ())
    reopened.close()


def test_reopened_domain_preserves_consumed_job_tombstones():
    authority = new_authority()
    journal, provider = start(authority=authority)
    journal = execute_target(journal, provider, 3, ())
    old = journal.receipts[0].job
    journal = p.rollback(journal, provider.read(), 'recover')
    finish_recovery(journal, provider)
    authority.close()
    reopened = p.JournalAuthority(authority.provider_domain)
    fresh, other = start(authority=reopened, app=replace(approval(), run_id='fresh'))
    fresh = approved_add(fresh, A, 'new-op')
    recycled = other.submit(fresh.active.operation)
    assert recycled.job_id == old.job_id
    with pytest.raises(p.PolicyError): p.record_job(fresh, recycled)
    with pytest.raises(p.PolicyError): p.record_job(fresh, old)
    reopened.close()  # Unresolved request fences further reopening.
    with pytest.raises(p.PolicyError): p.JournalAuthority(authority.provider_domain)


@pytest.mark.parametrize('uncertainty', [None, p.JobState.TIMEOUT, p.JobState.CANCELLED])
def test_shutdown_with_unresolved_work_fences_domain(uncertainty):
    authority = new_authority()
    journal, provider = start(authority=authority)
    journal = approved_add(journal, A, 'op')
    if uncertainty is not None:
        pending = provider.submit(journal.active.operation)
        journal = p.record_job(journal, pending)
        journal = p.record_job(journal, p.observe(pending, pending.operation, uncertainty))
        response = p.observe(journal.active, journal.active.operation,
                             p.JobState.COMPLETED_SUCCESS, pending.job_id)
    else:
        response = provider.submit(journal.active.operation)
    authority.close()
    assert authority.closed
    assert authority.lifecycle is p.AuthorityLifecycle.FENCED
    with pytest.raises(p.PolicyError): p.JournalAuthority(authority.provider_domain)
    with pytest.raises(p.PolicyError): p.record_job(journal, response)


@pytest.mark.parametrize('error', [MemoryError, RuntimeError])
@pytest.mark.parametrize('stage', ['construction_prepared', 'state_prepared', 'before_publish'])
def test_construction_failure_does_not_reserve_domain(monkeypatch, error, stage):
    authority = new_authority()
    domain = authority.provider_domain + '-construction'
    before = p._domains

    def fail(label):
        if label == stage: raise error('construction fault')

    with monkeypatch.context() as patch:
        patch.setattr(p, '_checkpoint', fail)
        with pytest.raises(error): p.JournalAuthority(domain)
    assert p._domains is before and domain not in p._domains
    replacement = p.JournalAuthority(domain)
    replacement.close()
    authority.close()


@pytest.mark.parametrize('error', [MemoryError, RuntimeError])
@pytest.mark.parametrize('stage', ['closure_prepared', 'state_prepared', 'before_publish'])
def test_failed_close_leaves_live_owner_registered(monkeypatch, error, stage):
    authority = new_authority()
    before = p._domains

    def fail(label):
        if label == stage: raise error('close fault')

    with monkeypatch.context() as patch:
        patch.setattr(p, '_checkpoint', fail)
        with pytest.raises(error): authority.close()
    assert p._domains is before and not authority.closed
    with pytest.raises(p.PolicyError): p.JournalAuthority(authority.provider_domain)
    authority.close()
    replacement = p.JournalAuthority(authority.provider_domain)
    replacement.close()


def test_wrong_provider_domain_or_authority_cannot_replace_current_identity():
    first, _ = start()
    other, _ = start()
    with pytest.raises(p.PolicyError):
        p.authorize_plan(replace(first, provider_domain=other.provider_domain), 'wrong-domain', 3, ())
    with pytest.raises(p.PolicyError):
        p.authorize_plan(replace(first, authority_id=other.authority_id), 'wrong-owner', 3, ())


def test_final_readback_observes_capture_time_not_later_external_state():
    journal, provider, recovery = recovered_head()
    observation = provider.read(recovery=journal)
    provider.state = replace(provider.state, default=3)
    assert provider.state != recovery.baseline
    result = p.restoration(journal, recovery, observation)
    # No host access exists here: final verification must be scoped/serialized by
    # the future trusted Windows adapter. RESTORED describes supplied observations.
    assert result.state is p.RecoveryState.RESTORED
    assert observation.snapshot == recovery.baseline
    with pytest.raises(p.PolicyError): p.restoration(journal, recovery, observation)


def test_concurrent_duplicate_authority_creation_has_one_owner(monkeypatch):
    seed = new_authority()
    domain = seed.provider_domain + '-race'
    results = race_at_entry(monkeypatch, [lambda: p.JournalAuthority(domain)] * 2)
    assert sum(type(result) is p.JournalAuthority for result in results) == 1
    assert sum(type(result) is p.PolicyError for result in results) == 1
    winner = next(result for result in results if type(result) is p.JournalAuthority)
    assert p._domains[domain].authority is winner
    winner.close()
    seed.close()


def test_close_and_prepare_are_serialized(monkeypatch):
    authority = new_authority()
    journal, _ = start(authority=authority)
    journal = p.authorize_plan(journal, 'plan', 3, ())
    mutation = p.next_mutation(journal)
    results = race_at_entry(monkeypatch, [lambda: authority.close(),
        lambda: p.prepare(journal, 'op', mutation, plan_version=journal.forward.version)])
    assert results[0] is None and authority.closed
    if type(results[1]) is p.Journal:
        assert authority.lifecycle is p.AuthorityLifecycle.FENCED
        with pytest.raises(p.PolicyError): p.JournalAuthority(authority.provider_domain)
    else:
        assert type(results[1]) is p.PolicyError
        reopened = p.JournalAuthority(authority.provider_domain)
        reopened.close()


def test_provider_records_submissions_completions_and_readbacks_separately():
    journal, provider = start()
    journal = approved_add(journal, A, 'independent')
    pending = provider.submit(journal.active.operation)
    assert provider.state.resource(A) is None and not provider.completions
    done, observed = provider.finish(pending.job_id)
    assert provider.jobs[pending.job_id].state is p.JobState.PENDING
    assert provider.completions[pending.job_id] == (done, observed)
    assert provider.readbacks[-1] is observed
    assert observed.snapshot.resource(A).instance_id is not None
    assert journal.active.operation.expected_readback.resource(A).instance_id is None


@pytest.mark.parametrize('error', [MemoryError, RuntimeError])
@pytest.mark.parametrize('allocation', [1, 2, 3])
def test_mapping_allocation_failure_never_partially_reserves_job(monkeypatch, error, allocation):
    journal, provider = start()
    journal = approved_add(journal, A, 'allocation')
    pending = provider.submit(journal.active.operation)
    authority = p._authority(journal)
    prior = authority._state()
    registry = p._domains
    original = p._put
    calls = []

    def allocate(mapping, key, value):
        calls.append(key)
        if len(calls) == allocation:
            raise error('mapping allocation')
        return original(mapping, key, value)

    with monkeypatch.context() as patch:
        patch.setattr(p, '_put', allocate)
        with pytest.raises(error): p.record_job(journal, pending)
    assert len(calls) == allocation
    assert p._domains is registry and authority._state() is prior
    assert pending.job_id not in prior.jobs
    assert prior.heads[journal.journal_id] is journal
    updated = p.record_job(journal, pending)
    assert authority._state().heads[journal.journal_id] is updated
    assert authority._state().jobs[pending.job_id] == (updated.journal_id, pending.operation)
