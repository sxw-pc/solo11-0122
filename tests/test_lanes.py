from pathlib import Path

import pytest

from roadcase import lanes
from roadcase.store import connect


def test_register_lane_and_authority_registry(tmp_path):
    fixture = Path(__file__).resolve().parents[1]/'fixtures'/'lane-authorities.json'
    db = tmp_path/'ledger.db'
    result = lanes.register(db, 'road-R1', fixture)
    assert result['lane_count'] == 4
    assert lanes.register(db, 'road-R1', fixture) == result
    with connect(db) as con:
        assert con.execute('SELECT count(*) FROM lane_registry').fetchone()[0] == 1


def test_registry_rejects_scope_and_identity_change(tmp_path):
    fixture = Path(__file__).resolve().parents[1]/'fixtures'/'lane-authorities.json'
    db = tmp_path/'ledger.db'
    lanes.register(db, 'road-R1', fixture)
    changed = tmp_path/'changed.json'
    changed.write_text(fixture.read_text().replace('"scope":["L2","L3"]','"scope":["L2","L5"]'))
    with pytest.raises(ValueError, match='bad_authority_scope'):
        lanes.register(db, 'road-R2', changed)
    changed.write_text(fixture.read_text().replace('"scope":["L2","L3"]','"scope":["L2"]'))
    with pytest.raises(ValueError, match='lane_registry_conflict'):
        lanes.register(db, 'road-R1', changed)
