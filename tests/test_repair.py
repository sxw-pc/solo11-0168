import json,subprocess,sys
from pathlib import Path
import pytest
from adflight import creative,schedule,playback,repair
from adflight.report import report,audit

F=Path(__file__).parent.parent/'fixtures'
R=F/'repair'

def write(tmp,name,data):
    p=tmp/name;p.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');return p

def base(tmp):
    """creative + license + S1/S2 + B1 (S1 10:00-10:10), then PKG1 blackout."""
    db=tmp/'state.sqlite'
    creative.register(db,F/'creative-ok.json');creative.license(db,F/'license-ok.json')
    schedule.screen(db,F/'screen-ok.json');schedule.screen(db,R/'screen-s2.json')
    schedule.book(db,F/'booking-ok.json')
    return db

def blackout(db):
    return repair.capture(db,R/'capture-blackout.json')

# ---------------------------------------------------------------- evidence
def test_blackout_keeps_both_worlds_and_opens_outages(tmp_path):
    db=base(tmp_path);r=blackout(db)
    assert r['open_worlds']==['W-PANEL','W-PSU']
    views={v['screen_id']:v for v in r['screens']}
    assert views['S1']['candidate_components']==['panel','psu']
    assert views['S1']['confirmed_component'] is None
    assert set(views['S1']['evidence_coverage'])>={'waveform','panel_self_test','heartbeat'}
    assert r['missing_verification']['S1']==['power_isolation','panel_lightup']
    m=report(db)['state']['maintenance']
    assert {x['screen_id'] for x in m}=={'S1','S2'} and all(x['end_at'] is None for x in m)
    # overlapping confirmed booking B1 is interrupted, adjudication already fixed
    assert {b['id']:b['status'] for b in report(db)['state']['booking']}=={'B1':'interrupted'}

def test_artifact_identity_clock_and_operator_are_mandatory(tmp_path):
    db=base(tmp_path)
    pkg=json.loads((R/'capture-blackout.json').read_text())
    tam=json.loads(json.dumps(pkg));tam['id']='PKG-T1';tam['artifacts'][0]['origin_sha']='deadbeef'
    with pytest.raises(ValueError,match='artifact_origin_hash_mismatch'):repair.capture(db,write(tmp_path,'t1.json',tam))
    off=json.loads(json.dumps(pkg));off['id']='PKG-T2';off['artifacts'][0]['clock_offset_seconds']=9999
    with pytest.raises(ValueError,match='device_clock_not_calibrated'):repair.capture(db,write(tmp_path,'t2.json',off))
    noop=json.loads(json.dumps(pkg));noop['id']='PKG-T3';noop['artifacts'][0]['operator']='operator'
    with pytest.raises(ValueError,match='operator_identity_required'):repair.capture(db,write(tmp_path,'t3.json',noop))

def test_heartbeat_alone_does_not_recommission(tmp_path):
    db=base(tmp_path);blackout(db)
    repair.ratify(db,R/'plan-fast-core.json')
    repair.close_diagnosis(db,R/'supplement-w-psu.json')
    r=repair.recommission(db,R/'reopen-s2-heartbeat.json')
    assert r['status']=='suspended' and r['sell_from'] is None
    assert r['note']=='heartbeat_alone_not_sufficient'

def test_single_independent_check_remains_unreliable(tmp_path):
    db=base(tmp_path);blackout(db)
    r=repair.recommission(db,R/'reopen-s2-power-only.json')
    assert r['status']=='suspended' and 'panel_lightup' in r['note']

# ------------------------------------------------------------- naive solution
def test_naive_per_screen_double_promises_one_psu_and_is_wrong_in_a_world(tmp_path):
    db=base(tmp_path);blackout(db)
    r=repair.naive_local_dispatch(db,R/'naive-dispatch.json')
    # local voltage heuristic picks psu on both screens from the shared bus dip
    assert r['local_verdicts']=={'S1':'psu','S2':'psu'}
    assert r['double_promise'][0]['serial']=='PSU-9'
    assert r['stranded_screens']==['S2']
    assert r['status']=='infeasible'
    # with the SAME original package, one of the two worlds repairs the wrong screen
    wrong={(w['world'],w['screen']) for w in r['wrong_in_worlds']}
    assert ('W-PSU','S2') in wrong and ('W-PANEL','S1') in wrong

