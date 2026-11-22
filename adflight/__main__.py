import argparse,json,tempfile
from pathlib import Path
from . import creative,schedule,playback,energy
from .report import report,audit

COMMANDS={'creative-import':creative.register,'license-import':creative.license,'screen-import':schedule.screen,
          'maintenance-import':schedule.maintenance,'booking-import':schedule.book,'cancel-import':schedule.cancel,
          'proof-import':playback.proof,'makegood-import':playback.makegood,
          'meter-import':energy.meter,'calibration-import':energy.calibration,'curve-import':energy.curve,
          'brightness-import':energy.brightness,'hvac-import':energy.hvac,'load-import':energy.load,
          'clock-import':energy.clock,'ups-import':energy.ups,'control-import':energy.control,
          'receipt-import':energy.receipt}
def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    for cmd in COMMANDS:
        x=s.add_parser(cmd);x.add_argument('file');x.add_argument('--db',required=True)
    q=s.add_parser('settle');q.add_argument('--db',required=True);q.add_argument('--booking',required=True)
    for cmd in ('report','audit'):
        x=s.add_parser(cmd);x.add_argument('--db',required=True)
    for cmd in ('attribute','energy-report'):
        x=s.add_parser(cmd);x.add_argument('--db',required=True);x.add_argument('--start',required=True);x.add_argument('--end',required=True)
    d=s.add_parser('demo');d.add_argument('--db')
    a=p.parse_args()
    if a.cmd in COMMANDS:out=COMMANDS[a.cmd](a.db,a.file)
    elif a.cmd=='settle':out=playback.settle(a.db,a.booking)
    elif a.cmd=='report':out=report(a.db)
    elif a.cmd=='audit':out=audit(a.db)
    elif a.cmd=='attribute':out=energy.attribute(a.db,a.start,a.end)
    elif a.cmd=='energy-report':out=energy.energy_report(a.db,a.start,a.end)
    else:
        db=a.db or str(Path(tempfile.mkdtemp())/'demo.sqlite');f=Path(__file__).resolve().parent.parent/'fixtures'
        for cmd,name in [('creative-import','creative-ok.json'),('license-import','license-ok.json'),('screen-import','screen-ok.json'),('booking-import','booking-ok.json'),('proof-import','proof-ok.json')]:COMMANDS[cmd](db,f/name)
        out={'db':db,'settlement':playback.settle(db,'B1'),'report':report(db)}
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
