from datetime import datetime
from .store import connect,source,already,at

def seconds(start,end):
    return int((datetime.fromisoformat(end[:-1]+'+00:00')-datetime.fromisoformat(start[:-1]+'+00:00')).total_seconds())

def proof(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'proof',file)
        if already(db,'proof',d['id'],sid):return {'id':d['id'],'replayed':True}
        b=db.execute('SELECT * FROM booking WHERE id=?',(d['booking_id'],)).fetchone()
        if not b:raise ValueError('unknown_booking')
        start,end=at(d['start_at']),at(d['end_at'])
        if start>=end or start<b['start_at'] or end>b['end_at']:raise ValueError('proof_outside_booking')
        if d['observer']=='operator' or d['screen_id']!=b['screen_id']:raise ValueError('non_independent_or_wrong_screen')
        overlapping=db.execute('SELECT 1 FROM proof WHERE booking_id=? AND status="valid" AND start_at<? AND end_at>?',(b['id'],end,start)).fetchone()
        if overlapping:raise ValueError('duplicate_playback_seconds')
        status='valid' if b['status']=='confirmed' else 'disputed'
        db.execute('INSERT INTO proof VALUES(?,?,?,?,?,?,?,?)',(d['id'],b['id'],d['screen_id'],start,end,d['observer'],status,sid))
        return {'id':d['id'],'status':status,'seconds':seconds(start,end) if status=='valid' else 0}

def settle(path,booking_id):
    with connect(path) as db,db:
        b=db.execute('SELECT * FROM booking WHERE id=?',(booking_id,)).fetchone()
        if not b:raise ValueError('unknown_booking')
        target=seconds(b['start_at'],b['end_at'])
        actual=sum(seconds(x['start_at'],x['end_at']) for x in db.execute("SELECT start_at,end_at FROM proof WHERE booking_id=? AND status='valid'",(booking_id,)))
        missing=max(0,target-actual)
        reason='maintenance_interrupted' if b['status']=='interrupted' else 'underplayed' if missing else 'fulfilled'
        # settle only manages playback-settlement obligations; municipal pause
        # obligations (reason 'no_broadcast_pause') are a separate ledger
        prior=db.execute("SELECT * FROM obligation WHERE booking_id=? AND status='due' AND reason IN ('maintenance_interrupted','underplayed') ORDER BY id DESC LIMIT 1",(booking_id,)).fetchone()
        if prior and (prior['missing_seconds']!=missing or prior['reason']!=reason):
            db.execute("UPDATE obligation SET status='superseded' WHERE id=?",(prior['id'],))
            prior=None
        if missing and not prior:
            db.execute('INSERT INTO obligation(booking_id,missing_seconds,status,reason) VALUES(?,?,?,?)',(booking_id,missing,'due',reason))
        return {'booking_id':booking_id,'planned_seconds':target,'valid_seconds':actual,'missing_seconds':missing,'status':'due' if missing else 'fulfilled'}

def makegood(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'makegood',file)
        if already(db,'makegood',d['id'],sid):return {'id':d['id'],'replayed':True}
        obligation=db.execute('SELECT * FROM obligation WHERE id=?',(d['obligation_id'],)).fetchone()
        booking=db.execute('SELECT * FROM booking WHERE id=?',(d['booking_id'],)).fetchone()
        original=db.execute('SELECT * FROM booking WHERE id=?',(obligation['booking_id'],)).fetchone() if obligation else None
        if not obligation or obligation['status']!='due' or not booking or booking['status']!='confirmed':raise ValueError('makegood_not_eligible')
        original_screen=db.execute('SELECT grade FROM screen WHERE id=?',(original['screen_id'],)).fetchone()['grade']
        new_screen=db.execute('SELECT grade FROM screen WHERE id=?',(booking['screen_id'],)).fetchone()['grade']
        if new_screen<original_screen:raise ValueError('makegood_grade_too_low')
        actual=sum(seconds(x['start_at'],x['end_at']) for x in db.execute("SELECT start_at,end_at FROM proof WHERE booking_id=? AND status='valid'",(booking['id'],)))
        if actual<obligation['missing_seconds']:raise ValueError('makegood_not_proven')
        db.execute('INSERT INTO makegood VALUES(?,?,?,?,?)',(d['id'],obligation['id'],booking['id'],'fulfilled',sid))
        db.execute("UPDATE obligation SET status='fulfilled' WHERE id=?",(obligation['id'],))
        return {'id':d['id'],'status':'fulfilled'}
