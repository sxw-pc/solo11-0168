import json,os,subprocess,sys
from pathlib import Path
import pytest
from adflight import creative,schedule,playback,auction
from adflight.report import report,audit

FIX=Path(__file__).parent.parent/'fixtures'
START,END='2026-06-10T10:00:00Z','2026-06-10T10:10:00Z'

def write(tmp,name,data):
    p=tmp/name;p.write_text(json.dumps(data),encoding='utf-8');return p

def pages():
    return [json.loads((FIX/f'auction-1-page{i}.json').read_text()) for i in (1,2)]

def snapshot():
    return auction.load_snapshot(pages())

def ready(tmp,name='state.sqlite'):
    db=tmp/name
    schedule.screen(db,FIX/'screen-ok.json');schedule.screen(db,FIX/'screen-s2.json')
    for f in ('creative-ada.json','creative-adb.json','creative-adc.json'):creative.register(db,FIX/f)
    for f in ('license-ada.json','license-adb.json','license-adc.json'):creative.license(db,FIX/f)
    return db

def ingested(tmp,objective=None,name='state.sqlite'):
    db=ready(tmp,name)
    p1,p2=pages()
    if objective:p1['objective']=p2['objective']=objective
    # portal pagination may reorder: ingest page 2 before page 1
    auction.ingest_page(db,write(tmp,'page2.json',p2))
    auction.ingest_page(db,write(tmp,'page1.json',p1))
    return db

def variant(tmp,name,mutate):
    db=ready(tmp,name)
    p1,p2=pages();mutate(p1,p2)
    auction.ingest_page(db,write(tmp,f'{name}-p1.json',p1))
    auction.ingest_page(db,write(tmp,f'{name}-p2.json',p2))
    return db

def publish(db,tmp,name='publish.json',**payload):
    return auction.publish(db,write(tmp,name,payload))

def test_naive_greedy_splits_package_and_overcharges():
    snap=snapshot()
    naive=auction.naive_greedy(snap)
    by_bid={c['bid_id']:c for c in naive['contracts']}
    # per-screen greedy splits BID-A's 900 package price (450/screen): S1 goes
    # to BID-B (600>450), S2 to BID-A (450>100) — A is charged the full 900
    # package price for half the package and welfare notice C is squeezed out.
    assert by_bid['BID-A']['screen_id']=='S2' and by_bid['BID-A']['charged']==900
    assert by_bid['BID-B']['screen_id']=='S1' and by_bid['BID-B']['charged']==600
    assert 'BID-C' not in by_bid
    assert naive['total_charged']==1500  # phantom revenue above the 900 optimum
    # after S2 fails the naive ledger has no package concept: A's 900 contract
    # stays on the books although its S2 slot can never be delivered.
    failed=auction.naive_greedy(snap,failed={'S2'})
    a=next(c for c in failed['contracts'] if c['bid_id']=='BID-A')
    assert a['charged']==900 and not a['deliverable']
    assert failed['total_charged']==1500

def test_naive_arrival_order_decides_winners():
    snap=snapshot()
    first=auction.naive_arrival(snap,['BID-A','BID-B','BID-C'])
    assert first=={'BID-A':['S1','S2'],'BID-B':[],'BID-C':[]}
    reordered=auction.naive_arrival(snap,['BID-B','BID-A','BID-C'])
    assert reordered=={'BID-B':['S1'],'BID-A':['S2'],'BID-C':[]}
    # same portal snapshot, different pagination order -> different winners,
    # and A's all-or-nothing package ends up split across clients.
    assert first!=reordered

def test_incomplete_watermark_blocks_allocation(tmp_path):
    db=ready(tmp_path)
    out=auction.ingest_page(db,FIX/'auction-1-page1.json')
    assert out['complete'] is False and (out['pages_received'],out['pages_total'])==(1,2)
    # unread pages are not zero-price bids: nothing may be allocated or published
    with pytest.raises(ValueError,match='incomplete_watermark'):auction.allocate(db,'AUC-1')
    with pytest.raises(ValueError,match='incomplete_watermark'):publish(db,tmp_path,auction_id='AUC-1')

