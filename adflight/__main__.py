import argparse,json,tempfile
from pathlib import Path
from . import creative,schedule,playback,colorpipe
from .report import report,audit

COMMANDS={'creative-import':creative.register,'license-import':creative.license,'screen-import':schedule.screen,
          'maintenance-import':schedule.maintenance,'booking-import':schedule.book,'cancel-import':schedule.cancel,
          'proof-import':playback.proof,'makegood-import':playback.makegood,
          'calibration-import':colorpipe.import_calibration,'panel-import':colorpipe.import_panel,
          'capture-import':colorpipe.import_capture,'color-target-import':colorpipe.import_target,
          'color-approval':colorpipe.certify_revision,'color-revoke':colorpipe.revoke_approval}
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
        out={'db':db,'settlement':playback.settle(db,'B1'),'report':report(db),
             'color':_color_demo(db,f)}
    print(json.dumps(out,ensure_ascii=False,indent=2))

def _color_demo(db,f):
    """End-to-end color certification on the synthetic S1 panel fixtures."""
    import json as _json
    from . import colorpipe
    cf=f/'color'
    capture_dir=Path(tempfile.mkdtemp())
    # rev2 creative must exist before its brand-color targets are registered
    rev2=_json.loads((f/'creative-ok.json').read_text());rev2.update(revision=2)
    c2=capture_dir/'creative2.json';c2.write_text(_json.dumps(rev2));creative.register(db,c2)
    for cmd,name in [('calibration-import','calibration-ok.json'),('calibration-import','calibration-camera.json'),
                     ('panel-import','panel.json'),('color-target-import','target-primary.json'),
                     ('color-target-import','target-fallback.json')]:
        COMMANDS[cmd](db,cf/name)
    captures=_json.loads((cf/'captures.json').read_text())
    for i,cap in enumerate(captures):
        p=capture_dir/f'capture-{i}.json';p.write_text(_json.dumps(cap))
        colorpipe.import_capture(db,p)
    observer=_json.loads((cf/'observer.json').read_text())
    approval_doc={'id':'AP-AD1-2-P1-primary','creative_id':'AD1','revision':2,'panel_id':'P1',
                  'route':'primary','grid_nm':observer['grid_nm'],'eye_rows':observer['eye_rows'],
                  'spectrometer_error_budget':0.0001}
    ap=capture_dir/'approval.json';ap.write_text(_json.dumps(approval_doc))
    primary=colorpipe.certify_revision(db,ap)
    fallback_doc=dict(approval_doc);fallback_doc.update(id='AP-AD1-2-P1-fallback',route='fallback')
    fb=capture_dir/'fallback.json';fb.write_text(_json.dumps(fallback_doc))
    fallback=colorpipe.certify_revision(db,fb)
    # a rev2 booking must name one of the approved routes before confirmation
    booking2={'id':'B2','creative_id':'AD1','revision':2,'screen_id':'S1',
              'start_at':'2026-06-20T10:00:00Z','end_at':'2026-06-20T10:10:00Z',
              'priority':10,'color_route':'primary'}
    b2=capture_dir/'booking2.json';b2.write_text(_json.dumps(booking2))
    booked=schedule.book(db,b2)
    return {'primary':primary,'fallback':fallback,'rev2_booking':booked,
            'toy_witness':_json.loads((cf/'toy-witness.json').read_text())['camera_identical']}
if __name__=='__main__':main()
