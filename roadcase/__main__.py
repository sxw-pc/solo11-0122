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
    sign_cmd=sub.add_parser("sign-record");sign_cmd.add_argument("--db",required=True);sign_cmd.add_argument("--input",required=True)
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
        print(work/"report.json")
        report(db,work/"report.json")
if __name__=="__main__":main()

