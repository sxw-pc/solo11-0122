"""Freeze named physical lanes and the public authorities that may govern them."""
from .source import canonical, digest, read, utc
from .store import connect


def register(db_path, registry_id, input_path):
    if not registry_id:
        raise ValueError('empty_registry_id')
    doc = read(input_path)
    lanes = doc.get('lanes')
    authorities = doc.get('authorities')
    if not isinstance(lanes, list) or not lanes or len(set(lanes)) != len(lanes):
        raise ValueError('bad_lane_map')
    if not isinstance(authorities, list) or not authorities:
        raise ValueError('bad_authority_registry')
    seen = set()
    for item in authorities:
        if (not item.get('id') or item['id'] in seen or
                not item.get('scope') or not set(item['scope']).issubset(lanes) or
                utc(item['from_at']) >= utc(item['until_at'])):
            raise ValueError('bad_authority_scope')
        seen.add(item['id'])
    body = canonical(doc)
    fingerprint = digest(doc)
    db = connect(db_path)
    db.execute('BEGIN IMMEDIATE')
    try:
        old = db.execute('SELECT body FROM lane_registry WHERE id=?',
                         (registry_id,)).fetchone()
        if old and old['body'] != body:
            raise ValueError('lane_registry_conflict')
        db.execute('INSERT OR IGNORE INTO lane_registry VALUES (?,?,?)',
                   (registry_id, fingerprint, body))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {'registry_id':registry_id,'digest':fingerprint,'lane_count':len(lanes)}
