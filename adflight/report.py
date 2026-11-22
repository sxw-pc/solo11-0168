from tempfile import TemporaryDirectory
from pathlib import Path
from .store import connect,state

def report(path):
    with connect(path) as db:
        return {'state':state(db),
                'sources':[{'kind':x['kind'],'sha':x['sha']} for x in db.execute('SELECT kind,sha FROM source ORDER BY id')],
                'audience':[dict(x) for x in db.execute('SELECT booking_id,COUNT(DISTINCT token) AS device_reach FROM audience_sample GROUP BY booking_id ORDER BY booking_id')],
                # Bounded reach: token-free provable ranges only; raw tokens never leave the local ledger.
                'audience_bounds':[dict(x) for x in db.execute(
                    'SELECT booking_id,version,min_persons,max_persons,gap,gap_reasons FROM reach_bound ORDER BY id')]}

def audit(path):
    from . import creative,schedule,playback,audience
    funcs={'creative':creative.register,'license':creative.license,'screen':schedule.screen,'maintenance':schedule.maintenance,
           'booking':schedule.book,'cancel':schedule.cancel,'proof':playback.proof,'makegood':playback.makegood,
           'audience':audience.ingest,'probe':audience.import_probe,'gate_count':audience.import_gate_counts,
           'calibration':audience.import_calibration,'geometry':audience.import_geometry,
           'contract':audience.import_contract,'bridge':audience.import_bridge}
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
        # Derived bounds/witnesses/settlements are deterministic functions of all raw evidence.
        with connect(target) as rdb:
            with rdb:
                audience.sync_derived(rdb)
        for item in original['state']['obligation']:
            with connect(target) as db:
                exists=db.execute('SELECT 1 FROM obligation WHERE id=?',(item['id'],)).fetchone()
            if not exists:playback.settle(target,item['booking_id'])
        rebuilt=report(target)
    return {'replay_ok':rebuilt==original,'events':len(raw)}
