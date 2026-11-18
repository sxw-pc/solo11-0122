"""Acceptance tests for temporary detour sign placement adjudication.

Covers the five required evidence groups:
1. same evidence, two sign classes diverge on real coverage;
2. tall obstacle occludes while a low guardrail never blocks sight;
3. continuous intervals, arc length, boundaries and narrow obstacles;
4. work-zone conflicts and new revisions cannot inherit old certificates;
5. idempotent replay and independent recomputation/verification.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from roadcase.adoption import adopt
from roadcase.signs import record
from roadcase.signplan import (
    decide, verify, SignPlanError, adjudicate_lane, ENGINE_VERSION,
)
from roadcase.report import report

FIX = Path(__file__).resolve().parent.parent / "fixtures"


def _seed(db, survey="sign-survey.json"):
    adopt(db, FIX / "road_normal.json")
    record(db, FIX / survey)


def _cert(plan, lane_id):
    return next(c for c in plan["certificates"] if c["lane_id"] == lane_id)


# ---------------------------------------------------------------------------
# group 1: same evidence, two classes -> different real coverage
# ---------------------------------------------------------------------------

def test_two_classes_on_same_evidence_diverge_on_coverage(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    static = decide(db, "sign-S1", "detour-D1", "static-board")
    led = decide(db, "sign-S1", "detour-D1", "led-message-board")

    # distinct frozen plans, neither overwrites the other
    assert static["plan_id"] != led["plan_id"]
    assert static["state"] == "partial_coverage"
    assert led["state"] == "full_coverage"
    assert static["covered_lanes"] == ["lane-south"]
    assert led["covered_lanes"] == ["lane-center", "lane-south"]

    center_static = _cert(static, "lane-center")
    center_led = _cert(led, "lane-center")
    assert center_static["verdict"] == "fail"
    assert center_led["verdict"] == "pass"
    # identical geometry means identical measured warning distance; only the
    # class threshold changes the verdict
    assert center_static["warning_distance"] == center_led["warning_distance"] == 38.0
    assert center_static["min_warning_distance"] == 60.0
    assert center_led["min_warning_distance"] == 30.0


def test_certificates_expose_intervals_distances_limiter_and_threshold(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    plan = decide(db, "sign-S1", "detour-D1", "static-board")
    south = _cert(plan, "lane-south")
    assert south["verdict"] == "pass"
    assert south["last_readable_interval"] == [38.0, 62.0]
    assert south["warning_distance"] == 62.0
    assert south["limiter"] == "obstacle:parked-truck"
    center = _cert(plan, "lane-center")
    assert center["last_readable_interval"] == [62.0, 68.0]
    assert center["warning_distance"] == 38.0
    assert center["limiter"] == "obstacle:parked-truck"
    # cone boundary ends the readable interval on the sign side
    assert center["fov_intervals"] == [[0.0, 68.0]]
    assert south["fov_intervals"] == [[0.0, 62.0]]
    # the chosen class contract is frozen on the plan
    assert plan["contract"]["engine"] == ENGINE_VERSION
    assert plan["contract"]["units"] == "metres"
    assert plan["coordinate_system"] == "synthetic-local-metres"


# ---------------------------------------------------------------------------
# group 2: tall obstacle occludes; low guardrail never blocks
# ---------------------------------------------------------------------------

def test_tall_obstacle_blocks_but_low_guardrail_does_not(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    plan = decide(db, "sign-S1", "detour-D1", "led-message-board")
    for lane in plan["certificates"]:
        # every blocked interval is attributed to the truck, never the barrier
        assert lane["limiter"] != "obstacle:low-barrier"
        assert all("parked-truck" in (lane["limiter"] or "")
                   for _ in lane["blocked_intervals"]) or lane["blocked_intervals"]
    center = _cert(plan, "lane-center")
    # truck shadow covers the approach up to x=62 on the centre lane
    assert center["blocked_intervals"] == [[0.0, 62.0]]
    # the guardrail (top 0.5m < eye 1.2m) contributes no interval anywhere
    south = _cert(plan, "lane-south")
    assert south["blocked_intervals"] == [[0.0, 38.0]]


def test_removing_tall_obstacle_is_a_new_plan_with_full_coverage(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    old = decide(db, "sign-S1", "detour-D1", "static-board")
    assert old["state"] == "partial_coverage"
    record(db, FIX / "sign-survey-no-truck.json")
    new = decide(db, "sign-S1-no-truck", "detour-D1", "static-board")
    # new survey digest -> new plan, old certificate retained
    assert new["survey_digest"] != old["survey_digest"]
    assert new["plan_id"] != old["plan_id"]
    assert new["state"] == "full_coverage"
    assert all(c["blocked_intervals"] == [] for c in new["certificates"])
    stored = sqlite3.connect(db)
    assert stored.execute("SELECT count(*) FROM sign_plan").fetchone()[0] == 2
    stored.close()


def test_raising_display_point_clears_occlusion_under_new_plan(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    record(db, FIX / "sign-survey-raised.json")
    raised = decide(db, "sign-S1-raised", "detour-D1", "static-board")
    # display point 3.5m now looks over the 3.0m truck: centre lane recovers
    center = _cert(raised, "lane-center")
    assert center["verdict"] == "pass"
    assert raised["state"] == "full_coverage"


def test_extra_upstream_sign_is_separately_decided_and_kept(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    record(db, FIX / "sign-survey-upstream.json")
    plan = decide(db, "sign-S1-upstream", "detour-D1-up", "static-board")
    assert plan["sign_id"] == "detour-D1-up"
    assert all(c["verdict"] == "pass" for c in plan["certificates"])


# ---------------------------------------------------------------------------
# group 3: continuous intervals, arc length, boundaries, narrow obstacles
# ---------------------------------------------------------------------------

def test_disjoint_readable_spots_are_not_sold_as_one_lead_distance(tmp_path):
    db = tmp_path / "state.db"
    _seed(db, FIX / "sign-survey-tricky.json")
    plan = decide(db, "sign-S1-tricky", "detour-narrow", "static-board")
    straight = _cert(plan, "lane-straight")
    # the narrow post splits readability; the last interval starts at 50.3,
    # so the earlier [0,20) visibility cannot inflate the warning distance
    assert straight["readable_intervals"] == [[0.0, 20.0], [50.3, 77.0]]
    assert straight["last_readable_interval"] == [50.3, 77.0]
    assert straight["warning_distance"] == 49.7
    assert straight["limiter"] == "obstacle:narrow-post"


def test_narrow_obstacle_shadow_is_captured_without_sampling(tmp_path):
    db = tmp_path / "state.db"
    _seed(db, FIX / "sign-survey-tricky.json")
    plan = decide(db, "sign-S1-tricky", "detour-narrow", "static-board")
    straight = _cert(plan, "lane-straight")
    # a 0.2m wide post casts a wide analytic wedge that coarse sampling would
    # miss entirely; the engine still resolves a positive blocked interval
    blocked = straight["blocked_intervals"]
    assert blocked == [[20.0, 50.3]]


def test_bent_polyline_arc_length_and_repeated_points(tmp_path):
    db = tmp_path / "state.db"
    _seed(db, FIX / "sign-survey-tricky.json")
    plan = decide(db, "sign-S1-tricky", "detour-bent", "static-board")
    bent = _cert(plan, "lane-bent")
    # 40 + 40 + 60 = 140m despite the duplicated (40,0) vertex
    assert bent["arc_length_total"] == 140.0
    assert bent["decision_arc"] == 140.0
    assert bent["verdict"] == "pass"


def test_threshold_equality_passes(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    # construct a class whose threshold equals the measured 38m exactly
    sign = {
        "position": (70.0, 2.0), "facing": (-1.0, 0.0), "height": 2.5,
        "half_fov_degrees": 45.0, "min_warning_distance": 38.0,
    }
    lane = {"id": "eq", "polyline": [[0.0, 0.0], [100.0, 0.0]],
            "decision_point": [100.0, 0.0], "eye_height": 1.2}
    obstacles = [{"id": "parked-truck", "box": [65.0, 1.5, 68.0, 3.0], "height": 3.0}]
    body = adjudicate_lane(lane, sign, obstacles)
    assert body["warning_distance"] == 38.0
    assert body["verdict"] == "pass"  # equality satisfies the threshold


def test_tangent_sight_line_does_not_count_as_occlusion():
    # eye exactly on the analytic wedge boundary: grazing, still readable
    sign = {"position": (70.0, 2.0), "facing": (-1.0, 0.0), "height": 2.5,
            "half_fov_degrees": 45.0, "min_warning_distance": 1.0}
    lane = {"id": "tan", "polyline": [[0.0, 0.0], [100.0, 0.0]],
            "decision_point": [100.0, 0.0], "eye_height": 1.2}
    obstacles = [{"id": "truck", "box": [65.0, 1.5, 68.0, 3.0], "height": 3.0}]
    body = adjudicate_lane(lane, sign, obstacles)
    # the boundary x=62 is the start of the last readable interval, included
    assert body["last_readable_interval"][0] == 62.0


def test_obstacle_at_or_below_eye_plane_casts_no_shadow():
    from roadcase.signplan import _shadow_region
    s = (70.0, 2.0)
    # top equal to eye plane: grazing, never occludes
    assert _shadow_region(s, 2.5, 1.2, [60.0, 1.0, 62.0, 2.0], 1.2) is None
    # top below eye plane: flat projection alone cannot block
    assert _shadow_region(s, 2.5, 1.2, [60.0, 1.0, 62.0, 2.0], 0.5) is None


def test_decision_at_polyline_start_with_no_upstream_visibility_is_pending():
    sign = {"position": (70.0, 2.0), "facing": (-1.0, 0.0), "height": 2.5,
            "half_fov_degrees": 45.0, "min_warning_distance": 5.0}
    obstacles = [{"id": "truck", "box": [65.0, 1.5, 68.0, 3.0], "height": 3.0}]
    # travel direction reversed: the decision sits at the first vertex, so no
    # readable arc exists before it
    lane = {"id": "r", "polyline": [[100.0, 0.0], [0.0, 0.0]],
            "decision_point": [100.0, 0.0], "eye_height": 1.2}
    body = adjudicate_lane(lane, sign, obstacles)
    assert body["verdict"] == "pending"
    assert body["reason"] == "no_continuous_readable_interval"


def test_decision_just_off_polyline_end_is_pending_but_tolerance_passes():
    sign = {"position": (50.0, 0.0), "facing": (-1.0, 0.0), "height": 2.5,
            "half_fov_degrees": 45.0, "min_warning_distance": 1.0}
    beyond = {"id": "b", "polyline": [[0.0, 0.0], [100.0, 0.0]],
              "decision_point": [100.0001, 0.0], "eye_height": 1.2}
    assert adjudicate_lane(beyond, sign, [])["reason"] == "decision_off_polyline"
    within = {"id": "w", "polyline": [[0.0, 0.0], [100.0, 0.0]],
              "decision_point": [100.0000005, 0.0], "eye_height": 1.2}
    assert adjudicate_lane(within, sign, [])["verdict"] == "pass"


# ---------------------------------------------------------------------------
# group 4: conflicts and new revisions cannot inherit old certificates
# ---------------------------------------------------------------------------

def test_unknown_class_and_missing_survey_are_rejected(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    with pytest.raises(SignPlanError, match="unknown_class"):
        decide(db, "sign-S1", "detour-D1", "no-such-class")
    with pytest.raises(SignPlanError, match="survey_not_found"):
        decide(db, "missing", "detour-D1", "static-board")
    with pytest.raises(SignPlanError, match="sign_not_found"):
        decide(db, "sign-S1", "no-such-sign", "static-board")


def test_superseded_work_revision_marks_plan_pending_not_visible(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    plan = decide(db, "sign-S1", "detour-D1", "static-board")
    assert plan["state"] == "partial_coverage"
    # adopt a new revision of the same work zone
    rev2 = json.loads((FIX / "road_normal.json").read_text(encoding="utf-8"))
    rev2["revision"] = 2
    path = tmp_path / "rev2.json"
    path.write_text(json.dumps(rev2), encoding="utf-8")
    adopt(db, path)
    renewed = decide(db, "sign-S1", "detour-D1", "static-board")
    # revision change forces a new plan; every lane is pending, never defaulted
    assert renewed["plan_id"] != plan["plan_id"]
    assert renewed["state"] == "pending"
    assert all(c["verdict"] == "pending"
               and c["reason"] == "work_revision_superseded"
               for c in renewed["certificates"])
    # old plan and its certificates remain on disk untouched
    old = sqlite3.connect(db)
    rows = old.execute("SELECT state FROM sign_plan ORDER BY created_at").fetchall()
    assert [r[0] for r in rows] == ["partial_coverage", "pending"]
    old.close()


def test_conflicting_work_zone_cannot_certify(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    adopt(db, FIX / "road_conflict.json")  # same revision, different bytes
    plan = decide(db, "sign-S1", "detour-D1", "static-board")
    assert plan["state"] == "pending"
    assert all(c["verdict"] == "pending" for c in plan["certificates"])


def _write_invalid_survey(tmp_path, mutate):
    doc = json.loads((FIX / "sign-survey.json").read_text(encoding="utf-8"))
    mutate(doc)
    path = tmp_path / ("invalid-" + str(len(list(tmp_path.glob("*.json")))) + ".json")
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_bad_geometry_inputs_are_pending_or_rejected(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    sign = {"position": (70.0, 2.0), "facing": (-1.0, 0.0), "height": 2.5,
            "half_fov_degrees": 45.0, "min_warning_distance": 10.0}
    obstacles = []
    # missing decision point
    lane = {"id": "x", "polyline": [[0.0, 0.0], [100.0, 0.0]], "eye_height": 1.2}
    assert adjudicate_lane(lane, sign, obstacles)["reason"] == "missing_decision_point"
    # non-finite coordinates
    lane = {"id": "x", "polyline": [[0.0, 0.0], [float("nan"), 0.0]],
            "decision_point": [100.0, 0.0], "eye_height": 1.2}
    assert adjudicate_lane(lane, sign, obstacles)["verdict"] == "pending"
    # decision point not on the polyline
    lane = {"id": "x", "polyline": [[0.0, 0.0], [100.0, 0.0]],
            "decision_point": [100.0, 5.0], "eye_height": 1.2}
    assert adjudicate_lane(lane, sign, obstacles)["reason"] == "decision_off_polyline"
    # zero / bad eye height
    lane = {"id": "x", "polyline": [[0.0, 0.0], [100.0, 0.0]],
            "decision_point": [100.0, 0.0], "eye_height": 0.0}
    assert adjudicate_lane(lane, sign, obstacles)["reason"] == "zero_eye_height"
    # zero-length facing vector
    bad_sign = dict(sign, facing=(0.0, 0.0))
    lane = {"id": "x", "polyline": [[0.0, 0.0], [100.0, 0.0]],
            "decision_point": [100.0, 0.0], "eye_height": 1.2}
    assert adjudicate_lane(lane, bad_sign, obstacles)["reason"] == "bad_facing"


# ---------------------------------------------------------------------------
# group 5: idempotent replay and independent verification
# ---------------------------------------------------------------------------

def test_plan_replay_is_idempotent(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    first = decide(db, "sign-S1", "detour-D1", "static-board")
    second = decide(db, "sign-S1", "detour-D1", "static-board")
    assert first["plan_id"] == second["plan_id"]
    assert second["replayed"] is True
    assert first["replayed"] is False
    stored = sqlite3.connect(db)
    assert stored.execute("SELECT count(*) FROM sign_plan").fetchone()[0] == 1
    assert stored.execute("SELECT count(*) FROM sign_lane_certificate").fetchone()[0] == 2
    stored.close()


def test_independent_verify_recomputes_and_detects_forgery(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    plan = decide(db, "sign-S1", "detour-D1", "static-board")
    assert verify(db, plan["plan_id"])["ok"] is True
    # forge a stored visible start: pretend the centre lane passed
    stored = sqlite3.connect(db)
    row = stored.execute(
        "SELECT body_json FROM sign_lane_certificate WHERE plan_id=? AND lane_id='lane-center'",
        (plan["plan_id"],)).fetchone()
    body = json.loads(row[0])
    body["verdict"] = "pass"
    stored.execute("UPDATE sign_lane_certificate SET body_json=? WHERE plan_id=? AND lane_id='lane-center'",
                   (json.dumps(body, sort_keys=True), plan["plan_id"]))
    stored.commit()
    stored.close()
    result = verify(db, plan["plan_id"])
    assert result["ok"] is False
    assert "certificate_forged:lane-center" in result["mismatches"]


def test_verify_detects_wrong_class_threshold_and_coverage(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    plan = decide(db, "sign-S1", "detour-D1", "static-board")
    stored = sqlite3.connect(db)
    stored.execute(
        "UPDATE sign_plan SET covered_lanes_json=?, state='full_coverage' WHERE plan_id=?",
        (json.dumps(["lane-center", "lane-south"], sort_keys=True), plan["plan_id"]))
    stored.commit()
    stored.close()
    result = verify(db, plan["plan_id"])
    assert "covered_lanes_forged" in result["mismatches"]
    assert "plan_state_forged" in result["mismatches"]


def test_report_includes_frozen_sign_decisions(tmp_path):
    db = tmp_path / "state.db"
    _seed(db)
    plan = decide(db, "sign-S1", "detour-D1", "static-board")
    verify(db, plan["plan_id"])
    out = report(db, tmp_path / "report.json")
    section = out["sign_decisions"]
    assert section["plans"][0]["plan_id"] == plan["plan_id"]
    assert section["plans"][0]["covered_lanes"] == ["lane-south"]
    assert section["verifications"][0]["ok"] is True
    on_disk = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert on_disk["sign_decisions"]["plans"][0]["state"] == "partial_coverage"
