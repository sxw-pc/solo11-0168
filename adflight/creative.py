from .store import connect,source,already,at

def register(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'creative',file)
        old=db.execute('SELECT source_id FROM creative WHERE id=? AND revision=?',(d['id'],d['revision'])).fetchone()
        if old:
            if old['source_id']!=sid:raise ValueError('creative_revision_conflict')
            return {'id':d['id'],'replayed':True}
        start,end=at(d['valid_from']),at(d['valid_to'])
        if start>=end:raise ValueError('invalid_creative_window')
        prior=db.execute('SELECT MAX(revision) AS r FROM creative WHERE id=?',(d['id'],)).fetchone()['r']
        if prior is not None and d['revision']!=prior+1:raise ValueError('creative_revision_order')
        status='withdrawn' if d.get('withdrawn') else 'submitted'
        db.execute('INSERT INTO creative VALUES(?,?,?,?,?,?,?,?)',(d['id'],d['revision'],d['category'],d['city'],start,end,status,sid))
        if status=='withdrawn':db.execute("UPDATE booking SET status='cancelled' WHERE creative_id=? AND status IN ('confirmed','waitlisted') AND start_at>=?",(d['id'],start))
        return {'id':d['id'],'revision':d['revision'],'status':status}

def license(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'license',file)
        if already(db,'license',d['id'],sid):return {'id':d['id'],'replayed':True}
        if not d['issuer'] or d['issuer']=='operator':raise ValueError('independent_license_issuer_required')
        if not db.execute('SELECT 1 FROM creative WHERE id=?',(d['creative_id'],)).fetchone():raise ValueError('unknown_creative')
        db.execute('INSERT INTO license VALUES(?,?,?,?,?,?,?)',(d['id'],d['creative_id'],d['category'],d['city'],at(d['valid_to']),d['issuer'],sid))
        return {'id':d['id'],'status':'licensed'}

def eligible(db,creative_id,revision,city,start,end):
    c=db.execute('SELECT * FROM creative WHERE id=? AND revision=?',(creative_id,revision)).fetchone()
    if not c or c['status']!='submitted' or c['city']!=city or c['valid_from']>start or c['valid_to']<end:return 'creative_not_eligible'
    latest=db.execute('SELECT MAX(revision) FROM creative WHERE id=?',(creative_id,)).fetchone()[0]
    if latest!=revision:return 'stale_creative_revision'
    l=db.execute('SELECT * FROM license WHERE creative_id=? AND category=? AND city=? AND valid_to>=? ORDER BY valid_to DESC LIMIT 1',(creative_id,c['category'],city,end)).fetchone()
    if not l:return 'license_missing_or_expired'
    return None
