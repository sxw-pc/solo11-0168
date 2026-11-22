import json,os,subprocess,sys
from pathlib import Path
import pytest
from adflight import creative,schedule,playback
from adflight.report import report,audit

FIX=Path(__file__).parent.parent/'fixtures'
def write(tmp,name,data):
    p=tmp/name;p.write_text(json.dumps(data),encoding='utf-8');return p
def ready(tmp):
    db=tmp/'state.sqlite'
    for func,name in [(creative.register,'creative-ok.json'),(creative.license,'license-ok.json'),(schedule.screen,'screen-ok.json')]:func(db,FIX/name)
    return db
def booked(tmp):
    db=ready(tmp);schedule.book(db,FIX/'booking-ok.json');return db

def test_demo_contract_playback_and_replay(tmp_path):
    print(__import__('adflight').__file__)
    db=booked(tmp_path)
    assert playback.proof(db,FIX/'proof-ok.json')['seconds']==480
    result=playback.settle(db,'B1')
    assert (result['planned_seconds'],result['valid_seconds'],result['missing_seconds'])==(600,480,120)
    assert audit(db)['replay_ok']

def test_creative_same_revision_conflict_and_geofence(tmp_path):
    db=ready(tmp_path)
    with pytest.raises(ValueError,match='creative_revision_conflict'):creative.register(db,FIX/'creative-bad.json')
    other=json.loads((FIX/'booking-ok.json').read_text());other['id']='B2';other['screen_id']='S2'
    schedule.screen(db,write(tmp_path,'screen2.json',{'id':'S2','city':'C2','grade':2}))
    with pytest.raises(ValueError,match='creative_not_eligible'):schedule.book(db,write(tmp_path,'other.json',other))

def test_license_expiry_blocks_booking(tmp_path):
    db=ready(tmp_path)
    late=json.loads((FIX/'booking-ok.json').read_text());late.update(id='B2',start_at='2026-07-02T10:00:00Z',end_at='2026-07-02T10:10:00Z')
    with pytest.raises(ValueError,match='creative_not_eligible'):schedule.book(db,write(tmp_path,'late.json',late))

def test_overlap_waitlist_then_cancel_promotes(tmp_path):
    db=booked(tmp_path)
    assert schedule.book(db,FIX/'booking-bad.json')['status']=='waitlisted'
    assert schedule.cancel(db,write(tmp_path,'cancel.json',{'booking_id':'B1'}))['promoted']=='B-bad'
    states={x['id']:x['status'] for x in report(db)['state']['booking']}
    assert states=={'B1':'cancelled','B-bad':'confirmed'}
    assert audit(db)['replay_ok']

def test_maintenance_interrupts_confirmed_schedule(tmp_path):
    db=booked(tmp_path)
    maintenance={'id':'M1','screen_id':'S1','start_at':'2026-06-10T10:04:00Z','end_at':'2026-06-10T10:06:00Z'}
    assert schedule.maintenance(db,write(tmp_path,'maintenance.json',maintenance))['interrupted']==['B1']
    assert playback.proof(db,FIX/'proof-ok.json')['status']=='disputed'
    assert playback.settle(db,'B1')['missing_seconds']==600
    assert audit(db)['replay_ok']

def test_independent_monitor_and_duplicate_seconds(tmp_path):
    db=booked(tmp_path)
    with pytest.raises(ValueError,match='non_independent_or_wrong_screen'):playback.proof(db,FIX/'proof-bad.json')
    playback.proof(db,FIX/'proof-ok.json')
    duplicate=json.loads((FIX/'proof-ok.json').read_text());duplicate.update(id='P2',start_at='2026-06-10T10:05:00Z',end_at='2026-06-10T10:09:00Z')
    with pytest.raises(ValueError,match='duplicate_playback_seconds'):playback.proof(db,write(tmp_path,'duplicate.json',duplicate))
    assert len(report(db)['state']['proof'])==1

def test_withdrawn_creative_cancels_future_booking(tmp_path):
    db=booked(tmp_path)
    withdrawn={'id':'AD1','revision':2,'category':'retail','city':'C1','valid_from':'2026-06-05T00:00:00Z','valid_to':'2026-07-01T00:00:00Z','withdrawn':True}
    assert creative.register(db,write(tmp_path,'withdraw.json',withdrawn))['status']=='withdrawn'
    assert report(db)['state']['booking'][0]['status']=='cancelled'
    assert audit(db)['replay_ok']

def test_makegood_requires_equal_grade_and_proven_minutes(tmp_path):
    db=booked(tmp_path);playback.proof(db,FIX/'proof-ok.json');due=playback.settle(db,'B1')
    assert due['missing_seconds']==120
    low={'id':'S2','city':'C1','grade':1};schedule.screen(db,write(tmp_path,'low.json',low))
    b={'id':'B2','creative_id':'AD1','revision':1,'screen_id':'S2','start_at':'2026-06-11T10:00:00Z','end_at':'2026-06-11T10:10:00Z','priority':1}
    schedule.book(db,write(tmp_path,'b2.json',b))
    p={'id':'P2','booking_id':'B2','screen_id':'S2','start_at':'2026-06-11T10:00:00Z','end_at':'2026-06-11T10:10:00Z','observer':'auditor-2'}
    playback.proof(db,write(tmp_path,'p2.json',p))
    item={'id':'MG1','obligation_id':1,'booking_id':'B2'}
    with pytest.raises(ValueError,match='makegood_grade_too_low'):playback.makegood(db,write(tmp_path,'mg.json',item))

def test_proven_equal_grade_makegood_fulfills_obligation(tmp_path):
    db=booked(tmp_path);playback.proof(db,FIX/'proof-ok.json');playback.settle(db,'B1')
    schedule.screen(db,write(tmp_path,'equal.json',{'id':'S2','city':'C1','grade':3}))
    b={'id':'B2','creative_id':'AD1','revision':1,'screen_id':'S2','start_at':'2026-06-11T10:00:00Z','end_at':'2026-06-11T10:10:00Z','priority':1}
    schedule.book(db,write(tmp_path,'b2.json',b))
    p={'id':'P2','booking_id':'B2','screen_id':'S2','start_at':'2026-06-11T10:00:00Z','end_at':'2026-06-11T10:02:00Z','observer':'auditor-2'}
    playback.proof(db,write(tmp_path,'p2.json',p))
    assert playback.makegood(db,write(tmp_path,'mg.json',{'id':'MG1','obligation_id':1,'booking_id':'B2'}))['status']=='fulfilled'
    assert report(db)['state']['obligation'][0]['status']=='fulfilled'
    assert audit(db)['replay_ok']

def test_public_cli_and_local_module(tmp_path):
    env={**os.environ,'PYTHONPATH':str(Path(__file__).parent.parent)}
    p=subprocess.run([sys.executable,'-m','adflight','demo','--db',str(tmp_path/'cli.sqlite')],env=env,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    assert json.loads(p.stdout)['settlement']['missing_seconds']==120
    assert str(Path(__import__('adflight').__file__).resolve()).startswith(str(Path(__file__).parent.parent.resolve()))
