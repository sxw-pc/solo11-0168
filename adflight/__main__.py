import argparse,json,tempfile
from pathlib import Path
from . import creative,schedule,playback,mobile_route
from .report import report,audit,route_report

COMMANDS={'creative-import':creative.register,'license-import':creative.license,'screen-import':schedule.screen,
          'maintenance-import':schedule.maintenance,'booking-import':schedule.book,'cancel-import':schedule.cancel,
          'proof-import':playback.proof,'makegood-import':playback.makegood,
          'gps-import':mobile_route.gps_import,'calibration-import':mobile_route.calibration_import,
          'map-import':mobile_route.map_import,'camera-import':mobile_route.camera_import,
          'adjudication-import':mobile_route.adjudicate,'decision-import':mobile_route.decide,
          'stop-import':mobile_route.stop_import}
def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    for cmd in COMMANDS:
        x=s.add_parser(cmd);x.add_argument('file');x.add_argument('--db',required=True)
    q=s.add_parser('settle');q.add_argument('--db',required=True);q.add_argument('--booking',required=True)
    q=s.add_parser('route-report');q.add_argument('--db',required=True);q.add_argument('--package',required=True)
    for cmd in ('report','audit'):
        x=s.add_parser(cmd);x.add_argument('--db',required=True)
    d=s.add_parser('demo');d.add_argument('--db')
    d=s.add_parser('route-demo');d.add_argument('--db')
    a=p.parse_args()
    if a.cmd in COMMANDS:out=COMMANDS[a.cmd](a.db,a.file)
    elif a.cmd=='settle':out=playback.settle(a.db,a.booking)
    elif a.cmd=='route-report':out=route_report(a.db,a.package)
    elif a.cmd=='report':out=report(a.db)
    elif a.cmd=='audit':out=audit(a.db)
    elif a.cmd=='demo':
        db=a.db or str(Path(tempfile.mkdtemp())/'demo.sqlite');f=Path(__file__).resolve().parent.parent/'fixtures'
        for cmd,name in [('creative-import','creative-ok.json'),('license-import','license-ok.json'),('screen-import','screen-ok.json'),('booking-import','booking-ok.json'),('proof-import','proof-ok.json')]:COMMANDS[cmd](db,f/name)
        out={'db':db,'settlement':playback.settle(db,'B1'),'report':report(db)}
    else:
        base=Path(a.db) if a.db else Path(tempfile.mkdtemp());base.mkdir(parents=True,exist_ok=True)
        f=Path(__file__).resolve().parent.parent/'fixtures'
        common=[('creative-import','creative-ok.json'),('license-import','license-ok.json'),('screen-import','screen-ok.json'),
                ('booking-import','booking-ok.json'),('gps-import','gps-package.json'),('calibration-import','calibration.json'),
                ('map-import','map-v1.json'),('adjudication-import','adjudication.json')]
        def build(name,decision,extra):
            db=str(base/name)
            for cmd,fixture in common+[('decision-import',decision)]+[('camera-import' if e.startswith('camera') else 'stop-import',e) for e in extra]:COMMANDS[cmd](db,f/fixture)
            return db
        pause=build('route-pause.sqlite','decision-pause.json',['stop-receipt.json'])
        avoid=build('route-avoid.sqlite','decision-avoid.json',['camera-outer.json'])
        out={'pause':{'db':pause,'route':route_report(pause,'P1'),'obligations':report(pause)['state']['obligation']},
             'avoid':{'db':avoid,'route':route_report(avoid,'P1')}}
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
