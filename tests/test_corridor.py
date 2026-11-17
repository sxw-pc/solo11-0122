from pathlib import Path

import pytest

from roadcase import corridor
from roadcase.store import connect


def test_register_corridor_contract(tmp_path):
    fixture = Path(__file__).resolve().parents[1]/'fixtures'/'corridor.json'
    db = tmp_path/'ledger.db'
    result = corridor.register(db, 'detour-A', fixture)
    assert result['edge_count'] == 4
    assert corridor.register(db, 'detour-A', fixture) == result
    with connect(db) as con:
        assert con.execute('SELECT count(*) FROM corridor_contract').fetchone()[0] == 1


def test_corridor_identity_is_immutable(tmp_path):
    fixture = Path(__file__).resolve().parents[1]/'fixtures'/'corridor.json'
    db = tmp_path/'ledger.db'
    corridor.register(db, 'detour-A', fixture)
    altered = tmp_path/'altered.json'
    altered.write_text(fixture.read_text().replace('"capacity":3','"capacity":4',1))
    with pytest.raises(ValueError, match='corridor_identity_conflict'):
        corridor.register(db, 'detour-A', altered)
