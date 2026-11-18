"""Synthetic tests for the night-noise permission-window subsystem.

All data is generated locally; no real site is contacted. The adopted road
notice is only an identity/effective-window prerequisite, never an acoustic
permission.
"""
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from roadcase.adoption import adopt
from roadcase.noise import record, freeze_plan, decide, verify
from roadcase.policy import (evaluate_noise_plan, to_energy, to_level,
                             round_db)
from roadcase.report import report
from roadcase.store import connect

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures'
CUTOFF = '2026-06-01T21:00:00Z'
BASE = json.loads((FIXTURE / 'night-noise.json').read_text(encoding='utf-8'))


def _write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding='utf-8')
    return path


def _noise_doc(tmp_path, mutate=None, name='noise.json'):
    doc = copy.deepcopy(BASE)
    if mutate:
        mutate(doc)
    return _write(tmp_path, name, doc)


def _plan(tmp_path, plan_id, selections, session_id='noise-N1'):
    return _write(tmp_path, plan_id + '.json',
                  {'plan_id': plan_id, 'session_id': session_id,
                   'source_id': 'road-1', 'revision': 1,
                   'evidence_cutoff': CUTOFF, 'selections': selections})


def _decide_file(tmp_path, plan_id):
    return _write(tmp_path, plan_id + '-decide.json', {'plan_id': plan_id})


def _setup(tmp_path, db_name='state.db', noise_mutate=None):
    db = tmp_path / db_name
    adopt(db, FIXTURE / 'road_normal.json')
    if noise_mutate is not None:
        record(db, _noise_doc(tmp_path, noise_mutate, 'custom-noise.json'))
    else:
        record(db, FIXTURE / 'night-noise.json')
    return db


# ---------------------------------------------------------------- energy --
def test_two_48db_sources_exceed_50db_only_via_linear_energy():
    energy = to_energy('48.00') + to_energy('48.00')
    assert round_db(to_level(energy)) == '51.01'
    # each source alone passes the same limit; the combination must not
    assert to_energy('48.00') <= to_energy('50.00')
    assert energy > to_energy('50.00')
    # dB values are never added directly, and the max source is not used
    assert round_db(48.0 + 48.0) != '51.01'
    assert round_db(max(48.0, 48.0)) != '51.01'


def test_baseline_two_crew_plan_is_blocked_not_permitted(tmp_path):
    db = _setup(tmp_path)
    freeze_plan(db, _plan(tmp_path, 'baseline', []))
    decision = decide(db, _decide_file(tmp_path, 'baseline'))
    assert decision['state'] == 'blocked'
    window = decision['windows'][0]
    assert window['slice_from'] == '2026-06-01T22:00:00Z'
    assert window['slice_until'] == '2026-06-01T23:00:00Z'
    assert window['level_db'] == '51.01'
    assert window['compliant'] is False
    blocker = decision['blockers'][0]
    assert blocker['code'] == 'limit_exceeded'
    # limit comparison happens on unrounded energy, stored alongside display
    assert float(window['energy']) == pytest.approx(
        to_energy('48.00') * 2.0)
    assert blocker['detail']['limit_db'] == '50.00'
    assert decision['completion_at'] == '2026-06-01T23:00:00Z'


def test_barrier_plan_combines_44_and_48_to_49_46_and_finishes_at_23(tmp_path):
    db = _setup(tmp_path)
    freeze_plan(db, _plan(tmp_path, 'barrier', [
        {'activity_id': 'crew-A'},
        {'activity_id': 'crew-B', 'option': 'barrier-4db'}]))
    decision = decide(db, _decide_file(tmp_path, 'barrier'))
    assert decision['state'] == 'permitted'
    window = decision['windows'][0]
    # unrounded level ~49.4554 derived from stored energy; display rounds 0.01
    assert to_level(float(window['energy'])) == pytest.approx(49.4554, abs=0.001)
    assert window['level_db'] == '49.46'
    assert float(window['energy']) == pytest.approx(
        to_energy('48.00') + to_energy('44.00'))
    sources = {s['activity_id']: s for s in window['sources']}
    assert sources['crew-B']['level_db'] == '44.00'
    assert sources['crew-B']['barrier_id'] == 'barrier-4db'
    assert sources['crew-A']['barrier_id'] is None
    assert decision['completion_at'] == '2026-06-01T23:00:00Z'


