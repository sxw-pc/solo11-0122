"""Evidence-facing report reconstructed from durable entities.

The excavation section independently recomputes per-utility clearances
from the frozen source polylines, depth ranges, uncertainties and method
envelopes — never from the stored ruling — so forged shortest distances,
borrowed unproven pothole uncertainties and omitted utilities surface as
discrepancies.
"""
import json
import math
from pathlib import Path
from .store import connect
from .policy import TABLES,EXTRA_TABLES

# Permit strings restated here on purpose: the report must not trust the
# ruling module it audits.
_PERMIT={"full_excavation":"allowed_full_excavation","exploratory_pothole":"allowed_to_probe"}

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
    excavations=_excavation_section(db)
    db.close()
    result={"notices":notices,"cases":cases,"source_evidence":evidence,"source_conflicts":conflicts,
            "auxiliary":auxiliary,"excavations":excavations}
    if out:
        target=Path(out);target.parent.mkdir(parents=True,exist_ok=True)
        temp=target.with_name(target.name+".pending")
        temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        temp.replace(target)
    return result

# ---------------------------------------------------------------------------
# Independent recomputation of excavation geometry (separate implementation
# from roadcase.utilities on purpose: agreement of two kernels is the check).
# ---------------------------------------------------------------------------

def _point_to_segment(px,py,ax,ay,bx,by):
    dx,dy=bx-ax,by-ay
    length2=dx*dx+dy*dy
    if length2==0.0:
        return math.hypot(px-ax,py-ay)
    t=((px-ax)*dx+(py-ay)*dy)/length2
    t=0.0 if t<0.0 else (1.0 if t>1.0 else t)
    return math.hypot(px-(ax+t*dx),py-(ay+t*dy))

