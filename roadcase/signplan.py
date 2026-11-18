"""Temporary detour sign placement adjudication.

The sign intake in :mod:`roadcase.signs` freezes geometry evidence.  This
module answers a separate visual question: for every approach lane, can a
driver read the chosen sign over one *continuous* interval that begins early
enough before the detour decision point?

The engine is deterministic and does not use fixed coarse sampling.  Every
readability boundary on a lane segment is produced analytically:

* the sign facing half-angle defines a planar cone expressed as two
  half-planes, clipped against each straight polyline segment;
* each obstacle whose top exceeds the driver eye plane casts a convex
  perspective shadow: the four top corners are projected from the display
  point onto the eye plane (``E = S + k(C - S)`` with
  ``k = (sign_z - eye_z) / (sign_z - top_height)``).  An eye inside that
  quadrilateral sees the sign across the obstacle top.  A low guardrail with
  a top at or below the eye plane casts no shadow, so it can never be
  mistaken for an occlusion by its flat projection;
* cone and shadow polygons clip each lane segment with exact half-plane
  arithmetic, so tangent, zero-width contacts never count as occlusion and
  arbitrarily narrow obstacles are never skipped.
"""

import hashlib
import json
import math

from .source import canonical
from .store import connect

ENGINE_VERSION = "signplan-1.0.0"

# Deterministic tolerances, all in the frozen local metre coordinate system.
EPS = 1e-9          # generic predicate slack (normalised quantities)
EPS_PLANAR = 1e-6   # metre slack for point-on-line / decision-point projection
EPS_ARC = 1e-6      # metre slack for merging adjacent arc intervals
ROUND = 6           # output rounding; keeps certificates checkable yet stable


class SignPlanError(ValueError):
    """Invalid plan request (distinct from an adjudicated ``pending`` lane)."""


# ---------------------------------------------------------------------------
# small 2D vector helpers
# ---------------------------------------------------------------------------

def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def _scale(k, v):
    return (k * v[0], k * v[1])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _norm(v):
    return math.hypot(v[0], v[1])


def _point_eq(a, b):
    return _norm(_sub(a, b)) <= EPS_PLANAR


def _rotate(v, angle):
    c, s = math.cos(angle), math.sin(angle)
    return (c * v[0] - s * v[1], s * v[0] + c * v[1])


# ---------------------------------------------------------------------------
# exact segment clipping against ordered left-side half-plane rays
# ---------------------------------------------------------------------------

def _clip_halfplanes(p0, p1, edges):
    """Liang-Barsky clipping of segment p0->p1 against left-side half-planes.

    Each edge ``(a, b)`` admits points ``p`` with ``cross(b-a, p-a) >= 0``.
    Returns the parameter interval ``(t0, t1)`` with positive width, or
    ``None`` for an empty / tangent contact.
    """
    lo, hi = 0.0, 1.0
    for a, b in edges:
        edge = _sub(b, a)
        f0 = _cross(edge, _sub(p0, a))
        f1 = _cross(edge, _sub(p1, a))
        if f0 >= -EPS_PLANAR and f1 >= -EPS_PLANAR:
            continue  # both inside
        if f0 <= EPS_PLANAR and f1 <= EPS_PLANAR:
            return None  # both outside (or both grazing)
        t = f0 / (f0 - f1)
        if f0 < -EPS_PLANAR:
            lo = max(lo, t)      # p0 outside: crossing is the new entry
        else:
            hi = min(hi, t)      # p1 outside: crossing is the new exit
        if lo > hi + EPS_PLANAR:
            return None
    if hi - lo <= EPS_PLANAR:
        return None
    return (lo, hi)


def _cone_edges(origin, axis, half_angle):
    """Readable cone as two left-side half-plane rays rooted at the sign.

    ``axis`` is the sign's unit facing normal, pointing toward approaching
    drivers.  The lower ray is reversed when stored so both interiors are
    left-side half-planes.
    """
    upper = _rotate(axis, half_angle)
    lower = _rotate(axis, -half_angle)
    return [
        (origin, _add(origin, lower)),
        (_add(origin, upper), origin),
    ]


# ---------------------------------------------------------------------------
# eye-plane shadow of an obstacle (perspective projection of its top)
# ---------------------------------------------------------------------------