# ------------------------------------------------- full fixed schemes only --
def test_two_complete_schemes_yield_distinct_windows_and_completion(tmp_path):
    db = _setup(tmp_path)
    freeze_plan(db, _plan(tmp_path, 'separation', [
        {'activity_id': 'crew-A'},
        {'activity_id': 'crew-B', 'option': 'after-A'}]))
    freeze_plan(db, _plan(tmp_path, 'barrier', [
        {'activity_id': 'crew-A'},
        {'activity_id': 'crew-B', 'option': 'barrier-4db'}]))
    sep = decide(db, _decide_file(tmp_path, 'separation'))
    bar = decide(db, _decide_file(tmp_path, 'barrier'))
    assert [w['level_db'] for w in sep['windows']] == ['48.00', '48.00']
    assert [w['compliant'] for w in sep['windows']] == [True, True]
    assert sep['completion_at'] == '2026-06-02T00:00:00Z'
    assert bar['completion_at'] == '2026-06-01T23:00:00Z'
    assert bar['windows'][0]['level_db'] == '49.46'
    # each scheme fixed one option per activity; no mixing of the two routes
    assert {m['activity_id'] for m in sep['measures']} == {'crew-A', 'crew-B'}


def test_contractor_cannot_mix_shift_and_barrier_on_one_activity(tmp_path):
    db = _setup(tmp_path)
    with pytest.raises(ValueError, match='noise_plan_conflicting_options'):
        freeze_plan(db, _plan(tmp_path, 'mixed', [
            {'activity_id': 'crew-A'},
            {'activity_id': 'crew-B', 'shift_id': 'after-A',
             'barrier_id': 'barrier-4db'}]))


# ------------------------------------ half-open boundaries and midnight ----
def test_half_open_boundary_adjacent_runs_are_not_combined(tmp_path):
    def mutate(doc):
        # crew-A ends exactly when crew-B starts: [22,23) vs [23,24)
        doc['activities'][1]['starts'] = '2026-06-01T23:00:00Z'
        doc['activities'][1]['ends'] = '2026-06-02T00:00:00Z'
        doc['activities'][1]['shift_options'] = []
        doc['activities'][1]['barrier_options'] = []
    db = _setup(tmp_path, noise_mutate=mutate)
    freeze_plan(db, _plan(tmp_path, 'adjacent', []))
    decision = decide(db, _decide_file(tmp_path, 'adjacent'))
    assert decision['state'] == 'permitted'
    levels = [(w['slice_from'], w['level_db']) for w in decision['windows']]
    assert levels == [
        ('2026-06-01T22:00:00Z', '48.00'),
        ('2026-06-01T23:00:00Z', '48.00')]
    assert all(len(w['sources']) == 1 for w in decision['windows'])


def test_shift_crossing_midnight_still_uses_half_open_slices(tmp_path):
    def mutate(doc):
        # quiet window reaches 06:00; shifted crew-B runs 00:00-01:00
        doc['activities'][1]['shift_options'][0] = {
            'id': 'after-A',
            'starts': '2026-06-02T00:00:00Z',
            'ends': '2026-06-02T01:00:00Z'}
    db = _setup(tmp_path, noise_mutate=mutate)
    freeze_plan(db, _plan(tmp_path, 'cross-midnight', [
        {'activity_id': 'crew-A'},
        {'activity_id': 'crew-B', 'option': 'after-A'}]))
    decision = decide(db, _decide_file(tmp_path, 'cross-midnight'))
    assert decision['state'] == 'permitted'
    assert decision['completion_at'] == '2026-06-02T01:00:00Z'
    assert [w['slice_until'] for w in decision['windows']] == [
        '2026-06-01T23:00:00Z', '2026-06-02T01:00:00Z']


def test_no_limit_is_inferred_outside_the_quiet_window(tmp_path):
    def mutate(doc):
        # one crew runs 21:00-22:00, before the quiet window starts
        doc['activities'][0]['starts'] = '2026-06-01T21:00:00Z'
        doc['activities'][0]['ends'] = '2026-06-01T22:00:00Z'
        doc['activities'][1] = copy.deepcopy(doc['activities'][0])
        doc['activities'][1]['id'] = 'crew-B'
    db = _setup(tmp_path, noise_mutate=mutate)
    freeze_plan(db, _plan(tmp_path, 'outside-quiet', []))
    decision = decide(db, _decide_file(tmp_path, 'outside-quiet'))
    window = decision['windows'][0]
    assert window['in_quiet_window'] is False
    assert window['compliant'] is None
    assert window['limit_db'] is None
    assert 'outside_quiet_window_no_limit_inferred' in window['reasons']
    # nothing provable inside the quiet window -> cannot be fully permitted
    assert decision['state'] == 'partial'


