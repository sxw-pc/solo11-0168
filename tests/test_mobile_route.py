import json,os,subprocess,sys
from pathlib import Path
import pytest
from adflight import creative,schedule,playback,mobile_route
from adflight.report import report,audit,route_report

FIX=Path(__file__).parent.parent/'fixtures'
def write(tmp,name,data):
    p=tmp/name;p.write_text(json.dumps(data),encoding='utf-8');return p
def booked(tmp):
    db=tmp/'state.sqlite'
    for func,name in [(creative.register,'creative-ok.json'),(creative.license,'license-ok.json'),
                      (schedule.screen,'screen-ok.json'),(schedule.book,'booking-ok.json')]:func(db,FIX/name)
    return db
def tracked(tmp):
    db=booked(tmp)
    for name in ('gps-package.json','calibration.json','map-v1.json'):getattr(mobile_route,{'gps-package.json':'gps_import','calibration.json':'calibration_import','map-v1.json':'map_import'}[name])(db,FIX/name)
    return db
def adjudicated(tmp):
    db=tracked(tmp);mobile_route.adjudicate(db,FIX/'adjudication.json');return db
def segment(rep,start,end):
    return next(s for s in rep['segments'] if s['start']==start and s['end']==end)
def claim(rep,booking,start,end):
    c=next(x for x in rep['permit_claims'] if x['booking_id']==booking)
    return next(s for s in c['segments'] if s['start']==start and s['end']==end)

INNER=('main_w','inner_a','inner_b','main_e');OUTER=('main_w','outer','main_e')

def test_same_bytes_two_complete_feasible_worlds(tmp_path):
    db=adjudicated(tmp_path)
    rep=route_report(db,'P1')
    # one shared-bias correction of the raw frames, two full network-connected trajectories
    assert rep['correction']['bias_seconds']==2.0
    assert [f['at'] for f in rep['frames']]==['2026-06-10T10:00:00Z','2026-06-10T10:02:00Z','2026-06-10T10:04:00Z']
    roads=sorted(tuple(c['roads']) for c in rep['candidates'])
    assert roads==sorted([INNER,OUTER])
    for c in rep['candidates']:
        assert c['within_limit'] and c['max_frame_error_m']<=50.0
    # local speed filtering cannot separate the two parallel roads: both pass
    assert {round(c['max_speed_ms'],1) for c in rep['candidates']}=={5.0}
    # no spliced fake route: no candidate mixes the inner entry with the outer exit
    for c in rep['candidates']:
        assert not ({'inner_a','inner_b'}&set(c['roads']) and 'outer' in c['roads'])
    # inner world must go dark 10:02-10:03, outer world may play: segment is ambiguous
    assert segment(rep,'2026-06-10T10:02:00Z','2026-06-10T10:03:00Z')['verdict']=='ambiguous'
    assert segment(rep,'2026-06-10T10:00:00Z','2026-06-10T10:01:00Z')['verdict']=='permitted'
    # the report keeps both feasible trajectories and their common safe zone
    assert rep['safe_zone']==[{'start':'2026-06-10T10:00:00Z','end':'2026-06-10T10:02:00Z'},
                              {'start':'2026-06-10T10:03:00Z','end':'2026-06-10T10:04:00Z'}]
    assert audit(db)['replay_ok']

def test_nearest_road_releases_wrongly_in_one_world(tmp_path):
    db=adjudicated(tmp_path)
    rep=route_report(db,'P1')
    # per-frame nearest road + shortest path picks the outer road as a single fake fact
    assert rep['nearest_route']['roads']==list(OUTER)
    assert rep['nearest_route']['would_release']==[{'start':'2026-06-10T10:00:00Z','end':'2026-06-10T10:04:00Z'}]
    # ...which is a wrong release in the inner world (school no-broadcast 10:02-10:03)
    assert rep['nearest_route']['unsound_release']==[{'start':'2026-06-10T10:02:00Z','end':'2026-06-10T10:03:00Z'}]
    inner=next(c for c in rep['candidates'] if 'inner_b' in c['roads'])
    assert any(z['zone']=='school_no_broadcast' and z['start']=='2026-06-10T10:02:00Z' and z['end']=='2026-06-10T10:03:00Z' for z in inner['zones'])

def test_ambiguous_window_is_not_claimable_without_evidence(tmp_path):
    db=adjudicated(tmp_path)
    rep=route_report(db,'P1')
    assert not claim(rep,'B1','2026-06-10T10:02:00Z','2026-06-10T10:03:00Z')['claimable']
    assert claim(rep,'B1','2026-06-10T10:00:00Z','2026-06-10T10:01:00Z')['claimable']
    # beyond the captured window there is no route evidence at all
    tail=claim(rep,'B1','2026-06-10T10:04:00Z','2026-06-10T10:10:00Z')
    assert tail['verdict']=='insufficient_evidence' and not tail['claimable']

