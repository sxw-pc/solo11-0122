from pathlib import Path
import pytest
from roadcase.adoption import adopt
from roadcase.spoil import open_lot, record_ticket, summary

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def test_spoil_ticket_intake(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    first = open_lot(db, FIX / 'spoil-lot.json')
    assert open_lot(db, FIX / 'spoil-lot.json') == first
    load = record_ticket(db, FIX / 'spoil-load.json')
    assert load['net_kg'] == 10000
    assert record_ticket(db, FIX / 'spoil-load.json') == load
    record_ticket(db, FIX / 'spoil-accept.json')
    assert summary(db, 'lot-R1-01')['ticket_count'] == 2
    with pytest.raises(ValueError, match='active_work_revision_required'):
        open_lot(tmp_path / 'other.db', FIX / 'spoil-lot.json')
