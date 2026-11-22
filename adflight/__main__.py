import argparse,json,tempfile
from pathlib import Path
from . import creative,schedule,playback,repair
from .report import report,audit

COMMANDS={'creative-import':creative.register,'license-import':creative.license,'screen-import':schedule.screen,
          'maintenance-import':schedule.maintenance,'booking-import':schedule.book,'cancel-import':schedule.cancel,
          'proof-import':playback.proof,'makegood-import':playback.makegood,
          'capture-import':repair.capture,'naive-dispatch':repair.naive_local_dispatch,
          'plan-import':repair.ratify,'diagnosis-close':repair.close_diagnosis,
          'repair-execute':repair.execute_repair,'recommission-import':repair.recommission}
def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    for cmd in COMMANDS:
        x=s.add_parser(cmd);x.add_argument('file');x.add_argument('--db',required=True)
    q=s.add_parser('settle');q.add_argument('--db',required=True);q.add_argument('--booking',required=True)
    for cmd in ('report','audit'):
        x=s.add_parser(cmd);x.add_argument('--db',required=True)
    d=s.add_parser('demo');d.add_argument('--db')
    a=p.parse_args()
    if a.cmd in COMMANDS:out=COMMANDS[a.cmd](a.db,a.file)
    elif a.cmd=='settle':out=playback.settle(a.db,a.booking)
    elif a.cmd=='report':out=report(a.db)
    elif a.cmd=='audit':out=audit(a.db)
    else:
        db=a.db or str(Path(tempfile.mkdtemp())/'demo.sqlite');f=Path(__file__).resolve().parent.parent/'fixtures'
        for cmd,name in [('creative-import','creative-ok.json'),('license-import','license-ok.json'),('screen-import','screen-ok.json'),('booking-import','booking-ok.json'),('proof-import','proof-ok.json')]:COMMANDS[cmd](db,f/name)
        out={'db':db,'settlement':playback.settle(db,'B1'),'report':report(db)}
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
