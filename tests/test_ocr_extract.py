import json
from pathlib import Path
import pytest
from roadcase.ocr import register, preview, extract, export, verify
from roadcase.adoption import adopt
from roadcase.assessment import assess
from roadcase.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def _db(tmp_path, name='ocr-bulletin.json'):
    db = tmp_path / 'state.db'
    register(db, FIX / name)
    return db


def test_r8_dual_contract_divergence(tmp_path):
    db = _db(tmp_path)
    strict = extract(db, 'scan-road-8', 'R8', 'strict_literal')
    assert strict['status'] == 'review'
    failed = {f['field']: f['reason'] for f in strict['failures']}
    assert failed['total_lanes'] == 'competing_candidates'
    corr = extract(db, 'scan-road-8', 'R8', 'corroborated')
    assert corr['status'] == 'ready_for_adoption'
    f = corr['fields']
    assert f['total_lanes']['value'] == 2
    assert f['lanes_closed']['value'] == 1
    assert f['remaining_lanes']['value'] == 1
    assert f['source_id']['value'] == 'road-ocr-8'
    assert f['revision']['value'] == 2
    assert f['authority']['value'] == 'city'
    assert f['action']['value'] == 'close'
    assert f['remaining_capacity']['value'] == 20
    assert f['published_at']['value'] == '2026-06-01T00:00:00Z'
    assert f['effective_from']['value'] == '2026-06-02T00:00:00Z'
    assert f['effective_until']['value'] == '2026-06-03T00:00:00Z'
    # low-confidence 2/1/1 wins only because the independent statement pins total=2
    total_ev = {(e['token_id'], e['alternative']) for e in f['total_lanes']['evidence']}
    assert ('p2-06', 1) in total_ev and ('p1-11', 0) in total_ev
    assert 'independent_statement' in f['total_lanes']['basis']
    reasons = {(r['token_id'], r['alternative']): r['reason'] for r in corr['rejected']}
    assert reasons[('p2-07', 0)] == 'arithmetic_inconsistent'  # 7 > any total
    assert reasons[('p2-06', 0)] == 'contradicts_independent_statement'
    assert reasons[('p2-08', 0)] == 'contradicts_independent_statement'
    # R9's high-confidence digits are never borrowed into R8
    used = {e['token_id'] for fld in f.values() for e in fld['evidence']}
    assert not ({'p2-10', 'p2-11', 'p2-12'} & used)


def test_ambiguous_scan_yields_two_distinct_witnesses(tmp_path):
    db = _db(tmp_path, 'ocr-bulletin-ambiguous.json')
    corr = extract(db, 'scan-road-8-amb', 'R8', 'corroborated')
    assert corr['status'] == 'review'
    assert corr['reason'] == 'multiple_solutions'
    assert corr['solutions'] == 2
    witnesses = corr['witnesses']
    assert len(witnesses) == 2 and witnesses[0] != witnesses[1]
    triples = {(w['total_lanes'], w['lanes_closed'], w['remaining_lanes']) for w in witnesses}
    assert triples == {(2, 1, 1), (3, 1, 2)}
    with pytest.raises(ValueError, match='extract_not_ready'):
        export(db, corr['extract_id'])


def test_r9_row_stays_isolated_from_r8(tmp_path):
    db = _db(tmp_path)
    corr = extract(db, 'scan-road-8', 'R9', 'corroborated')
    assert corr['status'] == 'ready_for_adoption'
    f = corr['fields']
    assert (f['total_lanes']['value'], f['lanes_closed']['value'],
            f['remaining_lanes']['value']) == (3, 2, 1)
    lane_tokens = {e['token_id'] for name in ('total_lanes', 'lanes_closed', 'remaining_lanes')
                   for e in f[name]['evidence']}
    assert lane_tokens == {'p2-10', 'p2-11', 'p2-12'}
    strict = extract(db, 'scan-road-8', 'R9', 'strict_literal')
    assert strict['status'] == 'ready_for_adoption'


