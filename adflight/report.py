from tempfile import TemporaryDirectory
import json
from pathlib import Path
from .store import connect,state,canonical

def report(path):
    with connect(path) as db:return {'state':state(db),'sources':[{'kind':x['kind'],'sha':x['sha']} for x in db.execute('SELECT kind,sha FROM source ORDER BY id')]}

def audit(path):
    from . import creative,schedule,playback
    funcs={'creative':creative.register,'license':creative.license,'screen':schedule.screen,'maintenance':schedule.maintenance,
           'booking':schedule.book,'cancel':schedule.cancel,'proof':playback.proof,'makegood':playback.makegood}
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

def _row(db,sql,args=()):
    found=db.execute(sql,args).fetchone()
    return dict(found) if found else None

def publication_report(path):
    """What the platform believes it published, separate from what clients accepted."""
    with connect(path) as db:
        routes=[dict(r) for r in db.execute('SELECT * FROM publication_route ORDER BY booking_id')]
        versions=[dict(v) for v in db.execute('SELECT * FROM package_version ORDER BY booking_id,seq')]
        pointers=[dict(p) for p in db.execute('SELECT * FROM publication_pointer ORDER BY booking_id')]
        events=[dict(e) for e in db.execute('SELECT * FROM package_event ORDER BY id')]
        receipts=[dict(r) for r in db.execute('SELECT * FROM package_receipt ORDER BY received_at,id')]
        claims=[dict(c) for c in db.execute('SELECT * FROM package_claim ORDER BY id')]
        files=[dict(f) for f in db.execute('SELECT * FROM package_file ORDER BY package_id,name')]
    current={p['booking_id']:p['current_package_id'] for p in pointers}
    for v in versions:
        v['is_current']=current.get(v['booking_id'])==v['id']
        v['files']=[f for f in files if f['package_id']==v['id']]
    return {'routes':routes,'packages':versions,'current_pointers':current,
            'platform_published':[v['id'] for v in versions if v['status'] in ('published','accepted','withdrawn','quarantined')],
            'client_accepted':[r['package_id'] for r in receipts if r['status']=='accepted'],
            'receipts':receipts,'claims':claims,'events':events}

def publication_audit(path):
    """Cross-check database claims against the actual bytes the client can download.

    Never deletes or rewrites files. Anything that cannot be reconciled is
    returned under ``manual_review`` so an operator decides.
    """
    from . import publisher
    findings=[];manual_review=[];replay_ok=True
    with connect(path) as db:
        versions=[dict(v) for v in db.execute('SELECT * FROM package_version ORDER BY id')]
        snapshot_rows={r['id']:dict(r) for r in db.execute('SELECT * FROM package_snapshot')}
    for v in versions:
        if v['status'] in ('staging','aborted','quarantined'):
            manual_review.append({'package_id':v['id'],'reason':f"package_status_{v['status']}"})
            continue
        result=publisher.verify_package(path,v['id'])
        if result['status']!='ok':
            replay_ok=False
            manual_review.append({'package_id':v['id'],'reason':result['status'],'detail':result.get('detail')})
            continue
        snap=snapshot_rows.get(v['snapshot_id'])
        manifest_doc=json.loads(result['manifest_raw'])
        if not snap or snap['payload'].encode('utf8')!=canonical(manifest_doc['snapshot']):
            replay_ok=False;manual_review.append({'package_id':v['id'],'reason':'snapshot_manifest_drift'})
            continue
        findings.append({'package_id':v['id'],'recipient':v['recipient'],'package_status':v['status'],
                         'manifest_digest':v['manifest_digest'],'content_digest':v['content_digest']})
    return {'verified':findings,'manual_review':manual_review,'replay_ok':replay_ok and not manual_review}
