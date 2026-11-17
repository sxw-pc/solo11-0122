import json
import sqlite3
from pathlib import Path

import pytest

from roadcase.adoption import adopt
from roadcase.utilities import record


FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures'


def test_utility_survey_intake_does_not_decide_excavation(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIXTURE / 'road_normal.json')
    assert record(db, FIXTURE / 'utility-plan.json') == 2
    assert record(db, FIXTURE / 'utility-plan.json') == 0
    con = sqlite3.connect(db)
    assert con.execute('SELECT count(*) FROM excavation_alignment').fetchone()[0] == 2
    assert con.execute('SELECT count(*) FROM utility_observation').fetchone()[0] == 2
    assert con.execute('SELECT count(*) FROM excavation_method').fetchone()[0] == 2
    con.close()


def test_utility_plan_conflict_does_not_change_original(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIXTURE / 'road_normal.json')
    record(db, FIXTURE / 'utility-plan.json')
    changed = json.loads((FIXTURE / 'utility-plan.json').read_text(encoding='utf-8'))
    changed['utilities'][0]['horizontal_uncertainty'] = 0.05
    other = tmp_path / 'other.json'
    other.write_text(json.dumps(changed), encoding='utf-8')
    with pytest.raises(ValueError, match='utility_plan_conflict'):
        record(db, other)
    con = sqlite3.connect(db)
    assert con.execute('SELECT horizontal_uncertainty FROM utility_observation WHERE utility_id=?',
                       ('gas-U1',)).fetchone()[0] == 0.4
    con.close()
