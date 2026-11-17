from pathlib import Path
import pytest
from roadcase.adoption import adopt
from roadcase.crew import register_shift, record_scan, summary

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def test_crew_shift_and_scan_intake(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    first = register_shift(db, FIX / 'crew-shift.json')
    assert register_shift(db, FIX / 'crew-shift.json') == first
    scan = record_scan(db, FIX / 'crew-scan-in.json')
    assert record_scan(db, FIX / 'crew-scan-in.json') == scan
    record_scan(db, FIX / 'crew-scan-out.json')
    for name in ('crew-scan-b-in.json', 'crew-scan-b-out.json',
                 'crew-scan-c-in.json', 'crew-scan-c-out.json'):
        record_scan(db, FIX / name)
    assert len(summary(db, 'shift-R1-01')['scans']) == 6
    with pytest.raises(ValueError, match='active_work_revision_required'):
        register_shift(tmp_path / 'other.db', FIX / 'crew-shift.json')
