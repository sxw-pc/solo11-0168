import json,os,subprocess,sys
from pathlib import Path
import pytest
from adflight import creative,schedule,playback,energy
from adflight.report import report,audit

FIX=Path(__file__).parent.parent/'fixtures'
START,END='2026-06-10T10:00:00Z','2026-06-10T10:10:00Z'
GAP_START,GAP_END='2026-06-10T10:02:00Z','2026-06-10T10:04:00Z'

def write(tmp,name,data):
    p=tmp/name;p.write_text(json.dumps(data),encoding='utf-8');return p

def booked(tmp):
    db=tmp/'state.sqlite'
    for func,name in [(creative.register,'creative-ok.json'),(creative.license,'license-ok.json'),
                      (schedule.screen,'screen-ok.json'),(schedule.book,'booking-ok.json')]:func(db,FIX/name)
    return db

def evidence(tmp):
    """同一 10:00–10:10 原件：总表+8kW、S1 分表 10:02–10:04 断采、空调 3kW 候选循环。"""
    db=booked(tmp)
    for func,name in [(energy.meter,'meter-main.json'),(energy.meter,'meter-s1.json'),(energy.meter,'meter-s2.json'),
                      (energy.calibration,'calibration.json'),(energy.curve,'curve-s1.json'),
                      (energy.brightness,'brightness-s1.json'),(energy.hvac,'hvac-cycle.json'),
                      (energy.load,'load-lighting.json'),(energy.clock,'clock.json'),(energy.ups,'ups-snapshot.json')]:
        func(db,FIX/name)
    return db

def test_two_worlds_power_flow_and_conservation_witnesses(tmp_path):
    db=evidence(tmp_path)
    out=energy.attribute(db,START,END)
    assert out['window']['seconds']==600
    assert [w['name'] for w in out['worlds']]==['W1','W2']
    low,high=out['worlds']            # 按屏机能耗排序：W1=W-low，W2=W-high
    assert low['hvac_on']==['HVAC1'] and high['hvac_on']==[]
    # 完整功率流：断采段 S1=5/8、空调=3/0、S2=2、照明=2、总表=12
    assert low['energy_kj']['S1']==pytest.approx(600.0)   # 5kW×120s
    assert low['energy_kj']['hvac']==pytest.approx(360.0) # 3kW×120s
    assert high['energy_kj']['S1']==pytest.approx(960.0)  # 8kW×120s
    assert high['energy_kj']['hvac']==pytest.approx(0.0)
    for w in (low,high):
        assert w['energy_kj']['S2']==pytest.approx(1200.0)
        assert w['energy_kj']['merchant-lighting']==pytest.approx(1200.0)
        assert w['energy_kj']['main']==pytest.approx(3360.0)
        assert w['conservation']['max_residual_kw']==pytest.approx(0.0)  # 每个子表守恒见证
        peak_seg=[seg for seg in w['flow']['main'] if seg['kw']==12.0]
        assert peak_seg==[{'start_at':GAP_START,'end_at':GAP_END,'kw':12.0}]
    s1_peak=[seg for seg in low['flow']['S1'] if seg['kw']==5.0]
    assert s1_peak==[{'start_at':GAP_START,'end_at':GAP_END,'kw':5.0}]
    s1_peak=[seg for seg in high['flow']['S1'] if seg['kw']==8.0]
    assert s1_peak==[{'start_at':GAP_START,'end_at':GAP_END,'kw':8.0}]

def test_tight_energy_bounds_and_shared_calibration(tmp_path):
    db=evidence(tmp_path)
    out=energy.attribute(db,START,END)
    s1=out['screens']['S1']
    assert s1['gap_seconds']==120
    assert s1['energy_kj']==[pytest.approx(600.0),pytest.approx(960.0)]           # 紧界，两端各有世界见证
    assert s1['true_energy_kj'][0]==pytest.approx(600/1.02)                       # 同一 eps 统一缩放
    assert s1['true_energy_kj'][1]==pytest.approx(960/0.98)
    assert out['screens']['S2']['energy_kj']==[pytest.approx(1200.0)]*2
    assert out['hvac']['energy_kj']==[pytest.approx(0.0),pytest.approx(360.0)]
    venue=out['venue']
    assert venue['metered_energy_kj']==pytest.approx(3360.0)
    assert venue['true_energy_kj'][0]==pytest.approx(3360/1.02)
    assert venue['true_energy_kj'][1]==pytest.approx(3360/0.98)
    assert venue['reactive_kvar_s']==pytest.approx(336.0)
    assert out['peak']['metered_kw']==pytest.approx(12.0)
    assert out['peak']['guaranteed_true_kw']==pytest.approx(12/0.98)              # 未控制时不可承诺 10kW
    assert out['calibration']=={'id':'CAL1','max_bias_pct':2.0,'shared':True}

