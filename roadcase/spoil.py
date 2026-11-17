"""Freeze excavation material lots and their weighed transfer observations."""
import json
from .source import read, digest, canonical, utc
from .store import connect
from .policy import TABLES

SCHEMA = """CREATE TABLE IF NOT EXISTS spoil_lot(
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
 declared_wet_kg INTEGER NOT NULL, approved_destinations TEXT NOT NULL,
 raw TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spoil_ticket(
 id TEXT PRIMARY KEY, lot_id TEXT NOT NULL, at TEXT NOT NULL,
 from_node TEXT NOT NULL, to_node TEXT NOT NULL, net_kg INTEGER NOT NULL,
 dry_fraction REAL NOT NULL, uncertainty_kg INTEGER NOT NULL,
 parents TEXT NOT NULL, raw TEXT NOT NULL, digest TEXT NOT NULL);
"""


def open_lot(db_path, input_path):
    doc = read(input_path)
    if (not doc.get('id') or not doc.get('source_id') or
            not isinstance(doc.get('source_revision'), int) or
            not isinstance(doc.get('declared_wet_kg'), int) or doc['declared_wet_kg'] <= 0 or
            not isinstance(doc.get('approved_destinations'), list) or
            not doc['approved_destinations']):
        raise ValueError('invalid_spoil_lot')
    fingerprint = digest(doc)
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        notice = db.execute(f"SELECT current_revision,state FROM {TABLES['notice']} WHERE id=?",
                            (doc['source_id'],)).fetchone()
        if not notice or notice['state'] != 'active' or notice['current_revision'] != doc['source_revision']:
            raise ValueError('active_work_revision_required')
        with db:
            old = db.execute('SELECT digest FROM spoil_lot WHERE id=?', (doc['id'],)).fetchone()
            if old and old['digest'] != fingerprint:
                raise ValueError('spoil_lot_conflict')
            db.execute('INSERT OR IGNORE INTO spoil_lot VALUES (?,?,?,?,?,?,?)',
                       (doc['id'], doc['source_id'], doc['source_revision'],
                        doc['declared_wet_kg'], canonical(doc['approved_destinations']),
                        canonical(doc), fingerprint))
        return {'id': doc['id'], 'digest': fingerprint}
    finally:
        db.close()


def record_ticket(db_path, input_path):
    doc = read(input_path)
    for key in ('id', 'lot_id', 'at', 'from_node', 'to_node', 'gross_kg', 'tare_kg',
                'dry_fraction', 'uncertainty_kg', 'parents'):
        if key not in doc:
            raise ValueError('ticket_missing_' + key)
    utc(doc['at'])
    gross, tare = doc['gross_kg'], doc['tare_kg']
    if (not doc['id'] or not doc['lot_id'] or not doc['from_node'] or not doc['to_node'] or
            doc['from_node'] == doc['to_node'] or
            not isinstance(gross, int) or not isinstance(tare, int) or gross <= tare or tare < 0 or
            not 0 < doc['dry_fraction'] <= 1 or
            not isinstance(doc['uncertainty_kg'], int) or doc['uncertainty_kg'] < 0 or
            not isinstance(doc['parents'], list) or len(set(doc['parents'])) != len(doc['parents'])):
        raise ValueError('invalid_spoil_ticket')
    fingerprint = digest(doc)
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        if not db.execute('SELECT 1 FROM spoil_lot WHERE id=?', (doc['lot_id'],)).fetchone():
            raise ValueError('unknown_spoil_lot')
        with db:
            old = db.execute('SELECT digest FROM spoil_ticket WHERE id=?', (doc['id'],)).fetchone()
            if old and old['digest'] != fingerprint:
                raise ValueError('spoil_ticket_conflict')
            db.execute('INSERT OR IGNORE INTO spoil_ticket VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (doc['id'], doc['lot_id'], utc(doc['at']).isoformat(), doc['from_node'],
                        doc['to_node'], gross-tare, doc['dry_fraction'], doc['uncertainty_kg'],
                        canonical(doc['parents']), canonical(doc), fingerprint))
        return {'id': doc['id'], 'net_kg': gross-tare, 'digest': fingerprint}
    finally:
        db.close()


def summary(db_path, lot_id):
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        if not db.execute('SELECT 1 FROM spoil_lot WHERE id=?', (lot_id,)).fetchone():
            raise ValueError('unknown_spoil_lot')
        rows = [dict(x) for x in db.execute('''SELECT id,from_node,to_node,net_kg,dry_fraction,
            uncertainty_kg,parents FROM spoil_ticket WHERE lot_id=? ORDER BY id''', (lot_id,))]
        return {'lot_id': lot_id, 'tickets': rows, 'ticket_count': len(rows)}
    finally:
        db.close()
