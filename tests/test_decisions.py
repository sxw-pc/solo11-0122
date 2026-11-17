import json
from pathlib import Path
from roadcase.adoption import adopt
from roadcase.assessment import assess
from roadcase.settlement import settle
from roadcase.report import report
from roadcase.detour import configure,reserve,release

FIX=Path(__file__).resolve().parent.parent/'fixtures'

def changed(tmp_path,name,mutate):
    doc=json.loads((FIX/name).read_text(encoding='utf-8'))
    mutate(doc)
    path=tmp_path/(str(len(list(tmp_path.glob('*.json'))))+'.json')
    path.write_text(json.dumps(doc),encoding='utf-8')
    return path

def test_cancelled_work_releases_route_and_stales_notice(tmp_path):
    db=tmp_path/'state.db';adopt(db,FIX/'road_normal.json')
    assert assess(db,FIX/'route_normal.json')['outcome']=='detour_required'
    cancellation=changed(tmp_path,'road_normal.json',lambda d:(d.update(revision=2),d['payload'].update(action='cancel')))
    assert adopt(db,cancellation)['state']=='cancelled'
    assert report(db)['cases'][0]['stale'] is True
    assert assess(db,FIX/'route_normal.json')['outcome']=='notice_not_active'

def test_authority_and_lane_capacity_change_business_result(tmp_path):
    invalid=changed(tmp_path,'road_normal.json',lambda d:d['payload'].update(authority='unlisted'))
    db=tmp_path/'invalid.db';assert adopt(db,invalid)['state']=='invalid'
    assert assess(db,FIX/'route_normal.json')['state']=='unaffected'
    reduced=changed(tmp_path,'road_normal.json',lambda d:d['payload'].update(lanes_closed=1,remaining_capacity=30))
    db2=tmp_path/'reduced.db';adopt(db2,reduced)
    assert assess(db2,FIX/'route_normal.json')['outcome']=='lane_reduction'
    outside=changed(tmp_path,'route_normal.json',lambda d:(d.update(case_id='outside'),d['facts'].update(route=['R9'])))
    assert assess(db2,outside)['outcome']=='route_clear'

def test_wrong_route_receipt_is_rejected_then_valid_proof_recovers(tmp_path):
    db=tmp_path/'state.db';adopt(db,FIX/'road_normal.json');assess(db,FIX/'route_normal.json')
    wrong=changed(tmp_path,'notice_normal.json',lambda d:d['receipts'][0].update(route_id='OLD'))
    result=settle(db,wrong)
    assert result['status']=='partial'
    assert report(db)['cases'][0]['obligations'][0]['receipts'][0]['reason']=='wrong_route_message'
    correct=changed(tmp_path,'notice_normal.json',lambda d:d['receipts'][0].update(event_key='bus-correct'))
    assert settle(db,correct)['status']=='complete'

def test_partial_audience_and_late_delivery_are_distinct(tmp_path):
    db=tmp_path/'state.db';adopt(db,FIX/'road_normal.json');assess(db,FIX/'route_normal.json')
    partial=changed(tmp_path,'notice_normal.json',lambda d:d['receipts'].pop())
    assert settle(db,partial)['status']=='partial'
    late=changed(tmp_path,'notice_late.json',lambda d:d['receipts'][0].update(event_key='another-late'))
    result=settle(db,late)
    assert result['status']=='overdue'
    assert result['obligations'][0]['late_amount']==1

def test_detour_capacity_competition_changes_route_and_notice(tmp_path):
    normal=json.loads((FIX/'detour_normal.json').read_text(encoding='utf-8'))
    conflict=json.loads((FIX/'detour_conflict.json').read_text(encoding='utf-8'))
    db=tmp_path/'state.db';adopt(db,FIX/'road_normal.json')
    configure(db,**normal['pool'])
    assert reserve(db,**normal['first'])['state']=='reserved'
    assert reserve(db,**conflict['second'])['state']=='waiting'
    assert assess(db,FIX/'route_normal.json')['outcome']=='detour_required'
    second=changed(tmp_path,'route_normal.json',lambda d:d.update(case_id='journey-2'))
    assert assess(db,second)['outcome']=='no_viable_detour'
    assert release(db,'H-1')['promoted']==['journey-2']
    assert report(db)['cases'][1]['stale'] is True
    reassessed=assess(db,second)
    assert reassessed['outcome']=='detour_required'
    assert reassessed['obligations'][0]['meta'].find('D1')>=0

def test_work_revision_invalidates_old_detour_holds(tmp_path):
    normal=json.loads((FIX/'detour_normal.json').read_text(encoding='utf-8'))
    db=tmp_path/'state.db';adopt(db,FIX/'road_normal.json');configure(db,**normal['pool'])
    reserve(db,**normal['first'])
    revision=changed(tmp_path,'road_normal.json',lambda d:d.update(revision=2))
    adopt(db,revision)
    from roadcase.store import connect
    con=connect(db)
    assert con.execute("SELECT state FROM detour_hold WHERE id='H-1'").fetchone()[0]=='stale'
    con.close()
