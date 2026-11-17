"""Freeze a local detour corridor and its time-window capacity evidence."""
from .source import canonical, digest, read, utc
from .store import connect


def register(db_path, corridor_id, input_path):
    if not corridor_id:
        raise ValueError('empty_corridor_id')
    doc = read(input_path)
    nodes = doc.get('nodes')
    edges = doc.get('edges')
    if not isinstance(nodes, list) or len(nodes) < 2 or len(set(nodes)) != len(nodes):
        raise ValueError('bad_corridor_nodes')
    if not isinstance(edges, list) or not edges:
        raise ValueError('bad_corridor_edges')
    keys = set()
    for edge in edges:
        key = (edge['id'], edge['from'], edge['to'])
        if (edge['from'] not in nodes or edge['to'] not in nodes or
                edge['from'] == edge['to'] or edge['capacity'] <= 0 or
                edge['minutes'] <= 0 or utc(edge['from_at']) >= utc(edge['until_at']) or
                key in keys):
            raise ValueError('bad_corridor_edge')
        keys.add(key)
    payload = canonical(doc)
    fingerprint = digest(doc)
    db = connect(db_path)
    db.execute('BEGIN IMMEDIATE')
    try:
        old = db.execute('SELECT payload FROM corridor_contract WHERE id=?',
                         (corridor_id,)).fetchone()
        if old and old['payload'] != payload:
            raise ValueError('corridor_identity_conflict')
        db.execute('INSERT OR IGNORE INTO corridor_contract(id,digest,payload) VALUES (?,?,?)',
                   (corridor_id, fingerprint, payload))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {'corridor_id': corridor_id, 'digest': fingerprint, 'edge_count': len(edges)}
