"""Subsurface survey intake and conservative excavation clearance rulings.

record() only ingests synthetic survey and method evidence; it never
certifies geometric clearance.  plan_excavation() freezes an excavation
plan against a work-zone revision, and decide_excavation() computes, per
utility, the conservative three-dimensional clearance between the trench
candidate and each utility envelope, issuing a per-method permit that is
valid only for the method's declared operation, alignment and depth
range.  A potholing permit never upgrades into a full machine excavation
permit, and recorded surveys or past rulings are never overwritten: new
pothole evidence or a new work-zone revision can only produce a new plan
or a new ruling row.
"""

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from .policy import TABLES
from .source import read, digest as doc_digest, canonical, utc
from .store import connect


EXPECTED_COORDINATE_SYSTEM = 'synthetic-local-metres'

# A permit is scoped to the method's declared operation; an operation we
# do not know cannot be certified and goes to pending review instead.
ALLOWED_PERMIT = {
    'full_excavation': 'allowed_full_excavation',
    'exploratory_pothole': 'allowed_to_probe',
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS utility_plan_source (
 plan_id TEXT PRIMARY KEY, work_source_id TEXT NOT NULL, work_revision INTEGER NOT NULL,
 segment TEXT NOT NULL, digest TEXT NOT NULL, raw BLOB NOT NULL,
 coordinate_system TEXT NOT NULL DEFAULT 'unknown',
 recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS excavation_alignment (
 plan_id TEXT NOT NULL REFERENCES utility_plan_source(plan_id),
 alignment_id TEXT NOT NULL, polyline TEXT NOT NULL,
 depth_low REAL NOT NULL, depth_high REAL NOT NULL,
 PRIMARY KEY(plan_id,alignment_id));
CREATE TABLE IF NOT EXISTS utility_observation (
 plan_id TEXT NOT NULL REFERENCES utility_plan_source(plan_id),
 utility_id TEXT NOT NULL, polyline TEXT NOT NULL,
 depth_low REAL NOT NULL, depth_high REAL NOT NULL,
 outer_radius REAL NOT NULL, horizontal_uncertainty REAL NOT NULL,
 vertical_uncertainty REAL NOT NULL,
 PRIMARY KEY(plan_id,utility_id));
CREATE TABLE IF NOT EXISTS excavation_method (
 plan_id TEXT NOT NULL REFERENCES utility_plan_source(plan_id),
 method_id TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(plan_id,method_id));
"""


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('utility_bad_number')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('utility_bad_number')
    return number


def _line(points):
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError('utility_bad_polyline')
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError('utility_bad_polyline')
        _number(point[0]); _number(point[1])


def record(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw)
    plan_id = doc['plan_id']
    work = doc['work']
    alignments = doc['alignments']
    utilities = doc['utilities']
    methods = doc['methods']
    if not plan_id or not alignments or not utilities or not methods:
        raise ValueError('utility_empty_plan')
    for alignment in alignments:
        _line(alignment['polyline'])
        if _number(alignment['depth_low']) >= _number(alignment['depth_high']):
            raise ValueError('utility_bad_depth')
    for utility in utilities:
        _line(utility['polyline'])
        if _number(utility['depth_low']) >= _number(utility['depth_high']):
            raise ValueError('utility_bad_depth')
        for key in ('outer_radius','horizontal_uncertainty','vertical_uncertainty'):
            if _number(utility[key]) < 0:
                raise ValueError('utility_bad_uncertainty')
    for method in methods:
        for key in ('trench_width','horizontal_error','vertical_error','required_clearance'):
            if _number(method[key]) < 0:
                raise ValueError('utility_bad_method')
    coordinate_system = doc.get('coordinate_system')
    if not isinstance(coordinate_system, str) or not coordinate_system:
        coordinate_system = 'unknown'
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        revision = db.execute('SELECT payload FROM work_revision WHERE source_id=? AND revision=?',
                              (work['source_id'], work['revision'])).fetchone()
        if not revision:
            raise ValueError('utility_unknown_work_revision')
        if json.loads(revision['payload']).get('segment') != work['segment']:
            raise ValueError('utility_work_segment_mismatch')
        prior = db.execute('SELECT digest FROM utility_plan_source WHERE plan_id=?',
                           (plan_id,)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('utility_plan_conflict')
            return 0
        with db:
            db.execute('INSERT INTO utility_plan_source'
                       '(plan_id,work_source_id,work_revision,segment,digest,raw,coordinate_system)'
                       ' VALUES (?,?,?,?,?,?,?)',
                       (plan_id, work['source_id'], work['revision'], work['segment'],
                        digest, raw, coordinate_system))
            for alignment in alignments:
                db.execute('INSERT INTO excavation_alignment VALUES (?,?,?,?,?)',
                           (plan_id, alignment['id'], json.dumps(alignment['polyline']),
                            alignment['depth_low'], alignment['depth_high']))
            for utility in utilities:
                db.execute('INSERT INTO utility_observation VALUES (?,?,?,?,?,?,?,?)',
                           (plan_id, utility['id'], json.dumps(utility['polyline']),
                            utility['depth_low'], utility['depth_high'],
                            utility['outer_radius'], utility['horizontal_uncertainty'],
                            utility['vertical_uncertainty']))
            for method in methods:
                db.execute('INSERT INTO excavation_method VALUES (?,?,?)',
                           (plan_id, method['id'], json.dumps(method,sort_keys=True)))
        return len(utilities)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Deterministic 2D geometry kernel over finite polylines.
# ---------------------------------------------------------------------------

def _round(value):
    return round(float(value), 9)


def _as_point(point):
    return (float(point[0]), float(point[1]))


def _valid_polyline(points):
    if not isinstance(points, list) or len(points) < 2:
        return False
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            return False
        for coordinate in point:
            if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
                return False
            if not math.isfinite(coordinate):
                return False
    return True


def _point_segment(point, a, b):
    """Distance from a point to a finite segment and the closest point."""
    ax, ay = a
    bx, by = b
    px, py = point
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0.0:
        return math.hypot(px - ax, py - ay), a
    t = ((px - ax) * dx + (py - ay) * dy) / length2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    closest = (ax + t * dx, ay + t * dy)
    return math.hypot(px - closest[0], py - closest[1]), closest


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segment_distance(p1, p2, q1, q2):
    """Minimum distance between two finite 2D segments plus witness points.

    Zero-length segments degenerate to points.  Proper crossings and
    collinear overlaps return distance zero with a deterministic witness;
    otherwise the best of the four endpoint-to-segment candidates wins,
    with ties broken by a fixed candidate order.
    """
    if p1 == p2 and q1 == q2:
        return math.hypot(p1[0] - q1[0], p1[1] - q1[1]), p1, q1
    if p1 == p2:
        distance, closest = _point_segment(p1, q1, q2)
        return distance, p1, closest
    if q1 == q2:
        distance, closest = _point_segment(q1, p1, p2)
        return distance, closest, q1
    d1 = _orient(q1, q2, p1)
    d2 = _orient(q1, q2, p2)
    d3 = _orient(p1, p2, q1)
    d4 = _orient(p1, p2, q2)
    if d1 == 0.0 and d2 == 0.0 and d3 == 0.0 and d4 == 0.0:
        # Collinear: measure the overlap on the dominant axis.
        axis = 0 if abs(p2[0] - p1[0]) >= abs(p2[1] - p1[1]) else 1
        span = p2[axis] - p1[axis]

        def parameter(point):
            return 0.0 if span == 0.0 else (point[axis] - p1[axis]) / span

        low = max(0.0, min(parameter(q1), parameter(q2)))
        high = min(1.0, max(parameter(q1), parameter(q2)))
        if low <= high:
            t = (low + high) / 2.0
            point = (p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))
            return 0.0, point, point
    elif (d1 != 0.0 and d2 != 0.0 and d3 != 0.0 and d4 != 0.0
          and (d1 > 0.0) != (d2 > 0.0) and (d3 > 0.0) != (d4 > 0.0)):
        # Proper crossing: deterministic intersection point.
        t = d1 / (d1 - d2)
        point = (p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))
        return 0.0, point, point
    candidates = []
    distance, closest = _point_segment(p1, q1, q2)
    candidates.append((distance, p1, closest))
    distance, closest = _point_segment(p2, q1, q2)
    candidates.append((distance, p2, closest))
    distance, closest = _point_segment(q1, p1, p2)
    candidates.append((distance, closest, q1))
    distance, closest = _point_segment(q2, p1, p2)
    candidates.append((distance, closest, q2))
    best = candidates[0]
    for candidate in candidates[1:]:
        if candidate[0] < best[0]:
            best = candidate
    return best


def _polyline_distance(line_a, line_b):
    """Minimum plan distance between two finite polylines with witness.

    Returns (distance, point_on_a, point_on_b, segment_index_a,
    segment_index_b); ties keep the first segment pair in iteration order.
    """
    best = None
    for i in range(len(line_a) - 1):
        for j in range(len(line_b) - 1):
            distance, point_a, point_b = _segment_distance(
                _as_point(line_a[i]), _as_point(line_a[i + 1]),
                _as_point(line_b[j]), _as_point(line_b[j + 1]))
            if best is None or distance < best[0]:
                best = (distance, point_a, point_b, i, j)
    return best


def _interval_gap(a_low, a_high, b_low, b_high):
    """Vertical gap between two depth intervals; touching counts as zero."""
    return max(0.0, max(a_low, b_low) - min(a_high, b_high))


def _evaluate_method(alignment, utilities, method):
    """Conservative per-utility 3D clearance for one method envelope.

    The trench envelope shrinks the plan distance by the method half
    width, the utility outer radius, the survey horizontal uncertainty
    and the machine horizontal error; the depth-interval gap shrinks by
    the outer radius, the vertical survey uncertainty and the machine
    vertical error.  The 3D clearance is the Euclidean combination of
    the positive axis gaps, so a deep utility crossed in plan is not a
    collision.  Nominal centreline distance is never reported as safe.
    """
    half_width = method['trench_width'] / 2.0
    required = method['required_clearance']
    raw_entries = []
    for utility in utilities:
        distance, trench_point, utility_point, trench_seg, utility_seg = _polyline_distance(
            alignment['polyline'], utility['polyline'])
        horizontal_clearance = distance - (half_width + utility['outer_radius']
                                           + utility['horizontal_uncertainty']
                                           + method['horizontal_error'])
        vertical_gap = _interval_gap(alignment['depth_low'], alignment['depth_high'],
                                     utility['depth_low'], utility['depth_high'])
        vertical_clearance = vertical_gap - (utility['outer_radius']
                                             + utility['vertical_uncertainty']
                                             + method['vertical_error'])
        clearance_3d = math.hypot(max(0.0, horizontal_clearance),
                                  max(0.0, vertical_clearance))
        raw_entries.append({
            'utility_id': utility['utility_id'],
            'distance': distance,
            'trench_point': trench_point,
            'utility_point': utility_point,
            'trench_segment': trench_seg,
            'utility_segment': utility_seg,
            'vertical_gap': vertical_gap,
            'horizontal_clearance': horizontal_clearance,
            'vertical_clearance': vertical_clearance,
            'clearance_3d': clearance_3d,
            'utility': utility,
        })
    limiting = min(entry['clearance_3d'] for entry in raw_entries)
    entries = []
    for entry in raw_entries:
        utility = entry['utility']
        entries.append({
            'utility_id': entry['utility_id'],
            'plan_distance': _round(entry['distance']),
            'witness': {
                'trench_point': [_round(entry['trench_point'][0]), _round(entry['trench_point'][1])],
                'utility_point': [_round(entry['utility_point'][0]), _round(entry['utility_point'][1])],
                'trench_segment': entry['trench_segment'],
                'utility_segment': entry['utility_segment'],
            },
            'vertical_gap': _round(entry['vertical_gap']),
            'deductions': {
                'half_width': _round(half_width),
                'outer_radius': _round(utility['outer_radius']),
                'horizontal_uncertainty': _round(utility['horizontal_uncertainty']),
                'machine_horizontal_error': _round(method['horizontal_error']),
                'vertical_uncertainty': _round(utility['vertical_uncertainty']),
                'machine_vertical_error': _round(method['vertical_error']),
            },
            'horizontal_clearance': _round(entry['horizontal_clearance']),
            'vertical_clearance': _round(entry['vertical_clearance']),
            'clearance_3d': _round(entry['clearance_3d']),
            'constrains': entry['clearance_3d'] < required,
        })
    operation = method['operation']
    blocked = any(entry['clearance_3d'] < required for entry in raw_entries)
    return {
        'method_id': method['method_id'],
        'operation': operation,
        'permit': 'blocked' if blocked else ALLOWED_PERMIT[operation],
        'required_clearance': _round(required),
        'limiting_clearance': _round(limiting),
        'limiting_utilities': sorted(entry['utility_id'] for entry in raw_entries
                                     if entry['clearance_3d'] == limiting),
        'scope': {
            'operation': operation,
            'alignment_id': alignment['alignment_id'],
            'depth_low': _round(alignment['depth_low']),
            'depth_high': _round(alignment['depth_high']),
        },
        'utilities': entries,
    }


# ---------------------------------------------------------------------------
# Excavation plans and rulings.
# ---------------------------------------------------------------------------

def _parse_recorded_at(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _compute_decision(db, plan_row, moment):
    """Evaluate a frozen plan against current evidence state.

    Returns (state, detail).  Any evidence problem produces 'pending'
    with deterministic reasons and no clearance numbers at all, so a
    nominal centreline distance can never masquerade as a safe clearance.
    """
    plan_input = json.loads(plan_row['input'])
    work = plan_input['work']
    source_ids = plan_input['survey_sources']
    reasons = []

    def flag(reason):
        if reason not in reasons:
            reasons.append(reason)

    sources = []
    for source_id in source_ids:
        row = db.execute('SELECT * FROM utility_plan_source WHERE plan_id=?',
                         (source_id,)).fetchone()
        if row is None:
            flag('survey_source_unknown')
        else:
            sources.append(row)
    declared_system = plan_input.get('coordinate_system')
    cutoff = utc(plan_input['evidence_cutoff'])
    for source in sources:
        if source['coordinate_system'] != EXPECTED_COORDINATE_SYSTEM:
            flag('coordinate_system_unverified')
        if declared_system is not None and source['coordinate_system'] != declared_system:
            flag('coordinate_system_unverified')
        if (source['work_source_id'], source['work_revision']) != (work['source_id'], work['revision']):
            flag('work_revision_mismatch')
        if _parse_recorded_at(source['recorded_at']) > cutoff:
            flag('evidence_after_cutoff')

    geometries = {}
    for source in sources:
        rows = db.execute('SELECT * FROM utility_observation WHERE plan_id=? ORDER BY utility_id',
                          (source['plan_id'],)).fetchall()
        if not rows:
            flag('survey_source_unknown')
        for row in rows:
            geometry = (row['polyline'], row['depth_low'], row['depth_high'],
                        row['outer_radius'], row['horizontal_uncertainty'],
                        row['vertical_uncertainty'])
            known = geometries.get(row['utility_id'])
            if known is not None and known != geometry:
                flag('conflicting_surveys')
            else:
                geometries[row['utility_id']] = geometry

    utility_list = []
    for utility_id in sorted(geometries):
        polyline_text, depth_low, depth_high, outer_radius, h_unc, v_unc = geometries[utility_id]
        polyline = json.loads(polyline_text)
        numbers = (depth_low, depth_high, outer_radius, h_unc, v_unc)
        if not _valid_polyline(polyline):
            flag('incomplete_polyline')
        elif not all(math.isfinite(number) for number in numbers):
            flag('invalid_envelope')
        elif depth_low >= depth_high or min(outer_radius, h_unc, v_unc) < 0.0:
            flag('invalid_envelope')
        else:
            utility_list.append({
                'utility_id': utility_id,
                'polyline': polyline,
                'depth_low': depth_low,
                'depth_high': depth_high,
                'outer_radius': outer_radius,
                'horizontal_uncertainty': h_unc,
                'vertical_uncertainty': v_unc,
            })

    alignment = None
    alignment_row = db.execute('SELECT * FROM excavation_alignment WHERE plan_id=? AND alignment_id=?',
                               (plan_input['utility_plan_id'], plan_input['alignment_id'])).fetchone()
    if alignment_row is None:
        flag('alignment_unknown')
    else:
        polyline = json.loads(alignment_row['polyline'])
        if not _valid_polyline(polyline):
            flag('incomplete_polyline')
        elif not (math.isfinite(alignment_row['depth_low'])
                  and math.isfinite(alignment_row['depth_high'])
                  and alignment_row['depth_low'] < alignment_row['depth_high']):
            flag('invalid_envelope')
        else:
            alignment = {
                'alignment_id': alignment_row['alignment_id'],
                'polyline': polyline,
                'depth_low': alignment_row['depth_low'],
                'depth_high': alignment_row['depth_high'],
            }

    method_list = []
    for method_id in plan_input['methods']:
        row = db.execute('SELECT body FROM excavation_method WHERE plan_id=? AND method_id=?',
                         (plan_input['utility_plan_id'], method_id)).fetchone()
        if row is None:
            flag('method_unknown')
            continue
        body = json.loads(row['body'])
        numbers = [body.get('trench_width'), body.get('horizontal_error'),
                   body.get('vertical_error'), body.get('required_clearance')]
        if (not all(isinstance(number, (int, float)) and not isinstance(number, bool)
                    and math.isfinite(number) for number in numbers)
                or min(numbers) < 0.0):
            flag('invalid_envelope')
            continue
        operation = body.get('operation')
        if operation not in ALLOWED_PERMIT:
            flag('operation_unsupported')
            continue
        method_list.append({
            'method_id': method_id,
            'operation': operation,
            'trench_width': float(body['trench_width']),
            'horizontal_error': float(body['horizontal_error']),
            'vertical_error': float(body['vertical_error']),
            'required_clearance': float(body['required_clearance']),
        })

    tables = TABLES
    notice = db.execute(f"SELECT * FROM {tables['notice']} WHERE id=?",
                        (work['source_id'],)).fetchone()
    if notice is None or notice['state'] == 'conflict':
        flag('work_source_unresolved')
    elif notice['current_revision'] != work['revision']:
        flag('work_revision_superseded')
    else:
        revision = db.execute(f"SELECT * FROM {tables['revision']} WHERE source_id=? AND revision=?",
                              (work['source_id'], work['revision'])).fetchone()
        if revision is None:
            flag('work_source_unresolved')
        elif revision['state'] != 'active':
            flag('work_not_active')
        elif not (utc(revision['effective_from']) <= moment < utc(revision['effective_until'])):
            flag('outside_work_window')

    detail = {
        'utility_plan_id': plan_input['utility_plan_id'],
        'alignment_id': plan_input['alignment_id'],
        'survey_sources': list(source_ids),
        'work': {'source_id': work['source_id'], 'revision': work['revision']},
        'coordinate_system': (sources[0]['coordinate_system'] if sources
                              else declared_system or 'unknown'),
        'evidence_cutoff': plan_input['evidence_cutoff'],
        'pending_reasons': reasons,
        'methods': [],
    }
    if reasons:
        return 'pending', detail
    detail['methods'] = [_evaluate_method(alignment, utility_list, method)
                         for method in method_list]
    return 'decided', detail


def plan_excavation(db_path, input_path):
    """Freeze an excavation plan: candidate line, methods, sources, cutoff."""
    doc = read(input_path)
    for key in ('excavation_id', 'utility_plan_id', 'alignment_id', 'methods',
                'work', 'evidence_cutoff'):
        if key not in doc:
            raise ValueError('excavation_plan_missing_' + key)
    if not isinstance(doc['excavation_id'], str) or not doc['excavation_id']:
        raise ValueError('excavation_plan_bad_id')
    if not isinstance(doc['utility_plan_id'], str) or not doc['utility_plan_id']:
        raise ValueError('excavation_plan_bad_survey')
    if not isinstance(doc['alignment_id'], str) or not doc['alignment_id']:
        raise ValueError('excavation_plan_bad_alignment')
    methods = doc['methods']
    if (not isinstance(methods, list) or not methods
            or not all(isinstance(method, str) and method for method in methods)):
        raise ValueError('excavation_plan_bad_methods')
    methods = list(dict.fromkeys(methods))
    work = doc['work']
    if (not isinstance(work, dict) or not isinstance(work.get('source_id'), str)
            or not work['source_id'] or isinstance(work.get('revision'), bool)
            or not isinstance(work.get('revision'), int) or work['revision'] < 1):
        raise ValueError('excavation_plan_bad_work')
    utc(doc['evidence_cutoff'])
    requested = doc.get('survey_sources')
    if requested is None:
        requested = []
    if (not isinstance(requested, list)
            or not all(isinstance(source, str) and source for source in requested)):
        raise ValueError('excavation_plan_bad_sources')
    sources = [doc['utility_plan_id']] + [source for source in requested
                                          if source != doc['utility_plan_id']]
    sources = list(dict.fromkeys(sources))
    coordinate_system = doc.get('coordinate_system')
    if coordinate_system is not None and (not isinstance(coordinate_system, str)
                                          or not coordinate_system):
        raise ValueError('excavation_plan_bad_coordinate_system')
    normalized = {
        'excavation_id': doc['excavation_id'],
        'utility_plan_id': doc['utility_plan_id'],
        'alignment_id': doc['alignment_id'],
        'methods': methods,
        'survey_sources': sources,
        'work': {'source_id': work['source_id'], 'revision': work['revision']},
        'evidence_cutoff': doc['evidence_cutoff'],
    }
    if coordinate_system is not None:
        normalized['coordinate_system'] = coordinate_system
    fingerprint = doc_digest(normalized)
    db = connect(db_path)
    db.execute('BEGIN IMMEDIATE')
    try:
        existing = db.execute('SELECT * FROM excavation_plan WHERE excavation_id=?',
                              (normalized['excavation_id'],)).fetchone()
        if existing:
            if existing['digest'] != fingerprint:
                raise ValueError('excavation_plan_conflict')
            result = dict(json.loads(existing['input']))
            result['registered'] = False
            db.commit()
            return result
        survey = db.execute('SELECT 1 FROM utility_plan_source WHERE plan_id=?',
                            (normalized['utility_plan_id'],)).fetchone()
        if survey is None:
            raise ValueError('utility_plan_unknown')
        db.execute('INSERT INTO excavation_plan'
                   '(excavation_id,utility_plan_id,alignment_id,input,digest) VALUES (?,?,?,?,?)',
                   (normalized['excavation_id'], normalized['utility_plan_id'],
                    normalized['alignment_id'], canonical(normalized), fingerprint))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    result = dict(normalized)
    result['registered'] = True
    return result


def _describe_decision(row):
    return {
        'excavation_id': row['excavation_id'],
        'seq': row['seq'],
        'state': row['state'],
        'decided_at': row['decided_at'],
        'detail': json.loads(row['detail']),
    }


def decide_excavation(db_path, input_path):
    """Compute and append a ruling for a frozen plan.

    Re-deciding an unchanged plan returns the stored ruling; changed
    evidence (for example a new work-zone revision) appends a new row
    and never overwrites the historical ruling.
    """
    doc = read(input_path)
    for key in ('excavation_id', 'at'):
        if key not in doc:
            raise ValueError('excavation_decide_missing_' + key)
    moment = utc(doc['at'])
    db = connect(db_path)
    db.execute('BEGIN IMMEDIATE')
    try:
        plan = db.execute('SELECT * FROM excavation_plan WHERE excavation_id=?',
                          (doc['excavation_id'],)).fetchone()
        if plan is None:
            raise ValueError('excavation_plan_unknown')
        state, detail = _compute_decision(db, plan, moment)
        latest = db.execute('SELECT * FROM excavation_decision WHERE excavation_id=?'
                            ' ORDER BY seq DESC LIMIT 1', (doc['excavation_id'],)).fetchone()
        if latest and latest['state'] == state and json.loads(latest['detail']) == detail:
            result = _describe_decision(latest)
            db.commit()
            return result
        seq = latest['seq'] + 1 if latest else 1
        db.execute('INSERT INTO excavation_decision(excavation_id,seq,decided_at,state,detail)'
                   ' VALUES (?,?,?,?,?)',
                   (doc['excavation_id'], seq, moment.isoformat(), state, canonical(detail)))
        row = db.execute('SELECT * FROM excavation_decision WHERE excavation_id=? AND seq=?',
                         (doc['excavation_id'], seq)).fetchone()
        result = _describe_decision(row)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return result


def verify_excavation(db_path, input_path):
    """Independently recompute the latest ruling from frozen sources.

    Catches forged clearances, borrowed unproven pothole uncertainties,
    omitted utilities and permit-scope upgrades by comparing the stored
    ruling against a fresh recomputation over the recorded evidence.
    """
    doc = read(input_path)
    if 'excavation_id' not in doc:
        raise ValueError('excavation_verify_missing_excavation_id')
    db = connect(db_path)
    try:
        plan = db.execute('SELECT * FROM excavation_plan WHERE excavation_id=?',
                          (doc['excavation_id'],)).fetchone()
        if plan is None:
            raise ValueError('excavation_plan_unknown')
        decision = db.execute('SELECT * FROM excavation_decision WHERE excavation_id=?'
                              ' ORDER BY seq DESC LIMIT 1', (doc['excavation_id'],)).fetchone()
        if decision is None:
            raise ValueError('excavation_not_decided')
        moment = _parse_recorded_at(decision['decided_at'])
        state, detail = _compute_decision(db, plan, moment)
        stored = json.loads(decision['detail'])
        checks = [{
            'check': 'state_consistent',
            'ok': decision['state'] == state,
            'stored': decision['state'],
            'recomputed': state,
        }]
        if state == 'pending':
            checks.append({
                'check': 'pending_reasons_match',
                'ok': sorted(stored.get('pending_reasons', [])) == sorted(detail['pending_reasons']),
                'stored': sorted(stored.get('pending_reasons', [])),
                'recomputed': sorted(detail['pending_reasons']),
            })
        else:
            _verify_methods(stored, detail, checks)
        return {
            'excavation_id': doc['excavation_id'],
            'seq': decision['seq'],
            'state': decision['state'],
            'ok': all(check['ok'] for check in checks),
            'checks': checks,
        }
    finally:
        db.close()


def _close_enough(stored, recomputed):
    if stored is None:
        return False
    return abs(stored - recomputed) <= 1e-9


def _verify_methods(stored, recomputed, checks):
    stored_methods = {m['method_id']: m for m in stored.get('methods', [])}
    recomputed_methods = {m['method_id']: m for m in recomputed.get('methods', [])}
    checks.append({
        'check': 'methods_complete',
        'ok': sorted(stored_methods) == sorted(recomputed_methods),
        'stored': sorted(stored_methods),
        'recomputed': sorted(recomputed_methods),
    })
    for method_id in sorted(recomputed_methods):
        want = recomputed_methods[method_id]
        got = stored_methods.get(method_id)
        if got is None:
            continue
        checks.append({
            'check': 'permit', 'method': method_id,
            'ok': got.get('permit') == want['permit'],
            'stored': got.get('permit'), 'recomputed': want['permit'],
        })
        checks.append({
            'check': 'permit_scope', 'method': method_id,
            'ok': got.get('scope') == want['scope'],
            'stored': got.get('scope'), 'recomputed': want['scope'],
        })
        checks.append({
            'check': 'limiting', 'method': method_id,
            'ok': (_close_enough(got.get('limiting_clearance'), want['limiting_clearance'])
                   and sorted(got.get('limiting_utilities', [])) == want['limiting_utilities']),
            'stored': {'clearance': got.get('limiting_clearance'),
                       'utilities': sorted(got.get('limiting_utilities', []))},
            'recomputed': {'clearance': want['limiting_clearance'],
                           'utilities': want['limiting_utilities']},
        })
        stored_utils = {u['utility_id']: u for u in got.get('utilities', [])}
        want_utils = {u['utility_id']: u for u in want['utilities']}
        checks.append({
            'check': 'utilities_complete', 'method': method_id,
            'ok': sorted(stored_utils) == sorted(want_utils),
            'stored': sorted(stored_utils), 'recomputed': sorted(want_utils),
        })
        for utility_id in sorted(want_utils):
            want_u = want_utils[utility_id]
            got_u = stored_utils.get(utility_id)
            if got_u is None:
                continue
            mismatched = []
            for field in ('plan_distance', 'vertical_gap', 'horizontal_clearance',
                          'vertical_clearance', 'clearance_3d'):
                if not _close_enough(got_u.get(field), want_u[field]):
                    mismatched.append(field)
            for field, want_value in want_u['deductions'].items():
                if not _close_enough(got_u.get('deductions', {}).get(field), want_value):
                    mismatched.append('deductions.' + field)
            checks.append({
                'check': 'utility_geometry', 'method': method_id, 'utility': utility_id,
                'ok': not mismatched, 'mismatched_fields': mismatched,
            })
            want_witness = want_u['witness']
            got_witness = got_u.get('witness', {})
            witness_ok = (
                got_witness.get('trench_segment') == want_witness['trench_segment']
                and got_witness.get('utility_segment') == want_witness['utility_segment']
                and all(_close_enough((got_witness.get(key) or [None, None])[axis],
                                      want_witness[key][axis])
                        for key in ('trench_point', 'utility_point') for axis in (0, 1)))
            checks.append({
                'check': 'witness', 'method': method_id, 'utility': utility_id,
                'ok': witness_ok,
                'stored': got_witness or None, 'recomputed': want_witness,
            })
            checks.append({
                'check': 'constrains', 'method': method_id, 'utility': utility_id,
                'ok': got_u.get('constrains') == want_u['constrains'],
                'stored': got_u.get('constrains'), 'recomputed': want_u['constrains'],
            })