# ------------------------------------------------- multi-receptor scoping ----
def test_barrier_on_one_receptor_does_not_protect_another(tmp_path):
    def mutate(doc):
        doc['receptors'].append({
            'id': 'homes-B',
            'quiet_from': '2026-06-01T22:00:00Z',
            'quiet_until': '2026-06-02T06:00:00Z',
            'limit_db': '50.00'})
        # crew-B barrier only declared for homes-A; crew-C hits homes-B raw
        doc['activities'].append({
            'id': 'crew-C', 'receptor': 'homes-B',
            'starts': '2026-06-01T22:00:00Z',
            'ends': '2026-06-01T23:00:00Z',
            'level_db': '52.00', 'shift_options': [],
            'barrier_options': []})
    db = _setup(tmp_path, noise_mutate=mutate)
    freeze_plan(db, _plan(tmp_path, 'multi-receptor', [
        {'activity_id': 'crew-A'},
        {'activity_id': 'crew-B', 'option': 'barrier-4db'},
        {'activity_id': 'crew-C'}]))
    decision = decide(db, _decide_file(tmp_path, 'multi-receptor'))
    assert decision['state'] == 'blocked'
    by_receptor = {}
    for w in decision['windows']:
        by_receptor.setdefault(w['receptor'], []).append(w)
    assert by_receptor['homes-A'][0]['compliant'] is True
    assert by_receptor['homes-B'][0]['compliant'] is False
    codes = {b['code'] for b in decision['blockers']}
    assert 'limit_exceeded' in codes
    detail = next(b['detail'] for b in decision['blockers']
                  if b['code'] == 'limit_exceeded')
    assert detail['receptor'] == 'homes-B'


def test_second_activity_past_barrier_cutoff_is_only_partially_compliant(tmp_path):
    def mutate(doc):
        # barrier-4db validity ends at 22:30 while crew-B runs until 23:00
        doc['activities'][1]['barrier_options'][0]['ends'] = \
            '2026-06-01T22:30:00Z'
    db = _setup(tmp_path, noise_mutate=mutate)
    freeze_plan(db, _plan(tmp_path, 'cutoff', [
        {'activity_id': 'crew-A'},
        {'activity_id': 'crew-B', 'option': 'barrier-4db'}]))
    decision = decide(db, _decide_file(tmp_path, 'cutoff'))
    assert decision['state'] == 'blocked'
    codes = {b['code'] for b in decision['blockers']}
    assert 'barrier_coverage_gap' in codes
    slices = {(w['slice_from'], w['slice_until']): w
              for w in decision['windows']}
    protected = slices[('2026-06-01T22:00:00Z', '2026-06-01T22:30:00Z')]
    exposed = slices[('2026-06-01T22:30:00Z', '2026-06-01T23:00:00Z')]
    assert protected['compliant'] is True and protected['level_db'] == '49.46'
    assert exposed['compliant'] is False and exposed['level_db'] == '51.01'


def test_midnight_crossing_barrier_gap_is_named_blocker(tmp_path):
    def mutate(doc):
        doc['activities'][1]['barrier_options'][0] = {
            'id': 'barrier-4db', 'attenuation_db': '4.00',
            'starts': '2026-06-01T22:00:00Z',
            'ends': '2026-06-02T00:00:00Z'}
        doc['activities'][1]['ends'] = '2026-06-02T00:30:00Z'
    db = _setup(tmp_path, noise_mutate=mutate)
    freeze_plan(db, _plan(tmp_path, 'midnight-gap', [
        {'activity_id': 'crew-A'},
        {'activity_id': 'crew-B', 'option': 'barrier-4db'}]))
    decision = decide(db, _decide_file(tmp_path, 'midnight-gap'))
    assert any(b['code'] == 'midnight_gap' for b in decision['blockers'])


