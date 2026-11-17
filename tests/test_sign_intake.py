import json
import sqlite3
from pathlib import Path

import pytest

from roadcase.adoption import adopt
from roadcase.signs import record


FIXTURE=Path(__file__).resolve().parent.parent/'fixtures'


def test_sign_geometry_intake_keeps_lane_and_obstacle_evidence(tmp_path):
    db=tmp_path/'state.db'
    adopt(db,FIXTURE/'road_normal.json')
    assert record(db,FIXTURE/'sign-survey.json')==2
    assert record(db,FIXTURE/'sign-survey.json')==0
    con=sqlite3.connect(db)
    assert con.execute('SELECT count(*) FROM sign_approach').fetchone()[0]==2
    assert con.execute('SELECT count(*) FROM temporary_sign').fetchone()[0]==1
    assert con.execute('SELECT count(*) FROM sign_obstacle').fetchone()[0]==2
    con.close()


def test_sign_survey_conflict_keeps_original(tmp_path):
    db=tmp_path/'state.db'
    adopt(db,FIXTURE/'road_normal.json')
    record(db,FIXTURE/'sign-survey.json')
    changed=json.loads((FIXTURE/'sign-survey.json').read_text(encoding='utf-8'))
    changed['obstacles'][0]['height']=1.0
    other=tmp_path/'other.json'
    other.write_text(json.dumps(changed),encoding='utf-8')
    with pytest.raises(ValueError,match='sign_survey_conflict'):
        record(db,other)
    con=sqlite3.connect(db)
    assert con.execute('SELECT count(*) FROM sign_survey_source').fetchone()[0]==1
    con.close()
