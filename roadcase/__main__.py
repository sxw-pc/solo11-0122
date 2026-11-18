"""Command line entry points for three business subsystems."""
import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .adoption import adopt
from .assessment import assess
from .settlement import settle
from .report import report

def _demo_signs(db,fixture):
    """Drive the sign placement adjudication on the local synthetic surveys."""
    from .signs import record
    from .signplan import decide,verify
    record(db,fixture/"sign-survey.json")
    summary=[]
    for class_id in ("static-board","led-message-board"):
        plan=decide(db,"sign-S1","detour-D1",class_id)
        verify(db,plan["plan_id"])
        summary.append({
            "class_id":class_id,"state":plan["state"],
            "covered_lanes":plan["covered_lanes"],
            "lanes":[{"lane_id":c["lane_id"],"verdict":c["verdict"],
                      "warning_distance":c["warning_distance"],
                      "last_readable_interval":c["last_readable_interval"],
                      "limiter":c["limiter"],
                      "min_warning_distance":c["min_warning_distance"]}
                     for c in plan["certificates"]]})
    # a changed survey (tall obstacle removed) is a new plan; old certs stay
    record(db,fixture/"sign-survey-no-truck.json")
    cleared=decide(db,"sign-S1-no-truck","detour-D1","static-board")
    print(json.dumps({"sign_adjudication":summary,
                      "new_survey_no_truck_state":cleared["state"]},ensure_ascii=False))

def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest="command",required=True)
    i=sub.add_parser("init");i.add_argument("--db",required=True)
    for name in ("adopt","assess","settle"):
        cmd=sub.add_parser(name);cmd.add_argument("--db",required=True);cmd.add_argument("--input",required=True)
    sign_cmd=sub.add_parser("sign-record");sign_cmd.add_argument("--db",required=True);sign_cmd.add_argument("--input",required=True)
    sd=sub.add_parser("sign-decide")
    sd.add_argument("--db",required=True);sd.add_argument("--survey",required=True)
    sd.add_argument("--sign",required=True);sd.add_argument("--class",dest="class_id",required=True)
    sd.add_argument("--lanes",help="comma-separated lane ids; default is every survey lane")
    sv=sub.add_parser("sign-verify");sv.add_argument("--db",required=True);sv.add_argument("--plan",required=True)
    r=sub.add_parser("report");r.add_argument("--db",required=True);r.add_argument("--out")
    d=sub.add_parser("demo");d.add_argument("--workdir")
    dc=sub.add_parser("detour-config");dc.add_argument("--db",required=True);dc.add_argument("--id",required=True);dc.add_argument("--segment",required=True);dc.add_argument("--capacity",required=True,type=int)
    dr=sub.add_parser("detour-reserve");dr.add_argument("--db",required=True);dr.add_argument("--id",required=True);dr.add_argument("--pool",required=True);dr.add_argument("--source",required=True);dr.add_argument("--journey",required=True);dr.add_argument("--flow",required=True,type=int)
    dl=sub.add_parser("detour-release");dl.add_argument("--db",required=True);dl.add_argument("--id",required=True)
    a=p.parse_args()
    if a.command=="init":
        db=connect(a.db);db.close();print(a.db)
    elif a.command=="adopt":print(json.dumps(adopt(a.db,a.input),ensure_ascii=False))
    elif a.command=="assess":print(json.dumps(assess(a.db,a.input),ensure_ascii=False))
    elif a.command=="settle":print(json.dumps(settle(a.db,a.input),ensure_ascii=False))
    elif a.command=="sign-record":
        from .signs import record
        print(record(a.db,a.input))
    elif a.command=="sign-decide":
        from .signplan import decide
        lanes=a.lanes.split(",") if a.lanes else None
        print(json.dumps(decide(a.db,a.survey,a.sign,a.class_id,lanes),ensure_ascii=False))
    elif a.command=="sign-verify":
        from .signplan import verify
        print(json.dumps(verify(a.db,a.plan),ensure_ascii=False))
    elif a.command=="report":print(json.dumps(report(a.db,a.out),ensure_ascii=False))
    elif a.command=="detour-config":
        from .detour import configure
        print(configure(a.db,a.id,a.segment,a.capacity))
    elif a.command=="detour-reserve":
        from .detour import reserve
        print(reserve(a.db,a.id,a.pool,a.source,a.journey,a.flow))
    elif a.command=="detour-release":
        from .detour import release
        print(release(a.db,a.id))
    else:
        work=Path(a.workdir or tempfile.mkdtemp(prefix="source-case-"));work.mkdir(parents=True,exist_ok=True)
        fixture=Path(__file__).resolve().parent.parent/"fixtures"
        db=work/"state.db"
        config=__import__(__package__+".policy",fromlist=["DEMO"]).DEMO
        print(adopt(db,fixture/config["adopt"]))
        print(assess(db,fixture/config["assess"]))
        print(settle(db,fixture/config["settle"]))
        _demo_signs(db,fixture)
        print(work/"report.json")
        report(db,work/"report.json")
if __name__=="__main__":main()

