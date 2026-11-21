"""Road works, route feasibility and rider notice rules."""
from datetime import timedelta
from .source import utc
TABLES={"notice":"work_notice","revision":"work_revision","case":"journey_case",
"assessment":"route_assessment","obligation":"notice_obligation","receipt":"delivery_receipt"}
DEMO={"adopt":"road_normal.json","assess":"route_normal.json","settle":"notice_normal.json"}

# A certificate must state how simultaneous public orders combine.  The relation is
# part of the certificate identity and can never be supplied later by a report view.
RELATIONS=("independent_orders","superseding_city")

def evidence_kind(payload):
    """Classify the lane evidence carried by a close notice.

    ``exact_lanes`` names the lanes an order actually closes.  ``count_within_scope``
    states only how many lanes inside the authority's scope are closed.  Legacy
    payloads only carry ``lanes_closed`` and therefore cannot be attributed to lanes.
    """
    has_exact="exact_lanes" in payload
    has_count="count_within_scope" in payload
    if has_exact and has_count:return "ambiguous"
    if has_exact:return "exact"
    if has_count:return "count"
    if "lanes_closed" in payload:return "legacy"
    return "none"

def _non_negative_int(value):
    return isinstance(value,int) and not isinstance(value,bool) and value>=0

def validate_notice(doc):
    p=doc["payload"]
    if p.get("authority") not in ("city","district"):return "invalid","unknown_authority"
    if p.get("action")=="cancel":return "cancelled","work_permit_cancelled"
    if p.get("action")!="close" or not p.get("segment"):
        return "invalid","invalid_work_zone"
    kind=evidence_kind(p)
    if kind=="ambiguous":return "invalid","ambiguous_evidence"
    if kind=="exact":
        values=p["exact_lanes"]
        if not isinstance(values,list) or any(not isinstance(x,str) or not x for x in values):
            return "invalid","invalid_exact_lanes"
    elif kind=="count":
        if not _non_negative_int(p["count_within_scope"]):
            return "invalid","invalid_count_within_scope"
    elif kind=="legacy":
        if not _non_negative_int(p.get("lanes_closed",0)):
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

