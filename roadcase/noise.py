"""Intake for synthetic night-work acoustic evidence and mitigation options.

The intake only preserves source evidence. Plan freezing, energy-based
permission decisions and independent verification live below; none of them
reuse the road notice outcome as an acoustic permission.
"""

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .source import utc, read, canonical, digest
from .store import connect
from .policy import evaluate_noise_plan


SCHEMA = """
CREATE TABLE IF NOT EXISTS noise_source (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, revision INTEGER NOT NULL,
 digest TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS noise_receptor (
 session_id TEXT NOT NULL REFERENCES noise_source(id), receptor TEXT NOT NULL,
 quiet_from TEXT NOT NULL, quiet_until TEXT NOT NULL, limit_db TEXT NOT NULL,
 PRIMARY KEY(session_id,receptor));
CREATE TABLE IF NOT EXISTS noise_activity (
 session_id TEXT NOT NULL REFERENCES noise_source(id), activity_id TEXT NOT NULL,
 receptor TEXT NOT NULL, starts TEXT NOT NULL, ends TEXT NOT NULL,
 level_db TEXT NOT NULL, shifts_json TEXT NOT NULL, barriers_json TEXT NOT NULL,
 PRIMARY KEY(session_id,activity_id,receptor));
"""


def _db_number(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('noise_bad_decibel') from exc
    if not number.is_finite() or number < 0:
        raise ValueError('noise_bad_decibel')
    return str(number)


def record(db_path, input_path):
    raw = Path(input_path).read_bytes()
    item = json.loads(raw)
    session_id = item['session_id']
    source_id = item['source_id']
    revision = item['revision']
    if not session_id or not source_id or type(revision) is not int or revision < 1:
        raise ValueError('noise_bad_identity')
    receptors = item['receptors']
    activities = item['activities']
    if not receptors or not activities:
        raise ValueError('noise_empty_plan')
    for r in receptors:
        if utc(r['quiet_from']) >= utc(r['quiet_until']):
            raise ValueError('noise_bad_quiet_window')
        _db_number(r['limit_db'])
    for a in activities:
        if utc(a['starts']) >= utc(a['ends']):
            raise ValueError('noise_bad_activity_window')
        _db_number(a['level_db'])
        for shift in a.get('shift_options', []):
            if utc(shift['starts']) >= utc(shift['ends']):
                raise ValueError('noise_bad_shift')
        for barrier in a.get('barrier_options', []):
            _db_number(barrier['attenuation_db'])
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        if not db.execute('SELECT 1 FROM work_revision WHERE source_id=? AND revision=?',
                          (source_id, revision)).fetchone():
            raise ValueError('noise_unknown_work_revision')
        prior = db.execute('SELECT digest FROM noise_source WHERE id=?',
                           (session_id,)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('noise_session_conflict')
            return 0
        with db:
            db.execute('INSERT INTO noise_source VALUES (?,?,?,?,?)',
                       (session_id, source_id, revision, digest, raw))
            for r in receptors:
                db.execute('INSERT INTO noise_receptor VALUES (?,?,?,?,?)',
                           (session_id, r['id'], r['quiet_from'],
                            r['quiet_until'], _db_number(r['limit_db'])))
            for a in activities:
                db.execute('INSERT INTO noise_activity VALUES (?,?,?,?,?,?,?,?)',
                           (session_id, a['id'], a['receptor'], a['starts'], a['ends'],
                            _db_number(a['level_db']),
                            json.dumps(a.get('shift_options',[]),sort_keys=True),
                            json.dumps(a.get('barrier_options',[]),sort_keys=True)))
        return len(activities)
    finally:
        db.close()


def _load_session(db, session_id):
    row = db.execute('SELECT * FROM noise_source WHERE id=?',
                     (session_id,)).fetchone()
    if not row:
        raise ValueError('noise_session_unknown')
    doc = json.loads(row['raw'])
    activities = []
    for a in db.execute(
            'SELECT * FROM noise_activity WHERE session_id=? ORDER BY activity_id',
            (session_id,)):
        item = {'id': a['activity_id'], 'receptor': a['receptor'],
                'starts': a['starts'], 'ends': a['ends'],
                'level_db': a['level_db'],
                'shift_options': json.loads(a['shifts_json']),
                'barrier_options': json.loads(a['barriers_json'])}
        activities.append(item)
    receptor_rows = []
    for r in db.execute(
            'SELECT * FROM noise_receptor WHERE session_id=? ORDER BY receptor',
            (session_id,)):
        receptor_rows.append({'id': r['receptor'],
                              'quiet_from': r['quiet_from'],
                              'quiet_until': r['quiet_until'],
                              'limit_db': r['limit_db']})
    return row, {'session_id': row['id'], 'source_id': row['source_id'],
                 'revision': row['revision'], 'receptors': receptor_rows,
                 'activities': activities}


def freeze_plan(db_path, input_path):
    """noise-plan: freeze the input summary, permit contract and cutoff."""
    doc = read(input_path)
    for key in ('plan_id', 'session_id', 'evidence_cutoff', 'selections'):
        if key not in doc:
            raise ValueError('noise_plan_missing_' + key)
    if not isinstance(doc['selections'], list):
        raise ValueError('noise_plan_selections_required')
    utc(doc['evidence_cutoff'])
    db = connect(db_path)
    try:
        source_row, session = _load_session(db, doc['session_id'])
        if doc.get('source_id') != source_row['source_id'] \
                or doc.get('revision') != source_row['revision']:
            raise ValueError('noise_plan_identity_mismatch')
        known = {a['id'] for a in session['activities']}
        for sel in doc['selections']:
            if sel.get('activity_id') not in known:
                raise ValueError('noise_plan_unknown_activity')
            if sel.get('shift_id') is not None and sel.get('barrier_id') is not None:
                raise ValueError('noise_plan_conflicting_options')
        input_digest = digest(doc)
        existing = db.execute('SELECT input_digest FROM noise_plan WHERE plan_id=?',
                              (doc['plan_id'],)).fetchone()
        if existing:
            if existing['input_digest'] != input_digest:
                raise ValueError('noise_plan_identity_conflict')
            return {'plan_id': doc['plan_id'], 'state': 'frozen',
                    'input_digest': input_digest, 'idempotent': True}
        with db:
            db.execute(
                'INSERT INTO noise_plan(plan_id,session_id,source_id,revision,'
                'evidence_cutoff,input_digest,source_digest,receptors_json,'
                'plan_json) VALUES (?,?,?,?,?,?,?,?,?)',
                (doc['plan_id'], doc['session_id'], source_row['source_id'],
                 source_row['revision'], utc(doc['evidence_cutoff']).isoformat(),
                 input_digest, source_row['digest'],
                 canonical(session['receptors']), canonical(doc)))
        return {'plan_id': doc['plan_id'], 'state': 'frozen',
                'input_digest': input_digest, 'idempotent': False}
    finally:
        db.close()


def decide(db_path, input_path):
    """noise-decide: energy-based permission windows for one fixed scheme."""
    doc = read(input_path)
    if 'plan_id' not in doc:
        raise ValueError('noise_decide_missing_plan_id')
    db = connect(db_path)
    db.execute('BEGIN IMMEDIATE')
    try:
        plan_row = db.execute('SELECT * FROM noise_plan WHERE plan_id=?',
                              (doc['plan_id'],)).fetchone()
        if not plan_row:
            raise ValueError('noise_plan_unknown')
        source_row, session = _load_session(db, plan_row['session_id'])
        if source_row['digest'] != plan_row['source_digest']:
            raise ValueError('noise_source_changed_under_plan')
        plan = json.loads(plan_row['plan_json'])
        work = db.execute(
            'SELECT * FROM work_revision WHERE source_id=? AND revision=?',
            (plan_row['source_id'], plan_row['revision'])).fetchone()
        work = dict(work) if work else None
        notice = db.execute('SELECT * FROM work_notice WHERE id=?',
                            (plan_row['source_id'],)).fetchone()
        notice_state = notice['state'] if notice else None

        prior = db.execute(
            'SELECT id FROM noise_decision WHERE plan_id=? ORDER BY id DESC LIMIT 1',
            (doc['plan_id'],)).fetchone()
        result = evaluate_noise_plan(session, plan, work)
        proven = [s for rr in result['receptors'] for s in rr['slices']
                  if s['in_quiet_window'] and s['compliant']]
        unproven = [s for rr in result['receptors'] for s in rr['slices']
                    if s['in_quiet_window'] and s['compliant'] is not True]
        if result['blockers']:
            state = 'blocked'
        elif unproven or len(proven) == 0:
            state = 'partial'
        else:
            state = 'permitted'

        # identical re-run is idempotent; underlying evidence change supersedes
        if prior:
            prev = db.execute('SELECT * FROM noise_decision WHERE id=?',
                              (prior['id'],)).fetchone()
            fingerprint = digest({'state': state,
                                  'blockers': result['blockers'],
                                  'receptors': result['receptors'],
                                  'completion_at': result['completion_at'],
                                  'source_digest': source_row['digest'],
                                  'work_digest': work['digest'] if work else None,
                                  'work_state': work['state'] if work else None})
            if prev['input_digest'] == fingerprint and not prev['superseded']:
                db.commit()
                return _describe_decision(db, prev['id'])
            # mark the previous proof expired inside the same transaction;
            # history rows are never rewritten or deleted
            db.execute('UPDATE noise_decision SET superseded=1, '
                       'supersede_reason=? WHERE id=?',
                       ('superseded_by_new_decision', prev['id']))

        source_summary = {
            'session_id': source_row['id'],
            'source_id': source_row['source_id'],
            'revision': source_row['revision'],
            'noise_digest': source_row['digest'],
            'plan_input_digest': plan_row['input_digest'],
            'evidence_cutoff': plan_row['evidence_cutoff'],
            'work_digest': work['digest'] if work else None,
            'work_state': work['state'] if work else None,
            'notice_state': notice_state,
            'receptors': session['receptors'],
            'activities': [{'activity_id': a['id'], 'receptor': a['receptor'],
                            'window': [a['starts'], a['ends']],
                            'level_db': a['level_db'],
                            'shift_options': a['shift_options'],
                            'barrier_options': a['barrier_options']}
                           for a in session['activities']],
            'selections': plan['selections']}
        fingerprint = digest({'state': state, 'blockers': result['blockers'],
                              'receptors': result['receptors'],
                              'completion_at': result['completion_at'],
                              'source_digest': source_row['digest'],
                              'work_digest': work['digest'] if work else None,
                              'work_state': work['state'] if work else None})
        cur = db.execute(
            'INSERT INTO noise_decision(plan_id,session_id,source_id,revision,'
            'state,input_digest,source_digest,work_digest,work_state,notice_state,'
            'work_window_json,completion_at,blockers_json,findings_json,'
            'measures_json,source_summary_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (doc['plan_id'], plan_row['session_id'], plan_row['source_id'],
             plan_row['revision'], state, fingerprint, source_row['digest'],
             work['digest'] if work else None,
             work['state'] if work else None, notice_state,
             canonical([work['effective_from'], work['effective_until']])
             if work else None,
             result['completion_at'], canonical(result['blockers']),
             canonical(result['findings']), canonical(plan['selections']),
             canonical(source_summary)))
        decision_id = cur.lastrowid
        for blocker in result['blockers']:
            db.execute('INSERT INTO noise_decision_blocker(decision_id,code,detail_json)'
                       ' VALUES (?,?,?)',
                       (decision_id, blocker['code'], canonical(blocker['detail'])))
        for rr in result['receptors']:
            for s in rr['slices']:
                db.execute(
                    'INSERT INTO noise_decision_window(decision_id,receptor,'
                    'slice_from,slice_until,in_quiet_window,sources_json,energy,'
                    'unrounded_level_db,level_db,limit_db,compliant,reasons_json) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (decision_id, s['receptor'], s['start'], s['end'],
                     1 if s['in_quiet_window'] else 0, canonical(s['sources']),
                     repr(s['energy']), repr(s['unrounded_level_db']),
                     s['level_db'], s['limit_db'],
                     None if s['compliant'] is None else (1 if s['compliant'] else 0),
                     canonical(s['reasons'])))
        db.commit()
        return _describe_decision(db, decision_id)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _describe_decision(db, decision_id):
    row = db.execute('SELECT * FROM noise_decision WHERE id=?',
                     (decision_id,)).fetchone()
    windows = [dict(x) for x in db.execute(
        'SELECT receptor,slice_from,slice_until,in_quiet_window,sources_json,'
        'energy,unrounded_level_db,level_db,limit_db,compliant,reasons_json FROM '
        'noise_decision_window WHERE decision_id=? ORDER BY receptor,slice_from',
        (decision_id,))]
    for w in windows:
        w['sources'] = json.loads(w.pop('sources_json'))
        w['reasons'] = json.loads(w.pop('reasons_json'))
        w['in_quiet_window'] = bool(w['in_quiet_window'])
        w['unrounded_level_db'] = float(w['unrounded_level_db'])
        w['compliant'] = None if w['compliant'] is None else bool(w['compliant'])
    blockers = [dict(x) for x in db.execute(
        'SELECT code,detail_json FROM noise_decision_blocker WHERE decision_id=?',
        (decision_id,))]
    for b in blockers:
        b['detail'] = json.loads(b.pop('detail_json'))
    return {'decision_id': decision_id, 'plan_id': row['plan_id'],
            'session_id': row['session_id'], 'source_id': row['source_id'],
            'revision': row['revision'], 'state': row['state'],
            'work_state': row['work_state'], 'notice_state': row['notice_state'],
            'completion_at': row['completion_at'],
            'superseded': bool(row['superseded']),
            'supersede_reason': row['supersede_reason'],
            'measures': json.loads(row['measures_json']),
            'blockers': blockers, 'windows': windows,
            'findings': json.loads(row['findings_json']),
            'source_summary': json.loads(row['source_summary_json'])}


