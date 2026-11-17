import json
from pathlib import Path
import pytest
from roadcase.adoption import adopt
from roadcase.assessment import assess
from roadcase.settlement import settle
from roadcase.report import report
from roadcase.policy import TABLES
from roadcase.store import connect
FIX=Path(__file__).resolve().parent.parent/"fixtures"

def test_three_business_decisions_and_evidence(tmp_path):
    db=tmp_path/"state.db"
    assert adopt(db,FIX/"road_normal.json")["state"]=="active"
    decision=assess(db,FIX/"route_normal.json")
    assert (decision["state"],decision["outcome"])==("rerouted","detour_required")
    assert settle(db,FIX/"notice_normal.json")["status"]=="complete"
    evidence=report(db,tmp_path/"report.json")
    assert evidence["source_evidence"] and evidence["cases"][0]["obligations"]
    assert json.loads((tmp_path/"report.json").read_text(encoding="utf-8"))["cases"][0]["outcome"]=="detour_required"
    con=connect(db)
    assert con.execute(f"SELECT count(*) FROM {TABLES['notice']}").fetchone()[0]==1
    assert con.execute(f"SELECT count(*) FROM {TABLES['revision']}").fetchone()[0]==1
    assert con.execute(f"SELECT count(*) FROM {TABLES['case']}").fetchone()[0]==1
    assert con.execute(f"SELECT count(*) FROM {TABLES['assessment']}").fetchone()[0]==1
    assert con.execute(f"SELECT count(*) FROM {TABLES['obligation']}").fetchone()[0]>=1
    assert con.execute(f"SELECT count(*) FROM {TABLES['receipt']}").fetchone()[0]>=1
    con.close()

def test_conflicting_public_evidence_blocks_downstream_case(tmp_path):
    db=tmp_path/"state.db";adopt(db,FIX/"road_normal.json")
    assert adopt(db,FIX/"road_conflict.json")["state"]=="conflict"
    decision=assess(db,FIX/"route_normal.json")
    assert decision["state"]=="uncertain"
    assert decision["obligations"]==[]
    assert report(db)["source_conflicts"][0]["reason"]=="same_revision_different_bytes"

def test_domain_failure_changes_assessment(tmp_path):
    db=tmp_path/"state.db";adopt(db,FIX/"road_normal.json")
    decision=assess(db,FIX/"route_blocked.json")
    assert (decision["state"],decision["outcome"])==("blocked","no_viable_detour")

def test_partial_or_bad_fulfillment_is_not_success(tmp_path):
    db=tmp_path/"state.db";adopt(db,FIX/"road_normal.json");assess(db,FIX/"route_normal.json")
    result=settle(db,FIX/"notice_late.json")
    assert result["status"]!="complete"
    assert any(x["state"]!="complete" for x in result["obligations"])
    assert any(x["receipts"] for x in report(db)["cases"][0]["obligations"])

def test_new_public_version_stales_assessment_and_obligation(tmp_path):
    db=tmp_path/"state.db";adopt(db,FIX/"road_normal.json");assess(db,FIX/"route_normal.json")
    next_doc=json.loads((FIX/"road_normal.json").read_text(encoding="utf-8"))
    next_doc["revision"]=2;next_doc["payload"]["action"]="cancel"
    path=tmp_path/"revision.json";path.write_text(json.dumps(next_doc),encoding="utf-8")
    assert adopt(db,path)["state"]=="cancelled"
    assert report(db)["cases"][0]["stale"] is True
    with pytest.raises(ValueError,match="assessment_stale"):
        settle(db,FIX/"notice_normal.json")
    updated=assess(db,FIX/"route_normal.json")
    assert updated["state"]=="unaffected"

def test_idempotent_receipt_and_identity_conflict(tmp_path):
    db=tmp_path/"state.db";adopt(db,FIX/"road_normal.json");assess(db,FIX/"route_normal.json")
    assert settle(db,FIX/"notice_normal.json")["status"]=="complete"
    assert settle(db,FIX/"notice_normal.json")["status"]=="complete"
    changed=json.loads((FIX/"notice_normal.json").read_text(encoding="utf-8"))
    changed["receipts"][0]["amount"]+=1
    path=tmp_path/"changed.json";path.write_text(json.dumps(changed),encoding="utf-8")
    with pytest.raises(ValueError,match="receipt_identity_conflict"):
        settle(db,path)

