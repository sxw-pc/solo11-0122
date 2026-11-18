"""Synthetic acceptance tests for excavation utility-avoidance rulings."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from roadcase.adoption import adopt
from roadcase.report import report
from roadcase.utilities import (
    record,
    plan_excavation,
    decide_excavation,
    verify_excavation,
    _interval_gap,
    _polyline_distance,
)

FIX = Path(__file__).resolve().parent.parent / 'fixtures'
ROOT = Path(__file__).resolve().parent.parent
AT = '2026-06-10T00:00:00Z'
CUTOFF = '2099-01-01T00:00:00Z'


def _write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding='utf-8')
    return path


def _plan_input(**overrides):
    doc = {
        'excavation_id': 'exc-1',
        'utility_plan_id': 'excavation-E1',
        'alignment_id': 'original',
        'methods': ['machine', 'vacuum'],
        'survey_sources': ['excavation-E1'],
        'work': {'source_id': 'road-1', 'revision': 1},
        'coordinate_system': 'synthetic-local-metres',
        'evidence_cutoff': CUTOFF,
    }
    doc.update(overrides)
    return doc


def _base(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    record(db, FIX / 'utility-plan.json')
    return db


def _plan_and_decide(db, tmp_path, plan_doc, at=AT):
    plan_excavation(db, _write(tmp_path, plan_doc['excavation_id'] + '.plan.json', plan_doc))
    return decide_excavation(db, _write(tmp_path, plan_doc['excavation_id'] + '.decide.json',
                                        {'excavation_id': plan_doc['excavation_id'], 'at': at}))


def _method(decision, method_id):
    return {m['method_id']: m for m in decision['detail']['methods']}[method_id]


def _utility(method, utility_id):
    return {u['utility_id']: u for u in method['utilities']}[utility_id]


# --- acceptance 1: same evidence, two methods, different real permits ------

def test_same_evidence_two_methods_different_permits(tmp_path):
    db = _base(tmp_path)
    decision = _plan_and_decide(db, tmp_path, _plan_input())
    assert decision['state'] == 'decided'
    machine = _method(decision, 'machine')
    assert machine['permit'] == 'blocked'
    assert machine['scope'] == {'operation': 'full_excavation', 'alignment_id': 'original',
                                'depth_low': 1.2, 'depth_high': 1.8}
    assert machine['limiting_clearance'] == pytest.approx(0.2)
    assert machine['limiting_utilities'] == ['gas-U1']
    gas = _utility(machine, 'gas-U1')
    assert gas['plan_distance'] == pytest.approx(1.0)
    # conservative envelope: 1.0 - 0.2 half width - 0.1 radius - 0.4 survey - 0.1 machine
    assert gas['deductions'] == {'half_width': 0.2, 'outer_radius': 0.1,
                                 'horizontal_uncertainty': 0.4, 'machine_horizontal_error': 0.1,
                                 'vertical_uncertainty': 0.1, 'machine_vertical_error': 0.1}
    assert gas['horizontal_clearance'] == pytest.approx(0.2)
    assert gas['clearance_3d'] == pytest.approx(0.2)
    assert gas['constrains'] is True
    vacuum = _method(decision, 'vacuum')
    assert vacuum['permit'] == 'allowed_to_probe'
    assert vacuum['scope']['operation'] == 'exploratory_pothole'
    assert vacuum['limiting_clearance'] == pytest.approx(0.35)
    assert vacuum['limiting_utilities'] == ['gas-U1']


def test_probe_permit_never_upgrades_to_machine_permit(tmp_path):
    db = _base(tmp_path)
    decision = _plan_and_decide(db, tmp_path, _plan_input())
    machine = _method(decision, 'machine')
    vacuum = _method(decision, 'vacuum')
    assert vacuum['permit'] == 'allowed_to_probe'
    assert machine['permit'] == 'blocked'
    # both rulings rest on the same frozen survey evidence
    gas_machine = _utility(machine, 'gas-U1')
    gas_vacuum = _utility(vacuum, 'gas-U1')
    assert gas_machine['plan_distance'] == gas_vacuum['plan_distance'] == 1.0
    assert (gas_machine['deductions']['horizontal_uncertainty']
            == gas_vacuum['deductions']['horizontal_uncertainty'] == 0.4)


def test_declared_pothole_option_does_not_shrink_uncertainty(tmp_path):
    db = _base(tmp_path)
    decision = _plan_and_decide(db, tmp_path, _plan_input())
    # the survey only declares a verification option; it is not a measurement
    for method_id in ('machine', 'vacuum'):
        gas = _utility(_method(decision, method_id), 'gas-U1')
        assert gas['deductions']['horizontal_uncertainty'] == pytest.approx(0.4)


# --- acceptance 2: machine permit after rerouting or after measurement ------

def test_rerouted_alignment_lifts_machine_clearance(tmp_path):
    db = _base(tmp_path)
    decision = _plan_and_decide(db, tmp_path,
                                _plan_input(excavation_id='exc-rerouted', alignment_id='rerouted'))
    machine = _method(decision, 'machine')
    assert machine['permit'] == 'allowed_full_excavation'
    assert machine['limiting_clearance'] == pytest.approx(0.6)
    gas = _utility(machine, 'gas-U1')
    assert gas['plan_distance'] == pytest.approx(1.4)
    assert gas['horizontal_clearance'] == pytest.approx(0.6)


def _potholed_survey(tmp_path):
    doc = json.loads((FIX / 'utility-plan.json').read_text(encoding='utf-8'))
    doc['plan_id'] = 'excavation-E1-potholed'
    doc['utilities'][0]['horizontal_uncertainty'] = 0.05
    doc['verification_options'] = [
        {'id': 'pothole-gas-U1', 'utility_id': 'gas-U1',
         'new_horizontal_uncertainty': 0.05, 'evidence': 'synthetic-pothole-measurement'}]
    return _write(tmp_path, 'utility-plan-potholed.json', doc)


def test_measured_pothole_creates_new_plan_and_machine_permit(tmp_path):
    db = _base(tmp_path)
    first = _plan_and_decide(db, tmp_path, _plan_input())
    assert _method(first, 'machine')['permit'] == 'blocked'
    assert record(db, _potholed_survey(tmp_path)) == 2
    decision = _plan_and_decide(db, tmp_path, _plan_input(
        excavation_id='exc-measured', utility_plan_id='excavation-E1-potholed',
        survey_sources=['excavation-E1-potholed']))
    machine = _method(decision, 'machine')
    assert machine['permit'] == 'allowed_full_excavation'
    assert machine['limiting_clearance'] == pytest.approx(0.55)
    gas = _utility(machine, 'gas-U1')
    assert gas['deductions']['horizontal_uncertainty'] == pytest.approx(0.05)
    # original survey and the historical ruling are not overwritten
    con = sqlite3.connect(db)
    assert con.execute('SELECT horizontal_uncertainty FROM utility_observation'
                       ' WHERE plan_id=? AND utility_id=?',
                       ('excavation-E1', 'gas-U1')).fetchone()[0] == 0.4
    rows = con.execute('SELECT seq,state FROM excavation_decision WHERE excavation_id=?'
                       ' ORDER BY seq', ('exc-1',)).fetchall()
    con.close()
    assert rows == [(1, 'decided')]
    again = decide_excavation(db, _write(tmp_path, 'again.json', {'excavation_id': 'exc-1', 'at': AT}))
    assert _method(again, 'machine')['permit'] == 'blocked'


# --- acceptance 3: plan crossing but deep utility is not a collision --------

def test_plan_crossing_but_deep_utility_is_safe(tmp_path):
    db = _base(tmp_path)
    decision = _plan_and_decide(db, tmp_path, _plan_input())
    machine = _method(decision, 'machine')
    drain = _utility(machine, 'drain-U2')
    assert drain['plan_distance'] == pytest.approx(0.0)  # 2D crossing at x=5
    assert drain['witness']['trench_point'] == [5.0, 0.0]
    assert drain['witness']['utility_point'] == [5.0, 0.0]
    assert drain['vertical_gap'] == pytest.approx(1.0)
    # 1.0 - 0.1 radius - 0.1 vertical survey - 0.1 machine depth error
    assert drain['vertical_clearance'] == pytest.approx(0.7)
    assert drain['clearance_3d'] == pytest.approx(0.7)
    assert drain['constrains'] is False
    assert machine['limiting_utilities'] == ['gas-U1']


# --- acceptance 4: conflict / boundary / version invalidation ---------------

def test_work_revision_change_forces_pending_and_preserves_history(tmp_path):
    db = _base(tmp_path)
    first = _plan_and_decide(db, tmp_path, _plan_input())
    assert first['state'] == 'decided'
    cancel = json.loads((FIX / 'road_normal.json').read_text(encoding='utf-8'))
    cancel['revision'] = 2
    cancel['payload']['action'] = 'cancel'
    adopt(db, _write(tmp_path, 'road-cancel.json', cancel))
    second = decide_excavation(db, _write(tmp_path, 'second.decide.json',
                                          {'excavation_id': 'exc-1', 'at': AT}))
    assert second['state'] == 'pending'
    assert 'work_revision_superseded' in second['detail']['pending_reasons']
    assert second['detail']['methods'] == []
    assert second['seq'] == 2
    con = sqlite3.connect(db)
    rows = con.execute('SELECT seq,state FROM excavation_decision WHERE excavation_id=?'
                       ' ORDER BY seq', ('exc-1',)).fetchall()
    con.close()
    assert rows == [(1, 'decided'), (2, 'pending')]


def test_conflicting_surveys_go_to_pending(tmp_path):
    db = _base(tmp_path)
    other = json.loads((FIX / 'utility-plan.json').read_text(encoding='utf-8'))
    other['plan_id'] = 'excavation-E2'
    other['utilities'][0]['polyline'] = [[0.0, 1.2], [10.0, 1.2]]
    record(db, _write(tmp_path, 'utility-plan-e2.json', other))
    decision = _plan_and_decide(db, tmp_path, _plan_input(
        excavation_id='exc-conflict', survey_sources=['excavation-E1', 'excavation-E2']))
    assert decision['state'] == 'pending'
    assert 'conflicting_surveys' in decision['detail']['pending_reasons']
    assert decision['detail']['methods'] == []


def test_unknown_coordinate_system_goes_to_pending(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    survey = json.loads((FIX / 'utility-plan.json').read_text(encoding='utf-8'))
    survey['plan_id'] = 'excavation-E3'
    survey['coordinate_system'] = 'unverified-cubits'
    record(db, _write(tmp_path, 'utility-plan-e3.json', survey))
    decision = _plan_and_decide(db, tmp_path, _plan_input(
        excavation_id='exc-units', utility_plan_id='excavation-E3',
        survey_sources=['excavation-E3']))
    assert decision['state'] == 'pending'
    assert 'coordinate_system_unverified' in decision['detail']['pending_reasons']


def test_negative_error_and_incomplete_polyline_go_to_pending(tmp_path):
    db = _base(tmp_path)
    con = sqlite3.connect(db)
    con.execute("UPDATE utility_observation SET horizontal_uncertainty=-0.1"
                " WHERE plan_id='excavation-E1' AND utility_id='gas-U1'")
    con.execute("INSERT INTO excavation_alignment VALUES ('excavation-E1','broken','[[0.0, 0.0]]',1.0,2.0)")
    con.commit()
    con.close()
    negative = _plan_and_decide(db, tmp_path, _plan_input(excavation_id='exc-neg'))
    assert negative['state'] == 'pending'
    assert 'invalid_envelope' in negative['detail']['pending_reasons']
    broken = _plan_and_decide(db, tmp_path, _plan_input(excavation_id='exc-broken', alignment_id='broken'))
    assert broken['state'] == 'pending'
    assert 'incomplete_polyline' in broken['detail']['pending_reasons']


def test_evidence_after_cutoff_goes_to_pending(tmp_path):
    db = _base(tmp_path)
    decision = _plan_and_decide(db, tmp_path, _plan_input(
        excavation_id='exc-cutoff', evidence_cutoff='2000-01-01T00:00:00Z'))
    assert decision['state'] == 'pending'
    assert 'evidence_after_cutoff' in decision['detail']['pending_reasons']


def test_frozen_revision_mismatch_goes_to_pending(tmp_path):
    db = _base(tmp_path)
    decision = _plan_and_decide(db, tmp_path, _plan_input(
        excavation_id='exc-rev', work={'source_id': 'road-1', 'revision': 2}))
    assert decision['state'] == 'pending'
    assert 'work_revision_mismatch' in decision['detail']['pending_reasons']
    assert 'work_revision_superseded' in decision['detail']['pending_reasons']


def test_unknown_alignment_method_and_window_go_to_pending(tmp_path):
    db = _base(tmp_path)
    ghost = _plan_and_decide(db, tmp_path, _plan_input(excavation_id='exc-ghost', alignment_id='ghost'))
    assert ghost['state'] == 'pending'
    assert 'alignment_unknown' in ghost['detail']['pending_reasons']
    dynamite = _plan_and_decide(db, tmp_path, _plan_input(excavation_id='exc-dynamite',
                                                          methods=['machine', 'dynamite']))
    assert dynamite['state'] == 'pending'
    assert 'method_unknown' in dynamite['detail']['pending_reasons']
    late = _plan_and_decide(db, tmp_path, _plan_input(excavation_id='exc-late'),
                            at='2026-08-01T00:00:00Z')
    assert late['state'] == 'pending'
    assert 'outside_work_window' in late['detail']['pending_reasons']


def test_clearance_exactly_at_required_boundary_is_allowed(tmp_path):
    db = tmp_path / 'state.db'
    adopt(db, FIX / 'road_normal.json')
    survey = {
        'plan_id': 'excavation-E4',
        'work': {'source_id': 'road-1', 'revision': 1, 'segment': 'R1'},
        'coordinate_system': 'synthetic-local-metres',
        'alignments': [{'id': 'a', 'polyline': [[0.0, 0.0], [10.0, 0.0]],
                        'depth_low': 1.0, 'depth_high': 2.0}],
        'utilities': [{'id': 'u', 'polyline': [[0.0, 1.0], [10.0, 1.0]],
                       'depth_low': 1.0, 'depth_high': 2.0,
                       'outer_radius': 0.0, 'horizontal_uncertainty': 0.0,
                       'vertical_uncertainty': 0.0}],
        'methods': [{'id': 'm', 'operation': 'full_excavation', 'trench_width': 1.0,
                     'horizontal_error': 0.0, 'vertical_error': 0.0, 'required_clearance': 0.5}],
    }
    record(db, _write(tmp_path, 'utility-plan-e4.json', survey))
    decision = _plan_and_decide(db, tmp_path, _plan_input(
        excavation_id='exc-boundary', utility_plan_id='excavation-E4', alignment_id='a',
        methods=['m'], survey_sources=['excavation-E4']))
    method = _method(decision, 'm')
    assert method['utilities'][0]['clearance_3d'] == pytest.approx(0.5)
    assert method['permit'] == 'allowed_full_excavation'


def test_geometry_kernel_edge_cases():
    # parallel lines
    distance, pa, pb, i, j = _polyline_distance([[0, 0], [10, 0]], [[0, 1], [10, 1]])
    assert distance == pytest.approx(1.0)
    # crossing segments
    distance, pa, pb, i, j = _polyline_distance([[0, 0], [10, 0]], [[5, -1], [5, 2]])
    assert distance == 0.0
    assert list(pa) == [5.0, 0.0] and list(pb) == [5.0, 0.0]
    # endpoint proximity on a shared line
    distance, pa, pb, i, j = _polyline_distance([[0, 0], [4, 0]], [[6, 0], [10, 0]])
    assert distance == pytest.approx(2.0)
    assert list(pa) == [4.0, 0.0] and list(pb) == [6.0, 0.0]
    # zero-length segment degenerates to a point
    distance, pa, pb, i, j = _polyline_distance([[3, 0], [3, 0]], [[0, 2], [10, 2]])
    assert distance == pytest.approx(2.0)
    assert list(pa) == [3.0, 0.0]
    # collinear overlap
    distance, pa, pb, i, j = _polyline_distance([[0, 0], [10, 0]], [[5, 0], [15, 0]])
    assert distance == 0.0
    # depth interval boundaries: touching counts as zero gap
    assert _interval_gap(1.2, 1.8, 1.8, 3.0) == 0.0
    assert _interval_gap(1.2, 1.8, 2.8, 3.0) == pytest.approx(1.0)
    assert _interval_gap(1.2, 1.8, 1.5, 1.6) == 0.0


# --- acceptance 5: repeated decisions and independent verification ----------

def test_repeated_plan_and_decision_are_idempotent(tmp_path):
    db = _base(tmp_path)
    plan_path = _write(tmp_path, 'plan.json', _plan_input())
    assert plan_excavation(db, plan_path)['registered'] is True
    assert plan_excavation(db, plan_path)['registered'] is False
    changed = _plan_input(alignment_id='rerouted')
    with pytest.raises(ValueError, match='excavation_plan_conflict'):
        plan_excavation(db, _write(tmp_path, 'plan-changed.json', changed))
    decide_path = _write(tmp_path, 'decide.json', {'excavation_id': 'exc-1', 'at': AT})
    one = decide_excavation(db, decide_path)
    two = decide_excavation(db, decide_path)
    assert (one['seq'], one['state']) == (two['seq'], two['state']) == (1, 'decided')
    assert one['detail'] == two['detail']
    con = sqlite3.connect(db)
    assert con.execute('SELECT count(*) FROM excavation_decision').fetchone()[0] == 1
    con.close()


def _tamper_detail(db, mutate):
    con = sqlite3.connect(db)
    row = con.execute("SELECT detail FROM excavation_decision WHERE excavation_id='exc-1'").fetchone()
    detail = json.loads(row[0])
    mutate(detail)
    con.execute("UPDATE excavation_decision SET detail=? WHERE excavation_id='exc-1'",
                (json.dumps(detail),))
    con.commit()
    con.close()


def test_verify_accepts_then_detects_forged_clearance(tmp_path):
    db = _base(tmp_path)
    _plan_and_decide(db, tmp_path, _plan_input())
    verify_input = _write(tmp_path, 'verify.json', {'excavation_id': 'exc-1'})
    assert verify_excavation(db, verify_input)['ok'] is True

    def forge(detail):
        for method in detail['methods']:
            for utility in method['utilities']:
                if utility['utility_id'] == 'gas-U1':
                    utility['clearance_3d'] = 0.9
    _tamper_detail(db, forge)
    result = verify_excavation(db, verify_input)
    assert result['ok'] is False
    assert any(not c['ok'] and c['check'] == 'utility_geometry' and c['utility'] == 'gas-U1'
               and 'clearance_3d' in c['mismatched_fields'] for c in result['checks'])


def test_verify_detects_borrowed_pothole_and_permit_upgrade(tmp_path):
    db = _base(tmp_path)
    _plan_and_decide(db, tmp_path, _plan_input())
    verify_input = _write(tmp_path, 'verify.json', {'excavation_id': 'exc-1'})

    def borrow(detail):
        for method in detail['methods']:
            for utility in method['utilities']:
                if utility['utility_id'] == 'gas-U1':
                    utility['deductions']['horizontal_uncertainty'] = 0.05
            if method['method_id'] == 'machine':
                method['permit'] = 'allowed_full_excavation'
    _tamper_detail(db, borrow)
    result = verify_excavation(db, verify_input)
    assert result['ok'] is False
    assert any(not c['ok'] and c['check'] == 'utility_geometry'
               and 'deductions.horizontal_uncertainty' in c['mismatched_fields']
               for c in result['checks'])
    assert any(not c['ok'] and c['check'] == 'permit' and c['method'] == 'machine'
               for c in result['checks'])


def test_report_recomputes_and_flags_forgery_borrowing_and_omission(tmp_path):
    db = _base(tmp_path)
    _plan_and_decide(db, tmp_path, _plan_input())
    excavations = report(db)['excavations']
    assert len(excavations) == 1
    assert excavations[0]['decisions'][0]['discrepancies'] == []

    def forge(detail):
        for method in detail['methods']:
            for utility in method['utilities']:
                if utility['utility_id'] == 'gas-U1':
                    utility['plan_distance'] = 3.3
    _tamper_detail(db, forge)
    types = [d['type'] for d in report(db)['excavations'][0]['decisions'][0]['discrepancies']]
    assert 'forged_plan_distance' in types

    def borrow(detail):
        for method in detail['methods']:
            for utility in method['utilities']:
                if utility['utility_id'] == 'gas-U1':
                    utility['deductions']['horizontal_uncertainty'] = 0.05
    _tamper_detail(db, borrow)
    types = [d['type'] for d in report(db)['excavations'][0]['decisions'][0]['discrepancies']]
    assert 'unproven_uncertainty' in types

    def omit(detail):
        for method in detail['methods']:
            method['utilities'] = [u for u in method['utilities'] if u['utility_id'] != 'drain-U2']
    _tamper_detail(db, omit)
    discrepancies = report(db)['excavations'][0]['decisions'][0]['discrepancies']
    assert any(d['type'] == 'utility_omitted' and d['utility'] == 'drain-U2' for d in discrepancies)


def test_report_keeps_route_and_notice_sections_untouched(tmp_path):
    db = _base(tmp_path)
    _plan_and_decide(db, tmp_path, _plan_input())
    evidence = report(db)
    assert evidence['notices'] and evidence['source_evidence']
    assert evidence['cases'] == []
    assert evidence['excavations'][0]['excavation_id'] == 'exc-1'


# --- CLI and demo smoke -----------------------------------------------------

def _run_cli(*args):
    return subprocess.run([sys.executable, '-m', 'roadcase', *args],
                          cwd=ROOT, capture_output=True, text=True)


def test_cli_excavation_flow(tmp_path):
    db = tmp_path / 'cli.db'
    assert _run_cli('adopt', '--db', str(db), '--input', str(FIX / 'road_normal.json')).returncode == 0
    assert _run_cli('utility-record', '--db', str(db), '--input', str(FIX / 'utility-plan.json')).returncode == 0
    plan = _write(tmp_path, 'cli.plan.json', _plan_input())
    assert _run_cli('excavation-plan', '--db', str(db), '--input', str(plan)).returncode == 0
    decide = _write(tmp_path, 'cli.decide.json', {'excavation_id': 'exc-1', 'at': AT})
    out = _run_cli('excavation-decide', '--db', str(db), '--input', str(decide))
    assert out.returncode == 0
    decision = json.loads(out.stdout)
    assert decision['state'] == 'decided'
    permits = {m['method_id']: m['permit'] for m in decision['detail']['methods']}
    assert permits == {'machine': 'blocked', 'vacuum': 'allowed_to_probe'}
    verify = _write(tmp_path, 'cli.verify.json', {'excavation_id': 'exc-1'})
    out = _run_cli('excavation-verify', '--db', str(db), '--input', str(verify))
    assert out.returncode == 0
    assert json.loads(out.stdout)['ok'] is True


def test_demo_runs_end_to_end(tmp_path):
    workdir = tmp_path / 'demo'
    out = _run_cli('demo', '--workdir', str(workdir))
    assert out.returncode == 0, out.stderr
    data = json.loads((workdir / 'report.json').read_text(encoding='utf-8'))
    assert data['excavations'][0]['excavation_id'] == 'exc-1'
    assert data['excavations'][0]['decisions'][0]['state'] == 'decided'
    assert data['excavations'][0]['decisions'][0]['discrepancies'] == []
