"""Evidence-facing report reconstructed from durable entities."""
import json
from pathlib import Path
from .store import connect
from .policy import TABLES
from .noise import verify_db as noise_verify

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
    noise=_noise_report(db)
    db.close()
    result={"notices":notices,"cases":cases,"source_evidence":evidence,"source_conflicts":conflicts,
            "night_noise":noise}
    if out:
        target=Path(out);target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_name(target.name+".pending")
        temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        temp.replace(target)
    return result

def _noise_report(db):
    """Reconstruct the acoustic subsystem and independently recompute it.

    The road notice appears only as prerequisite identity/effective-window
    evidence; the queryable construction-window conclusion is recomputed from
    the saved raw noise declaration, work revision and fixed option scheme.
    """
    tables={name for (name,) in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    required=("noise_plan","noise_source","noise_decision",
              "noise_decision_window","noise_decision_blocker")
    if not all(name in tables for name in required):
        return {"sources":[],"plans":[],"decisions":[],"verification":None}
    sources=[dict(x) for x in db.execute(
        "SELECT id,source_id,revision,digest FROM noise_source ORDER BY id")]
    plans=[]
    for p in db.execute("SELECT * FROM noise_plan ORDER BY plan_id"):
        item=dict(p)
        item["plan"]=json.loads(item.pop("plan_json"))
        item["receptors"]=json.loads(item.pop("receptors_json"))
        plans.append(item)
    decisions=[]
    for d in db.execute("SELECT * FROM noise_decision ORDER BY id"):
        windows=[dict(x) for x in db.execute(
            "SELECT receptor,slice_from,slice_until,in_quiet_window,sources_json,"
            "energy,unrounded_level_db,level_db,limit_db,compliant,reasons_json FROM "
            "noise_decision_window WHERE decision_id=? ORDER BY receptor,slice_from",
            (d["id"],))]
        for w in windows:
            w["sources"]=json.loads(w.pop("sources_json"))
            w["reasons"]=json.loads(w.pop("reasons_json"))
            w["in_quiet_window"]=bool(w["in_quiet_window"])
            w["unrounded_level_db"]=float(w["unrounded_level_db"])
            w["compliant"]=None if w["compliant"] is None else bool(w["compliant"])
        blockers=[dict(x) for x in db.execute(
            "SELECT code,detail_json FROM noise_decision_blocker WHERE decision_id=?",
            (d["id"],))]
        for b in blockers:
            b["detail"]=json.loads(b.pop("detail_json"))
        decisions.append({"decision_id":d["id"],"plan_id":d["plan_id"],
            "session_id":d["session_id"],"source_id":d["source_id"],
            "revision":d["revision"],"state":d["state"],
            "work_state":d["work_state"],"notice_state":d["notice_state"],
            "completion_at":d["completion_at"],"superseded":bool(d["superseded"]),
            "supersede_reason":d["supersede_reason"],
            "measures":json.loads(d["measures_json"]),
            "source_summary":json.loads(d["source_summary_json"]),
            "blockers":blockers,"windows":windows})
    verification=noise_verify(db)
    return {"sources":sources,"plans":plans,"decisions":decisions,
            "verification":verification}