def _shadow_region(sign_xy, sign_z, eye_z, box, top_height):
    """Half-plane edges describing an obstacle's eye-plane occlusion region.

    The sign display point ``S`` is the projection centre and the eye plane
    sits below it.  Two regimes follow from the vertical sight profile:

    * ``top_height >= sign_z`` (obstacle at least as tall as the sign): the
      display point cannot see over the top.  The planar occlusion region is
      the unbounded wedge swept by rays from ``S`` through the rectangle: its
      two boundary rays pass through the two rectangle corners that are
      tangent as seen from ``S``, and the near edge closes it on the sign
      side so only points beyond the obstacle qualify.
    * ``eye_z < top_height < sign_z``: the top is visible from above; its
      four corners project to a bounded convex quadrilateral on the eye
      plane via similar triangles
      (``E = S + u(C - S)``,
      ``u = (sign_z - eye_z)/(sign_z - top_height) > 1``), extending past
      the obstacle toward the approaching drivers.

    A region whose top is at or below the eye plane (a low guardrail) casts
    no shadow at all and returns ``None``, so flat projection alone never
    flags it as an occlusion.  The returned edges are ordered for
    :func:`_clip_halfplanes` (interior on the left side).
    """
    if top_height <= eye_z + EPS:
        return None
    x0, y0, x1, y1 = box
    s = sign_xy
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if top_height + EPS >= sign_z:
        # Find the two tangent rays from S to the rectangle without relying on
        # polar-angle extrema (which break across the +/-pi branch cut).  A
        # corner is tangent when the whole rectangle lies on one side of the
        # ray S->corner.
        tangents = []
        for c in corners:
            ray = _sub(c, s)
            sides = {1 if _cross(ray, _sub(other, s)) >= -EPS_PLANAR else -1
                     for other in corners if not _point_eq(other, c)}
            if len(sides) == 1:
                tangents.append((c, sides.pop()))
        if len(tangents) != 2:
            return None
        (ca, side_a), (cb, side_b) = tangents
        # Encode each tangent as a left-side half-plane facing the wedge
        # interior (reverse the ray when the rectangle is on its right).
        edge_a = (s, ca) if side_a > 0 else (ca, s)
        edge_b = (s, cb) if side_b > 0 else (cb, s)
        # Near edge: keep only points beyond the obstacle, away from S.
        mid_near = _scale(0.5, _add(ca, cb))
        near_edge = _sub(cb, ca)
        away = _sub(mid_near, s)
        if _cross(near_edge, away) < 0.0:
            near_a, near_b = cb, ca
        else:
            near_a, near_b = ca, cb
        return [edge_a, edge_b, (near_a, near_b)]
    u = (sign_z - eye_z) / (sign_z - top_height)
    projected = [_add(s, _scale(u, _sub(c, s))) for c in corners]
    hull = _convex_hull(projected)
    return [(hull[i], hull[(i + 1) % len(hull)]) for i in range(len(hull))]