def test_ingest_is_idempotent_and_pages_cannot_be_replaced(tmp_path):
    db=ready(tmp_path)
    auction.ingest_page(db,FIX/'auction-1-page1.json')
    assert auction.ingest_page(db,FIX/'auction-1-page1.json')['replayed'] is True
    replaced=pages()[0];replaced['bids']=replaced['bids'][:1]
    with pytest.raises(ValueError,match='auction_page_conflict'):
        auction.ingest_page(db,write(tmp_path,'replaced.json',replaced))

def test_max_revenue_route_awards_whole_package(tmp_path):
    db=ingested(tmp_path)
    preview=auction.allocate(db,'AUC-1')
    assert preview['total_amount']==900
    assert [w['bid_id'] for w in preview['winners']]==['BID-A']
    assert {s['screen_id'] for w in preview['winners'] for s in w['slots']}=={'S1','S2'}
    assert {l['bid_id']:l['reason'] for l in preview['losers']}=={'BID-B':'outbid','BID-C':'outbid'}
    out=publish(db,tmp_path,auction_id='AUC-1')
    assert out['status']=='published' and out['total_amount']==900
    assert sorted(out['bookings'])==['AUC-1-BID-A-0','AUC-1-BID-A-1']
    states={x['id']:x['status'] for x in report(db)['state']['booking']}
    assert states=={'AUC-1-BID-A-0':'confirmed','AUC-1-BID-A-1':'confirmed'}
    assert audit(db)['replay_ok']

def test_public_welfare_route_guarantees_notice_then_revenue(tmp_path):
    db=ingested(tmp_path,objective='public_welfare_first')
    preview=auction.allocate(db,'AUC-1')
    assert preview['total_amount']==700
    assert {w['bid_id'] for w in preview['winners']}=={'BID-B','BID-C'}
    assert {l['bid_id']:l['reason'] for l in preview['losers']}=={'BID-A':'outbid'}
    publish(db,tmp_path,auction_id='AUC-1')
    slots={x['id']:x for x in report(db)['state']['booking']}
    assert slots['AUC-1-BID-B-0']['screen_id']=='S1'
    assert slots['AUC-1-BID-C-0']['screen_id']=='S2'
    assert all(x['status']=='confirmed' for x in slots.values())
    assert audit(db)['replay_ok']

def test_objective_locked_once_published_to_bidders(tmp_path):
    db=ingested(tmp_path)
    shifted=pages()[0];shifted['objective']='public_welfare_first'
    with pytest.raises(ValueError,match='auction_header_conflict'):
        auction.ingest_page(db,write(tmp_path,'shifted.json',shifted))
    # allocation still follows the objective published before the deadline
    assert auction.allocate(db,'AUC-1')['objective']=='max_revenue'

def test_screen_failure_before_publish_recomputes(tmp_path):
    db=ingested(tmp_path)
    schedule.maintenance(db,write(tmp_path,'maint.json',{'id':'M1','screen_id':'S2','start_at':START,'end_at':END}))
    preview=auction.allocate(db,'AUC-1')
    reasons={l['bid_id']:l['reason'] for l in preview['losers']}
    assert reasons['BID-A']=='infeasible:screen_under_maintenance'
    assert reasons['BID-C']=='infeasible:screen_under_maintenance'
    assert [w['bid_id'] for w in preview['winners']]==['BID-B']
    out=publish(db,tmp_path,auction_id='AUC-1')
    # the package promise is re-adjudicated: A never gets S1 alone for 900
    assert out['total_amount']==600 and out['bookings']==['AUC-1-BID-B-0']
    assert audit(db)['replay_ok']