# ------------------------------------ conflicts / missing / stale evidence --
def test_shift_beyond_work_effective_window_is_blocked(tmp_path):
    def mutate(doc):
        doc['activities'][1]['shift_options'][0] = {
            'id': 'after-A',
            'starts': '2026-07-01T00:00:00Z',
            'ends': '2026-07-01T01:00:00Z'}
    db = _setup(tmp_path, noise_mutate=mutate)
    freeze_plan(db, _plan(tmp_path, 'out-of-work', [
        {'activity_id': 'crew-A'},
        {'activity_id': 'crew-B', 'option': 'after-A'}]))
    decision = decide(db, _decide_file(tmp_path, 'out-of-work'))
    assert any(b['code'] == 'shift_outside_work_window'
               for b in decision['blockers'])


def test_missing_shift_evidence_and_unknown_activity_are_blockers(tmp_path):
    db = _setup(tmp_path)
    plan = _plan(tmp_path, 'missing', [
        {'activity_id': 'crew-A'},
        {'activity_id': 'crew-B', 'shift_id': 'ghost-shift'}])
    # conflicting option shape is rejected at freeze; build plan row directly
    raw = json.loads(plan.read_text(encoding='utf-8'))
    raw['selections'][1] = {'activity_id': 'crew-B',
                            'option': 'ghost-shift'}
    freeze_plan(db, _write(tmp_path, 'missing2.json', raw))
    decision = decide(db, _decide_file(tmp_path, 'missing'))
    assert any(b['code'] == 'unknown_mitigation_option'
               for b in decision['blockers'])
    with pytest.raises(ValueError, match='noise_plan_unknown_activity'):
        freeze_plan(db, _plan(tmp_path, 'no-source',
                              [{'activity_id': 'crew-Z'}]))


def test_negative_attenuation_tampering_is_detected(tmp_path):
    db = _setup(tmp_path)
    # plant a forged negative-attenuation barrier directly in evidence
    con = sqlite3.connect(db)
    con.execute("UPDATE noise_activity SET barriers_json=? WHERE session_id=? "
                "AND activity_id='crew-B'",
                (json.dumps([{'id': 'barrier-4db', 'attenuation_db': '-4.00',
                              'starts': '2026-06-01T22:00:00Z',
                              'ends': '2026-06-01T23:00:00Z'}]),
                 'noise-N1'))
    con.commit()
    con.close()
    freeze_plan(db, _plan(tmp_path, 'negative', [
        {'activity_id': 'crew-A'},
        {'activity_id': 'crew-B', 'option': 'barrier-4db'}]))
    decision = decide(db, _decide_file(tmp_path, 'negative'))
    assert any(b['code'] == 'negative_attenuation'
               for b in decision['blockers'])


def test_same_session_rewritten_text_is_rejected_and_stale_work_blocks(tmp_path):
    db = _setup(tmp_path)
    changed = copy.deepcopy(BASE)
    changed['activities'][0]['level_db'] = '49.00'
    with pytest.raises(ValueError, match='noise_session_conflict'):
        record(db, _write(tmp_path, 'rewrite.json', changed))
    # cancelled work revision cannot serve as the prerequisite
    cancel = json.loads((FIXTURE / 'road_normal.json').read_text(
        encoding='utf-8'))
    cancel['revision'] = 2
    cancel['payload']['action'] = 'cancel'
    adopt(db, _write(tmp_path, 'cancel.json', cancel))
    freeze_plan(db, _plan(tmp_path, 'stale-work', []))
    decision = decide(db, _decide_file(tmp_path, 'stale-work'))
    # the session pins revision 1; adopting rev 2 marks that revision superseded
    assert any(b['code'] == 'work_revision_superseded'
               for b in decision['blockers'])
    assert decision['state'] == 'blocked'


def test_cancelled_work_revision_is_itself_an_inactive_prerequisite(tmp_path):
    doc = copy.deepcopy(BASE)
    work = {'effective_from': '2026-06-01T00:00:00Z',
            'effective_until': '2026-07-01T00:00:00Z', 'state': 'cancelled'}
    result = evaluate_noise_plan(doc, {'selections': []}, work)
    assert any(b['code'] == 'work_revision_not_active'
               and b['detail']['state'] == 'cancelled'
               for b in result['blockers'])