def test_naive_full_attribution_overbills_advertiser(tmp_path):
    """常规解：把总表 8kW 跳变全算 S1 → W-low 中空调 3kW 被错记到广告主。"""
    db=evidence(tmp_path)
    out=energy.attribute(db,START,END)
    naive_s1_bill=8.0*120                       # 全部跳变归屏机
    w_low_actual=600.0                          # W-low 中 S1 实际 5kW×120s
    assert naive_s1_bill==960.0
    assert naive_s1_bill-w_low_actual==pytest.approx(360.0)                     # 错账 = 空调的 3kW×120s
    lo,hi=out['screens']['S1']['energy_kj']
    assert lo==pytest.approx(600.0) and hi==pytest.approx(960.0)
    assert lo < naive_s1_bill                   # 可证账单须落在区间内，不能取跳变全量

def test_per_second_independent_allocation_is_contradictory(tmp_path):
    """逐秒独立分摊：S1≤8 与空调≤3 同秒各自取极值 → 15kW 与总表 12kW 矛盾。"""
    db=evidence(tmp_path)
    out=energy.attribute(db,START,END)
    naive_total=8.0+3.0+2.0+2.0                 # 每秒看似安全的上界
    assert naive_total==15.0
    for w in out['worlds']:                     # 每个相容世界都逐秒守恒在 12kW
        assert w['conservation']['max_residual_kw']==pytest.approx(0.0)
        assert w['energy_kj']['main']==pytest.approx(12.0*120+4.0*480)
    # 矛盾分摊若用于电池定容：15/0.98-10=5.31kW 超过逆变器 3kW（过载路径）
    assert naive_total/0.98-10.0>3.0
    bad=json.loads((FIX/'control-discharge.json').read_text());bad.update(id='CTL-X',discharge_kw=naive_total/0.98-10.0)
    with pytest.raises(ValueError,match='inverter_overload'):energy.control(db,write(tmp_path,'x.json',bad))
    # 逐秒取有利校准极值定容：12/1.02-10=1.76kW → W-high 实际 eps=-2% 时超承诺
    low=json.loads((FIX/'control-discharge.json').read_text());low.update(id='CTL-Y',discharge_kw=12/1.02-10.0+0.01)
    with pytest.raises(ValueError,match='discharge_insufficient'):energy.control(db,write(tmp_path,'y.json',low))

def test_dim_route_holds_in_all_worlds_and_receipt_gives_remedy(tmp_path):
    db=evidence(tmp_path)
    out=energy.control(db,FIX/'control-dim.json')
    assert out['guaranteed_peak_kw']==pytest.approx(9/0.98)   # 2+3+2+2=9kW，统一校准上包络
    rep=energy.energy_report(db,START,END)
    assert rep['commitment']['route']=='dim'
    assert rep['commitment']['guaranteed_peak_kw']==pytest.approx(9/0.98)
    assert rep['commitment']['holds'] is True                 # 所有相容世界下 <10kW
    assert rep['commitment']['worlds_checked']==['W1','W2']
    assert rep['ups']['reserved_kwh']==0.0                    # UPS 不消耗
    assert rep['advertiser']['remedy_seconds']==0             # 无回执 → 无补救权益，不靠功率反推
    r=energy.receipt(db,FIX/'receipt-dim.json')
    assert r['dimmed_seconds']==120
    rep=energy.energy_report(db,START,END)
    assert rep['controls'][0]['status']=='acked'
    dim=rep['advertiser']['dimmed_intervals'][0]
    assert (dim['start_at'],dim['end_at'],dim['level'])==(GAP_START,GAP_END,40)
    assert rep['advertiser']['remedy_seconds']==120           # 明示降档的补偿时段来自真实回执
    assert audit(db)['replay_ok']

