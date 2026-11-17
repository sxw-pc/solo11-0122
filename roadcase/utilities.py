"""Intake for synthetic subsurface survey and excavation method evidence.

Recording a survey does not certify geometric clearance or permit digging.
"""

import hashlib
import json
import math
from pathlib import Path

from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS utility_plan_source (
 plan_id TEXT PRIMARY KEY, work_source_id TEXT NOT NULL, work_revision INTEGER NOT NULL,
 segment TEXT NOT NULL, digest TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS excavation_alignment (
 plan_id TEXT NOT NULL REFERENCES utility_plan_source(plan_id),
 alignment_id TEXT NOT NULL, polyline TEXT NOT NULL,
 depth_low REAL NOT NULL, depth_high REAL NOT NULL,
 PRIMARY KEY(plan_id,alignment_id));
CREATE TABLE IF NOT EXISTS utility_observation (
 plan_id TEXT NOT NULL REFERENCES utility_plan_source(plan_id),
 utility_id TEXT NOT NULL, polyline TEXT NOT NULL,
 depth_low REAL NOT NULL, depth_high REAL NOT NULL,
 outer_radius REAL NOT NULL, horizontal_uncertainty REAL NOT NULL,
 vertical_uncertainty REAL NOT NULL,
 PRIMARY KEY(plan_id,utility_id));
CREATE TABLE IF NOT EXISTS excavation_method (
 plan_id TEXT NOT NULL REFERENCES utility_plan_source(plan_id),
 method_id TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(plan_id,method_id));
"""


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('utility_bad_number')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('utility_bad_number')
    return number


def _line(points):
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError('utility_bad_polyline')
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError('utility_bad_polyline')
        _number(point[0]); _number(point[1])


def record(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw)
    plan_id = doc['plan_id']
    work = doc['work']
    alignments = doc['alignments']
    utilities = doc['utilities']
    methods = doc['methods']
    if not plan_id or not alignments or not utilities or not methods:
        raise ValueError('utility_empty_plan')
    for alignment in alignments:
        _line(alignment['polyline'])
        if _number(alignment['depth_low']) >= _number(alignment['depth_high']):
            raise ValueError('utility_bad_depth')
    for utility in utilities:
        _line(utility['polyline'])
        if _number(utility['depth_low']) >= _number(utility['depth_high']):
            raise ValueError('utility_bad_depth')
        for key in ('outer_radius','horizontal_uncertainty','vertical_uncertainty'):
            if _number(utility[key]) < 0:
                raise ValueError('utility_bad_uncertainty')
    for method in methods:
        for key in ('trench_width','horizontal_error','vertical_error','required_clearance'):
            if _number(method[key]) < 0:
                raise ValueError('utility_bad_method')
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        revision = db.execute('SELECT payload FROM work_revision WHERE source_id=? AND revision=?',
                              (work['source_id'], work['revision'])).fetchone()
        if not revision:
            raise ValueError('utility_unknown_work_revision')
        if json.loads(revision['payload']).get('segment') != work['segment']:
            raise ValueError('utility_work_segment_mismatch')
        prior = db.execute('SELECT digest FROM utility_plan_source WHERE plan_id=?',
                           (plan_id,)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('utility_plan_conflict')
            return 0
        with db:
            db.execute('INSERT INTO utility_plan_source VALUES (?,?,?,?,?,?)',
                       (plan_id, work['source_id'], work['revision'], work['segment'],
                        digest, raw))
            for alignment in alignments:
                db.execute('INSERT INTO excavation_alignment VALUES (?,?,?,?,?)',
                           (plan_id, alignment['id'], json.dumps(alignment['polyline']),
                            alignment['depth_low'], alignment['depth_high']))
            for utility in utilities:
                db.execute('INSERT INTO utility_observation VALUES (?,?,?,?,?,?,?,?)',
                           (plan_id, utility['id'], json.dumps(utility['polyline']),
                            utility['depth_low'], utility['depth_high'],
                            utility['outer_radius'], utility['horizontal_uncertainty'],
                            utility['vertical_uncertainty']))
            for method in methods:
                db.execute('INSERT INTO excavation_method VALUES (?,?,?)',
                           (plan_id, method['id'], json.dumps(method,sort_keys=True)))
        return len(utilities)
    finally:
        db.close()
