"""Finite detour capacity reservations for competing journeys."""
from .store import connect
from .policy import TABLES

SCHEMA="""
CREATE TABLE IF NOT EXISTS detour_pool(
 id TEXT PRIMARY KEY, segment TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity>0));
CREATE TABLE IF NOT EXISTS detour_hold(
 id TEXT PRIMARY KEY, pool_id TEXT NOT NULL REFERENCES detour_pool(id),
 source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
 journey_id TEXT NOT NULL, flow INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('reserved','waiting','released','stale')),
 UNIQUE(pool_id,journey_id,source_revision));
"""

def configure(db_path,pool_id,segment,capacity):
    if capacity<1:raise ValueError('invalid_detour_capacity')
    db=connect(db_path);db.executescript(SCHEMA)
    old=db.execute('SELECT * FROM detour_pool WHERE id=?',(pool_id,)).fetchone()
    if old and (old['segment'],old['capacity'])!=(segment,capacity):
        raise ValueError('detour_pool_conflict')
    with db:db.execute('INSERT OR IGNORE INTO detour_pool VALUES (?,?,?)',(pool_id,segment,capacity))
    db.close();return {'pool':pool_id,'capacity':capacity}

def reserve(db_path,hold_id,pool_id,source_id,journey_id,flow):
    if flow<1:raise ValueError('invalid_detour_flow')
    db=connect(db_path);db.executescript(SCHEMA);db.execute('BEGIN IMMEDIATE')
    try:
        t=TABLES
        pool=db.execute('SELECT * FROM detour_pool WHERE id=?',(pool_id,)).fetchone()
        notice=db.execute(f'SELECT * FROM {t["notice"]} WHERE id=?',(source_id,)).fetchone()
        if not pool or not notice or notice['state']!='active':raise ValueError('active_work_and_detour_required')
        import json
        revision=db.execute(f'SELECT payload FROM {t["revision"]} WHERE source_id=? AND revision=?',
                            (source_id,notice['current_revision'])).fetchone()
        if json.loads(revision['payload'])['segment']!=pool['segment']:
            raise ValueError('detour_segment_mismatch')
        if flow>pool['capacity']:raise ValueError('detour_flow_exceeds_pool')
        old=db.execute('SELECT * FROM detour_hold WHERE id=?',(hold_id,)).fetchone()
        if old:
            if (old['pool_id'],old['source_id'],old['journey_id'],old['flow'])!=(pool_id,source_id,journey_id,flow):
                raise ValueError('detour_hold_conflict')
            state=old['state']
        else:
            used=db.execute("SELECT COALESCE(sum(flow),0) FROM detour_hold WHERE pool_id=? AND state='reserved'",(pool_id,)).fetchone()[0]
            state='reserved' if used+flow<=pool['capacity'] else 'waiting'
            db.execute('INSERT INTO detour_hold VALUES (?,?,?,?,?,?,?)',
                       (hold_id,pool_id,source_id,notice['current_revision'],journey_id,flow,state))
            _stale_journey(db,journey_id)
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return {'hold':hold_id,'state':state,'journey':journey_id}

def release(db_path,hold_id):
    db=connect(db_path);db.executescript(SCHEMA);db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT * FROM detour_hold WHERE id=?',(hold_id,)).fetchone()
        if not row or row['state']!='reserved':raise ValueError('detour_hold_not_reserved')
        db.execute("UPDATE detour_hold SET state='released' WHERE id=?",(hold_id,))
        capacity=db.execute('SELECT capacity FROM detour_pool WHERE id=?',(row['pool_id'],)).fetchone()[0]
        used=db.execute("SELECT COALESCE(sum(flow),0) FROM detour_hold WHERE pool_id=? AND state='reserved'",(row['pool_id'],)).fetchone()[0]
        promoted=[]
        for candidate in db.execute("SELECT * FROM detour_hold WHERE pool_id=? AND state='waiting' ORDER BY id",(row['pool_id'],)):
            if used+candidate['flow']<=capacity:
                db.execute("UPDATE detour_hold SET state='reserved' WHERE id=?",(candidate['id'],))
                used+=candidate['flow'];promoted.append(candidate['journey_id'])
                _stale_journey(db,candidate['journey_id'])
        _stale_journey(db,row['journey_id'])
        db.commit()
    except Exception:
        db.rollback();raise
    finally:db.close()
    return {'hold':hold_id,'state':'released','promoted':promoted}

def _stale_journey(db,journey_id):
    t=TABLES
    db.execute(f'UPDATE {t["assessment"]} SET stale=1 WHERE case_id=?',(journey_id,))
    db.execute(f"UPDATE {t['obligation']} SET state='stale' WHERE assessment_id IN "
               f"(SELECT id FROM {t['assessment']} WHERE case_id=?)",(journey_id,))

def reserved_flow(db,pool_id,source_id,revision,journey_id):
    row=db.execute("SELECT flow FROM detour_hold WHERE pool_id=? AND source_id=? AND source_revision=? AND journey_id=? AND state='reserved'",
                   (pool_id,source_id,revision,journey_id)).fetchone()
    return row['flow'] if row else 0