def test_battery_route_soc_drops_and_recharge_cost(tmp_path):
    db=evidence(tmp_path)
    out=energy.control(db,FIX/'control-discharge.json')
    assert out['reserved_kwh']==pytest.approx(2.25*120/3600/0.9)
    assert out['guaranteed_peak_kw']==pytest.approx(12/0.98-2.25)
    energy.meter(db,FIX/'meter-ups.json')                     # UPS 实际放电回采
    rep=energy.energy_report(db,START,END)
    assert rep['commitment']['route']=='discharge'
    assert rep['commitment']['holds'] is True                 # 12/0.98-2.25≈9.995<10
    ups=rep['ups']
    assert ups['discharged_kwh']==pytest.approx(2.25*120/3600/0.9)
    assert ups['remaining_kwh']==pytest.approx(0.6-2.25*120/3600/0.9)
    assert ups['solvent'] is True                             # 全窗不透支
    assert ups['recharge_cost_kwh']==pytest.approx(ups['discharged_kwh'])  # 场馆承担下期充电成本
    assert rep['advertiser']['remedy_seconds']==0             # 客户原亮度履行
    assert audit(db)['replay_ok']

def test_discharge_requires_battery_capacity_evidence(tmp_path):
    db=booked(tmp_path)                                       # 未导 UPS 快照
    for func,name in [(energy.meter,'meter-main.json'),(energy.meter,'meter-s1.json'),(energy.meter,'meter-s2.json'),
                      (energy.calibration,'calibration.json'),(energy.curve,'curve-s1.json'),
                      (energy.brightness,'brightness-s1.json'),(energy.hvac,'hvac-cycle.json'),
                      (energy.load,'load-lighting.json'),(energy.clock,'clock.json')]:func(db,FIX/name)
    with pytest.raises(ValueError,match='ups_evidence_missing'):
        energy.control(db,FIX/'control-discharge.json')       # 缺少电池容量证据不能假设电池路线可执行

def test_replayed_discharge_command_cannot_overdischarge(tmp_path):
    db=evidence(tmp_path)
    energy.control(db,FIX/'control-discharge.json')
    again=energy.control(db,FIX/'control-discharge.json')     # 断网恢复后重发同一指令
    assert again['replayed'] is True
    rep=energy.energy_report(db,START,END)
    assert rep['ups']['reserved_kwh']==pytest.approx(2.25*120/3600/0.9)     # 只留一份放电预留
    # 不同 id 的重复放电令：累计功率先受逆变器约束
    dup=json.loads((FIX/'control-discharge.json').read_text());dup['id']='CTL-BAT2'
    with pytest.raises(ValueError,match='inverter_overload'):energy.control(db,write(tmp_path,'dup.json',dup))

def test_small_ups_energy_guard(tmp_path):
    db=booked(tmp_path)
    for func,name in [(energy.meter,'meter-main.json'),(energy.meter,'meter-s1.json'),(energy.meter,'meter-s2.json'),
                      (energy.calibration,'calibration.json'),(energy.curve,'curve-s1.json'),
                      (energy.brightness,'brightness-s1.json'),(energy.hvac,'hvac-cycle.json'),
                      (energy.load,'load-lighting.json'),(energy.clock,'clock.json')]:func(db,FIX/name)
    small={'id':'UPS-S','ts':'2026-06-10T09:50:00Z','usable_kwh':0.1,'inverter_efficiency':0.9,'max_discharge_kw':10.0}
    energy.ups(db,write(tmp_path,'ups-small.json',small))
    energy.control(db,FIX/'control-discharge.json')           # 0.0833kWh ≤ 0.1
    dup=json.loads((FIX/'control-discharge.json').read_text());dup['id']='CTL-BAT2'
    with pytest.raises(ValueError,match='ups_energy_exceeded'):
        energy.control(db,write(tmp_path,'dup.json',dup))     # 0.1667kWh > 0.1 → 拒绝过放

def test_control_requires_effective_booking_and_pre_peak_decision(tmp_path):
    db=evidence(tmp_path)
    maintenance={'id':'M1','screen_id':'S1','start_at':'2026-06-10T10:04:00Z','end_at':'2026-06-10T10:06:00Z'}
    assert schedule.maintenance(db,write(tmp_path,'m1.json',maintenance))['interrupted']==['B1']
    assert playback.proof(db,FIX/'proof-ok.json')['status']=='disputed'     # 原因果链保持
    with pytest.raises(ValueError,match='no_effective_booking'):
        energy.control(db,FIX/'control-dim.json')             # B1 已 interrupted，无有效预约
    db=evidence(tmp_path/'late')
    late=json.loads((FIX/'control-dim.json').read_text());late.update(id='CTL-L',decided_at='2026-06-10T10:03:00Z')
    with pytest.raises(ValueError,match='strategy_after_peak'):
        energy.control(db,write(tmp_path,'late.json',late))   # 策略须在尖峰前固定