def _convex_hull(points):
    """Andrew monotone-chain hull; input points are nearly-convex quads."""
    ordered = sorted(set((round(p[0], 12), round(p[1], 12)) for p in points))
    if len(ordered) <= 1:
        return list(ordered)

    def cross_o(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in ordered:
        while len(lower) >= 2 and cross_o(lower[-2], lower[-1], p) < -EPS_PLANAR:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(ordered):
        while len(upper) >= 2 and cross_o(upper[-2], upper[-1], p) < -EPS_PLANAR:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


# ---------------------------------------------------------------------------
# polyline validation and arc-length bookkeeping
# ---------------------------------------------------------------------------

def _clean_polyline(polyline):
    """Drop repeated consecutive points; return [] for unusable geometry."""
    cleaned = []
    for point in polyline:
        if not (isinstance(point, list) and len(point) == 2
                and _finite(point[0]) and _finite(point[1])):
            return []
        p = (float(point[0]), float(point[1]))
        if not cleaned or not _point_eq(cleaned[-1], p):
            cleaned.append(p)
    return cleaned if len(cleaned) >= 2 else []


def _segment_table(polyline):
    """Cumulative arc length at every vertex plus per-segment geometry."""
    arcs = [0.0]
    segs = []
    for a, b in zip(polyline, polyline[1:]):
        d = _sub(b, a)
        length = _norm(d)
        if length <= EPS_PLANAR:
            return None
        segs.append((a, b, d, _scale(1.0 / length, d), length))
        arcs.append(arcs[-1] + length)
    return arcs, segs


def _project_decision(segs, decision):
    """Arc-length position of the decision point on the polyline.

    The decision point must lie on the polyline (end points included); a
    nearby point is admitted only within ``EPS_PLANAR``.
    """
    best = None
    base = 0.0
    for a, b, d, _unit, length in segs:
        t = _dot(_sub(decision, a), d) / (length * length)
        if -EPS_PLANAR / length <= t <= 1.0 + EPS_PLANAR / length:
            tc = min(1.0, max(0.0, t))
            q = _add(a, _scale(tc, d))
            gap = _norm(_sub(decision, q))
            if gap <= EPS_PLANAR and (best is None or gap < best[0]):
                best = (gap, base + tc * length)
        base += length
    return None if best is None else best[1]


# ---------------------------------------------------------------------------
# interval arithmetic on the arc-length axis
# ---------------------------------------------------------------------------

def _merge(intervals):
    if not intervals:
        return []
    out = [list(ordered) for ordered in sorted(intervals)[:1]]
    for lo, hi in sorted(intervals)[1:]:
        if lo <= out[-1][1] + EPS_ARC:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def _subtract(a_intervals, b_intervals):
    """Set difference of two merged interval lists (positive-width output)."""
    result = []
    for alo, ahi in a_intervals:
        cur_lo = alo
        for blo, bhi in b_intervals:
            if bhi <= cur_lo + EPS_ARC:
                continue
            if blo >= ahi - EPS_ARC:
                break
            if blo > cur_lo + EPS_ARC:
                result.append((cur_lo, min(ahi, blo)))
            cur_lo = max(cur_lo, bhi)
            if cur_lo >= ahi - EPS_ARC:
                break
        if cur_lo < ahi - EPS_ARC:
            result.append((cur_lo, ahi))
    return _merge([(lo, hi) for lo, hi in result if hi - lo > EPS_ARC])


# ---------------------------------------------------------------------------
# per-segment / per-lane adjudication
# ---------------------------------------------------------------------------

def _blocked_arc_intervals(seg_index, a, b, d, length, arcs, sign, eye_z, obstacles):
    """Occluded arc intervals on one straight lane segment.

    Every obstacle whose top exceeds the eye plane produces a convex set of
    left-side half-plane edges (an unbounded wedge or a bounded shadow
    quadrilateral, see :func:`_shadow_region`).  Clipping the lane segment
    against those edges gives exact entry/exit parameters; no sampling is
    involved, so arbitrarily narrow shadows are never missed and tangent
    (zero-width) contacts are ignored.
    """
    sign_xy, sign_z = sign["position"], sign["height"]
    intervals = []
    boundary_sources = {}
    for obstacle in obstacles:
        edges = _shadow_region(sign_xy, sign_z, eye_z,
                               obstacle["box"], obstacle["height"])
        if not edges:
            continue
        clipped = _clip_halfplanes(a, b, edges)
        if clipped is None:
            continue
        t0, t1 = clipped
        s0, s1 = arcs[seg_index] + t0 * length, arcs[seg_index] + t1 * length
        if s1 - s0 <= EPS_ARC:
            continue
        intervals.append((s0, s1))
        boundary_sources[round(s0, ROUND)] = "obstacle:" + obstacle["id"]
        boundary_sources[round(s1, ROUND)] = "obstacle:" + obstacle["id"]
    return _merge(intervals), boundary_sources


def _readable_analysis(polyline, arcs, segs, sign, obstacles, eye_height):
    facing = sign["facing"]
    f_len = _norm(facing)
    if f_len <= EPS_PLANAR:
        return None, "bad_facing"
    axis = _scale(1.0 / f_len, facing)
    half_angle = math.radians(sign["half_fov_degrees"])
    if not (0.0 < half_angle < math.pi / 2.0 - EPS):
        return None, "bad_fov"
    edges = _cone_edges(sign["position"], axis, half_angle)

    fov_intervals, fov_starts = [], set()
    blocked_intervals, blocker_sources = [], {}
    for i, (a, b, d, _unit, length) in enumerate(segs):
        cone = _clip_halfplanes(a, b, edges)
        if cone is not None:
            t0, t1 = cone
            s0, s1 = arcs[i] + t0 * length, arcs[i] + t1 * length
            if s1 - s0 > EPS_ARC:
                fov_intervals.append((s0, s1))
                fov_starts.add(round(s0, ROUND))
        seg_blocked, sources = _blocked_arc_intervals(
            i, a, b, d, length, arcs, sign, eye_height, obstacles)
        blocked_intervals.extend(seg_blocked)
        blocker_sources.update(sources)

    fov_intervals = _merge(fov_intervals)
    blocked_intervals = _merge(blocked_intervals)
    readable = _subtract(fov_intervals, blocked_intervals)
    return {
        "fov": fov_intervals,
        "blocked": blocked_intervals,
        "readable": readable,
        "fov_starts": fov_starts,
        "blocker_sources": blocker_sources,
    }, None


def _limiter_for(arc_start, analysis):
    """Explain why the last continuous readable interval starts here."""
    key = round(arc_start, ROUND)
    if key in analysis["blocker_sources"]:
        return analysis["blocker_sources"][key]
    if key in analysis["fov_starts"]:
        return "fov"
    return "polyline_start"


def _round_interval(interval):
    return [round(interval[0], ROUND), round(interval[1], ROUND)]


def adjudicate_lane(lane, sign, obstacles):
    """Pure geometry adjudication for one approach lane.

    Returns a certificate body whose ``verdict`` is ``pass``, ``fail`` or
    ``pending``.  A pending lane never defaults to visible.
    """
    polyline = _clean_polyline(lane.get("polyline", []))
    if not polyline:
        return _pending(lane, "empty_polyline")
    table = _segment_table(polyline)
    if table is None:
        return _pending(lane, "invalid_coordinates")
    arcs, segs = table

    decision = lane.get("decision_point")
    if not (isinstance(decision, list) and len(decision) == 2
            and _finite(decision[0]) and _finite(decision[1])):
        return _pending(lane, "missing_decision_point")
    decision = (float(decision[0]), float(decision[1]))
    decision_arc = _project_decision(segs, decision)
    if decision_arc is None:
        return _pending(lane, "decision_off_polyline")

    eye_height = lane.get("eye_height")
    if not _finite(eye_height) or float(eye_height) <= 0.0:
        return _pending(lane, "zero_eye_height")
    eye_height = float(eye_height)

    analysis, error = _readable_analysis(polyline, arcs, segs, sign, obstacles, eye_height)
    if error:
        return _pending(lane, error)

    before = [(lo, hi) for lo, hi in analysis["readable"] if lo < decision_arc - EPS_ARC]
    last_interval = max(before, key=lambda item: item[0], default=None)
    body = {
        "lane_id": lane["id"],
        "verdict": "pending",
        "arc_length_total": round(arcs[-1], ROUND),
        "decision_arc": round(decision_arc, ROUND),
        "fov_intervals": [_round_interval(x) for x in analysis["fov"]
                          if x[0] < decision_arc - EPS_ARC],
        "blocked_intervals": [_round_interval(x) for x in analysis["blocked"]
                              if x[0] < decision_arc - EPS_ARC],
        "readable_intervals": [_round_interval(x) for x in before],
        "last_readable_interval": None,
        "warning_distance": None,
        "min_warning_distance": sign["min_warning_distance"],
        "limiter": None,
        "reason": None,
    }
    if last_interval is None:
        body["reason"] = "no_continuous_readable_interval"
        return body
    warning = decision_arc - last_interval[0]
    body["last_readable_interval"] = _round_interval(last_interval)
    body["warning_distance"] = round(warning, ROUND)
    body["limiter"] = _limiter_for(last_interval[0], analysis)
    if warning + EPS >= sign["min_warning_distance"]:
        body["verdict"] = "pass"
    else:
        body["verdict"] = "fail"
        body["reason"] = "warning_distance_below_class_threshold"
    return body


def _pending(lane, reason):
    return {
        "lane_id": lane.get("id"), "verdict": "pending", "reason": reason,
        "arc_length_total": None, "decision_arc": None, "fov_intervals": [],
        "blocked_intervals": [], "readable_intervals": [],
        "last_readable_interval": None, "warning_distance": None,
        "min_warning_distance": None, "limiter": None,
    }


# ---------------------------------------------------------------------------
# frozen evidence loading and durable plan identity
# ---------------------------------------------------------------------------

PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS sign_plan(
 plan_id TEXT PRIMARY KEY, survey_id TEXT NOT NULL, sign_id TEXT NOT NULL,
 class_id TEXT NOT NULL, survey_digest TEXT NOT NULL, work_source_id TEXT NOT NULL,
 work_revision INTEGER NOT NULL, work_state TEXT NOT NULL, lanes_json TEXT NOT NULL,
 coordinate_system TEXT NOT NULL, contract_json TEXT NOT NULL, state TEXT NOT NULL,
 covered_lanes_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS sign_lane_certificate(
 plan_id TEXT NOT NULL REFERENCES sign_plan(plan_id), lane_id TEXT NOT NULL,
 verdict TEXT NOT NULL, body_json TEXT NOT NULL, PRIMARY KEY(plan_id,lane_id));
CREATE TABLE IF NOT EXISTS sign_verify_record(
 verify_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, ok INTEGER NOT NULL,
 mismatches_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""


def _load_survey(db, survey_id):
    row = db.execute("SELECT * FROM sign_survey_source WHERE survey_id=?", (survey_id,)).fetchone()
    if row is None:
        return None
    doc = json.loads(row["raw"])
    lanes = []
    for approach in db.execute(
            "SELECT * FROM sign_approach WHERE survey_id=? ORDER BY lane_id", (survey_id,)):
        lanes.append({
            "id": approach["lane_id"],
            "polyline": json.loads(approach["polyline"]),
            "decision_point": json.loads(approach["decision_point"]),
            "eye_height": approach["eye_height"],
        })
    parsed_signs = []
    for sign in db.execute("SELECT * FROM temporary_sign WHERE survey_id=? ORDER BY sign_id", (survey_id,)):
        parsed_signs.append({
            "id": sign["sign_id"],
            "position": json.loads(sign["position"]),
            "facing": json.loads(sign["facing"]),
            "height": sign["height"],
            "classes": json.loads(sign["class_rules"]),
        })
    obstacles = [{
        "id": obstacle["obstacle_id"],
        "box": json.loads(obstacle["box"]),
        "height": obstacle["height"],
    } for obstacle in db.execute(
        "SELECT * FROM sign_obstacle WHERE survey_id=? ORDER BY obstacle_id", (survey_id,))]
    return {"row": dict(row), "doc": doc, "lanes": lanes,
            "signs": parsed_signs, "obstacles": obstacles}


def _work_state(db, source_id, revision):
    notice = db.execute("SELECT * FROM work_notice WHERE id=?", (source_id,)).fetchone()
    if notice is None:
        return None, "work_revision_missing"
    if notice["state"] == "conflict":
        return None, "work_version_conflict"
    rev = db.execute(
        "SELECT state FROM work_revision WHERE source_id=? AND revision=?",
        (source_id, revision)).fetchone()
    if rev is None:
        return None, "work_revision_missing"
    if notice["current_revision"] != revision:
        return None, "work_revision_superseded"
    return rev["state"], None


def _plan_identity(survey_digest, sign_id, class_id, lane_ids, contract,
                   work_source_id, work_revision, work_state):
    material = canonical({
        "survey_digest": survey_digest, "sign_id": sign_id, "class_id": class_id,
        "lanes": lane_ids, "contract": contract,
        "work_source_id": work_source_id, "work_revision": work_revision,
        "work_state": work_state,
    })
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _build_contract(survey_doc, spec):
    return {
        "engine": ENGINE_VERSION,
        "coordinate_system": survey_doc.get("coordinate_system"),
        "units": "metres",
        "half_fov_degrees": spec["half_fov_degrees"],
        "min_warning_distance": spec["min_warning_distance"],
        "threshold_rule": "warning_distance >= min_warning_distance (equality passes)",
        "occlusion_rule": "obstacle top projected as a convex shadow on the driver "
                          "eye plane; eye inside shadow is occluded; an obstacle top at "
                          "or below the eye plane casts no shadow; tangent contact never occludes",
        "interval_rule": "exact half-plane clipping of lane segments against cone and "
                         "shadow polygons, no coarse sampling",
        "eps_planar": EPS_PLANAR, "eps_arc": EPS_ARC, "round": ROUND,
    }


def decide(db_path, survey_id, sign_id, class_id, lane_ids=None):
    """Create (or replay) a frozen sign placement plan and lane certificates."""
    db = connect(db_path)
    db.executescript(PLAN_SCHEMA)
    try:
        survey = _load_survey(db, survey_id)
        if survey is None:
            raise SignPlanError("survey_not_found")
        sign = next((x for x in survey["signs"] if x["id"] == sign_id), None)
        if sign is None:
            raise SignPlanError("sign_not_found")
        spec = next((x for x in sign["classes"] if x["id"] == class_id), None)
        if spec is None:
            raise SignPlanError("unknown_class")

        all_lane_ids = [lane["id"] for lane in survey["lanes"]]
        chosen = sorted(lane_ids) if lane_ids else all_lane_ids
        if any(lane_id not in all_lane_ids for lane_id in chosen):
            raise SignPlanError("lane_not_in_survey")
        lanes = [lane for lane in survey["lanes"] if lane["id"] in chosen]

        row = survey["row"]
        work_state, work_error = _work_state(db, row["work_source_id"], row["work_revision"])
        if work_error:
            work_state = work_error
        contract = _build_contract(survey["doc"], spec)
        # The identity binds the frozen evidence, class, lanes, the work
        # revision it references AND the current adjudicated work state.  When
        # a newer revision or a conflict changes that state, a fresh, distinct
        # plan is produced instead of silently inheriting old certificates.
        plan_id = _plan_identity(row["digest"], sign_id, class_id, chosen, contract,
                                 row["work_source_id"], row["work_revision"], work_state)
        if db.execute("SELECT 1 FROM sign_plan WHERE plan_id=?", (plan_id,)).fetchone():
            return _describe_plan(db, plan_id, replayed=True)

        sign_view = {
            "position": tuple(sign["position"]), "facing": tuple(sign["facing"]),
            "height": float(sign["height"]),
            "half_fov_degrees": float(spec["half_fov_degrees"]),
            "min_warning_distance": float(spec["min_warning_distance"]),
        }
        if work_error:
            certificates = []
            for lane in lanes:
                body = _pending(lane, work_error)
                body["min_warning_distance"] = spec["min_warning_distance"]
                certificates.append(body)
        else:
            certificates = [adjudicate_lane(lane, sign_view, survey["obstacles"]) for lane in lanes]

        verdicts = {body["lane_id"]: body["verdict"] for body in certificates}
        covered = sorted(lane_id for lane_id, verdict in verdicts.items() if verdict == "pass")
        if work_error or any(verdict == "pending" for verdict in verdicts.values()):
            state = "pending"
        elif covered == chosen:
            state = "full_coverage"
        else:
            state = "partial_coverage"

        with db:
            db.execute(
                "INSERT INTO sign_plan(plan_id,survey_id,sign_id,class_id,survey_digest,"
                "work_source_id,work_revision,work_state,lanes_json,coordinate_system,"
                "contract_json,state,covered_lanes_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, survey_id, sign_id, class_id, row["digest"],
                 row["work_source_id"], row["work_revision"], work_state,
                 canonical(chosen), survey["doc"].get("coordinate_system"),
                 canonical(contract), state, canonical(covered)))
            for body in certificates:
                db.execute("INSERT INTO sign_lane_certificate VALUES (?,?,?,?)",
                           (plan_id, body["lane_id"], body["verdict"], canonical(body)))
        return _describe_plan(db, plan_id, replayed=False)
    finally:
        db.close()


def _describe_plan(db, plan_id, replayed):
    plan = db.execute("SELECT * FROM sign_plan WHERE plan_id=?", (plan_id,)).fetchone()
    certificates = [json.loads(row["body_json"]) for row in db.execute(
        "SELECT body_json FROM sign_lane_certificate WHERE plan_id=? ORDER BY lane_id", (plan_id,))]
    return {
        "plan_id": plan_id, "survey_id": plan["survey_id"], "sign_id": plan["sign_id"],
        "class_id": plan["class_id"],
        "survey_digest": plan["survey_digest"],
        "work": {"source_id": plan["work_source_id"], "revision": plan["work_revision"],
                 "state": plan["work_state"]},
        "lanes": json.loads(plan["lanes_json"]),
        "coordinate_system": plan["coordinate_system"],
        "contract": json.loads(plan["contract_json"]),
        "state": plan["state"],
        "covered_lanes": json.loads(plan["covered_lanes_json"]),
        "certificates": certificates, "replayed": replayed,
    }


# ---------------------------------------------------------------------------
# independent verification from the frozen raw evidence
# ---------------------------------------------------------------------------

def _recompute(db, plan, survey):
    sign = next((x for x in survey["signs"] if x["id"] == plan["sign_id"]), None)
    if sign is None:
        return None, "sign_not_found"
    spec = next((x for x in sign["classes"] if x["id"] == plan["class_id"]), None)
    if spec is None:
        return None, "unknown_class"
    lane_ids = json.loads(plan["lanes_json"])
    _work, work_error = _work_state(db, plan["work_source_id"], plan["work_revision"])
    sign_view = {
        "position": tuple(sign["position"]), "facing": tuple(sign["facing"]),
        "height": float(sign["height"]),
        "half_fov_degrees": float(spec["half_fov_degrees"]),
        "min_warning_distance": float(spec["min_warning_distance"]),
    }
    recomputed = {}
    for lane in survey["lanes"]:
        if lane["id"] not in lane_ids:
            continue
        if work_error:
            body = _pending(lane, work_error)
            body["min_warning_distance"] = spec["min_warning_distance"]
        else:
            body = adjudicate_lane(lane, sign_view, survey["obstacles"])
        recomputed[lane["id"]] = body
    return recomputed, work_error


def verify(db_path, plan_id):
    """Recompute every certificate from frozen raw rows and detect tampering."""
    db = connect(db_path)
    db.executescript(PLAN_SCHEMA)
    try:
        plan = db.execute("SELECT * FROM sign_plan WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise SignPlanError("plan_not_found")
        mismatches = []
        survey = _load_survey(db, plan["survey_id"])
        if survey is None:
            mismatches.append("survey_not_found")
        elif survey["row"]["digest"] != plan["survey_digest"]:
            mismatches.append("survey_digest_changed")
        else:
            recomputed, work_error = _recompute(db, plan, survey)
            if isinstance(recomputed, dict):
                stored = {row["lane_id"]: json.loads(row["body_json"])
                          for row in db.execute(
                              "SELECT * FROM sign_lane_certificate WHERE plan_id=?", (plan_id,))}
                if set(stored) != set(recomputed):
                    mismatches.append("lane_set_changed")
                for lane_id, body in recomputed.items():
                    if lane_id in stored and canonical(stored[lane_id]) != canonical(body):
                        mismatches.append("certificate_forged:" + lane_id)
                covered = sorted(lane_id for lane_id, body in recomputed.items()
                                 if body["verdict"] == "pass")
                if canonical(covered) != plan["covered_lanes_json"]:
                    mismatches.append("covered_lanes_forged")
                expected = _expected_state(work_error, recomputed, json.loads(plan["lanes_json"]))
                if expected != plan["state"]:
                    mismatches.append("plan_state_forged")
            else:
                mismatches.append(recomputed)
        verify_id = hashlib.sha256(
            canonical({"plan_id": plan_id, "mismatches": mismatches}).encode("utf-8")).hexdigest()
        with db:
            db.execute(
                "INSERT OR REPLACE INTO sign_verify_record"
                "(verify_id,plan_id,ok,mismatches_json,created_at) "
                "VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
                (verify_id, plan_id, 0 if mismatches else 1, canonical(mismatches)))
        return {"verify_id": verify_id, "plan_id": plan_id,
                "ok": not mismatches, "mismatches": mismatches}
    finally:
        db.close()


def _expected_state(work_error, recomputed, lane_ids):
    if work_error or any(body["verdict"] == "pending" for body in recomputed.values()):
        return "pending"
    covered = sorted(lane_id for lane_id, body in recomputed.items() if body["verdict"] == "pass")
    return "full_coverage" if covered == lane_ids else "partial_coverage"
