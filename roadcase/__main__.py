"""Command line entry points for three business subsystems."""
import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .adoption import adopt
from .assessment import assess
from .settlement import settle
from .report import report

def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest="command",required=True)
    i=sub.add_parser("init");i.add_argument("--db",required=True)
    for name in ("adopt","assess","settle"):
        cmd=sub.add_parser(name);cmd.add_argument("--db",required=True);cmd.add_argument("--input",required=True)
    noise_cmd=sub.add_parser("noise-record");noise_cmd.add_argument("--db",required=True);noise_cmd.add_argument("--input",required=True)
    for name in ("noise-plan","noise-decide"):
        cmd=sub.add_parser(name);cmd.add_argument("--db",required=True);cmd.add_argument("--input",required=True)
    verify_cmd=sub.add_parser("noise-verify");verify_cmd.add_argument("--db",required=True)
    r=sub.add_parser("report");r.add_argument("--db",required=True);r.add_argument("--out")
    d=sub.add_parser("demo");d.add_argument("--workdir")
    a=p.parse_args()
    if a.command=="init":
        db=connect(a.db);db.close();print(a.db)
    elif a.command=="adopt":print(json.dumps(adopt(a.db,a.input),ensure_ascii=False))
    elif a.command=="assess":print(json.dumps(assess(a.db,a.input),ensure_ascii=False))
    elif a.command=="settle":print(json.dumps(settle(a.db,a.input),ensure_ascii=False))
    elif a.command=="noise-record":
        from .noise import record
        print(record(a.db,a.input))
    elif a.command=="noise-plan":
        from .noise import freeze_plan
        print(json.dumps(freeze_plan(a.db,a.input),ensure_ascii=False))
    elif a.command=="noise-decide":
        from .noise import decide
        print(json.dumps(decide(a.db,a.input),ensure_ascii=False,indent=2))
    elif a.command=="noise-verify":
        from .noise import verify
        print(json.dumps(verify(a.db),ensure_ascii=False,indent=2))
    elif a.command=="report":print(json.dumps(report(a.db,a.out),ensure_ascii=False))
    else:
        work=Path(a.workdir or tempfile.mkdtemp(prefix="source-case-"));work.mkdir(parents=True,exist_ok=True)
        fixture=Path(__file__).resolve().parent.parent/"fixtures"
        if not fixture.exists():
            # installed-package layout: fixtures ship beside the repository root
            import sys
            for candidate in (Path.cwd()/"fixtures", Path(sys.prefix)/"fixtures"):
                if candidate.exists():
                    fixture=candidate;break
        db=work/"state.db"
        config=__import__(__package__+".policy",fromlist=["DEMO"]).DEMO
        print(adopt(db,fixture/config["adopt"]))
        print(assess(db,fixture/config["assess"]))
        print(settle(db,fixture/config["settle"]))
        _noise_demo(db,fixture,work)
        print(work/"report.json")
        report(db,work/"report.json")

def _noise_demo(db,fixture,work):
    """Synthetic night-noise workflow on local data only."""
    from .noise import record,freeze_plan,decide,verify
    record(db,fixture/"night-noise.json")
    cutoff="2026-06-01T21:00:00Z"
    plans={
      "plan-baseline":{"selections":[]},
      "plan-time-separation":{"selections":[
        {"activity_id":"crew-A"},
        {"activity_id":"crew-B","option":"after-A"}]},
      "plan-barrier":{"selections":[
        {"activity_id":"crew-A"},
        {"activity_id":"crew-B","option":"barrier-4db"}]}}
    for plan_id,body in plans.items():
        doc={"plan_id":plan_id,"session_id":"noise-N1","source_id":"road-1",
             "revision":1,"evidence_cutoff":cutoff,**body}
        path=work/(plan_id+".json")
        path.write_text(json.dumps(doc),encoding="utf-8")
        print(json.dumps(freeze_plan(db,path),ensure_ascii=False))
        decide_path=work/(plan_id+"-decide.json")
        decide_path.write_text(json.dumps({"plan_id":plan_id}),encoding="utf-8")
        decision=decide(db,decide_path)
        print(json.dumps({"plan_id":plan_id,"state":decision["state"],
                          "completion_at":decision["completion_at"],
                          "blockers":[b["code"] for b in decision["blockers"]],
                          "windows":[{"slice":[w["slice_from"],w["slice_until"]],
                                      "level_db":w["level_db"],
                                      "compliant":w["compliant"]}
                                     for w in decision["windows"]]},
                         ensure_ascii=False))
    check=verify(db)
    print(json.dumps({"noise_verify":{"verified":check["verified"],
                                      "forged":check["forged"],
                                      "stale":check["stale"]}},ensure_ascii=False))

if __name__=="__main__":main()