def test_dim_level_must_meet_limit(tmp_path):
    db=evidence(tmp_path)
    weak=json.loads((FIX/'control-dim.json').read_text());weak.update(id='CTL-W',level=100)
    with pytest.raises(ValueError,match='dim_insufficient'):  # 8+3+2+2=15kW 压不到 10kW 以下
        energy.control(db,write(tmp_path,'weak.json',weak))

def test_attribution_requires_clock_and_calibration(tmp_path):
    db=booked(tmp_path)
    for func,name in [(energy.meter,'meter-main.json'),(energy.meter,'meter-s1.json'),(energy.meter,'meter-s2.json'),
                      (energy.curve,'curve-s1.json'),(energy.brightness,'brightness-s1.json'),
                      (energy.hvac,'hvac-cycle.json'),(energy.load,'load-lighting.json')]:func(db,FIX/name)
    with pytest.raises(ValueError,match='calibration_missing'):energy.attribute(db,START,END)
    energy.calibration(db,FIX/'calibration.json')
    with pytest.raises(ValueError,match='clock_missing'):energy.attribute(db,START,END)

def test_inconsistent_readings_have_no_feasible_world(tmp_path):
    db=booked(tmp_path)
    for func,name in [(energy.calibration,'calibration.json'),(energy.curve,'curve-s1.json'),
                      (energy.brightness,'brightness-s1.json'),(energy.hvac,'hvac-cycle.json'),
                      (energy.load,'load-lighting.json'),(energy.clock,'clock.json')]:func(db,FIX/name)
    energy.meter(db,FIX/'meter-s1.json');energy.meter(db,FIX/'meter-s2.json')
    main=json.loads((FIX/'meter-main.json').read_text())
    main['segments'][1]['active_kw']=16.0                     # 跳变 12kW 超出所有相容解释
    energy.meter(db,write(tmp_path,'main16.json',main))
    with pytest.raises(ValueError,match='no_feasible_world'):energy.attribute(db,START,END)

def test_meter_replay_and_reading_conflict(tmp_path):
    db=evidence(tmp_path)
    again=energy.meter(db,FIX/'meter-main.json')
    assert again['replayed'] is True and again['readings']==0
    conflict=json.loads((FIX/'meter-s2.json').read_text())
    conflict['segments'][0]['active_kw']=2.5
    with pytest.raises(ValueError,match='reading_conflict'):
        energy.meter(db,write(tmp_path,'conflict.json',conflict))

def test_report_preserves_raw_sources_and_energy_state(tmp_path):
    db=evidence(tmp_path)
    energy.control(db,FIX/'control-dim.json');energy.receipt(db,FIX/'receipt-dim.json')
    rep=report(db)
    kinds=[s['kind'] for s in rep['sources']]
    for k in ('meter','calibration','curve','brightness','hvac','load','clock','ups','control','receipt'):
        assert k in kinds                                      # 原始读数与共用校准偏差留痕
    assert rep['state']['control'][0]['status']=='acked'
    basis=json.loads(rep['state']['control'][0]['basis'])
    assert basis['usable_kwh']==0.6 and basis['inverter_efficiency']==0.9  # 提交前快照电池证据
    assert basis['curve']['S1']['100']==[5.0,8.0]                            # 亮度耗电曲线
    assert basis['bookings'][0]['id']=='B1' and basis['bookings'][0]['status']=='confirmed'
    assert audit(db)['replay_ok']

def test_cli_attribute_and_energy_report(tmp_path):
    db=evidence(tmp_path)
    energy.control(db,FIX/'control-dim.json');energy.receipt(db,FIX/'receipt-dim.json')
    env={**os.environ,'PYTHONPATH':str(Path(__file__).parent.parent)}
    for cmd in ('attribute','energy-report'):
        p=subprocess.run([sys.executable,'-m','adflight',cmd,'--db',str(db),'--start',START,'--end',END],
                         env=env,capture_output=True,text=True)
        assert p.returncode==0,p.stderr
        out=json.loads(p.stdout)
        assert out['window']['seconds']==600
        assert out['screens']['S1']['energy_kj']==[pytest.approx(600.0),pytest.approx(960.0)]
    p=subprocess.run([sys.executable,'-m','adflight','audit','--db',str(db)],env=env,capture_output=True,text=True)
    assert json.loads(p.stdout)['replay_ok']
