from tempfile import TemporaryDirectory
from pathlib import Path
from .store import connect,state

def report(path):
    with connect(path) as db:return {'state':state(db),'sources':[{'kind':x['kind'],'sha':x['sha']} for x in db.execute('SELECT kind,sha FROM source ORDER BY id')]}

def audit(path):
    from . import creative,schedule,playback,colorpipe
    funcs={'creative':creative.register,'license':creative.license,'screen':schedule.screen,'maintenance':schedule.maintenance,
           'booking':schedule.book,'cancel':schedule.cancel,'proof':playback.proof,'makegood':playback.makegood,
           'calibration':colorpipe.import_calibration,'panel':colorpipe.import_panel,'capture':colorpipe.import_capture,
           'color_target':colorpipe.import_target,'color_approval':colorpipe.certify_revision,
           'revocation':colorpipe.revoke_approval}
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