def test_screen_failure_after_publish_becomes_obligation(tmp_path):
    db=ingested(tmp_path)
    publish(db,tmp_path,auction_id='AUC-1')
    hit=schedule.maintenance(db,write(tmp_path,'maint.json',{'id':'M1','screen_id':'S2','start_at':'2026-06-10T10:04:00Z','end_at':'2026-06-10T10:06:00Z'}))
    assert hit['interrupted']==['AUC-1-BID-A-1']
    due=playback.settle(db,'AUC-1-BID-A-1')
    assert due['missing_seconds']==600 and due['status']=='due'
    assert report(db)['state']['obligation'][0]['reason']=='maintenance_interrupted'
    # the published auction is not silently reopened: the same event replays,
    # a different publish payload is rejected, the allocation stays committed.
    assert publish(db,tmp_path,auction_id='AUC-1')['replayed'] is True
    with pytest.raises(ValueError,match='auction_already_published'):
        publish(db,tmp_path,name='publish2.json',auction_id='AUC-1',note='retry')
    with pytest.raises(ValueError,match='auction_already_published'):
        auction.allocate(db,'AUC-1')
    assert audit(db)['replay_ok']

def test_correction_after_deadline_recorded_not_applied(tmp_path):
    db=ingested(tmp_path)
    out=auction.correct(db,FIX/'auction-1-correction.json')
    assert out=={'id':'COR-1','status':'recorded','applied':False}
    assert auction.allocate(db,'AUC-1')['total_amount']==900  # not 1200
    assert report(db)['state']['correction'][0]['note'].startswith('agency-a')
    early=json.loads((FIX/'auction-1-correction.json').read_text())
    early.update(id='COR-0',submitted_at='2026-06-09T10:00:00Z')
    with pytest.raises(ValueError,match='correction_must_follow_deadline'):
        auction.correct(db,write(tmp_path,'early.json',early))

def test_withdrawn_version_before_deadline_excludes_bid(tmp_path):
    def m(p1,p2):
        p1['bids'].append({**pages()[0]['bids'][0],'version':2,'withdrawn':True,'submitted_at':'2026-06-09T12:00:00Z'})
    db=variant(tmp_path,'withdrawn.sqlite',m)
    preview=auction.allocate(db,'AUC-1')
    assert {l['bid_id']:l['reason'] for l in preview['losers']}=={'BID-A':'withdrawn'}
    assert {w['bid_id'] for w in preview['winners']}=={'BID-B','BID-C'}
    assert preview['total_amount']==700

def test_post_deadline_version_stays_recorded_but_sealed(tmp_path):
    def m(p1,p2):
        p1['bids'].append({**pages()[0]['bids'][0],'version':2,'amount':1200,'submitted_at':'2026-06-09T20:00:00Z'})
    db=variant(tmp_path,'late-version.sqlite',m)
    assert auction.allocate(db,'AUC-1')['total_amount']==900  # sealed v1, not the post-deadline 1200
    versions=sorted((r['version'],r['amount']) for r in report(db)['state']['bid'] if r['id']=='BID-A')
    assert versions==[(1,900),(2,1200)]  # the correction is on record, not applied

def test_late_bid_budget_cap_and_qualification(tmp_path):
    def late(p1,p2):p1['bids'][1]['submitted_at']='2026-06-09T19:00:00Z'
    db=variant(tmp_path,'late.sqlite',late)
    assert {l['bid_id']:l['reason'] for l in auction.allocate(db,'AUC-1')['losers']}=={'BID-B':'late','BID-C':'outbid'}
    def budget(p1,p2):p1['bids'][1]['budget_cap']=500
    db=variant(tmp_path,'budget.sqlite',budget)
    assert {l['bid_id']:l['reason'] for l in auction.allocate(db,'AUC-1')['losers']}=={'BID-B':'budget_exceeded','BID-C':'outbid'}
    def qual(p1,p2):p1['bids'][1]['qualification']='unverified'
    db=variant(tmp_path,'qual.sqlite',qual)
    assert {l['bid_id']:l['reason'] for l in auction.allocate(db,'AUC-1')['losers']}=={'BID-B':'qualification_missing','BID-C':'outbid'}

