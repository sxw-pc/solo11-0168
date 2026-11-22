"""Offline sealed-bid package auction ingestion and allocation.

The operator ingests paginated snapshots exported from an agency's local
mock bidding portal. Pages may arrive reordered; allocation is blocked
until the watermark (every declared page) is complete, so bids not yet
read are never treated as zero-price. Bids are sealed at the published
deadline: versions or withdrawals submitted after it stay on record but
never change the sealed view, and post-deadline corrections live in a
separate ledger instead of editing published bids.

Allocation first verifies creative licenses and screen maintenance
windows for every requested slot, then jointly chooses a fulfillable set
of packages under the auction's pre-published objective. An
all-or-nothing package is awarded every slot or none, a one-of package
at most one of its alternatives, and no screen second is sold to two
packages. ``publish`` commits the allocation and creates the concrete
bookings in one transaction; once published, a screen failure is handled
by the maintenance/obligation flow, never by silently reopening the
auction.
"""
import hashlib,json
from itertools import product
from .store import connect,source,already,at
from .creative import eligible
from .schedule import _book

OBJECTIVES=('max_revenue','public_welfare_first')
MODES=('all_or_nothing','one_of')

def _auction(db,auction_id):
    return db.execute('SELECT * FROM auction WHERE id=?',(auction_id,)).fetchone()

def _validate_page(d):
    for k in ('auction_id','objective','deadline','page','pages_total','accepted_qualifications','bids'):
        if k not in d:raise ValueError('missing_page_field')
    if d['objective'] not in OBJECTIVES:raise ValueError('unknown_objective')
    at(d['deadline'])
    if not 1<=int(d['page'])<=int(d['pages_total']):raise ValueError('invalid_page_number')
    if not d['accepted_qualifications']:raise ValueError('qualification_required')

def _validate_bid(bid):
    for k in ('id','agent','submitted_at','version','amount','budget_cap','qualification','mode','slots'):
        if k not in bid:raise ValueError('missing_bid_field')
    at(bid['submitted_at'])
    if int(bid['version'])<1:raise ValueError('invalid_bid_version')
    if int(bid['amount'])<0 or int(bid['budget_cap'])<0:raise ValueError('invalid_amount')
    if bid['mode'] not in MODES:raise ValueError('unknown_package_mode')
    if not bid['slots']:raise ValueError('package_slots_required')
    for s in bid['slots']:
        for k in ('screen_id','creative_id','revision','start_at','end_at'):
            if k not in s:raise ValueError('missing_slot_field')
        if at(s['start_at'])>=at(s['end_at']):raise ValueError('invalid_slot_window')

def _slots_of(db,auction_id,bid_id,version):
    return [dict(r) for r in db.execute(
        'SELECT idx,screen_id,start_at,end_at,creative_id,revision,priority FROM bid_slot WHERE auction_id=? AND bid_id=? AND version=? ORDER BY idx',
        (auction_id,bid_id,version))]

def _same_bid(db,row,bid):
    slots=_slots_of(db,row['auction_id'],row['id'],row['version'])
    want=[{'idx':i,'screen_id':s['screen_id'],'start_at':s['start_at'],'end_at':s['end_at'],
           'creative_id':s['creative_id'],'revision':int(s['revision']),'priority':int(s.get('priority',0))}
          for i,s in enumerate(bid['slots'])]
    return (row['agent']==bid['agent'] and row['submitted_at']==bid['submitted_at']
            and row['withdrawn']==(1 if bid.get('withdrawn') else 0) and row['amount']==int(bid['amount'])
            and row['budget_cap']==int(bid['budget_cap']) and row['qualification']==bid['qualification']
            and row['public_welfare']==(1 if bid.get('public_welfare') else 0) and row['mode']==bid['mode']
            and slots==want)

def _ingest_bid(db,auction_id,bid,sid):
    _validate_bid(bid)
    old=db.execute('SELECT * FROM bid WHERE auction_id=? AND id=? AND version=?',(auction_id,bid['id'],int(bid['version']))).fetchone()
    if old:
        if not _same_bid(db,old,bid):raise ValueError('bid_version_conflict')
        return
    db.execute('INSERT INTO bid VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (auction_id,bid['id'],int(bid['version']),bid['agent'],bid['submitted_at'],
                1 if bid.get('withdrawn') else 0,int(bid['amount']),int(bid['budget_cap']),
                bid['qualification'],1 if bid.get('public_welfare') else 0,bid['mode'],sid))
    for idx,s in enumerate(bid['slots']):
        db.execute('INSERT INTO bid_slot VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (auction_id,bid['id'],int(bid['version']),idx,s['screen_id'],s['start_at'],s['end_at'],
                    s['creative_id'],int(s['revision']),int(s.get('priority',0)),sid))