def test_avoid_route_camera_releases_permit(tmp_path):
    db=adjudicated(tmp_path)
    out=mobile_route.decide(db,FIX/'decision-avoid.json')
    assert out['status']=='pending' and out['branch_at']=='2026-06-10T10:01:00Z'
    rep=route_report(db,'P1')
    assert segment(rep,'2026-06-10T10:02:00Z','2026-06-10T10:03:00Z')['verdict']=='ambiguous'
    mobile_route.camera_import(db,FIX/'camera-outer.json')
    rep=route_report(db,'P1')
    # independent intersection camera proves the outer entry: permit stays valid
    assert segment(rep,'2026-06-10T10:02:00Z','2026-06-10T10:03:00Z')['verdict']=='released'
    assert all(s['claimable'] for s in rep['permit_claims'][0]['segments'][:3])
    assert rep['decision']['status']=='finalized'
    assert audit(db)['replay_ok']

def test_decision_must_predate_the_branch(tmp_path):
    db=adjudicated(tmp_path)
    late=json.loads((FIX/'decision-avoid.json').read_text());late.update(id='D-late',decided_at='2026-06-10T10:02:00Z')
    with pytest.raises(ValueError,match='decision_after_branch'):
        mobile_route.decide(db,write(tmp_path,'late.json',late))
    # a camera on the wrong road cannot finalize the avoidance either
    mobile_route.decide(db,FIX/'decision-avoid.json')
    wrong=json.loads((FIX/'camera-outer.json').read_text());wrong.update(id='CAM2',road_id='inner_a',node='A')
    mobile_route.camera_import(db,write(tmp_path,'wrong-cam.json',wrong))
    assert route_report(db,'P1')['decision']['status']=='pending'
    assert not claim(route_report(db,'P1'),'B1','2026-06-10T10:02:00Z','2026-06-10T10:03:00Z')['claimable']

def test_pause_route_stop_receipt_and_makegood(tmp_path):
    db=adjudicated(tmp_path)
    mobile_route.decide(db,FIX/'decision-pause.json')
    mobile_route.stop_import(db,FIX/'stop-receipt.json')
    rep=route_report(db,'P1')
    # hardware stop receipt: the ambiguous stretch is verifiably dark, not permitted
    assert segment(rep,'2026-06-10T10:02:00Z','2026-06-10T10:03:00Z')['verdict']=='stopped'
    assert not claim(rep,'B1','2026-06-10T10:02:00Z','2026-06-10T10:03:00Z')['claimable']
    assert rep['stop']['attestation']=='hardware'
    # customer make-good credit for the whole 10:01-10:04 dark window
    obligations=report(db)['state']['obligation']
    assert len(obligations)==1 and obligations[0]['missing_seconds']==180
    assert obligations[0]['reason']=='no_broadcast_pause' and obligations[0]['status']=='due'
    # the credit is fulfilled through the existing makegood flow
    schedule.screen(db,write(tmp_path,'equal.json',{'id':'S2','city':'C1','grade':3}))
    b={'id':'B2','creative_id':'AD1','revision':1,'screen_id':'S2','start_at':'2026-06-11T10:00:00Z','end_at':'2026-06-11T10:10:00Z','priority':1}
    schedule.book(db,write(tmp_path,'b2.json',b))
    p={'id':'P2','booking_id':'B2','screen_id':'S2','start_at':'2026-06-11T10:00:00Z','end_at':'2026-06-11T10:03:00Z','observer':'auditor-2'}
    playback.proof(db,write(tmp_path,'p2.json',p))
    assert playback.makegood(db,write(tmp_path,'mg.json',{'id':'MG1','obligation_id':obligations[0]['id'],'booking_id':'B2'}))['status']=='fulfilled'
    assert audit(db)['replay_ok']

def test_stop_receipt_must_cover_commanded_window_on_same_device(tmp_path):
    db=adjudicated(tmp_path)
    mobile_route.decide(db,FIX/'decision-pause.json')
    bad=json.loads((FIX/'stop-receipt.json').read_text());bad.update(id='SR2',device_id='DEV8')
    with pytest.raises(ValueError,match='stop_device_mismatch'):
        mobile_route.stop_import(db,write(tmp_path,'bad-device.json',bad))
    short=json.loads((FIX/'stop-receipt.json').read_text());short.update(id='SR3',end_at='2026-06-10T10:03:00Z')
    with pytest.raises(ValueError,match='stop_receipt_incomplete'):
        mobile_route.stop_import(db,write(tmp_path,'short.json',short))
    soft=json.loads((FIX/'stop-receipt.json').read_text());soft.update(id='SR4',attestation='software-flag')
    with pytest.raises(ValueError,match='hardware_receipt_required'):
        mobile_route.stop_import(db,write(tmp_path,'soft.json',soft))
    assert route_report(db,'P1')['decision']['status']=='pending'