def _segments_touch(p1,p2,q1,q2):
    def orient(a,b,c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    d1,d2=orient(q1,q2,p1),orient(q1,q2,p2)
    d3,d4=orient(p1,p2,q1),orient(p1,p2,q2)
    if (d1>0)!=(d2>0) and (d3>0)!=(d4>0):
        return True
    def on_segment(a,b,c):
        return (min(a[0],b[0])<=c[0]<=max(a[0],b[0])
                and min(a[1],b[1])<=c[1]<=max(a[1],b[1]))
    return ((d1==0 and on_segment(q1,q2,p1)) or (d2==0 and on_segment(q1,q2,p2))
            or (d3==0 and on_segment(p1,p2,q1)) or (d4==0 and on_segment(p1,p2,q2)))

def _polyline_plan_distance(line_a,line_b):
    best=None
    for i in range(len(line_a)-1):
        for j in range(len(line_b)-1):
            p1,p2=line_a[i],line_a[i+1]
            q1,q2=line_b[j],line_b[j+1]
            if _segments_touch(p1,p2,q1,q2):
                distance=0.0
            else:
                distance=min(
                    _point_to_segment(p1[0],p1[1],q1[0],q1[1],q2[0],q2[1]),
                    _point_to_segment(p2[0],p2[1],q1[0],q1[1],q2[0],q2[1]),
                    _point_to_segment(q1[0],q1[1],p1[0],p1[1],p2[0],p2[1]),
                    _point_to_segment(q2[0],q2[1],p1[0],p1[1],p2[0],p2[1]))
            if best is None or distance<best:
                best=distance
    return best

def _expected_geometry(db,plan_input):
    """Recompute per-method clearances straight from frozen source rows."""
    needed=("utility_plan_source","utility_observation","excavation_alignment","excavation_method")
    if not all(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone()
               for name in needed):
        return None
    alignment=db.execute("SELECT * FROM excavation_alignment WHERE plan_id=? AND alignment_id=?",
                         (plan_input["utility_plan_id"],plan_input["alignment_id"])).fetchone()
    if alignment is None:
        return None
    trench=json.loads(alignment["polyline"])
    utilities={}
    for source_id in plan_input["survey_sources"]:
        for row in db.execute("SELECT * FROM utility_observation WHERE plan_id=?",(source_id,)):
            utilities[row["utility_id"]]=row
    methods={}
    for method_id in plan_input["methods"]:
        row=db.execute("SELECT body FROM excavation_method WHERE plan_id=? AND method_id=?",
                       (plan_input["utility_plan_id"],method_id)).fetchone()
        if row:
            methods[method_id]=json.loads(row["body"])
    result={}
    for method_id,method in methods.items():
        half=method["trench_width"]/2.0
        per_utility={}
        for utility_id,utility in utilities.items():
            line=json.loads(utility["polyline"])
            distance=_polyline_plan_distance(trench,line)
            h_clear=distance-(half+utility["outer_radius"]+utility["horizontal_uncertainty"]
                              +method["horizontal_error"])
            v_gap=max(0.0,max(alignment["depth_low"],utility["depth_low"])
                      -min(alignment["depth_high"],utility["depth_high"]))
            v_clear=v_gap-(utility["outer_radius"]+utility["vertical_uncertainty"]
                           +method["vertical_error"])
            clearance=math.hypot(max(0.0,h_clear),max(0.0,v_clear))
            per_utility[utility_id]={
                "plan_distance":round(distance,9),
                "clearance_3d":round(clearance,9),
                "horizontal_uncertainty":utility["horizontal_uncertainty"],
                "half_width":half,
            }
        constraining=[u for u,v in per_utility.items() if v["clearance_3d"]<method["required_clearance"]]
        operation=method.get("operation")
        permit="blocked" if constraining else _PERMIT.get(operation,"unsupported")
        result[method_id]={"permit":permit,"operation":operation,"utilities":per_utility}
    return result

def _compare_decision(stored,expected):
    discrepancies=[]
    if expected is None:
        return discrepancies
    stored_methods={m["method_id"]:m for m in stored.get("methods",[])}
    for method_id,want in expected.items():
        got=stored_methods.get(method_id)
        if got is None:
            discrepancies.append({"type":"method_omitted","method":method_id})
            continue
        got_utils={u["utility_id"]:u for u in got.get("utilities",[])}
        for utility_id,want_u in want["utilities"].items():
            got_u=got_utils.get(utility_id)
            if got_u is None:
                discrepancies.append({"type":"utility_omitted","method":method_id,"utility":utility_id})
                continue
            if abs(got_u.get("plan_distance",float("nan"))-want_u["plan_distance"])>1e-6:
                discrepancies.append({"type":"forged_plan_distance","method":method_id,"utility":utility_id,
                                      "stored":got_u.get("plan_distance"),"recomputed":want_u["plan_distance"]})
            if abs(got_u.get("clearance_3d",float("nan"))-want_u["clearance_3d"])>1e-6:
                discrepancies.append({"type":"forged_clearance","method":method_id,"utility":utility_id,
                                      "stored":got_u.get("clearance_3d"),"recomputed":want_u["clearance_3d"]})
            deductions=got_u.get("deductions",{})
            if abs(deductions.get("horizontal_uncertainty",float("nan"))-want_u["horizontal_uncertainty"])>1e-9:
                discrepancies.append({"type":"unproven_uncertainty","method":method_id,"utility":utility_id,
                                      "stored":deductions.get("horizontal_uncertainty"),
                                      "recorded":want_u["horizontal_uncertainty"]})
            if abs(deductions.get("half_width",float("nan"))-want_u["half_width"])>1e-9:
                discrepancies.append({"type":"envelope_mismatch","method":method_id,"utility":utility_id,
                                      "stored":deductions.get("half_width"),"recorded":want_u["half_width"]})
        for utility_id in got_utils:
            if utility_id not in want["utilities"]:
                discrepancies.append({"type":"utility_unexpected","method":method_id,"utility":utility_id})
        if got.get("permit")!=want["permit"]:
            discrepancies.append({"type":"permit_mismatch","method":method_id,
                                  "stored":got.get("permit"),"recomputed":want["permit"]})
        scope=got.get("scope",{})
        if scope.get("operation")!=want["operation"]:
            discrepancies.append({"type":"permit_scope_violation","method":method_id,
                                  "stored":scope.get("operation"),"recorded":want["operation"]})
    return discrepancies

def _excavation_section(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='excavation_plan'").fetchone():
        return []
    plans=[]
    for plan in db.execute("SELECT * FROM excavation_plan ORDER BY excavation_id"):
        plan_input=json.loads(plan["input"])
        notice=db.execute(f"SELECT current_revision FROM {TABLES['notice']} WHERE id=?",
                          (plan_input["work"]["source_id"],)).fetchone()
        entry={"excavation_id":plan["excavation_id"],
               "utility_plan_id":plan_input["utility_plan_id"],
               "alignment_id":plan_input["alignment_id"],
               "survey_sources":plan_input["survey_sources"],
               "work":plan_input["work"],
               "evidence_cutoff":plan_input["evidence_cutoff"],
               "current_work_revision":notice["current_revision"] if notice else None,
               "decisions":[]}
        expected=_expected_geometry(db,plan_input)
        for decision in db.execute("SELECT * FROM excavation_decision WHERE excavation_id=? ORDER BY seq",
                                   (plan["excavation_id"],)):
            stored=json.loads(decision["detail"])
            discrepancies=_compare_decision(stored,expected) if decision["state"]=="decided" else []
            entry["decisions"].append({"seq":decision["seq"],"state":decision["state"],
                                       "decided_at":decision["decided_at"],
                                       "discrepancies":discrepancies})
        plans.append(entry)
    return plans
