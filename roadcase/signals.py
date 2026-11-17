"""Intake for synthetic temporary crossing signal and manual phase evaluation.

Service capacities already account for a fixed all-red clearance within each
slot. A phase change at a slot boundary has no additional capacity penalty.
This module does not find an optimal phase sequence.
"""

import hashlib
import json
from pathlib import Path

from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_site (
 site_id TEXT PRIMARY KEY, work_source_id TEXT NOT NULL,
 work_revision INTEGER NOT NULL, digest TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS signal_manual_run (
 run_id TEXT PRIMARY KEY, site_id TEXT NOT NULL REFERENCES signal_site(site_id),
 digest TEXT NOT NULL, raw BLOB NOT NULL, result TEXT NOT NULL);
"""


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('signal_bad_count')
    return value


def register(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw)
    if not doc['site_id'] or not doc['slots']:
        raise ValueError('signal_empty_site')
    _count(doc['vehicle_service_per_v_slot'])
    _count(doc['ped_service_per_p_slot'])
    _count(doc['max_vehicle_queue'])
    _count(doc['max_ped_queue'])
    for slot in doc['slots']:
        _count(slot['vehicle_arrivals'])
        _count(slot['ped_arrivals'])
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        work = doc['work']
        rev = db.execute(
            'SELECT state,payload FROM work_revision WHERE source_id=? AND revision=?',
            (work['source_id'], work['revision'])).fetchone()
        if not rev or rev['state'] != 'active':
            raise ValueError('signal_work_not_active')
        if json.loads(rev['payload'])['segment'] != work['segment']:
            raise ValueError('signal_wrong_segment')
        prior = db.execute('SELECT digest FROM signal_site WHERE site_id=?',
                           (doc['site_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('signal_site_conflict')
            return doc['site_id']
        with db:
            db.execute('INSERT INTO signal_site VALUES (?,?,?,?,?)',
                       (doc['site_id'], work['source_id'], work['revision'], digest, raw))
        return doc['site_id']
    finally:
        db.close()


def record_manual(db_path, input_path):
    raw = Path(input_path).read_bytes()
    run = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        prior = db.execute('SELECT digest,result FROM signal_manual_run WHERE run_id=?',
                           (run['run_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('signal_run_conflict')
            return json.loads(prior['result'])
        row = db.execute('SELECT raw FROM signal_site WHERE site_id=?',
                         (run['site_id'],)).fetchone()
        if not row:
            raise ValueError('signal_unknown_site')
        site = json.loads(row['raw'])
        if len(run['phases']) != len(site['slots']) or any(x not in ('V','P') for x in run['phases']):
            raise ValueError('signal_bad_phases')
        vehicle = 0
        ped = []
        vehicle_delay = max_ped_wait = switches = 0
        levels = []
        prior_phase = None
        feasible = True
        reasons = []
        for i, (slot, phase) in enumerate(zip(site['slots'], run['phases'])):
            vehicle += slot['vehicle_arrivals']
            ped.extend([i] * slot['ped_arrivals'])
            if phase == 'V':
                vehicle = max(0, vehicle - site['vehicle_service_per_v_slot'])
            else:
                for _ in range(min(len(ped), site['ped_service_per_p_slot'])):
                    arrival = ped.pop(0)
                    max_ped_wait = max(max_ped_wait, i - arrival)
            if prior_phase is not None and phase != prior_phase:
                switches += 1
            prior_phase = phase
            vehicle_delay += vehicle
            if vehicle > site['max_vehicle_queue'] or len(ped) > site['max_ped_queue']:
                feasible = False
                reasons.append(slot['id'] + ':queue_storage')
            levels.append({'slot': slot['id'], 'phase': phase,
                           'vehicle_queue': vehicle, 'ped_queue': len(ped)})
        if vehicle or ped:
            feasible = False
            reasons.append('terminal_backlog')
        result = {'site_id': run['site_id'], 'run_id': run['run_id'],
                  'feasible': feasible, 'vehicle_delay': vehicle_delay,
                  'max_ped_wait': max_ped_wait, 'phase_switches': switches,
                  'levels': levels, 'reasons': reasons}
        with db:
            db.execute('INSERT INTO signal_manual_run VALUES (?,?,?,?,?)',
                       (run['run_id'], run['site_id'], digest, raw,
                        json.dumps(result, sort_keys=True)))
        return result
    finally:
        db.close()
