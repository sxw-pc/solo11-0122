"""Road works, route feasibility and rider notice rules."""
from datetime import timedelta
from .source import utc
TABLES={"notice":"work_notice","revision":"work_revision","case":"journey_case",
"assessment":"route_assessment","obligation":"notice_obligation","receipt":"delivery_receipt"}
DEMO={"adopt":"road_normal.json","assess":"route_normal.json","settle":"notice_normal.json"}
EXTRA_TABLES=["detour_pool","detour_hold","spoil_seal","spoil_result"]
def validate_notice(doc):
    p=doc["payload"]
    if p.get("authority") not in ("city","district"):return "invalid","unknown_authority"
    if p.get("action")=="cancel":return "cancelled","work_permit_cancelled"
    if p.get("action")!="close" or not p.get("segment") or p.get("lanes_closed",0)<0:
        return "invalid","invalid_work_zone"
    return "active","valid_work_zone"
def assess_case(doc,p):
    f=doc["facts"];route=f.get("route",[]);segment=p["segment"]
    if segment not in route:return {"state":"unaffected","outcome":"route_clear",
        "detail":{"segment":segment,"reason":"outside_route"},"obligations":[]}
    flow=f.get("flow",0);remaining=p.get("total_lanes",0)-p.get("lanes_closed",0)
    detour=f.get("detour",{});available=detour.get("capacity",0)>=flow and bool(detour.get("id"))
    if flow<1:return {"state":"uncertain","outcome":"flow_unknown","detail":{"segment":segment},"obligations":[]}
    if remaining>0 and p.get("remaining_capacity",0)>=flow:
        state,outcome="constrained","lane_reduction"
    elif available:state,outcome="rerouted","detour_required"
    else:state,outcome="blocked","no_viable_detour"
    deadline=(utc(doc["at"])+timedelta(hours=2)).isoformat()
    audiences=sorted(set(f.get("audiences",[])))
    obligations=[{"target":x,"deadline":deadline,"required":1,
                  "meta":{"route_id":detour.get("id") if available else "closure","segment":segment}} for x in audiences]
    return {"state":state,"outcome":outcome,
      "detail":{"segment":segment,"flow":flow,"remaining_lanes":remaining,"detour_available":available,
                "audiences":audiences},"obligations":obligations}
def receipt_verdict(r,ob,version):
    import json
    meta=json.loads(ob["meta"])
    if r["version"]!=version:return "invalid","stale_route_version"
    if r.get("route_id")!=meta["route_id"]:return "invalid","wrong_route_message"
    if r["amount"]!=1:return "invalid","audience_receipt_count"
    return "valid","delivered_for_current_route"

