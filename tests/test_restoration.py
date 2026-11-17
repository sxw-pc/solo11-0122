import json
from pathlib import Path

import pytest

from roadcase.restoration import record_observations, register_design


ROOT = Path(__file__).resolve().parents[1]


def test_restoration_fixture_registers_and_records(tmp_path):
    db = tmp_path / "restoration.db"
    design = register_design(db, ROOT / "fixtures/restoration/design.json")
    assert design["frame"] == {"W": 20, "E": 20}
    assert len(design["selected"]["W"]) == 18
    assert len(design["selected"]["E"]) == 5
    result = record_observations(
        db, design["case_id"], ROOT / "fixtures/restoration/observations.csv"
    )
    assert result["count"] == 23
    assert register_design(db, ROOT / "fixtures/restoration/design.json") == design


def test_restoration_observations_must_match_frozen_draw(tmp_path):
    db = tmp_path / "restoration.db"
    design = register_design(db, ROOT / "fixtures/restoration/design.json")
    bad = tmp_path / "bad.csv"
    bad.write_text("unit_id,measured\nW01,96.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen draw"):
        record_observations(db, design["case_id"], bad)


def test_restoration_notice_and_frame_are_pinned(tmp_path):
    source = json.loads((ROOT / "fixtures/restoration/design.json").read_text())
    source["frame_sha256"] = "0" * 64
    copy = ROOT / "fixtures/restoration/pinned-tamper.tmp.json"
    try:
        copy.write_text(json.dumps(source), encoding="utf-8")
        with pytest.raises(ValueError, match="source bytes changed"):
            register_design(tmp_path / "restoration.db", copy)
    finally:
        copy.unlink(missing_ok=True)
