"""Freeze a restoration inspection frame before reading field observations."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import sys
from pathlib import Path

from .source import utc
from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS restoration_design(
  case_id TEXT PRIMARY KEY, design_hash TEXT NOT NULL, raw TEXT NOT NULL,
  selected TEXT NOT NULL, source_hash TEXT NOT NULL, frame_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS restoration_observation(
  case_id TEXT PRIMARY KEY REFERENCES restoration_design(case_id),
  source_hash TEXT NOT NULL, raw TEXT NOT NULL);
"""


def _file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("restoration path must be relative")
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or not target.is_file() or target.is_symlink():
        raise ValueError(f"restoration path unavailable: {relative}")
    return target


def _csv(raw: bytes, columns: list[str]) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if reader.fieldnames != columns:
        raise ValueError("restoration CSV columns differ")
    rows = list(reader)
    if not rows or any(None in row for row in rows):
        raise ValueError("restoration CSV empty or malformed")
    return rows


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def register_design(db_path: str | Path, design_path: str | Path) -> dict:
    root = Path(__file__).resolve().parents[1]
    design_file = Path(design_path).resolve()
    if not design_file.is_relative_to(root) or not design_file.is_file():
        raise ValueError("design must be inside repository")
    design_raw = design_file.read_bytes()
    doc = json.loads(design_raw.decode("utf-8-sig"))
    if set(doc) != {"case_id", "notice_path", "notice_sha256", "frame_path",
                    "frame_sha256", "segment", "completion_at", "draw_seed",
                    "sample_sizes", "limit", "uncertainty", "alpha",
                    "max_bad_fraction"}:
        raise ValueError("restoration design fields differ")
    if not doc["case_id"] or not doc["draw_seed"] or not (0 < doc["alpha"] < 1) or \
            not (0 <= doc["max_bad_fraction"] <= 1) or doc["uncertainty"] < 0:
        raise ValueError("invalid restoration design")
    utc(doc["completion_at"])
    notice_raw = _file(root, doc["notice_path"]).read_bytes()
    frame_raw = _file(root, doc["frame_path"]).read_bytes()
    if _hash(notice_raw) != doc["notice_sha256"] or _hash(frame_raw) != doc["frame_sha256"]:
        raise ValueError("restoration source bytes changed")
    notice = json.loads(notice_raw.decode("utf-8-sig"))
    if notice.get("payload", {}).get("segment") != doc["segment"] or \
            utc(doc["completion_at"]) < utc(notice["effective_until"]):
        raise ValueError("restoration notice or completion differs")
    frame = _csv(frame_raw, ["unit_id", "section"])
    ids = [row["unit_id"] for row in frame]
    if len(set(ids)) != len(ids) or any(not item for item in ids):
        raise ValueError("duplicate or empty inspection unit")
    by_section = {}
    for row in frame:
        by_section.setdefault(row["section"], []).append(row["unit_id"])
    if set(by_section) != set(doc["sample_sizes"]):
        raise ValueError("sample strata differ from frame")
    selected = {}
    for section, units in by_section.items():
        n = doc["sample_sizes"][section]
        if not isinstance(n, int) or isinstance(n, bool) or not 0 < n <= len(units):
            raise ValueError("invalid sample size")
        selected[section] = sorted(units, key=lambda item: _hash(
            f"{doc['draw_seed']}:{item}".encode()))[:n]
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        with db:
            old = db.execute("SELECT design_hash FROM restoration_design WHERE case_id=?",
                             (doc["case_id"],)).fetchone()
            if old and old["design_hash"] != _hash(design_raw):
                raise ValueError("restoration design identity conflict")
            db.execute("INSERT OR IGNORE INTO restoration_design VALUES (?,?,?,?,?,?)",
                       (doc["case_id"], _hash(design_raw), design_raw.decode("utf-8-sig"),
                        json.dumps(selected, sort_keys=True), _hash(notice_raw), _hash(frame_raw)))
    finally:
        db.close()
    return {"case_id": doc["case_id"], "design_hash": _hash(design_raw),
            "frame": {key: len(value) for key, value in by_section.items()},
            "selected": selected}


def record_observations(db_path: str | Path, case_id: str, observations: str | Path) -> dict:
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        design = db.execute("SELECT * FROM restoration_design WHERE case_id=?",
                            (case_id,)).fetchone()
        if not design:
            raise ValueError("register inspection design first")
        raw = Path(observations).read_bytes()
        rows = _csv(raw, ["unit_id", "measured"])
        selected = {item for units in json.loads(design["selected"]).values() for item in units}
        seen = [row["unit_id"] for row in rows]
        if set(seen) != selected or len(seen) != len(selected):
            raise ValueError("observations do not match frozen draw")
        if any(not math.isfinite(float(row["measured"])) for row in rows):
            raise ValueError("nonfinite inspection measurement")
        with db:
            old = db.execute("SELECT source_hash FROM restoration_observation WHERE case_id=?",
                             (case_id,)).fetchone()
            if old and old["source_hash"] != _hash(raw):
                raise ValueError("observation identity conflict")
            db.execute("INSERT OR IGNORE INTO restoration_observation VALUES (?,?,?)",
                       (case_id, _hash(raw), raw.decode("utf-8-sig")))
        return {"case_id": case_id, "source_hash": _hash(raw), "count": len(rows)}
    finally:
        db.close()


def main() -> int:
    import tempfile
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "inspection.db"
        design = register_design(db, root / "fixtures" / "restoration" / "design.json")
        recorded = record_observations(db, design["case_id"],
                                       root / "fixtures" / "restoration" / "observations.csv")
    print(json.dumps({"design": design, "observations": recorded}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
