import json
import subprocess
import sys
from pathlib import Path

import pytest

from roadcase.adoption import adopt
from roadcase.spoil import open_lot, record_ticket, seal, verify, result, summary
from roadcase.source import canonical
from roadcase.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    return path


def fixture_doc(name):
    return json.loads((FIX / name).read_text(encoding='utf-8'))


def seal_doc(seal_id, lot='lot-R1-01', contract='as_weighed', balances=None):
    doc = {'id': seal_id, 'lot_id': lot, 'contract': contract}
    if balances is not None:
        doc['balances'] = balances
    return doc


def base_lot(db):
    adopt(db, FIX / 'road_normal.json')
    open_lot(db, FIX / 'spoil-lot.json')
    record_ticket(db, FIX / 'spoil-load.json')
    record_ticket(db, FIX / 'spoil-accept.json')


def split_lot(db):
    adopt(db, FIX / 'road_normal.json')
    open_lot(db, FIX / 'spoil-split-lot.json')
    for i in range(1, 7):
        record_ticket(db, FIX / f'spoil-split-{i}.json')


def test_two_contracts_on_same_records(tmp_path):
    db = tmp_path / 'state.db'
    base_lot(db)
    seal(db, write(tmp_path, 's1.json', seal_doc('seal-wet', contract='as_weighed')))
    wet = verify(db, 'seal-wet')
    assert wet['verdict'] == 'blocked'
    assert any(v['kind'] == 'node_imbalance' and v['node'] == 'STOCK-A'
               for v in wet['detail']['violations'])
    assert wet['detail']['assignment'] is None

    seal(db, write(tmp_path, 's2.json', seal_doc('seal-dry', contract='dry_solids')))
    dry = verify(db, 'seal-dry')
    assert dry['verdict'] == 'consistent_with_records'
    assigned = dry['detail']['assignment']['tickets']
    assert assigned['load-1'] == {'wet_kg': 10000, 'contract_kg': 8000}
    assert assigned['accept-1'] == {'wet_kg': 8000, 'contract_kg': 8000}

    # both verdicts persist with their own mass basis, assignment and evidence
    kept_wet, kept_dry = result(db, 'seal-wet'), result(db, 'seal-dry')
    assert (kept_wet['verdict'], kept_wet['detail']['contract']) == ('blocked', 'as_weighed')
    assert (kept_dry['verdict'], kept_dry['detail']['contract']) == (
        'consistent_with_records', 'dry_solids')
    tickets = {t['id']: t for t in kept_dry['detail']['tickets']}
    assert tickets['load-1']['wet_interval_kg'] == [9900, 10100]
    assert tickets['load-1']['dry_interval_kg'] == [7920, 8080]
    assert tickets['accept-1']['dry_interval_kg'] == [7900, 8100]
    assert kept_dry['detail']['scope'] == 'ticket_records_only'
    assert '不构成' in kept_dry['detail']['assurance']
    # idempotent replay recomputes the identical verdict
    assert verify(db, 'seal-dry')['digest'] == dry['digest']


def test_frozen_balance_allows_as_weighed(tmp_path):
    db = tmp_path / 'state.db'
    base_lot(db)
    balance = [{'node': 'STOCK-A', 'wet_kg': 2000, 'uncertainty_kg': 100, 'dry_fraction': 1.0}]
    seal(db, write(tmp_path, 's.json', seal_doc('seal-bal', balances=balance)))
    outcome = verify(db, 'seal-bal')
    assert outcome['verdict'] == 'consistent_with_records'
    assert 1900 <= outcome['detail']['assignment']['node_balance_kg']['STOCK-A'] <= 2100


