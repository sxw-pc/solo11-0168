from tempfile import TemporaryDirectory
from pathlib import Path
from .store import connect,state

def report(path):
    with connect(path) as db:
        data={'state':state(db),'sources':[{'kind':x['kind'],'sha':x['sha']} for x in db.execute('SELECT kind,sha FROM source ORDER BY id')]}
        data['repair']=repair_overview(db)
        return data

def repair_overview(db):
    outages=[dict(r) for r in db.execute('SELECT * FROM screen_outage ORDER BY screen_id')]
    recom=[dict(r) for r in db.execute('SELECT * FROM screen_recommission ORDER BY screen_id')]
    plans=[dict(r) for r in db.execute('SELECT id,route,status,locked_at,closed_at,commitments FROM dispatch_plan ORDER BY id')]
    for p in plans:
        p['commitments']=__import__('json').loads(p['commitments'])
    return {'outages':outages,'recommissions':recom,'plans':plans,
            'spare_parts':[dict(r) for r in db.execute('SELECT serial,kind,status,installed_screen,installed_at FROM spare_part ORDER BY serial')],
            'part_movements':[dict(r) for r in db.execute('SELECT serial,movement,screen_id,other_serial,at_ FROM part_movement ORDER BY id')]}

def audit(path):
    from . import creative,schedule,playback,repair
    funcs={'creative':creative.register,'license':creative.license,'screen':schedule.screen,'maintenance':schedule.maintenance,
           'booking':schedule.book,'cancel':schedule.cancel,'proof':playback.proof,'makegood':playback.makegood,
           'capture':repair.capture,'naive_plan':repair.naive_local_dispatch,'dispatch_plan':repair.ratify,
           'repair':repair.execute_repair,'recommission':repair.recommission}
    original=report(path)
    with connect(path) as db:raw=[(x['kind'],x['raw']) for x in db.execute('SELECT kind,raw FROM source ORDER BY id')]
    # Decisions from settle are recomputed after all imported evidence.
    with TemporaryDirectory() as tmp:
        target=Path(tmp)/'replay.sqlite'
        for i,(kind,data) in enumerate(raw):
            if kind=='makegood':
                import json
                obligation_id=json.loads(data)['obligation_id']
                prior=next((x for x in original['state']['obligation'] if x['id']==obligation_id),None)
                if prior:playback.settle(target,prior['booking_id'])
            f=Path(tmp)/f'{i}.json';f.write_bytes(data);funcs[kind](target,f)
        for item in original['state']['obligation']:
            with connect(target) as db:
                exists=db.execute('SELECT 1 FROM obligation WHERE id=?',(item['id'],)).fetchone()
            if not exists:playback.settle(target,item['booking_id'])
        rebuilt=report(target)
    return {'replay_ok':rebuilt==original,'events':len(raw)}