# -------------------------------- repeated decisions / verification --------
def test_repeated_decision_is_idempotent_then_supersedes_on_new_evidence(tmp_path):
    db = _setup(tmp_path)
    freeze_plan(db, _plan(tmp_path, 'baseline', []))
    first = decide(db, _decide_file(tmp_path, 'baseline'))
    again = decide(db, _decide_file(tmp_path, 'baseline'))
    assert again['decision_id'] == first['decision_id']
    # new revision of the same work: a fresh decision marks the old one stale
    rev2 = json.loads((FIXTURE / 'road_normal.json').read_text(encoding='utf-8'))
    rev2['revision'] = 2
    adopt(db, _write(tmp_path, 'rev2.json', rev2))
    third = decide(db, _decide_file(tmp_path, 'baseline'))
    assert third['decision_id'] != first['decision_id']
    con = connect(db)
    old = con.execute('SELECT superseded,supersede_reason FROM noise_decision '
                      'WHERE id=?', (first['decision_id'],)).fetchone()
    assert bool(old['superseded'])
    assert old['supersede_reason'] == 'superseded_by_new_decision'
    con.close()
    # historical evidence rows are never rewritten
    con = sqlite3.connect(db)
    assert con.execute(
        'SELECT count(*) FROM noise_decision').fetchone()[0] == 2
    con.close()
    # independent verification: old proof is stale (not forged), new one valid
    check = verify(db)
    verdicts = {d['decision_id']: d for d in check['decisions']}
    assert verdicts[first['decision_id']]['verdict'] == 'stale'
    assert 'work_state_changed' in verdicts[first['decision_id']]['drift']
    assert verdicts[third['decision_id']]['verdict'] == 'verified'
    assert (check['verified'], check['forged'], check['stale']) == (1, 0, 1)


def test_independent_verification_detects_forged_levels_and_permission(tmp_path):
    db = _setup(tmp_path)
    freeze_plan(db, _plan(tmp_path, 'baseline', []))
    decision = decide(db, _decide_file(tmp_path, 'baseline'))
    check = verify(db)
    assert check['forged'] == 0 and check['verified'] == 1
    # forge a stored display level and the permission flag
    con = sqlite3.connect(db)
    con.execute('UPDATE noise_decision_window SET level_db=? WHERE decision_id=?',
                ('49.99', decision['decision_id']))
    con.execute('UPDATE noise_decision_window SET compliant=1 WHERE decision_id=?',
                (decision['decision_id'],))
    con.execute("UPDATE noise_decision SET state='permitted' WHERE id=?",
                (decision['decision_id'],))
    con.commit()
    con.close()
    check = verify(db)
    forged = check['decisions'][0]
    assert forged['verdict'] == 'forged'
    assert {'display_level_tampered', 'permission_slice_forged',
            'decision_state_forged'} <= set(forged['issues'])
    # forging the contributing-source list of a slice is detected too
    con = sqlite3.connect(db)
    con.execute('UPDATE noise_decision_window SET sources_json=? WHERE decision_id=?',
                (json.dumps([{'activity_id': 'crew-A', 'level_db': '48.00',
                              'barrier_id': None}]),
                 decision['decision_id']))
    con.commit()
    con.close()
    check = verify(db)
    assert 'source_set_forged' in check['decisions'][0]['issues']
    # the report independently flags the same forgery
    rendered = report(db)
    nv = rendered['night_noise']
    assert nv['verification']['forged'] == 1
    assert nv['decisions'][0]['state'] == 'permitted'


def test_engine_rejects_unknown_receptor_and_wrong_receptor_measure(tmp_path):
    doc = copy.deepcopy(BASE)
    work = {'effective_from': '2026-06-01T00:00:00Z',
            'effective_until': '2026-07-01T00:00:00Z', 'state': 'active'}
    bad = copy.deepcopy(doc)
    bad['activities'][0]['receptor'] = 'homes-Z'
    result = evaluate_noise_plan(bad, {'selections': []}, work)
    assert any(b['code'] == 'receptor_outside_plan_contract'
               for b in result['blockers'])
    plan = {'selections': [
        {'activity_id': 'crew-A'},
        {'activity_id': 'crew-B', 'option': 'barrier-4db',
         'receptor': 'homes-OTHER'}]}
    result = evaluate_noise_plan(doc, plan, work)
    assert any(b['code'] == 'measure_wrong_receptor'
               for b in result['blockers'])
    # the mis-scoped barrier must not reduce crew-B: slice stays at 51.01
    slice_only = result['receptors'][0]['slices'][0]
    assert slice_only['level_db'] == '51.01'
    assert all(s['barrier_id'] is None for s in slice_only['sources'])