def test_split_merge_network_consistent(tmp_path):
    db = tmp_path / 'state.db'
    split_lot(db)
    seal(db, write(tmp_path, 's.json', seal_doc('seal-sm', lot='lot-R1-02')))
    outcome = verify(db, 'seal-sm')
    assert outcome['verdict'] == 'consistent_with_records'
    assert outcome['detail']['ticket_count'] == 6
    nodes = {n['node']: n for n in outcome['detail']['nodes']}
    assert nodes['PIT-R1']['role'] == 'source'
    assert nodes['YARD-B']['role'] == 'terminal'
    assert nodes['STOCK-F']['in_interval_kg'] == [5900, 6100]
    tickets = {t['id']: t for t in outcome['detail']['tickets']}
    assert tickets['sm-6']['parents'] == ['sm-4', 'sm-5']
    edges = {(e['parent'], e['child']) for e in outcome['detail']['assignment']['edges']}
    assert ('sm-4', 'sm-6') in edges and ('sm-5', 'sm-6') in edges

    seal(db, write(tmp_path, 's2.json', seal_doc('seal-sm-dry', lot='lot-R1-02',
                                                 contract='dry_solids')))
    assert verify(db, 'seal-sm-dry')['verdict'] == 'consistent_with_records'


def test_shared_parent_spent_twice_is_blocked(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    open_lot(db, FIX / 'spoil-lot.json')
    record_ticket(db, FIX / 'spoil-load.json')
    base = fixture_doc('spoil-accept.json')
    fork1 = dict(base, id='fork-1', gross_kg=8000, at='2026-06-01T10:00:00Z')
    fork2 = dict(base, id='fork-2', gross_kg=8000, at='2026-06-01T11:00:00Z')
    record_ticket(db, write(tmp_path, 'f1.json', fork1))
    record_ticket(db, write(tmp_path, 'f2.json', fork2))
    seal(db, write(tmp_path, 's.json', seal_doc('seal-fork')))
    outcome = verify(db, 'seal-fork')
    assert outcome['verdict'] == 'blocked'
    overspent = [v for v in outcome['detail']['violations'] if v['kind'] == 'parent_overspent']
    assert overspent and overspent[0]['parent'] == 'load-1'
    assert overspent[0]['demanded_kg'] > overspent[0]['available_kg']


def test_local_intervals_connect_but_globally_infeasible(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    open_lot(db, FIX / 'spoil-lot.json')
    record_ticket(db, FIX / 'spoil-load.json')
    second_root = dict(fixture_doc('spoil-accept.json'), gross_kg=12000, parents=[])
    record_ticket(db, write(tmp_path, 'a.json', second_root))
    seal(db, write(tmp_path, 's.json', seal_doc('seal-two-roots')))
    outcome = verify(db, 'seal-two-roots')
    assert outcome['verdict'] == 'blocked'
    assert any(v['kind'] == 'declaration_exceeded'
               for v in outcome['detail']['violations'])
    # locally the two weighing intervals at STOCK-A do intersect
    node = {n['node']: n for n in outcome['detail']['nodes']}['STOCK-A']
    assert node['in_interval_kg'] == [9900, 10100]
    assert node['root_out_interval_kg'] == [9900, 10100]


def test_lineage_infeasible_when_nodes_look_balanced(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    lot = {'id': 'lot-R1-03', 'source_id': 'road-1', 'source_revision': 1,
           'declared_wet_kg': 20000, 'approved_destinations': ['YARD-B', 'YARD-X']}
    open_lot(db, write(tmp_path, 'lot.json', lot))

    def ticket(tid, to, gross, parents, at):
        return {'id': tid, 'lot_id': 'lot-R1-03', 'at': at, 'from_node': 'PIT-R1' if not parents
                else 'STOCK-A', 'to_node': to, 'gross_kg': gross, 'tare_kg': 2000,
                'dry_fraction': 1.0, 'uncertainty_kg': 100, 'parents': parents}

    record_ticket(db, write(tmp_path, 'p1.json', ticket('p1', 'STOCK-A', 12000, [],
                                                        '2026-06-03T08:00:00Z')))
    record_ticket(db, write(tmp_path, 'p2.json', ticket('p2', 'STOCK-A', 12000, [],
                                                        '2026-06-03T08:10:00Z')))
    record_ticket(db, write(tmp_path, 'c1.json', ticket('c1', 'YARD-B', 17000, ['p1'],
                                                        '2026-06-03T09:00:00Z')))
    record_ticket(db, write(tmp_path, 'c2.json', ticket('c2', 'YARD-X', 7000, ['p2'],
                                                        '2026-06-03T09:10:00Z')))
    seal(db, write(tmp_path, 's.json', seal_doc('seal-lineage', lot='lot-R1-03')))
    outcome = verify(db, 'seal-lineage')
    assert outcome['verdict'] == 'blocked'
    assert any(v['kind'] == 'parent_overspent' and v['parent'] == 'p1'
               for v in outcome['detail']['violations'])
    # node STOCK-A alone looks balanced: in [19800,20200], out [19800,20200]
    assert not any(v['kind'] == 'node_imbalance' and v['node'] == 'STOCK-A'
                   for v in outcome['detail']['violations'])


def test_wrong_destination_is_blocked(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    open_lot(db, FIX / 'spoil-lot.json')
    record_ticket(db, FIX / 'spoil-load.json')
    wrong = dict(fixture_doc('spoil-accept.json'), to_node='YARD-Z')
    record_ticket(db, write(tmp_path, 'a.json', wrong))
    seal(db, write(tmp_path, 's.json', seal_doc('seal-dest', contract='dry_solids')))
    outcome = verify(db, 'seal-dest')
    assert outcome['verdict'] == 'blocked'
    assert any(v['kind'] == 'node_imbalance' and v['node'] == 'YARD-Z'
               for v in outcome['detail']['violations'])


def test_missing_parent_is_indeterminate(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    open_lot(db, FIX / 'spoil-lot.json')
    record_ticket(db, FIX / 'spoil-load.json')
    ghost = dict(fixture_doc('spoil-accept.json'), parents=['ghost-9'])
    record_ticket(db, write(tmp_path, 'a.json', ghost))
    seal(db, write(tmp_path, 's.json', seal_doc('seal-ghost', contract='dry_solids')))
    outcome = verify(db, 'seal-ghost')
    assert outcome['verdict'] == 'indeterminate'
    assert outcome['detail']['gaps'] == [
        {'kind': 'missing_parent', 'ticket': 'accept-1', 'parent': 'ghost-9'}]


def test_unweighed_ticket_is_indeterminate(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    open_lot(db, FIX / 'spoil-lot.json')
    record_ticket(db, FIX / 'spoil-load.json')
    unweighed = dict(fixture_doc('spoil-accept.json'), gross_kg=None, tare_kg=None)
    saved = record_ticket(db, write(tmp_path, 'a.json', unweighed))
    assert saved['net_kg'] is None
    seal(db, write(tmp_path, 's.json', seal_doc('seal-unweighed')))
    outcome = verify(db, 'seal-unweighed')
    assert outcome['verdict'] == 'indeterminate'
    assert any(g['kind'] == 'unweighed' and g['ticket'] == 'accept-1'
               for g in outcome['detail']['gaps'])


def test_unknown_dry_fraction_is_not_treated_as_one(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    open_lot(db, FIX / 'spoil-lot.json')
    record_ticket(db, FIX / 'spoil-load.json')
    no_dry = dict(fixture_doc('spoil-accept.json'), dry_fraction=None)
    record_ticket(db, write(tmp_path, 'a.json', no_dry))
    seal(db, write(tmp_path, 's.json', seal_doc('seal-no-dry', contract='dry_solids')))
    outcome = verify(db, 'seal-no-dry')
    assert outcome['verdict'] == 'indeterminate'
    assert any(g['kind'] == 'missing_dry_fraction' for g in outcome['detail']['gaps'])


def test_declared_mass_not_arrived_is_indeterminate_not_zero(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    open_lot(db, FIX / 'spoil-lot.json')
    partial = dict(fixture_doc('spoil-load.json'), gross_kg=7000)
    record_ticket(db, write(tmp_path, 'l.json', partial))
    seal(db, write(tmp_path, 's.json', seal_doc('seal-partial')))
    outcome = verify(db, 'seal-partial')
    assert outcome['verdict'] == 'indeterminate'
    assert any(g['kind'] == 'declared_not_arrived' for g in outcome['detail']['gaps'])

    empty = tmp_path / 'empty.db'
    adopt(empty, FIX / 'road_normal.json')
    open_lot(empty, FIX / 'spoil-lot.json')
    seal(empty, write(tmp_path, 's2.json', seal_doc('seal-empty')))
    assert verify(empty, 'seal-empty')['verdict'] == 'indeterminate'


def test_time_reversed_parent_is_blocked(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    open_lot(db, FIX / 'spoil-lot.json')
    record_ticket(db, FIX / 'spoil-load.json')
    early = dict(fixture_doc('spoil-accept.json'), at='2026-06-01T07:00:00Z')
    record_ticket(db, write(tmp_path, 'a.json', early))
    seal(db, write(tmp_path, 's.json', seal_doc('seal-time')))
    outcome = verify(db, 'seal-time')
    assert outcome['verdict'] == 'blocked'
    assert any(v['kind'] == 'time_order' and v['parent'] == 'load-1'
               for v in outcome['detail']['violations'])


def test_location_jump_is_blocked(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    open_lot(db, FIX / 'spoil-lot.json')
    record_ticket(db, FIX / 'spoil-load.json')
    jump = dict(fixture_doc('spoil-accept.json'), from_node='STOCK-ELSEWHERE')
    record_ticket(db, write(tmp_path, 'a.json', jump))
    seal(db, write(tmp_path, 's.json', seal_doc('seal-jump')))
    outcome = verify(db, 'seal-jump')
    assert outcome['verdict'] == 'blocked'
    assert any(v['kind'] == 'location_jump' for v in outcome['detail']['violations'])


def test_same_ticket_id_different_bytes_is_rejected(tmp_path):
    db = tmp_path / 'state.db'
    base_lot(db)
    changed = dict(fixture_doc('spoil-accept.json'), gross_kg=9900)
    with pytest.raises(ValueError, match='spoil_ticket_conflict'):
        record_ticket(db, write(tmp_path, 'a.json', changed))
    seal(db, write(tmp_path, 's.json', seal_doc('seal-id', contract='dry_solids')))
    first = verify(db, 'seal-id')
    assert first['verdict'] == 'consistent_with_records'
    assert verify(db, 'seal-id')['digest'] == first['digest']


def test_seal_freezes_ticket_set_and_new_tickets_need_new_seal(tmp_path):
    db = tmp_path / 'state.db'
    base_lot(db)
    sealed = seal(db, write(tmp_path, 's.json', seal_doc('seal-v1', contract='dry_solids')))
    assert sealed['ticket_count'] == 2
    assert seal(db, write(tmp_path, 's.json', seal_doc('seal-v1', contract='dry_solids'))) == sealed
    extra = dict(fixture_doc('spoil-accept.json'), id='accept-2', from_node='YARD-B',
                 to_node='STOCK-A', at='2026-06-01T12:00:00Z', parents=['accept-1'])
    record_ticket(db, write(tmp_path, 'e.json', extra))
    assert summary(db, 'lot-R1-01')['ticket_count'] == 3
    with pytest.raises(ValueError, match='spoil_seal_conflict'):
        seal(db, write(tmp_path, 's.json', seal_doc('seal-v1', contract='dry_solids')))
    again = seal(db, write(tmp_path, 's2.json', seal_doc('seal-v2', contract='dry_solids')))
    assert again['ticket_count'] == 3
    # the frozen v1 verdict is replayed unchanged from the original two tickets
    assert verify(db, 'seal-v1')['verdict'] == 'consistent_with_records'
    assert verify(db, 'seal-v1')['detail']['ticket_count'] == 2


def test_new_work_revision_stales_seal_but_keeps_old_result(tmp_path):
    db = tmp_path / 'state.db'
    base_lot(db)
    seal(db, write(tmp_path, 's.json', seal_doc('seal-dry', contract='dry_solids')))
    before = verify(db, 'seal-dry')
    assert before['verdict'] == 'consistent_with_records'
    revision = dict(fixture_doc('road_normal.json'), revision=2)
    assert adopt(db, write(tmp_path, 'r2.json', revision))['state'] == 'active'
    with pytest.raises(ValueError, match='spoil_seal_stale'):
        verify(db, 'seal-dry')
    kept = result(db, 'seal-dry')
    assert kept['state'] == 'stale'
    assert kept['verdict'] == 'consistent_with_records'
    assert kept['digest'] == before['digest']
    with pytest.raises(ValueError, match='active_work_revision_required'):
        seal(db, write(tmp_path, 's2.json', seal_doc('seal-new', contract='dry_solids')))


def test_conflicting_notice_blocks_new_seal(tmp_path):
    db = tmp_path / 'state.db'
    base_lot(db)
    assert adopt(db, FIX / 'road_conflict.json')['state'] == 'conflict'
    with pytest.raises(ValueError, match='active_work_revision_required'):
        seal(db, write(tmp_path, 's.json', seal_doc('seal-conflict')))


def test_verify_recomputes_from_frozen_raws_and_detects_tampering(tmp_path):
    db = tmp_path / 'state.db'
    base_lot(db)
    seal(db, write(tmp_path, 's.json', seal_doc('seal-dry', contract='dry_solids')))
    first = verify(db, 'seal-dry')
    assert verify(db, 'seal-dry')['digest'] == first['digest']

    con = connect(db)
    raw = con.execute("SELECT raw FROM spoil_ticket WHERE id='accept-1'").fetchone()[0]
    forged = json.loads(raw)
    forged['gross_kg'] = 12000
    con.execute("UPDATE spoil_ticket SET raw=? WHERE id='accept-1'", (canonical(forged),))
    con.commit()
    con.close()
    with pytest.raises(ValueError, match='spoil_evidence_tampered'):
        verify(db, 'seal-dry')

    db2 = tmp_path / 'deleted.db'
    base_lot(db2)
    seal(db2, write(tmp_path, 's2.json', seal_doc('seal-dry', contract='dry_solids')))
    verify(db2, 'seal-dry')
    con = connect(db2)
    con.execute("DELETE FROM spoil_ticket WHERE id='accept-1'")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match='spoil_evidence_tampered'):
        verify(db2, 'seal-dry')


def test_stored_result_tampering_breaks_replay(tmp_path):
    db = tmp_path / 'state.db'
    base_lot(db)
    seal(db, write(tmp_path, 's.json', seal_doc('seal-dry', contract='dry_solids')))
    verify(db, 'seal-dry')
    con = connect(db)
    con.execute("UPDATE spoil_result SET detail='{}' WHERE seal_id='seal-dry'")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match='spoil_result_conflict'):
        verify(db, 'seal-dry')


def test_unknown_and_invalid_inputs(tmp_path):
    db = tmp_path / 'state.db'
    with pytest.raises(ValueError, match='unknown_spoil_seal'):
        verify(db, 'nope')
    adopt(db, FIX / 'road_normal.json')
    with pytest.raises(ValueError, match='unknown_spoil_lot'):
        seal(db, write(tmp_path, 's.json', seal_doc('seal-x', lot='nope')))
    open_lot(db, FIX / 'spoil-lot.json')
    with pytest.raises(ValueError, match='invalid_spoil_seal'):
        seal(db, write(tmp_path, 's2.json', seal_doc('seal-x', contract='volume')))
    seal(db, write(tmp_path, 's3.json', seal_doc('seal-x')))
    with pytest.raises(ValueError, match='spoil_result_missing'):
        result(db, 'seal-x')


def test_cli_seal_verify_result(tmp_path):
    db = tmp_path / 'cli.db'

    def run(*args):
        out = subprocess.run([sys.executable, '-B', '-m', 'roadcase', *args, '--db', str(db)],
                             cwd=ROOT, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    run('adopt', '--input', str(FIX / 'road_normal.json'))
    run('spoil-open', '--input', str(FIX / 'spoil-lot.json'))
    run('spoil-ticket', '--input', str(FIX / 'spoil-load.json'))
    run('spoil-ticket', '--input', str(FIX / 'spoil-accept.json'))
    seal_input = write(tmp_path, 'seal.json', seal_doc('cli-seal', contract='dry_solids'))
    sealed = json.loads(run('spoil-seal', '--input', str(seal_input)))
    assert sealed['ticket_count'] == 2
    verified = json.loads(run('spoil-verify', '--id', 'cli-seal'))
    assert verified['verdict'] == 'consistent_with_records'
    kept = json.loads(run('spoil-result', '--id', 'cli-seal'))
    assert kept['digest'] == verified['digest']
    assert kept['detail']['assignment']['tickets']['load-1']['contract_kg'] == 8000
