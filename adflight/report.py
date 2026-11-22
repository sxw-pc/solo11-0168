from tempfile import TemporaryDirectory
from pathlib import Path
from .store import connect,state

def report(path):
    with connect(path) as db:
        return {'state':state(db),
                'sources':[{'kind':x['kind'],'sha':x['sha']} for x in db.execute('SELECT kind,sha FROM source ORDER BY id')]}

# The replay path reconstructs derived decision tables (obligations, rts and
# maintenance windows) from the immutable source stream.  It is intentionally
# tolerant of inputs that the live path refused: audit must always run.
def _replay(source_rows,target):
    import json
    from . import creative,schedule,playback,repair
    funcs={'creative':creative.register,'license':creative.license,'screen':schedule.screen,
           'maintenance':schedule.maintenance,'booking':schedule.book,'cancel':schedule.cancel,
           'proof':playback.proof,'makegood':playback.makegood,
           'repair-intake':repair.intake,'repair-dispatch':repair.dispatch}
    pending=[]
    for i,(kind,data) in enumerate(source_rows):
        if kind=='settle':
            playback.settle(target,json.loads(data)['booking_id']);continue
        if kind=='makegood':
            obligation_id=json.loads(data)['obligation_id']
            with connect(target) as db:
                row=db.execute('SELECT booking_id FROM obligation WHERE id=?',(obligation_id,)).fetchone()
            if row:playback.settle(target,row['booking_id'])
        if kind not in funcs:
            continue
        f=Path(target.parent)/f'replay-{i}.json';f.write_bytes(data)
        try:funcs[kind](target,f)
        except ValueError:
            # A refused live event is not part of the accepted ledger.
            continue
    with connect(target) as db:
        for row in db.execute('SELECT * FROM obligation ORDER BY id').fetchall():
            playback.settle(target,row['booking_id'])

def audit(path):
    original=report(path)
    with connect(path) as db:
        raw=[(x['kind'],x['raw']) for x in db.execute('SELECT kind,raw FROM source ORDER BY id')]
    with TemporaryDirectory() as tmp:
        target=Path(tmp)/'replay.sqlite'
        _replay(raw,target)
        rebuilt=report(target)
    return {'replay_ok':rebuilt==original,'events':len(raw)}
