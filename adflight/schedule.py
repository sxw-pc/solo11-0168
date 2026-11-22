from .store import connect,source,already,at
from .creative import eligible

def screen(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'screen',file)
        if already(db,'screen',d['id'],sid):return {'id':d['id'],'replayed':True}
        if not 1<=int(d['grade'])<=5:raise ValueError('invalid_grade')
        db.execute('INSERT INTO screen VALUES(?,?,?,?)',(d['id'],d['city'],int(d['grade']),sid))
        if d.get('site_id'):
            db.execute('INSERT OR REPLACE INTO screen_site VALUES(?,?)',(d['id'],d['site_id']))
        return {'id':d['id'],'status':'available'}

def maintenance(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'maintenance',file)
        if already(db,'maintenance',d['id'],sid):return {'id':d['id'],'replayed':True}
        start,end=at(d['start_at']),at(d['end_at'])
        if start>=end:raise ValueError('invalid_maintenance_window')
        if not db.execute('SELECT 1 FROM screen WHERE id=?',(d['screen_id'],)).fetchone():raise ValueError('unknown_screen')
        db.execute('INSERT INTO maintenance VALUES(?,?,?,?,?)',(d['id'],d['screen_id'],start,end,sid))
        affected=db.execute("SELECT id FROM booking WHERE screen_id=? AND start_at<? AND end_at>? AND status='confirmed'",(d['screen_id'],end,start)).fetchall()
        for row in affected:
            db.execute("UPDATE booking SET status='interrupted' WHERE id=?",(row['id'],))
        return {'id':d['id'],'interrupted':[x['id'] for x in affected]}

def book(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'booking',file)
        if already(db,'booking',d['id'],sid):return {'id':d['id'],'replayed':True}
        start,end=at(d['start_at']),at(d['end_at'])
        if start>=end:raise ValueError('invalid_booking_window')
        screen=db.execute('SELECT * FROM screen WHERE id=?',(d['screen_id'],)).fetchone()
        if not screen:raise ValueError('unknown_screen')
        reason=eligible(db,d['creative_id'],d['revision'],screen['city'],start,end)
        if reason:raise ValueError(reason)
        reason=not_sellable(db,d['screen_id'],start,end)
        if reason:raise ValueError(reason)
        closed=db.execute("SELECT 1 FROM rts WHERE screen_id=? AND status='closed' AND closed_at<=?",(d['screen_id'],start)).fetchone()
        if not closed and db.execute('SELECT 1 FROM maintenance WHERE screen_id=? AND start_at<? AND end_at>?',(d['screen_id'],end,start)).fetchone():raise ValueError('screen_under_maintenance')
        overlap=db.execute("SELECT id FROM booking WHERE screen_id=? AND start_at<? AND end_at>? AND status='confirmed'",(d['screen_id'],end,start)).fetchone()
        status='waitlisted' if overlap else 'confirmed'
        db.execute('INSERT INTO booking VALUES(?,?,?,?,?,?,?,?,?)',(d['id'],d['creative_id'],d['revision'],d['screen_id'],start,end,int(d['priority']),status,sid))
        return {'id':d['id'],'status':status,'blocked_by':overlap['id'] if overlap else None}

def _inside(start,end,window_start,window_end):
    return window_start<end and window_end>start

def screen_open(db,screen_id,start):
    """A screen may take new business only from a closed return-to-service time.

    A maintenance row alone proves a visit occurred.  It does not prove that
    both independent rechecks (supply and picture) closed.  Repair cases are
    therefore screen-scoped: one restored screen may be sold again while its
    sibling at another site is still paused, and past interrupted bookings are
    never rewritten.
    """
    rts=db.execute("SELECT * FROM rts WHERE screen_id=? AND status='closed' ORDER BY closed_at DESC LIMIT 1",(screen_id,)).fetchone()
    if rts and rts['closed_at']<=start:
        return None
    case=db.execute("SELECT 1 FROM repair_case_screen WHERE screen_id=?",(screen_id,)).fetchone()
    if case:
        return 'screen_awaiting_return_to_service'
    return None

def not_sellable(db,screen_id,start,end):
    return screen_open(db,screen_id,start)

def cancel(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'cancel',file)
        b=db.execute('SELECT * FROM booking WHERE id=?',(d['booking_id'],)).fetchone()
        if not b:raise ValueError('unknown_booking')
        if b['status']=='cancelled':return {'id':b['id'],'status':'cancelled','replayed':True}
        if b['status'] not in ('confirmed','waitlisted'):raise ValueError('booking_not_cancellable')
        db.execute("UPDATE booking SET status='cancelled' WHERE id=?",(b['id'],))
        candidates=db.execute("SELECT * FROM booking WHERE screen_id=? AND status='waitlisted' AND start_at<? AND end_at>? ORDER BY priority DESC,start_at,id",(b['screen_id'],b['end_at'],b['start_at'])).fetchall()
        promoted=None
        for candidate in candidates:
            conflict=db.execute("SELECT 1 FROM booking WHERE screen_id=? AND status='confirmed' AND start_at<? AND end_at>?",(b['screen_id'],candidate['end_at'],candidate['start_at'])).fetchone()
            if not conflict:
                db.execute("UPDATE booking SET status='confirmed' WHERE id=?",(candidate['id'],));promoted=candidate['id'];break
        return {'id':b['id'],'status':'cancelled','promoted':promoted}