def ingest_page(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'auction_page',file)
        if replay:return {'auction_id':d['auction_id'],'page':d['page'],'replayed':True}
        _validate_page(d)
        quals=sorted(d['accepted_qualifications'])
        a=_auction(db,d['auction_id'])
        if a:
            if a['status']=='published':raise ValueError('auction_already_published')
            if (a['objective'],a['deadline'],a['pages_total'],json.loads(a['accepted_qualifications']))!=(d['objective'],d['deadline'],int(d['pages_total']),quals):
                raise ValueError('auction_header_conflict')
        else:
            db.execute('INSERT INTO auction VALUES(?,?,?,?,?,?,?)',
                       (d['auction_id'],d['objective'],d['deadline'],int(d['pages_total']),json.dumps(quals),'collecting',sid))
        old=db.execute('SELECT source_id FROM auction_page WHERE auction_id=? AND page=?',(d['auction_id'],int(d['page']))).fetchone()
        if old:
            if old['source_id']!=sid:raise ValueError('auction_page_conflict')
            return {'auction_id':d['auction_id'],'page':d['page'],'replayed':True}
        db.execute('INSERT INTO auction_page VALUES(?,?,?,?)',(d['auction_id'],int(d['page']),len(d['bids']),sid))
        for bid in d['bids']:_ingest_bid(db,d['auction_id'],bid,sid)
        got=db.execute('SELECT COUNT(*) FROM auction_page WHERE auction_id=?',(d['auction_id'],)).fetchone()[0]
        return {'auction_id':d['auction_id'],'page':int(d['page']),'pages_received':got,
                'pages_total':int(d['pages_total']),'complete':got==int(d['pages_total'])}

def correct(path,file):
    """Record a post-deadline correction in its own ledger. Corrections are
    kept for audit but never applied to the sealed, published bids."""
    with connect(path) as db,db:
        sid,d,replay=source(db,'correction',file)
        if already(db,'correction',d['id'],sid):return {'id':d['id'],'replayed':True}
        a=_auction(db,d['auction_id'])
        if not a:raise ValueError('unknown_auction')
        if not db.execute('SELECT 1 FROM bid WHERE auction_id=? AND id=?',(d['auction_id'],d['bid_id'])).fetchone():raise ValueError('unknown_bid')
        if at(d['submitted_at'])<=a['deadline']:raise ValueError('correction_must_follow_deadline')
        db.execute('INSERT INTO correction VALUES(?,?,?,?,?,?)',(d['id'],d['auction_id'],d['bid_id'],d.get('note',''),d['submitted_at'],sid))
        return {'id':d['id'],'status':'recorded','applied':False}

def _watermark(db,auction_id):
    a=_auction(db,auction_id)
    if not a:raise ValueError('unknown_auction')
    got=db.execute('SELECT COUNT(*) FROM auction_page WHERE auction_id=?',(auction_id,)).fetchone()[0]
    if got<a['pages_total']:raise ValueError('incomplete_watermark')
    return a

def _sealed(db,a):
    """Latest version of each bid submitted by the deadline. Later versions
    stay recorded in the bid ledger but never change the sealed view."""
    by_id={}
    for r in db.execute('SELECT * FROM bid WHERE auction_id=? ORDER BY id,version',(a['id'],)).fetchall():
        by_id.setdefault(r['id'],[]).append(r)
    sealed,late=[],[]
    for versions in by_id.values():
        ontime=[v for v in versions if v['submitted_at']<=a['deadline']]
        (sealed if ontime else late).append(ontime[-1] if ontime else versions[-1])
    return sealed,late

def _slot_reason(db,slot):
    screen=db.execute('SELECT * FROM screen WHERE id=?',(slot['screen_id'],)).fetchone()
    if not screen:return 'unknown_screen'
    if db.execute('SELECT 1 FROM maintenance WHERE screen_id=? AND start_at<? AND end_at>?',(slot['screen_id'],slot['end_at'],slot['start_at'])).fetchone():return 'screen_under_maintenance'
    reason=eligible(db,slot['creative_id'],slot['revision'],screen['city'],slot['start_at'],slot['end_at'])
    if reason:return reason
    if db.execute("SELECT 1 FROM booking WHERE screen_id=? AND status='confirmed' AND start_at<? AND end_at>?",(slot['screen_id'],slot['end_at'],slot['start_at'])).fetchone():return 'capacity_conflict'
    return None

