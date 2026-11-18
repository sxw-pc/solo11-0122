"""Road works, route feasibility and rider notice rules."""
from datetime import timedelta
from .source import utc
TABLES={"notice":"work_notice","revision":"work_revision","case":"journey_case",
"assessment":"route_assessment","obligation":"notice_obligation","receipt":"delivery_receipt"}
DEMO={"adopt":"road_normal.json","assess":"route_normal.json","settle":"notice_normal.json"}
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


# ---------------------------------------------------------------------------
# Night construction noise: energy-based level engine and permission windows.
# Kept separate from the road rules above: an adopted road notice is only an
# identity/effective-window prerequisite, never an acoustic permission.
# ---------------------------------------------------------------------------
from decimal import Decimal, ROUND_HALF_UP


def to_energy(level_db):
    """Convert a dB(A) level to linear acoustic energy (10**(L/10))."""
    return 10.0 ** (float(level_db) / 10.0)


def to_level(energy):
    """Convert linear energy back to an unrounded dB(A) level."""
    import math
    return 10.0 * math.log10(energy)


def round_db(level):
    """Display rounding only: 0.01 dB, half up. Never used for limit tests."""
    return str(Decimal(str(level)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def evaluate_noise_plan(session, plan, work):
    """Pure decision engine over frozen evidence and one fixed full scheme.

    ``session`` is the recorded raw noise document (source, receptors,
    activities with shift/barrier options). ``plan`` is the contractor's
    single fixed option selection per activity. ``work`` is the adopted work
    revision row (or None). Returns receptors' half-open slices with linear
    energy totals, the proven completion instant, blockers and findings.
    """
    from .source import utc
    blockers = []
    findings = []

    def block(code, **detail):
        blockers.append({"code": code, "detail": detail})

    def find(code, **detail):
        findings.append({"code": code, "detail": detail})

    # ---- work revision identity / effective window (prerequisite only) ----
    if work is None:
        block("work_revision_missing",
              source_id=session["source_id"], revision=session["revision"])
        return {"blockers": blockers, "findings": findings,
                "receptors": [], "completion_at": None}
    if work.get("state") == "conflict":
        block("work_revision_conflict", source_id=session["source_id"],
              revision=session["revision"])
    elif work.get("state") in ("cancelled", "invalid"):
        block("work_revision_not_active", source_id=session["source_id"],
              revision=session["revision"], state=work.get("state"))
    elif work.get("state") == "superseded":
        block("work_revision_superseded", source_id=session["source_id"],
              revision=session["revision"])
    work_from = utc(work["effective_from"])
    work_until = utc(work["effective_until"])

    receptors = {r["id"]: dict(r) for r in session["receptors"]}
    activities = {a["id"]: dict(a) for a in session["activities"]}
    seen_activities = set()
    selections = {}
    for sel in plan.get("selections", []):
        aid = sel.get("activity_id")
        if aid in seen_activities:
            block("plan_duplicate_activity_selection", activity_id=aid)
        seen_activities.add(aid)
        selections[aid] = sel

    # ---- validate the fixed option selections and build actual runs -------
    runs = []  # activity actually running on a receptor during [start, end)
    for aid, activity in activities.items():
        receptor = activity["receptor"]
        if receptor not in receptors:
            block("receptor_outside_plan_contract", activity_id=aid,
                  receptor=receptor)
            continue
        sel = selections.get(aid, {"activity_id": aid})
        option = sel.get("option")
        shift_opts = {o["id"]: o for o in activity.get("shift_options", [])}
        barrier_opts = {o["id"]: o for o in activity.get("barrier_options", [])}
        if sel.get("shift_id") is not None and sel.get("barrier_id") is not None:
            block("conflicting_mitigation_options", activity_id=aid,
                  shift_id=sel["shift_id"], barrier_id=sel["barrier_id"])
            continue
        if option is not None and option not in shift_opts \
                and option not in barrier_opts:
            block("unknown_mitigation_option", activity_id=aid, option=option)
            continue
        shift_id = sel.get("shift_id")
        barrier_id = sel.get("barrier_id")
        if option in shift_opts and shift_id is None:
            shift_id = option
        if option in barrier_opts and barrier_id is None:
            barrier_id = option
        if shift_id is not None and shift_id not in shift_opts:
            block("missing_shift_evidence", activity_id=aid, shift_id=shift_id)
            continue
        if barrier_id is not None and barrier_id not in barrier_opts:
            block("missing_barrier_evidence", activity_id=aid,
                  barrier_id=barrier_id)
            continue
        if shift_id is not None:
            opt = shift_opts[shift_id]
            run_start, run_end = utc(opt["starts"]), utc(opt["ends"])
            find("shift_applied", activity_id=aid, receptor=receptor,
                 option_id=shift_id,
                 window=[opt["starts"], opt["ends"]],
                 baseline_window=[activity["starts"], activity["ends"]])
        else:
            run_start, run_end = utc(activity["starts"]), utc(activity["ends"])
        if not (work_from <= run_start and run_end <= work_until):
            block("shift_outside_work_window", activity_id=aid,
                  receptor=receptor,
                  run_window=[_iso(run_start), _iso(run_end)],
                  work_window=[work["effective_from"], work["effective_until"]])
        # mitigation must act on the receptor it was declared for
        target = sel.get("receptor", receptor)
        wrong_receptor = target != receptor
        if wrong_receptor:
            block("measure_wrong_receptor", activity_id=aid,
                  declared_receptor=target, activity_receptor=receptor)
        barrier = None
        if barrier_id is not None and not wrong_receptor:
            opt = barrier_opts[barrier_id]
            attenuation = float(opt["attenuation_db"])
            if attenuation < 0:
                block("negative_attenuation", activity_id=aid,
                      barrier_id=barrier_id, attenuation_db=opt["attenuation_db"])
                # a negative "attenuation" is not evidence of reduction:
                # keep the raw source with no measure instead of amplifying it
            else:
                b_start, b_end = utc(opt["starts"]), utc(opt["ends"])
                barrier = {"id": barrier_id, "start": b_start, "end": b_end,
                           "attenuation": attenuation}
                # coverage of the actual run: midnight-crossing gaps vs same-day gaps
                if not (b_start <= run_start and run_end <= b_end):
                    cross_midnight = (b_end.date() != b_start.date()
                                      or run_end.date() != run_start.date())
                    if cross_midnight:
                        block("midnight_gap", activity_id=aid, receptor=receptor,
                              barrier_id=barrier_id,
                              barrier_window=[_iso(b_start), _iso(b_end)],
                              run_window=[_iso(run_start), _iso(run_end)])
                    else:
                        block("barrier_coverage_gap", activity_id=aid,
                              receptor=receptor, barrier_id=barrier_id,
                              barrier_window=[_iso(b_start), _iso(b_end)],
                              run_window=[_iso(run_start), _iso(run_end)])
                    find("partial_barrier_coverage", activity_id=aid,
                         receptor=receptor, barrier_id=barrier_id)
                else:
                    find("barrier_applied", activity_id=aid, receptor=receptor,
                         option_id=barrier_id, attenuation_db=opt["attenuation_db"],
                         window=[opt["starts"], opt["ends"]])
        runs.append({"activity_id": aid, "receptor": receptor,
                     "start": run_start, "end": run_end,
                     "base_level": float(activity["level_db"]),
                     "barrier": barrier})

    # referenced activities that have no source record at all
    for sel in plan.get("selections", []):
        if sel.get("activity_id") not in activities:
            block("missing_activity_source", activity_id=sel.get("activity_id"))

    # ---- half-open slicing per receptor, linear energy combination --------
    receptor_results = []
    for rid, receptor in receptors.items():
        quiet_from = utc(receptor["quiet_from"])
        quiet_until = utc(receptor["quiet_until"])
        limit = float(receptor["limit_db"])
        mine = [r for r in runs if r["receptor"] == rid]
        boundaries = {quiet_from, quiet_until}
        for r in mine:
            boundaries.add(r["start"])
            boundaries.add(r["end"])
            if r["barrier"]:
                boundaries.add(r["barrier"]["start"])
                boundaries.add(r["barrier"]["end"])
        ordered = sorted(boundaries)
        slices = []
        for left, right in zip(ordered, ordered[1:]):
            active = []
            for r in mine:
                if r["start"] <= left and right <= r["end"]:
                    level = r["base_level"]
                    barrier_used = None
                    if r["barrier"] and r["barrier"]["start"] <= left \
                            and right <= r["barrier"]["end"]:
                        level = level - r["barrier"]["attenuation"]
                        barrier_used = r["barrier"]["id"]
                    active.append({"activity_id": r["activity_id"],
                                   "level_db": round_db(level),
                                   "barrier_id": barrier_used,
                                   "energy": to_energy(level)})
            if not active:
                continue
            energy = sum(a["energy"] for a in active)  # linear sum, unrounded
            level = to_level(energy)
            in_quiet = quiet_from <= left and right <= quiet_until
            reasons = []
            compliant = None
            if in_quiet:
                # compare unrounded energy against the limit energy, never dB
                if energy > to_energy(limit) + 1e-12:
                    compliant = False
                    reasons.append("limit_exceeded")
                    block("limit_exceeded", receptor=rid,
                          slice=[_iso(left), _iso(right)],
                          level_db=round_db(level), limit_db=round_db(limit),
                          sources=[a["activity_id"] for a in active],
                          combined_energy=energy)
                else:
                    compliant = True
                    reasons.append("within_limit")
            else:
                reasons.append("outside_quiet_window_no_limit_inferred")
                find("no_limit_outside_quiet_window", receptor=rid,
                     slice=[_iso(left), _iso(right)])
            slices.append({"receptor": rid, "start": _iso(left),
                           "end": _iso(right), "in_quiet_window": in_quiet,
                           "sources": [{"activity_id": a["activity_id"],
                                        "level_db": a["level_db"],
                                        "barrier_id": a["barrier_id"]}
                                       for a in active],
                           "energy": energy,
                           "unrounded_level_db": level,
                           "level_db": round_db(level),
                           "limit_db": round_db(limit) if in_quiet else None,
                           "compliant": compliant, "reasons": reasons})
        receptor_results.append({"receptor": rid,
                                 "quiet_window": [receptor["quiet_from"],
                                                  receptor["quiet_until"]],
                                 "limit_db": round_db(limit), "slices": slices})

    completion = max((r["end"] for r in runs), default=None)
    return {"blockers": blockers, "findings": findings,
            "receptors": receptor_results,
            "completion_at": _iso(completion) if completion else None}