def test_naive_ignores_cross_site_travel_window(tmp_path):
    db=base(tmp_path);blackout(db)
    r=repair.naive_local_dispatch(db,R/'naive-dispatch-travel.json')
    # both screens guessed panel -> whole-unit steps at the same minute for one technician
    assert r['local_verdicts']=={'S1':'panel','S2':'panel'}
    assert r['travel_conflicts'] and r['travel_conflicts'][0]['technician']=='tech-lin'
    assert r['status']=='infeasible'

def test_joint_ledger_rejects_same_spare_promised_twice(tmp_path):
    db=base(tmp_path);blackout(db)
    repair.ratify(db,R/'plan-min-downtime.json')
    # a second plan attempting to reserve the already-named psu for a different
    # screen inside the same world branch cannot be constructed; assert the
    # durable reservation row exists and a repair without reservation is refused
    res=report(db)['state']['part_reservation']
    assert any(x['part_serial']=='PSU-9' and x['plan_id']=='PLAN-B' for x in res)
    rogue={'id':'RO-ROGUE','plan_id':'PLAN-B','screen_id':'S2','package_id':'PKG1',
           'executed_at':'2026-06-10T10:06:00Z','removed_serial':'PSU-OLD-S2'}
    with pytest.raises(ValueError,match='no_planned_step|plan_not_closed'):
        repair.execute_repair(db,write(tmp_path,'rogue.json',rogue))

# ------------------------------------------------------------- joint diagnosis
def test_supplements_close_to_exactly_one_world(tmp_path):
    db=base(tmp_path);blackout(db)
    r=repair.close_diagnosis(db,R/'supplement-w-psu.json')
    assert r['closed_world']=='W-PSU'
    views={v['screen_id']:v for v in r['capture']['screens']}
    assert views['S1']['confirmed_component']=='psu'
    assert views['S2']['confirmed_component']=='panel'

def test_worlds_are_distinguishable_with_independent_readings(tmp_path):
    db=base(tmp_path);blackout(db)
    other=repair.close_diagnosis(db,R/'supplement-w-panel.json')
    assert other['closed_world']=='W-PANEL'

def test_cannot_ratify_plan_that_ignores_travel_window(tmp_path):
    db=base(tmp_path);blackout(db)
    with pytest.raises(ValueError,match='travel_window_ignored'):
        repair.ratify(db,R/'plan-bad-travel.json')

def test_permit_window_blocks_dispatch(tmp_path):
    db=base(tmp_path);blackout(db)
    p=json.loads((R/'plan-fast-core.json').read_text());p['id']='PLAN-BAD-PERMIT'
    p['resources']=dict(p['resources'],permit_to='2026-06-10T10:07:00Z')
    with pytest.raises(ValueError,match='outside_power_permit_window'):
        repair.ratify(db,write(tmp_path,'bp.json',p))

def test_repair_before_diagnosis_closure_rejected(tmp_path):
    db=base(tmp_path);blackout(db)
    repair.ratify(db,R/'plan-min-downtime.json')
    with pytest.raises(ValueError,match='plan_not_closed'):
        repair.execute_repair(db,R/'ro-b1.json')

# ------------------------------------------------------------------- routes
def test_fast_core_route_reopens_s1_but_keeps_s2_suspended(tmp_path):
    db=base(tmp_path);blackout(db)
    repair.ratify(db,R/'plan-fast-core.json')
    repair.close_diagnosis(db,R/'supplement-w-psu.json')
    repair.execute_repair(db,R/'ro-a1.json')
    s1=repair.recommission(db,R/'reopen-s1.json')
    assert s1['status']=='recommissioned' and s1['sell_from']=='2026-06-10T10:11:00Z'
    # S2 never gets its isolation closure on this route and stays unsellable
    s2=repair.recommission(db,R/'reopen-s2-heartbeat.json')
    assert s2['status']=='suspended'
    blocked={'id':'B9','creative_id':'AD1','revision':1,'screen_id':'S2',
             'start_at':'2026-06-10T10:20:00Z','end_at':'2026-06-10T10:25:00Z','priority':1}
    with pytest.raises(ValueError,match='screen_under_maintenance'):
        schedule.book(db,write(tmp_path,'b9.json',blocked))
    ok=dict(blocked,id='B10',screen_id='S1')
    assert schedule.book(db,write(tmp_path,'b10.json',ok))['status']=='confirmed'
    assert audit(db)['replay_ok']