def test_cross_page_continuation_and_misaligned_columns(tmp_path):
    db = _db(tmp_path, 'ocr-bulletin-split.json')
    corr = extract(db, 'scan-road-9', 'R8', 'corroborated')
    assert corr['status'] == 'ready_for_adoption'
    f = corr['fields']
    assert f['total_lanes']['value'] == 2 and f['lanes_closed']['value'] == 1
    assert f['revision']['value'] == 1
    assert f['published_at']['value'] == '2026-07-01T00:00:00Z'
    assert f['effective_from']['value'] == '2026-07-02T00:00:00Z'
    assert f['effective_until']['value'] == '2026-07-03T00:00:00Z'
    assert f['authority']['value'] == 'district'
    assert f['remaining_capacity']['value'] == 15
    # row continuation: the remaining-lanes cell sits on its own visual line
    assert 'p2-08' in {e['token_id'] for e in f['remaining_lanes']['evidence']}
    # page-3 table columns are shifted; per-page layout still attributes correctly
    total_ev = {e['token_id'] for e in f['total_lanes']['evidence']}
    assert 'p3-06' in total_ev and 'p2-06' in total_ev
    assert 'cross_page_consistent' in f['total_lanes']['basis']
    # page footers never feed fields
    used = {e['token_id'] for fld in f.values() for e in fld['evidence']}
    assert not ({'p1-12', 'p2-13', 'p3-15'} & used)
    # one token candidate serves at most one field
    seen = {}
    for name, fld in f.items():
        for e in fld['evidence']:
            key = (e['token_id'], e['alternative'])
            assert seen.get(key, name) == name
            seen[key] = name
    strict = extract(db, 'scan-road-9', 'R8', 'strict_literal')
    assert strict['status'] == 'review'


def test_statement_contradicting_table_is_conflict(tmp_path):
    db = _db(tmp_path, 'ocr-bulletin-conflict.json')
    corr = extract(db, 'scan-road-8-conf', 'R8', 'corroborated')
    assert corr['status'] == 'conflict'
    assert corr['reason'] == 'statement_contradiction'
    assert corr['solutions'] == 0
    with pytest.raises(ValueError, match='extract_not_ready'):
        export(db, corr['extract_id'])


def test_missing_segment_unknown_scan_and_bad_policy(tmp_path):
    db = _db(tmp_path)
    miss = extract(db, 'scan-road-8', 'R99', 'corroborated')
    assert miss['status'] == 'conflict' and miss['reason'] == 'segment_not_found'
    strict = extract(db, 'scan-road-8', 'R99', 'strict_literal')
    assert strict['status'] == 'review'
    with pytest.raises(ValueError, match='unknown_ocr_scan'):
        extract(db, 'scan-nope', 'R8', 'corroborated')
    with pytest.raises(ValueError, match='unknown_extraction_policy'):
        extract(db, 'scan-road-8', 'R8', 'lenient')


def test_verify_replay_and_detail_tamper_detection(tmp_path):
    db = _db(tmp_path)
    first = extract(db, 'scan-road-8', 'R8', 'corroborated')
    assert first['status'] == 'ready_for_adoption'
    again = extract(db, 'scan-road-8', 'R8', 'corroborated')
    assert again['extract_id'] == first['extract_id']
    con = connect(db)
    assert con.execute('SELECT count(*) FROM ocr_extract').fetchone()[0] == 1
    con.close()
    ok = verify(db, first['extract_id'])
    assert ok['status'] == 'verified' and ok['extract_status'] == 'ready_for_adoption'
    con = connect(db)
    con.execute('UPDATE ocr_extract SET detail=? WHERE id=?',
                ('{"status":"ready_for_adoption","tampered":true}', first['extract_id']))
    con.commit()
    con.close()
    assert verify(db, first['extract_id'])['reason'] == 'extract_mismatch'


def test_verify_detects_scan_token_tampering(tmp_path):
    db = _db(tmp_path)
    first = extract(db, 'scan-road-8', 'R8', 'corroborated')
    con = connect(db)
    raw = json.loads(con.execute("SELECT raw FROM ocr_scan WHERE id='scan-road-8'").fetchone()[0])
    raw['tokens'][5]['alternatives'][0]['text'] = '2026-06-09T00:00:00Z'
    con.execute("UPDATE ocr_scan SET raw=? WHERE id='scan-road-8'", (json.dumps(raw),))
    con.commit()
    con.close()
    assert verify(db, first['extract_id'])['reason'] == 'scan_tokens_changed'