def _overlap(a,b):
    return a['screen_id']==b['screen_id'] and a['start_at']<b['end_at'] and a['end_at']>b['start_at']

def _conflict(slots):
    return any(_overlap(slots[i],slots[j]) for i in range(len(slots)) for j in range(i+1,len(slots)))

def _feasible(db,bid,slots):
    if bid['mode']=='all_or_nothing':
        if _conflict(slots):return None,'infeasible:self_overlap'
        for s in slots:
            reason=_slot_reason(db,s)
            if reason:return None,'infeasible:'+reason
        return {'options':None},None
    ok=[s for s in slots if _slot_reason(db,s) is None]
    if not ok:return None,'infeasible:'+_slot_reason(db,slots[0])
    return {'options':ok},None

def _optimize(candidates,objective):
    """Exhaustive deterministic search over package subsets (and one-of
    alternatives). Mock scale only; ties break on sorted bid ids/screens."""
    best=None
    for mask in range(1<<len(candidates)):
        chosen=[c for i,c in enumerate(candidates) if mask>>i&1]
        for combo in product(*[(c['options'] if c['options'] is not None else [None]) for c in chosen]or[()]):
            slots=[s for c,opt in zip(chosen,combo) for s in ([opt] if opt is not None else c['slots'])]
            if _conflict(slots):continue
            welfare=sum(1 for c in chosen if c['bid']['public_welfare'])
            total=sum(c['bid']['amount'] for c in chosen)
            score=(total,) if objective=='max_revenue' else (welfare,total)
            tie=(tuple(sorted(c['bid']['id'] for c in chosen)),tuple(sorted((s['screen_id'],s['start_at'],s['end_at']) for s in slots)))
            if best is None or score>best[0] or (score==best[0] and tie<best[1]):best=(score,tie,list(zip(chosen,combo)))
    winners=[(c,[opt] if opt is not None else c['slots']) for c,opt in best[2]]
    return winners,best[0][-1]

def _booking_slot(auction_id,bid_id,slot):
    return {'booking_id':f'{auction_id}-{bid_id}-{slot["idx"]}','screen_id':slot['screen_id'],'start_at':slot['start_at'],
            'end_at':slot['end_at'],'creative_id':slot['creative_id'],'revision':slot['revision'],'priority':slot['priority']}

