"""Intake for synthetic night-work acoustic evidence and mitigation options.

No combined sound level or permitted construction window is decided here.
"""

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .source import utc
from .store import connect


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
