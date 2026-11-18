"""Evidence-facing report reconstructed from durable entities."""
import json
from pathlib import Path
from .store import connect
from .policy import TABLES,EXTRA_TABLES

def _sign_section(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sign_plan'").fetchone():
        return {"plans": [], "verifications": []}
    plans = []
    for row in db.execute("SELECT * FROM sign_plan ORDER BY created_at,plan_id"):
        certificates = [json.loads(item["body_json"]) for item in db.execute(
            "SELECT body_json FROM sign_lane_certificate WHERE plan_id=? ORDER BY lane_id",
            (row["plan_id"],))]
        plans.append({
            "plan_id": row["plan_id"], "survey_id": row["survey_id"],
            "sign_id": row["sign_id"], "class_id": row["class_id"],
            "survey_digest": row["survey_digest"],
            "work": {"source_id": row["work_source_id"], "revision": row["work_revision"],
                     "state": row["work_state"]},
            "lanes": json.loads(row["lanes_json"]),
            "coordinate_system": row["coordinate_system"],
            "contract": json.loads(row["contract_json"]),
            "state": row["state"],
            "covered_lanes": json.loads(row["covered_lanes_json"]),
            "certificates": certificates,
        })
    verifications = [dict(item) for item in db.execute(
        "SELECT verify_id,plan_id,ok,mismatches_json,created_at FROM sign_verify_record "
        "ORDER BY created_at,verify_id")]
    for item in verifications:
        item["ok"] = bool(item["ok"])
        item["mismatches"] = json.loads(item.pop("mismatches_json"))
    return {"plans": plans, "verifications": verifications}

def report(db_path,out=None):
    db=connect(db_path);t=TABLES
    notices=[dict(x) for x in db.execute(f"SELECT * FROM {t['notice']} ORDER BY id")]
    cases=[]
    for row in db.execute(f"SELECT * FROM {t['case']} ORDER BY id"):
        result=db.execute(f"SELECT * FROM {t['assessment']} WHERE case_id=? ORDER BY id DESC LIMIT 1",
                          (row["id"],)).fetchone()
        obligations=[dict(x) for x in db.execute(f"SELECT * FROM {t['obligation']} WHERE assessment_id=? ORDER BY target",
                     (result["id"],))] if result else []
        for ob in obligations:
            ob["receipts"]=[dict(x) for x in db.execute(f"SELECT event_key,at,amount,verdict,reason FROM {t['receipt']} WHERE obligation_id=? ORDER BY event_key",(ob["id"],))]
        cases.append({"case":row["id"],"source":row["source_id"],"input_hash":row["input_hash"],
                      "state":result["state"] if result else None,
                      "outcome":result["outcome"] if result else None,
                      "revision":result["source_revision"] if result else None,
                      "stale":bool(result["stale"]) if result else None,
                      "detail":json.loads(result["detail"]) if result else None,
                      "obligations":obligations})
    evidence=[dict(x) for x in db.execute("SELECT source_id,revision,digest,captured_at FROM source_event ORDER BY source_id,revision")]
    conflicts=[dict(x) for x in db.execute("SELECT source_id,revision,digest,reason FROM source_conflict ORDER BY id")]
    auxiliary={name:[dict(x) for x in db.execute(f"SELECT * FROM {name} ORDER BY 1")]
               for name in EXTRA_TABLES if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone()}
    sign_section=_sign_section(db)
    db.close()
    result={"notices":notices,"cases":cases,"source_evidence":evidence,"source_conflicts":conflicts,
            "auxiliary":auxiliary,"sign_decisions":sign_section}
    if out:
        target=Path(out);target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_name(target.name+".pending")
        temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        temp.replace(target)
    return result

