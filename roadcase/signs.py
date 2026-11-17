"""Intake for synthetic temporary road-sign geometry and sight obstacles.

This module does not determine whether an approaching lane has enough readable
distance before its detour decision point.
"""

import hashlib
import json
import math
from pathlib import Path

from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS sign_survey_source (
 survey_id TEXT PRIMARY KEY, work_source_id TEXT NOT NULL, work_revision INTEGER NOT NULL,
 digest TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS sign_approach (
 survey_id TEXT NOT NULL REFERENCES sign_survey_source(survey_id),
 lane_id TEXT NOT NULL, polyline TEXT NOT NULL, decision_point TEXT NOT NULL,
 eye_height REAL NOT NULL, PRIMARY KEY(survey_id,lane_id));
CREATE TABLE IF NOT EXISTS temporary_sign (
 survey_id TEXT NOT NULL REFERENCES sign_survey_source(survey_id),
 sign_id TEXT NOT NULL, position TEXT NOT NULL, facing TEXT NOT NULL,
 height REAL NOT NULL, class_rules TEXT NOT NULL,
 PRIMARY KEY(survey_id,sign_id));
CREATE TABLE IF NOT EXISTS sign_obstacle (
 survey_id TEXT NOT NULL REFERENCES sign_survey_source(survey_id),
 obstacle_id TEXT NOT NULL, box TEXT NOT NULL, height REAL NOT NULL,
 PRIMARY KEY(survey_id,obstacle_id));
"""


def _number(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)):
        raise ValueError('sign_bad_number')
    result=float(value)
    if not math.isfinite(result):
        raise ValueError('sign_bad_number')
    return result


def _point(value):
    if not isinstance(value,list) or len(value)!=2:
        raise ValueError('sign_bad_point')
    _number(value[0]);_number(value[1])


def record(db_path,input_path):
    raw=Path(input_path).read_bytes()
    doc=json.loads(raw)
    survey_id=doc['survey_id']
    work=doc['work']
    approaches=doc['approaches']
    signs=doc['signs']
    obstacles=doc['obstacles']
    if not survey_id or not approaches or not signs:
        raise ValueError('sign_empty_survey')
    for lane in approaches:
        if len(lane['polyline'])<2:
            raise ValueError('sign_bad_polyline')
        for point in lane['polyline']:_point(point)
        _point(lane['decision_point'])
        if _number(lane['eye_height'])<=0:raise ValueError('sign_bad_eye_height')
    for sign in signs:
        _point(sign['position']);_point(sign['facing'])
        if _number(sign['height'])<=0 or not sign['classes']:
            raise ValueError('sign_bad_height_or_class')
        for spec in sign['classes']:
            if _number(spec['min_warning_distance'])<=0:
                raise ValueError('sign_bad_warning_distance')
    for obstacle in obstacles:
        box=obstacle['box']
        if len(box)!=4 or _number(box[0])>=_number(box[2]) or _number(box[1])>=_number(box[3]):
            raise ValueError('sign_bad_obstacle')
        if _number(obstacle['height'])<0:raise ValueError('sign_bad_obstacle')
    digest=hashlib.sha256(raw).hexdigest()
    db=connect(db_path)
    try:
        db.executescript(SCHEMA)
        if not db.execute('SELECT 1 FROM work_revision WHERE source_id=? AND revision=?',
                          (work['source_id'],work['revision'])).fetchone():
            raise ValueError('sign_unknown_work_revision')
        prior=db.execute('SELECT digest FROM sign_survey_source WHERE survey_id=?',
                         (survey_id,)).fetchone()
        if prior:
            if prior['digest']!=digest:raise ValueError('sign_survey_conflict')
            return 0
        with db:
            db.execute('INSERT INTO sign_survey_source VALUES (?,?,?,?,?)',
                       (survey_id,work['source_id'],work['revision'],digest,raw))
            for lane in approaches:
                db.execute('INSERT INTO sign_approach VALUES (?,?,?,?,?)',
                           (survey_id,lane['id'],json.dumps(lane['polyline']),
                            json.dumps(lane['decision_point']),lane['eye_height']))
            for sign in signs:
                db.execute('INSERT INTO temporary_sign VALUES (?,?,?,?,?,?)',
                           (survey_id,sign['id'],json.dumps(sign['position']),
                            json.dumps(sign['facing']),sign['height'],
                            json.dumps(sign['classes'],sort_keys=True)))
            for obstacle in obstacles:
                db.execute('INSERT INTO sign_obstacle VALUES (?,?,?,?)',
                           (survey_id,obstacle['id'],json.dumps(obstacle['box']),
                            obstacle['height']))
        return len(approaches)
    finally:
        db.close()