def test_map_revision_readjudicates_only_affected_windows(tmp_path):
    db=adjudicated(tmp_path)
    mobile_route.decide(db,FIX/'decision-pause.json')
    mobile_route.stop_import(db,FIX/'stop-receipt.json')
    mobile_route.map_import(db,FIX/'map-v2.json')
    rep=route_report(db,'P1')
    current={(r['start_at'],r['end_at']):(r['verdict'],r['map_version']) for r in rep['rulings']['current']}
    # school zone extended over inner_a: only 10:01-10:02 is re-adjudicated under MAP2
    assert current[('2026-06-10T10:01:00Z','2026-06-10T10:02:00Z')]==('ambiguous','MAP2')
    # unaffected windows keep the original map version as current
    assert current[('2026-06-10T10:00:00Z','2026-06-10T10:01:00Z')]==('permitted','MAP1')
    assert current[('2026-06-10T10:03:00Z','2026-06-10T10:04:00Z')]==('permitted','MAP1')
    # the enforcement receipt (stopped window) keeps its original version
    assert current[('2026-06-10T10:02:00Z','2026-06-10T10:03:00Z')]==('stopped','MAP1')
    # history preserves the superseded MAP1 ruling
    assert any(r['start_at']=='2026-06-10T10:01:00Z' and r['verdict']=='permitted' and r['map_version']=='MAP1'
               for r in rep['rulings']['history'])
    assert audit(db)['replay_ok']

def test_map_revision_keeps_released_ruling_version(tmp_path):
    db=adjudicated(tmp_path)
    mobile_route.decide(db,FIX/'decision-avoid.json')
    mobile_route.camera_import(db,FIX/'camera-outer.json')
    mobile_route.map_import(db,FIX/'map-v2.json')
    rep=route_report(db,'P1')
    current={(r['start_at'],r['end_at']):(r['verdict'],r['map_version']) for r in rep['rulings']['current']}
    # camera-released window is enforcement-grade: it keeps MAP1
    assert current[('2026-06-10T10:02:00Z','2026-06-10T10:03:00Z')]==('released','MAP1')
    # only the affected 10:01-10:02 window moves to MAP2
    assert current[('2026-06-10T10:01:00Z','2026-06-10T10:02:00Z')]==('ambiguous','MAP2')
    assert audit(db)['replay_ok']

def test_missing_calibration_blocks_adjudication(tmp_path):
    db=booked(tmp_path)
    mobile_route.gps_import(db,FIX/'gps-package.json')
    with pytest.raises(ValueError,match='calibration_missing'):
        mobile_route.adjudicate(db,FIX/'adjudication.json')

def test_gps_sequence_and_independent_issuers(tmp_path):
    db=booked(tmp_path)
    bad=json.loads((FIX/'gps-package.json').read_text());bad['frames']=[bad['frames'][1],bad['frames'][0],bad['frames'][2]]
    with pytest.raises(ValueError,match='gps_sequence_error'):
        mobile_route.gps_import(db,write(tmp_path,'reordered.json',bad))
    mobile_route.gps_import(db,FIX/'gps-package.json')
    own=json.loads((FIX/'calibration.json').read_text());own.update(id='CAL2',issuer='operator')
    with pytest.raises(ValueError,match='independent_calibration_required'):
        mobile_route.calibration_import(db,write(tmp_path,'own-cal.json',own))
    mobile_route.calibration_import(db,FIX/'calibration.json')
    mobile_route.map_import(db,FIX/'map-v1.json')
    own_cam=json.loads((FIX/'camera-outer.json').read_text());own_cam.update(id='CAM9',issuer='operator')
    with pytest.raises(ValueError,match='non_independent_camera'):
        mobile_route.camera_import(db,write(tmp_path,'own-cam.json',own_cam))

def test_original_city_geofence_still_decides_booking(tmp_path):
    db=tracked(tmp_path)
    other=json.loads((FIX/'booking-ok.json').read_text());other['id']='B2'
    schedule.screen(db,write(tmp_path,'screen2.json',{'id':'S2','city':'C2','grade':2}))
    other['screen_id']='S2'
    with pytest.raises(ValueError,match='creative_not_eligible'):schedule.book(db,write(tmp_path,'other.json',other))
    assert report(db)['state']['booking'][0]['status']=='confirmed'

def test_public_cli_route_demo_and_report(tmp_path):
    env={**os.environ,'PYTHONPATH':str(Path(__file__).parent.parent)}
    p=subprocess.run([sys.executable,'-m','adflight','route-demo','--db',str(tmp_path/'cli')],env=env,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    out=json.loads(p.stdout)
    pause=out['pause']['route'];avoid=out['avoid']['route']
    assert segment(pause,'2026-06-10T10:02:00Z','2026-06-10T10:03:00Z')['verdict']=='stopped'
    assert segment(avoid,'2026-06-10T10:02:00Z','2026-06-10T10:03:00Z')['verdict']=='released'
    assert out['pause']['obligations'][0]['missing_seconds']==180
    assert len(pause['candidates'])==2 and len(avoid['candidates'])==2
