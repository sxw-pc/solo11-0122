"""Freeze a work shift and locally collected access-reader events."""
from .source import read, digest, canonical, utc
from .store import connect
from .policy import TABLES

SCHEMA = """CREATE TABLE IF NOT EXISTS crew_shift(
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
 raw TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS crew_scan(
 id TEXT PRIMARY KEY, shift_id TEXT NOT NULL, badge_id TEXT NOT NULL,
 direction TEXT NOT NULL, gate TEXT NOT NULL, at TEXT NOT NULL,
 error_seconds INTEGER NOT NULL, raw TEXT NOT NULL, digest TEXT NOT NULL);
"""


def register_shift(db_path, input_path):
    doc = read(input_path)
    for key in ('id', 'source_id', 'source_revision', 'shift_from', 'shift_until',
                'active_work', 'required_roles', 'badges'):
        if key not in doc:
            raise ValueError('shift_missing_' + key)
    if (not doc['id'] or not doc['source_id'] or
            not isinstance(doc['source_revision'], int) or
            utc(doc['shift_from']) >= utc(doc['shift_until']) or
            not isinstance(doc['active_work'], list) or
            not isinstance(doc['required_roles'], dict) or
            not isinstance(doc['badges'], list)):
        raise ValueError('invalid_crew_shift')
    for interval in doc['active_work']:
        if (not isinstance(interval, list) or len(interval) != 2 or
                utc(interval[0]) >= utc(interval[1]) or
                utc(interval[0]) < utc(doc['shift_from']) or
                utc(interval[1]) > utc(doc['shift_until'])):
            raise ValueError('invalid_active_work')
    fingerprint = digest(doc)
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        notice = db.execute(f"SELECT current_revision,state FROM {TABLES['notice']} WHERE id=?",
                            (doc['source_id'],)).fetchone()
        if not notice or notice['state'] != 'active' or notice['current_revision'] != doc['source_revision']:
            raise ValueError('active_work_revision_required')
        with db:
            old = db.execute('SELECT digest FROM crew_shift WHERE id=?', (doc['id'],)).fetchone()
            if old and old['digest'] != fingerprint:
                raise ValueError('crew_shift_conflict')
            db.execute('INSERT OR IGNORE INTO crew_shift VALUES (?,?,?,?,?)',
                       (doc['id'], doc['source_id'], doc['source_revision'], canonical(doc), fingerprint))
        return {'id': doc['id'], 'digest': fingerprint}
    finally:
        db.close()


def record_scan(db_path, input_path):
    doc = read(input_path)
    for key in ('id', 'shift_id', 'badge_id', 'direction', 'gate', 'at', 'error_seconds'):
        if key not in doc:
            raise ValueError('scan_missing_' + key)
    if (not doc['id'] or not doc['shift_id'] or not doc['badge_id'] or not doc['gate'] or
            doc['direction'] not in ('in', 'out') or
            not isinstance(doc['error_seconds'], int) or doc['error_seconds'] < 0):
        raise ValueError('invalid_crew_scan')
    at = utc(doc['at']).isoformat()
    fingerprint = digest(doc)
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        if not db.execute('SELECT 1 FROM crew_shift WHERE id=?', (doc['shift_id'],)).fetchone():
            raise ValueError('unknown_crew_shift')
        with db:
            old = db.execute('SELECT digest FROM crew_scan WHERE id=?', (doc['id'],)).fetchone()
            if old and old['digest'] != fingerprint:
                raise ValueError('crew_scan_conflict')
            db.execute('INSERT OR IGNORE INTO crew_scan VALUES (?,?,?,?,?,?,?,?,?)',
                       (doc['id'], doc['shift_id'], doc['badge_id'], doc['direction'],
                        doc['gate'], at, doc['error_seconds'], canonical(doc), fingerprint))
        return {'id': doc['id'], 'digest': fingerprint}
    finally:
        db.close()


def summary(db_path, shift_id):
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        row = db.execute('SELECT raw,digest FROM crew_shift WHERE id=?', (shift_id,)).fetchone()
        if not row:
            raise ValueError('unknown_crew_shift')
        scans = [dict(x) for x in db.execute('''SELECT id,badge_id,direction,gate,at,error_seconds
            FROM crew_scan WHERE shift_id=? ORDER BY at,id''', (shift_id,))]
        return {'shift_id': shift_id, 'shift_digest': row['digest'], 'scans': scans}
    finally:
        db.close()
