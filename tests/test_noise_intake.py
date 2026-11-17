import json
import sqlite3
from pathlib import Path

import pytest

from roadcase.adoption import adopt
from roadcase.assessment import assess
from roadcase.noise import record


FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures'


def test_night_noise_evidence_is_not_yet_a_work_permission(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIXTURE / 'road_normal.json')
    assert record(db, FIXTURE / 'night-noise.json') == 2
    assert record(db, FIXTURE / 'night-noise.json') == 0
    con = sqlite3.connect(db)
    assert con.execute('SELECT count(*) FROM noise_receptor').fetchone()[0] == 1
    assert con.execute('SELECT count(*) FROM noise_activity').fetchone()[0] == 2
    con.close()
    assert assess(db, FIXTURE / 'route_night.json')['outcome'] == 'detour_required'


def test_noise_session_identity_conflict_keeps_original(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIXTURE / 'road_normal.json')
    record(db, FIXTURE / 'night-noise.json')
    changed = json.loads((FIXTURE / 'night-noise.json').read_text(encoding='utf-8'))
    changed['activities'][0]['level_db'] = '49.00'
    other = tmp_path / 'changed.json'
    other.write_text(json.dumps(changed), encoding='utf-8')
    with pytest.raises(ValueError, match='noise_session_conflict'):
        record(db, other)
    con = sqlite3.connect(db)
    assert con.execute('SELECT count(*) FROM noise_source').fetchone()[0] == 1
    con.close()