def verify(db_path, plan_id=None):
    """noise-verify: recompute every decision from saved originals."""
    db = connect(db_path)
    try:
        return verify_db(db, plan_id)
    finally:
        db.close()


def verify_db(db, plan_id=None):
    """Connection-based verification reused by the evidence report."""
    rows = db.execute('SELECT * FROM noise_decision ORDER BY id').fetchall()
    verdicts = []
    for row in rows:
        if plan_id is not None and row['plan_id'] != plan_id:
            continue
        source_row, session = _load_session(db, row['session_id'])
        plan_row = db.execute('SELECT * FROM noise_plan WHERE plan_id=?',
                              (row['plan_id'],)).fetchone()
        plan = json.loads(plan_row['plan_json'])
        work = db.execute(
            'SELECT * FROM work_revision WHERE source_id=? AND revision=?',
            (row['source_id'], row['revision'])).fetchone()
        work = dict(work) if work else None
        issues = []
        drift = []
        if source_row['digest'] != row['source_digest']:
            # intake forbids rewriting a session, so a mismatch is tampering
            issues.append('noise_digest_tampered')
        if work is not None and row['work_digest'] != work['digest']:
            # a newer legitimate adoption expires the old proof, not forgery
            drift.append('work_version_changed')
        if work is not None and row['work_state'] != work['state']:
            # same bytes, new adoption state (e.g. superseded): proof expires
            drift.append('work_state_changed')
        work_drifted = bool(drift)
        fresh = evaluate_noise_plan(session, plan, work)
        stored_windows = db.execute(
            'SELECT * FROM noise_decision_window WHERE decision_id=? '
            'ORDER BY receptor,slice_from', (row['id'],)).fetchall()
        fresh_slices = [s for rr in fresh['receptors'] for s in rr['slices']]
        if len(stored_windows) != len(fresh_slices):
            issues.append('slice_set_forged')
        else:
            for stored, s in zip(stored_windows, fresh_slices):
                if stored['slice_from'] != s['start'] \
                        or stored['slice_until'] != s['end'] \
                        or stored['receptor'] != s['receptor']:
                    issues.append('slice_window_forged')
                if abs(float(stored['energy']) - s['energy']) > 1e-12:
                    issues.append('energy_tampered')
                if abs(float(stored['unrounded_level_db'])
                       - s['unrounded_level_db']) > 1e-12:
                    issues.append('unrounded_level_tampered')
                if stored['level_db'] != s['level_db']:
                    issues.append('display_level_tampered')
                if (stored['compliant'] is None) != (s['compliant'] is None) \
                        or (stored['compliant'] is not None
                            and bool(stored['compliant']) != s['compliant']):
                    issues.append('permission_slice_forged')
                if json.loads(stored['sources_json']) != s['sources']:
                    issues.append('source_set_forged')
        expected_blockers = sorted(b['code'] for b in fresh['blockers'])
        stored_blockers = sorted(x['code'] for x in db.execute(
            'SELECT code FROM noise_decision_blocker WHERE decision_id=?',
            (row['id'],)))
        if expected_blockers != stored_blockers:
            if work_drifted:
                drift.append('blocker_set_drifted')
            else:
                issues.append('blocker_set_forged')
        if fresh['completion_at'] != row['completion_at']:
            issues.append('completion_forged')
        proven = [s for rr in fresh['receptors'] for s in rr['slices']
                  if s['in_quiet_window'] and s['compliant']]
        unproven = [s for rr in fresh['receptors'] for s in rr['slices']
                    if s['in_quiet_window'] and s['compliant'] is not True]
        state = ('blocked' if fresh['blockers']
                 else ('partial' if unproven or not proven else 'permitted'))
        if state != row['state']:
            if work_drifted:
                drift.append('decision_state_drifted')
            else:
                issues.append('decision_state_forged')
        if issues:
            verdict = 'forged'
        elif drift:
            verdict = 'stale'
        else:
            verdict = 'verified'
        verdicts.append({'decision_id': row['id'], 'plan_id': row['plan_id'],
                         'state': row['state'], 'verdict': verdict,
                         'superseded': bool(row['superseded']),
                         'issues': issues, 'drift': drift,
                         'recomputed_state': state,
                         'recomputed_windows': fresh_slices})
    return {'verified': sum(1 for v in verdicts if v['verdict'] == 'verified'),
            'forged': sum(1 for v in verdicts if v['verdict'] == 'forged'),
            'stale': sum(1 for v in verdicts if v['verdict'] == 'stale'),
            'decisions': verdicts}