def test_one_of_package_awards_exactly_one_option(tmp_path):
    db=ready(tmp_path)
    slot=lambda screen,creative:{'screen_id':screen,'start_at':START,'end_at':END,'creative_id':creative,'revision':1,'priority':5}
    page={'auction_id':'AUC-2','objective':'max_revenue','deadline':'2026-06-09T18:00:00Z','page':1,'pages_total':1,
          'accepted_qualifications':['agency-licensed'],
          'bids':[{'id':'BID-D','agent':'agency-d','submitted_at':'2026-06-09T09:00:00Z','version':1,'amount':500,
                   'budget_cap':600,'qualification':'agency-licensed','mode':'one_of',
                   'slots':[slot('S1','ADB'),slot('S2','ADB')]},
                  {'id':'BID-E','agent':'agency-e','submitted_at':'2026-06-09T09:30:00Z','version':1,'amount':400,
                   'budget_cap':600,'qualification':'agency-licensed','mode':'all_or_nothing',
                   'slots':[slot('S1','ADA')]}]}
    auction.ingest_page(db,write(tmp_path,'oneof.json',page))
    preview=auction.allocate(db,'AUC-2')
    winners={w['bid_id']:w for w in preview['winners']}
    assert set(winners)=={'BID-D','BID-E'} and preview['total_amount']==900
    # the one-of package takes exactly one alternative even though both were free
    assert len(winners['BID-D']['slots'])==1
    assert winners['BID-D']['slots'][0]['screen_id']=='S2'
    assert winners['BID-E']['slots'][0]['screen_id']=='S1'
    out=auction.publish(db,write(tmp_path,'pub.json',{'auction_id':'AUC-2'}))
    assert len(out['bookings'])==2
    assert audit(db)['replay_ok']

def test_existing_confirmed_bookings_constrain_capacity(tmp_path):
    db=ready(tmp_path)
    creative.register(db,FIX/'creative-ok.json');creative.license(db,FIX/'license-ok.json')
    schedule.book(db,FIX/'booking-ok.json')  # B1 confirmed on S1 10:00-10:10
    for i,p in enumerate(pages(),1):auction.ingest_page(db,write(tmp_path,f'p{i}.json',p))
    preview=auction.allocate(db,'AUC-1')
    reasons={l['bid_id']:l['reason'] for l in preview['losers']}
    assert reasons=={'BID-A':'infeasible:capacity_conflict','BID-B':'infeasible:capacity_conflict'}
    assert [w['bid_id'] for w in preview['winners']]==['BID-C']
    assert preview['total_amount']==100

def test_allocation_is_recomputable(tmp_path):
    db=ingested(tmp_path)
    first=auction.allocate(db,'AUC-1')
    assert auction.allocate(db,'AUC-1')==first
    publish(db,tmp_path,auction_id='AUC-1')
    stored=report(db)['state']['allocation'][0]
    assert stored['fingerprint']==first['fingerprint']
    assert json.loads(stored['winners'])==first['winners']
    assert json.loads(stored['losers'])==first['losers']
    assert stored['total_amount']==first['total_amount']==900

def test_auction_cli(tmp_path):
    env={**os.environ,'PYTHONPATH':str(Path(__file__).parent.parent)}
    db=str(tmp_path/'cli.sqlite')
    def run(*args):
        p=subprocess.run([sys.executable,'-m','adflight',*args],env=env,capture_output=True,text=True)
        assert p.returncode==0,p.stderr
        return json.loads(p.stdout)
    for f in ('screen-ok.json','screen-s2.json'):run('screen-import',str(FIX/f),'--db',db)
    for f in ('creative-ada.json','creative-adb.json','creative-adc.json'):run('creative-import',str(FIX/f),'--db',db)
    for f in ('license-ada.json','license-adb.json','license-adc.json'):run('license-import',str(FIX/f),'--db',db)
    run('auction-ingest',str(FIX/'auction-1-page2.json'),'--db',db)
    run('auction-ingest',str(FIX/'auction-1-page1.json'),'--db',db)
    assert run('auction-allocate','--db',db,'--auction','AUC-1')['total_amount']==900
    out=run('auction-publish','--db',db,'--auction','AUC-1')
    assert out['status']=='published' and len(out['bookings'])==2
    assert run('audit','--db',db)['replay_ok']