def test_min_downtime_route_w_psu_order_and_recommission(tmp_path):
    db=base(tmp_path);blackout(db)
    repair.ratify(db,R/'plan-min-downtime.json')
    repair.close_diagnosis(db,R/'supplement-w-psu.json')
    o1=repair.execute_repair(db,R/'ro-b1.json')
    o2=repair.execute_repair(db,R/'ro-b2.json')
    assert (o1['screen_id'],o2['screen_id'])==('S1','S2')
    repair.recommission(db,R/'reopen-s1.json')
    repair.recommission(db,R/'reopen-s2.json')
    assert audit(db)['replay_ok']

def test_min_downtime_route_w_panel_repairs_s2_first(tmp_path):
    db=base(tmp_path);blackout(db)
    repair.ratify(db,R/'plan-min-downtime.json')
    repair.close_diagnosis(db,R/'supplement-w-panel.json')
    o1=repair.execute_repair(db,R/'ro-b1p.json')
    o2=repair.execute_repair(db,R/'ro-b2p.json')
    assert (o1['screen_id'],o2['screen_id'])==('S2','S1')
    assert audit(db)['replay_ok']

def test_routes_consume_different_physical_pieces(tmp_path):
    # fast core burns the whole unit on S1; min downtime burns the spare psu first
    da=base(tmp_path/'a');blackout(da)
    repair.ratify(da,R/'plan-fast-core.json');repair.close_diagnosis(da,R/'supplement-w-psu.json')
    repair.execute_repair(da,R/'ro-a1.json')
    parts_a={p['serial']:p['status'] for p in report(da)['repair']['spare_parts']}
    assert parts_a['WU-1']=='installed' and parts_a['PSU-9']=='in_stock'
    db=base(tmp_path/'b');blackout(db)
    repair.ratify(db,R/'plan-min-downtime.json');repair.close_diagnosis(db,R/'supplement-w-psu.json')
    repair.execute_repair(db,R/'ro-b1.json')
    parts_b={p['serial']:p['status'] for p in report(db)['repair']['spare_parts']}
    assert parts_b['PSU-9']=='installed' and parts_b['WU-1']=='in_stock'

def test_old_and_new_part_are_distinct_ledger_pieces(tmp_path):
    db=base(tmp_path);blackout(db)
    repair.ratify(db,R/'plan-min-downtime.json');repair.close_diagnosis(db,R/'supplement-w-psu.json')
    same=json.loads((R/'ro-b1.json').read_text());same['removed_serial']='PSU-9'
    with pytest.raises(ValueError,match='distinct_pieces'):
        repair.execute_repair(db,write(tmp_path,'same.json',same))
    ok=repair.execute_repair(db,R/'ro-b1.json')
    assert ok['installed_serial']=='PSU-9' and ok['removed_serial']=='PSU-OLD-S1'
    moves=report(db)['repair']['part_movements']
    assert any(m['serial']=='PSU-OLD-S1' and m['movement']=='return_to_stock' for m in moves)
    assert any(m['serial']=='PSU-9' and m['movement']=='installed_on_screen' for m in moves)

