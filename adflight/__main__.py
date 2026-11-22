import argparse,json,tempfile
from pathlib import Path
from . import creative,schedule,playback,publisher
from .report import report,audit,publication_report,publication_audit

COMMANDS={'creative-import':creative.register,'license-import':creative.license,'screen-import':schedule.screen,
          'maintenance-import':schedule.maintenance,'booking-import':schedule.book,'cancel-import':schedule.cancel,
          'proof-import':playback.proof,'makegood-import':playback.makegood}
def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    for cmd in COMMANDS:
        x=s.add_parser(cmd);x.add_argument('file');x.add_argument('--db',required=True)
    q=s.add_parser('settle');q.add_argument('--db',required=True);q.add_argument('--booking',required=True)
    for cmd in ('report','audit','publication-report','publication-audit'):
        x=s.add_parser(cmd);x.add_argument('--db',required=True)
    d=s.add_parser('demo');d.add_argument('--db')
    pub=s.add_parser('publish');pub.add_argument('file');pub.add_argument('--db',required=True);pub.add_argument('--out',required=True)
    ack=s.add_parser('client-ack');ack.add_argument('file');ack.add_argument('--db',required=True)
    claim=s.add_parser('package-claim');claim.add_argument('file');claim.add_argument('--db',required=True)
    withdraw=s.add_parser('package-withdraw');withdraw.add_argument('file');withdraw.add_argument('--db',required=True)
    rec=s.add_parser('package-recover');rec.add_argument('--db',required=True);rec.add_argument('--out',required=True)
    a=p.parse_args()
    if a.cmd in COMMANDS:out=COMMANDS[a.cmd](a.db,a.file)
    elif a.cmd=='settle':out=playback.settle(a.db,a.booking)
    elif a.cmd=='report':out=report(a.db)
    elif a.cmd=='audit':out=audit(a.db)
    elif a.cmd=='publication-report':out=publication_report(a.db)
    elif a.cmd=='publication-audit':out=publication_audit(a.db)
    elif a.cmd=='publish':out=publisher.publish(a.db,a.file,a.out)
    elif a.cmd=='client-ack':out=publisher.acknowledge(a.db,a.file)
    elif a.cmd=='package-claim':out=publisher.claim(a.db,a.file)
    elif a.cmd=='package-withdraw':out=publisher.withdraw(a.db,a.file)
    elif a.cmd=='package-recover':out=publisher.recover(a.db,a.out)
    else:
        db=a.db or str(Path(tempfile.mkdtemp())/'demo.sqlite');f=Path(__file__).resolve().parent.parent/'fixtures'
        for cmd,name in [('creative-import','creative-ok.json'),('license-import','license-ok.json'),('screen-import','screen-ok.json'),('booking-import','booking-ok.json'),('proof-import','proof-ok.json')]:COMMANDS[cmd](db,f/name)
        out={'db':db,'settlement':playback.settle(db,'B1'),'report':report(db)}
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