def _fingerprint(db,a,sealed):
    def rows(t):return [dict(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1')]
    payload={'objective':a['objective'],
             'bids':[{**dict(v),'slots':_slots_of(db,a['id'],v['id'],v['version'])} for v in sealed],
             'screen':rows('screen'),'maintenance':rows('maintenance'),'creative':rows('creative'),
             'license':rows('license'),
             'confirmed':[dict(r) for r in db.execute("SELECT * FROM booking WHERE status='confirmed' ORDER BY id")]}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()

def _allocate(db,auction_id):
    a=_watermark(db,auction_id)
    quals=json.loads(a['accepted_qualifications'])
    sealed,late=_sealed(db,a)
    losers=[{'bid_id':v['id'],'agent':v['agent'],'reason':'late'} for v in late]
    candidates=[]
    for v in sealed:
        entry={'bid_id':v['id'],'agent':v['agent']}
        slots=_slots_of(db,a['id'],v['id'],v['version'])
        if v['withdrawn']:losers.append({**entry,'reason':'withdrawn'});continue
        if v['amount']>v['budget_cap']:losers.append({**entry,'reason':'budget_exceeded'});continue
        if v['qualification'] not in quals:losers.append({**entry,'reason':'qualification_missing'});continue
        plan,reason=_feasible(db,v,slots)
        if reason:losers.append({**entry,'reason':reason});continue
        candidates.append({'bid':v,'slots':slots,'options':plan['options']})
    winners,total=_optimize(candidates,a['objective'])
    won={c['bid']['id'] for c,_ in winners}
    losers+=[{'bid_id':c['bid']['id'],'agent':c['bid']['agent'],'reason':'outbid'} for c in candidates if c['bid']['id'] not in won]
    result={'auction_id':a['id'],'objective':a['objective'],'total_amount':total,
            'winners':sorted(({'bid_id':c['bid']['id'],'agent':c['bid']['agent'],'amount':c['bid']['amount'],
                               'public_welfare':bool(c['bid']['public_welfare']),
                               'slots':[_booking_slot(a['id'],c['bid']['id'],s) for s in chosen]}
                              for c,chosen in winners),key=lambda w:w['bid_id']),
            'losers':sorted(losers,key=lambda l:l['bid_id'])}
    result['fingerprint']=_fingerprint(db,a,sealed)
    return result

def allocate(path,auction_id):
    """Read-only, recomputable preview of the adjudication with win/loss reasons."""
    with connect(path) as db:
        a=_auction(db,auction_id)
        if not a:raise ValueError('unknown_auction')
        if a['status']=='published':raise ValueError('auction_already_published')
        return _allocate(db,auction_id)

def publish(path,file):
    """Commit the allocation and create every winning booking atomically.
    Recomputes from current state, so a screen failing before publication
    changes the outcome instead of leaving a stale contract; after
    publication the auction can never be silently reopened."""
    with connect(path) as db,db:
        sid,d,replay=source(db,'publish',file)
        if 'auction_id' not in d:raise ValueError('missing_auction_id')
        a=_auction(db,d['auction_id'])
        if not a:raise ValueError('unknown_auction')
        if replay:
            row=db.execute('SELECT total_amount FROM allocation WHERE auction_id=?',(a['id'],)).fetchone()
            out={'auction_id':a['id'],'replayed':True}
            if row:out.update(status='published',total_amount=row['total_amount'])
            return out
        if a['status']=='published':raise ValueError('auction_already_published')
        result=_allocate(db,a['id'])
        db.execute('INSERT INTO allocation VALUES(?,?,?,?,?,?,?)',
                   (a['id'],result['fingerprint'],a['objective'],result['total_amount'],
                    json.dumps(result['winners'],sort_keys=True),json.dumps(result['losers'],sort_keys=True),sid))
        bookings=[]
        for w in result['winners']:
            for s in w['slots']:
                doc={'id':s['booking_id'],'creative_id':s['creative_id'],'revision':s['revision'],
                     'screen_id':s['screen_id'],'start_at':s['start_at'],'end_at':s['end_at'],'priority':s['priority']}
                if _book(db,doc,sid)['status']!='confirmed':raise ValueError('publish_conflict')
                bookings.append(s['booking_id'])
        db.execute("UPDATE auction SET status='published' WHERE id=?",(a['id'],))
        return {'auction_id':a['id'],'status':'published','total_amount':result['total_amount'],
                'bookings':bookings,'winners':result['winners'],'losers':result['losers']}

def load_snapshot(pages):
    """Aggregate portal pages (any order) into one canonical snapshot."""
    pages=sorted(pages,key=lambda p:p['page'])
    snap={k:pages[0][k] for k in ('auction_id','objective','deadline')}
    snap['bids']=[bid for p in pages for bid in p['bids']]
    return snap

def naive_greedy(snapshot,failed=()):
    """Baseline: each screen goes to the highest per-screen share of a bid.
    Splits package prices across screens and ignores all-or-nothing/one-of
    semantics, licenses and screen health, so it overcharges and leaves
    undeliverable contracts standing."""
    offers={}
    for bid in snapshot['bids']:
        share=bid['amount']/len(bid['slots'])
        for slot in bid['slots']:offers.setdefault(slot['screen_id'],[]).append((share,bid))
    contracts=[]
    for screen_id in sorted(offers):
        share,bid=sorted(offers[screen_id],key=lambda o:(-o[0],o[1]['id']))[0]
        contracts.append({'screen_id':screen_id,'bid_id':bid['id'],'charged':bid['amount'],
                          'share':share,'deliverable':screen_id not in set(failed)})
    return {'contracts':contracts,'total_charged':sum(c['charged'] for c in contracts)}

def naive_arrival(snapshot,order):
    """Baseline: books slot-by-slot in arrival order, so portal pagination
    order decides the winners and packages get split across clients."""
    occupied={}
    confirmed={}
    for bid_id in order:
        bid=next(b for b in snapshot['bids'] if b['id']==bid_id)
        got=[]
        for slot in bid['slots']:
            wins=occupied.setdefault(slot['screen_id'],[])
            if any(slot['start_at']<e and slot['end_at']>s for s,e in wins):continue
            wins.append((slot['start_at'],slot['end_at']));got.append(slot['screen_id'])
        confirmed[bid_id]=got
    return confirmed