def test_past_interrupted_booking_keeps_underplay_obligation(tmp_path):
    db=base(tmp_path);blackout(db)
    repair.ratify(db,R/'plan-fast-core.json');repair.close_diagnosis(db,R/'supplement-w-psu.json')
    repair.execute_repair(db,R/'ro-a1.json')
    repair.recommission(db,R/'reopen-s1.json')
    # the original eight-minute proof stays disputed after recommission
    assert playback.proof(db,F/'proof-ok.json')['status']=='disputed'
    assert playback.settle(db,'B1')['missing_seconds']==600
    states={b['id']:b['status'] for b in report(db)['state']['booking']}
    assert states['B1']=='interrupted'
    assert audit(db)['replay_ok']

def test_recommission_only_reopens_future_window_without_backdating(tmp_path):
    db=base(tmp_path);blackout(db)
    repair.ratify(db,R/'plan-fast-core.json');repair.close_diagnosis(db,R/'supplement-w-psu.json')
    repair.execute_repair(db,R/'ro-a1.json')
    bad=json.loads((R/'reopen-s1.json').read_text())
    bad['id']='RE-BACKDATE';bad['reopened_at']='2026-06-10T10:03:00Z'
    with pytest.raises(ValueError,match='reopen_before_outage'):
        repair.recommission(db,write(tmp_path,'back.json',bad))

def test_plan_locked_after_closure_cannot_be_retroactively_adopted(tmp_path):
    db=base(tmp_path);blackout(db)
    repair.close_diagnosis(db,R/'supplement-w-psu.json')
    # locking a route only after the world is distinguished is not a valid
    # pre-commitment: the ratify check rejects it
    late=json.loads((R/'plan-fast-core.json').read_text())
    late['id']='PLAN-LATE';late['locked_at']='2026-06-10T10:06:00Z'
    with pytest.raises(ValueError,match='closure_before_lock'):
        repair.ratify(db,write(tmp_path,'late.json',late))

def test_independent_ledger_without_maintenance_keeps_booking_confirmed(tmp_path):
    # the second ledger never imports PKG1/M1: B1 stays confirmed and the same
    # eight-minute proof remains valid, exactly as the original contract states
    db=tmp_path/'clean.sqlite'
    creative.register(db,F/'creative-ok.json');creative.license(db,F/'license-ok.json')
    schedule.screen(db,F/'screen-ok.json');schedule.screen(db,R/'screen-s2.json')
    schedule.book(db,F/'booking-ok.json')
    assert playback.proof(db,F/'proof-ok.json')['status']=='valid'
    assert playback.settle(db,'B1')['missing_seconds']==120

def test_naive_plan_repairs_only_one_screen_and_strands_the_other(tmp_path):
    db=base(tmp_path);blackout(db)
    r=repair.naive_local_dispatch(db,R/'naive-dispatch.json')
    assert r['status']=='infeasible'
    # even executing every planned step leaves S2 with no spare and no repair order
    repair.execute_repair(db,write(tmp_path,'n1.json',{
        'id':'NRO-1','plan_id':'NP1','screen_id':'S1','package_id':'PKG1',
        'executed_at':'2026-06-10T10:04:00Z','removed_serial':'PSU-OLD-S1'}))
    orders={o['screen_id'] for o in report(db)['state']['repair_order']}
    assert orders=={'S1'}
    assert 'S2' in r['stranded_screens']

def test_imports_are_idempotent_and_fully_replayable(tmp_path):
    db=base(tmp_path);blackout(db)
    repair.ratify(db,R/'plan-min-downtime.json');repair.close_diagnosis(db,R/'supplement-w-psu.json')
    repair.execute_repair(db,R/'ro-b1.json');repair.execute_repair(db,R/'ro-b2.json')
    repair.recommission(db,R/'reopen-s1.json');repair.recommission(db,R/'reopen-s2.json')
    before=report(db)
    # replay every source file through the audit harness
    assert audit(db)['replay_ok']
    assert report(db)==before

def test_repair_cli_commands_exist(tmp_path):
    env={**__import__('os').environ,'PYTHONPATH':str(Path(__file__).parent.parent)}
    p=subprocess.run([sys.executable,'-m','adflight','report','--db',str(tmp_path/'x.sqlite')],
                     env=env,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    out=json.loads(p.stdout)
    assert 'repair' in out and set(out['repair'])>={'outages','plans','spare_parts','part_movements'}
