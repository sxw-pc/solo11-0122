import json
from pathlib import Path

import pytest

from roadcase.adoption import adopt
from roadcase.signals import record_manual, register


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def test_signal_manual_phase_evaluation_and_replay(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    assert register(db, FIX / 'signal-site.json') == 'crossing-R1'
    assert register(db, FIX / 'signal-site.json') == 'crossing-R1'
    result = record_manual(db, FIX / 'signal-manual.json')
    assert result == record_manual(db, FIX / 'signal-manual.json')
    assert result['feasible'] is True
    assert result['vehicle_delay'] == 0
    assert result['max_ped_wait'] == 3
    assert result['phase_switches'] == 1


def test_signal_bad_manual_run_is_not_approved(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    register(db, FIX / 'signal-site.json')
    run = {'run_id': 'bad-1', 'site_id': 'crossing-R1',
           'phases': ['P', 'P', 'P', 'P', 'P']}
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(run), encoding='utf-8')
    result = record_manual(db, path)
    assert result['feasible'] is False
    assert 'terminal_backlog' in result['reasons']
    run['phases'] = ['V', 'V', 'V', 'P', 'P']
    path.write_text(json.dumps(run), encoding='utf-8')
    with pytest.raises(ValueError, match='signal_run_conflict'):
        record_manual(db, path)