def test_export_file_tampering_invalidates_ready(tmp_path):
    db = _db(tmp_path)
    first = extract(db, 'scan-road-8', 'R8', 'corroborated')
    out = tmp_path / 'notice.json'
    res = export(db, first['extract_id'], out)
    assert res['doc']['payload']['lanes_closed'] == 1
    assert res['doc']['provenance']['extract_id'] == first['extract_id']
    assert verify(db, first['extract_id'], str(out))['status'] == 'verified'
    doc = json.loads(out.read_text(encoding='utf-8'))
    doc['payload']['lanes_closed'] = 2
    out.write_text(json.dumps(doc), encoding='utf-8')
    assert verify(db, first['extract_id'], str(out))['reason'] == 'export_file_changed'


def test_ready_extract_exports_adoptable_notice(tmp_path):
    db = _db(tmp_path)
    first = extract(db, 'scan-road-8', 'R8', 'corroborated')
    out = tmp_path / 'notice.json'
    export(db, first['extract_id'], out)
    adopted = adopt(db, out)
    assert adopted['state'] == 'active' and adopted['source'] == 'road-ocr-8'
    case = {'case_id': 'journey-ocr', 'source_id': 'road-ocr-8',
            'at': '2026-06-02T08:00:00Z',
            'facts': {'route': ['R8'], 'flow': 20, 'audiences': ['bus']}}
    case_path = tmp_path / 'case.json'
    case_path.write_text(json.dumps(case), encoding='utf-8')
    decision = assess(db, case_path)
    assert decision['state'] == 'constrained' and decision['outcome'] == 'lane_reduction'
    # legacy OCR preview is untouched by extraction
    text = preview(db, 'scan-road-8')['flat_text']
    assert 'road-ocr-8' in text and 'R8' in text


def test_new_recognition_version_creates_separate_extract(tmp_path):
    db = _db(tmp_path)
    first = extract(db, 'scan-road-8', 'R8', 'corroborated')
    doc = json.loads((FIX / 'ocr-bulletin.json').read_text(encoding='utf-8'))
    doc['scan_id'] = 'scan-road-8-v2'
    next(x for x in doc['tokens'] if x['id'] == 'p2-06')['alternatives'] = [
        {'text': '2', 'confidence': 0.93}]
    path = tmp_path / 'v2.json'
    path.write_text(json.dumps(doc), encoding='utf-8')
    register(db, path)
    second = extract(db, 'scan-road-8-v2', 'R8', 'corroborated')
    assert second['extract_id'] != first['extract_id']
    assert second['status'] == 'ready_for_adoption'
    con = connect(db)
    assert con.execute('SELECT count(*) FROM ocr_extract').fetchone()[0] == 2
    con.close()
    # the old extraction is neither overwritten nor invalidated by the new scan
    assert extract(db, 'scan-road-8', 'R8', 'corroborated')['extract_id'] == first['extract_id']
    assert verify(db, first['extract_id'])['status'] == 'verified'


def test_cli_extract_export_verify_roundtrip(tmp_path, monkeypatch, capsys):
    db = tmp_path / 'state.db'
    register(db, FIX / 'ocr-bulletin.json')
    from roadcase.__main__ import main
    monkeypatch.setattr('sys.argv', ['roadcase', 'ocr-extract', '--db', str(db),
                                     '--id', 'scan-road-8', '--segment', 'R8',
                                     '--policy', 'corroborated'])
    main()
    printed = json.loads(capsys.readouterr().out)
    assert printed['status'] == 'ready_for_adoption'
    out = tmp_path / 'notice.json'
    monkeypatch.setattr('sys.argv', ['roadcase', 'ocr-export', '--db', str(db),
                                     '--id', printed['extract_id'], '--out', str(out)])
    main()
    capsys.readouterr()
    assert json.loads(out.read_text(encoding='utf-8'))['payload']['segment'] == 'R8'
    monkeypatch.setattr('sys.argv', ['roadcase', 'ocr-verify', '--db', str(db),
                                     '--id', printed['extract_id'], '--export', str(out)])
    main()
    assert json.loads(capsys.readouterr().out)['status'] == 'verified'
