"""离线总表/分表采录、功率归属与尖峰控制。

测量模型（合成数据）：
- 所有电表共用同一份校准证书：相对偏差 eps∈[-δ,+δ] 是单一未知量，在整个
  窗口、所有表上相同（共同校准偏差）。读数 = 真值×(1+eps)。归属计算在读数
  空间进行（耗电曲线、额定功率与读数同一空间），真值包络由同一个 eps 统一
  缩放：真值∈[读数/(1+δ), 读数/(1-δ)]。不允许逐秒各自取 eps 极值，也不允许
  同一秒为不同负载同时取互斥的极值（须满足跨时守恒）。
- 空调压缩机循环是原子候选：整个循环要么投入（额定功率）要么退出。
- 分表断采的屏机由亮度档+耗电曲线给出功率包络；同一秒至多一块分表断采。
- 控制令在尖峰前固定（decided_at < 窗口起点），提交前把电池 SOC、逆变效率、
  屏机亮度耗电曲线与有效预约快照进 basis 留痕；同一文件重发按来源 SHA 幂等，
  累计放电受逆变器功率与 UPS 可用能量双重约束，防止断网重发造成过放。
- 广告主的降档时段与补救权益只来自屏控回执（receipt），不从用电推测画面。
"""
import json
from datetime import datetime,timezone
from .store import connect,source,already,at
from .playback import seconds
from . import schedule

EPS=1e-9

def _epoch(ts):return int(datetime.fromisoformat(ts[:-1]+'+00:00').timestamp())
def _iso(t):return datetime.fromtimestamp(t,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def _expand(d):
    for r in d.get('readings',[]):
        yield at(r['ts']),float(r['active_kw']),float(r.get('reactive_kvar',0.0))
    for seg in d.get('segments',[]):
        start,end=at(seg['start_at']),at(seg['end_at'])
        if start>=end:raise ValueError('invalid_segment_window')
        kw,kvar=float(seg['active_kw']),float(seg.get('reactive_kvar',0.0))
        for t in range(_epoch(start),_epoch(end)):yield _iso(t),kw,kvar

# ---------------------------------------------------------------- 采录导入
def meter(path,file):
    """总表/分表/UPS 逐秒有功无功采录；segments 展开为逐秒行，原始字节留 source。"""
    with connect(path) as db,db:
        sid,d,replay=source(db,'meter',file)
        mid,kind=d['meter_id'],d['kind']
        if kind not in ('main','sub','ups'):raise ValueError('invalid_meter_kind')
        scr=d.get('screen_id')
        if kind=='sub' and not scr:raise ValueError('sub_meter_screen_required')
        old=db.execute('SELECT * FROM meter WHERE id=?',(mid,)).fetchone()
        if old:
            if old['kind']!=kind:raise ValueError('meter_kind_conflict')
            if old['screen_id']!=scr:raise ValueError('meter_screen_conflict')
        else:db.execute('INSERT INTO meter VALUES(?,?,?,?)',(mid,kind,scr,sid))
        n=0
        for ts,kw,kvar in _expand(d):
            row=db.execute('SELECT active_kw,reactive_kvar FROM reading WHERE meter_id=? AND ts=?',(mid,ts)).fetchone()
            if row:
                if abs(row['active_kw']-kw)>EPS or abs(row['reactive_kvar']-kvar)>EPS:raise ValueError('reading_conflict')
                continue
            db.execute('INSERT INTO reading VALUES(?,?,?,?,?)',(mid,ts,kw,kvar,sid));n+=1
        return {'meter_id':mid,'kind':kind,'readings':n,'replayed':replay}

def calibration(path,file):
    """各表共用的计量校验证书：单一相对偏差上界，独立机构签发。"""
    with connect(path) as db,db:
        sid,d,replay=source(db,'calibration',file)
        if already(db,'calibration',d['id'],sid):return {'id':d['id'],'replayed':True}
        start,end=at(d['valid_from']),at(d['valid_to'])
        if start>=end:raise ValueError('invalid_calibration_window')
        if not d['issuer'] or d['issuer']=='operator':raise ValueError('independent_calibration_issuer_required')
        if not 0<float(d['max_bias_pct'])<10:raise ValueError('invalid_bias')
        db.execute('INSERT INTO calibration VALUES(?,?,?,?,?,?)',(d['id'],float(d['max_bias_pct']),start,end,d['issuer'],sid))
        return {'id':d['id'],'status':'certified','shared':True}

def curve(path,file):
    """屏机亮度档→耗电区间曲线（设备校准出具，与电表同一读数空间）。"""
    with connect(path) as db,db:
        sid,d,replay=source(db,'curve',file)
        if replay:return {'screen_id':d['screen_id'],'replayed':True}
        for p in d['points']:
            level,lo,hi=int(p['level']),float(p['min_kw']),float(p['max_kw'])
            if lo<0 or hi<lo:raise ValueError('invalid_curve_point')
            old=db.execute('SELECT min_kw,max_kw FROM curve WHERE screen_id=? AND level=?',(d['screen_id'],level)).fetchone()
            if old:
                if abs(old['min_kw']-lo)>EPS or abs(old['max_kw']-hi)>EPS:raise ValueError('curve_conflict')
                continue
            db.execute('INSERT INTO curve VALUES(?,?,?,?,?)',(d['screen_id'],level,lo,hi,sid))
        return {'screen_id':d['screen_id'],'points':len(d['points'])}

def brightness(path,file):
    """屏机驱动器亮度档记录。"""
    with connect(path) as db,db:
        sid,d,replay=source(db,'brightness',file)
        if already(db,'brightness',d['id'],sid):return {'id':d['id'],'replayed':True}
        start,end=at(d['start_at']),at(d['end_at'])
        if start>=end:raise ValueError('invalid_brightness_window')
        level=int(d['level'])
        if not 0<=level<=100:raise ValueError('invalid_brightness_level')
        db.execute('INSERT INTO brightness VALUES(?,?,?,?,?,?)',(d['id'],d['screen_id'],level,start,end,sid))
        return {'id':d['id'],'status':'recorded'}

def hvac(path,file):
    """共用空调压缩机候选循环：整段投入额定功率或整段退出。"""
    with connect(path) as db,db:
        sid,d,replay=source(db,'hvac',file)
        if already(db,'hvac',d['id'],sid):return {'id':d['id'],'replayed':True}
        start,end=at(d['start_at']),at(d['end_at'])
        if start>=end:raise ValueError('invalid_hvac_window')
        if float(d['rated_kw'])<=0:raise ValueError('invalid_rated_kw')
        db.execute('INSERT INTO hvac VALUES(?,?,?,?,?)',(d['id'],float(d['rated_kw']),start,end,sid))
        return {'id':d['id'],'status':'candidate'}

def load(path,file):
    """商户照明等恒定负载声明。"""
    with connect(path) as db,db:
        sid,d,replay=source(db,'load',file)
        if already(db,'declared_load',d['id'],sid):return {'id':d['id'],'replayed':True}
        start,end=at(d['start_at']),at(d['end_at'])
        if start>=end:raise ValueError('invalid_load_window')
        if float(d['kw'])<0:raise ValueError('invalid_load_kw')
        db.execute('INSERT INTO declared_load VALUES(?,?,?,?,?,?)',(d['id'],d['name'],float(d['kw']),start,end,sid))
        return {'id':d['id'],'status':'declared'}

def clock(path,file):
    """场馆时钟同步证书：各设备时钟相对 UTC 的最大偏移。"""
    with connect(path) as db,db:
        sid,d,replay=source(db,'clock',file)
        if already(db,'clock',d['id'],sid):return {'id':d['id'],'replayed':True}
        if not d['issuer']:raise ValueError('clock_issuer_required')
        if int(d['max_offset_ms'])<0:raise ValueError('invalid_clock_offset')
        db.execute('INSERT INTO clock VALUES(?,?,?,?)',(d['id'],int(d['max_offset_ms']),d['issuer'],sid))
        return {'id':d['id'],'status':'synchronized'}

def ups(path,file):
    """UPS 快照：可用能量、逆变效率、最大放电功率（电池容量证据）。"""
    with connect(path) as db,db:
        sid,d,replay=source(db,'ups',file)
        if already(db,'ups',d['id'],sid):return {'id':d['id'],'replayed':True}
        ts=at(d['ts'])
        if float(d['usable_kwh'])<0:raise ValueError('invalid_usable_kwh')
        if not 0<float(d['inverter_efficiency'])<=1:raise ValueError('invalid_inverter_efficiency')
        if float(d['max_discharge_kw'])<=0:raise ValueError('invalid_max_discharge')
        db.execute('INSERT INTO ups VALUES(?,?,?,?,?,?)',(d['id'],ts,float(d['usable_kwh']),float(d['inverter_efficiency']),float(d['max_discharge_kw']),sid))
        return {'id':d['id'],'status':'snapshot'}

# ---------------------------------------------------------------- 证据装配
def _calibration(db,start,end):
    rows=[dict(r) for r in db.execute('SELECT * FROM calibration WHERE valid_from<=? AND valid_to>=?',(start,end))]
    if not rows:raise ValueError('calibration_missing')
    if len({r['max_bias_pct'] for r in rows})>1:raise ValueError('calibration_ambiguous')
    return rows[0]

def _clock(db):
    row=db.execute('SELECT * FROM clock ORDER BY id LIMIT 1').fetchone()
    if not row:raise ValueError('clock_missing')
    return dict(row)

def _ups_snapshot(db,ts):
    row=db.execute('SELECT * FROM ups WHERE ts<=? ORDER BY ts DESC,id DESC LIMIT 1',(ts,)).fetchone()
    if not row:raise ValueError('ups_evidence_missing')
    return dict(row)

def _series(db,start,end):
    """把窗口内每秒的已知量装配出来：总表、分表、恒定负载、候选循环、断采包络。"""
    cal=_calibration(db,start,end);clk=_clock(db)
    t0,t1=_epoch(start),_epoch(end)
    if t1<=t0:raise ValueError('invalid_window')
    readings,reactive={},{}
    for r in db.execute('SELECT meter_id,ts,active_kw,reactive_kvar FROM reading'):
        readings.setdefault(r['meter_id'],{})[r['ts']]=r['active_kw']
        reactive.setdefault(r['meter_id'],{})[r['ts']]=r['reactive_kvar']
    mains=[r['id'] for r in db.execute("SELECT id FROM meter WHERE kind='main' ORDER BY id")]
    if not mains:raise ValueError('main_meter_missing')
    subs={r['id']:r['screen_id'] for r in db.execute("SELECT id,screen_id FROM meter WHERE kind='sub' ORDER BY id")}
    loads=[dict(r) for r in db.execute('SELECT * FROM declared_load ORDER BY id')]
    cycles=[dict(r) for r in db.execute('SELECT * FROM hvac ORDER BY id')]
    bright=[dict(r) for r in db.execute('SELECT * FROM brightness ORDER BY id')]
    curves={}
    for r in db.execute('SELECT * FROM curve ORDER BY level'):curves.setdefault(r['screen_id'],{})[r['level']]=(r['min_kw'],r['max_kw'])
    per_sec=[]
    for t in range(t0,t1):
        iso=_iso(t)
        vals=[readings.get(m,{}).get(iso) for m in mains]
        if any(v is None for v in vals):raise ValueError('main_readings_missing')
        known=sum(l['kw'] for l in loads if l['start_at']<=iso<l['end_at'])
        gaps=[]
        for m,scr in subs.items():
            v=readings.get(m,{}).get(iso)
            if v is None:
                lvl=next((b['level'] for b in bright if b['screen_id']==scr and b['start_at']<=iso<b['end_at']),None)
                if lvl is None:raise ValueError('brightness_evidence_missing')
                bound=curves.get(scr,{}).get(lvl)
                if bound is None:raise ValueError('curve_evidence_missing')
                gaps.append({'screen':scr,'level':lvl,'min_kw':bound[0],'max_kw':bound[1]})
            else:known+=v
        if len(gaps)>1:raise ValueError('ambiguous_gap')
        hv=[{'id':c['id'],'rated_kw':c['rated_kw']} for c in cycles if c['start_at']<=iso<c['end_at']]
        per_sec.append({'t':t,'iso':iso,'main':sum(vals),'reactive':sum(reactive.get(m,{}).get(iso,0.0) for m in mains),
                        'known':known,'gap':gaps[0] if gaps else None,'hvac':hv})
    return {'start':start,'end':end,'t0':t0,'t1':t1,'per_sec':per_sec,'cycles':cycles,'loads':loads,
            'subs':subs,'readings':readings,'delta':cal['max_bias_pct']/100.0,'calibration':cal,'clock':clk}

def _worlds(db,start,end):
    """枚举与采录相容的全部完整物理世界（候选循环 2^k 组合 × 跨时守恒）。"""
    s=_series(db,start,end)
    ids=[c['id'] for c in s['cycles']]
    worlds=[]
    for mask in range(1<<len(ids)):
        on=frozenset(ids[i] for i in range(len(ids)) if mask>>i&1)
        alloc=[];ok=True
        for sec in s['per_sec']:
            h=sum(x['rated_kw'] for x in sec['hvac'] if x['id'] in on)
            rem=sec['main']-sec['known']-h
            g=sec['gap']
            lo,hi=(g['min_kw'],g['max_kw']) if g else (0.0,0.0)
            if rem<lo-EPS or rem>hi+EPS:ok=False;break
            alloc.append({'t':sec['t'],'iso':sec['iso'],'main':sec['main'],'known':sec['known'],'hvac':h,
                          'gap_kw':rem if g else None,'screen':g['screen'] if g else None})
        if ok:worlds.append({'hvac_on':sorted(on),'alloc':alloc})
    if not worlds:raise ValueError('no_feasible_world')
    def gap_energy(w):return sum(a['gap_kw'] or 0.0 for a in w['alloc'])
    worlds.sort(key=lambda w:(gap_energy(w),w['hvac_on']))
    s['worlds']=worlds
    return s

def _metered_peak(s):return max(sec['main'] for sec in s['per_sec'])
def _true_hi(x,delta):return x/(1.0-delta)
def _true_lo(x,delta):return x/(1.0+delta)

def _dim_peak(s,cap):
    """降档后所有相容世界中的最大总功率（读数空间）。"""
    worst=0.0
    for w in s['worlds']:
        for a in w['alloc']:
            worst=max(worst,a['known']+(cap if a['screen'] else 0.0)+a['hvac'])
    return worst

def _required_discharge(s,limit):
    return max(0.0,_true_hi(_metered_peak(s),s['delta'])-limit)

# ---------------------------------------------------------------- 尖峰控制
def control(path,file):
    """事前授权的尖峰控制令：dim=屏机降档，discharge=UPS 承担可证最坏超额。"""
    with connect(path) as db,db:
        sid,d,replay=source(db,'control',file)
        if already(db,'control',d['id'],sid):return {'id':d['id'],'replayed':True}
        kind=d.get('kind')
        start,end=at(d['start_at']),at(d['end_at'])
        if start>=end:raise ValueError('invalid_control_window')
        decided=at(d['decided_at'])
        if decided>=start:raise ValueError('strategy_after_peak')
        limit=float(d['limit_kw'])
        if limit<=0:raise ValueError('invalid_limit')
        up=_ups_snapshot(db,decided)
        s=_worlds(db,start,end)
        basis={'usable_kwh':up['usable_kwh'],'inverter_efficiency':up['inverter_efficiency'],
               'max_discharge_kw':up['max_discharge_kw'],'limit_kw':limit,
               'worlds':[{'hvac_on':w['hvac_on']} for w in s['worlds']]}
        if kind=='dim':
            scr,level=d['screen_id'],int(d['level'])
            pt=db.execute('SELECT min_kw,max_kw FROM curve WHERE screen_id=? AND level=?',(scr,level)).fetchone()
            if not pt:raise ValueError('curve_evidence_missing')
            basis['curve']={scr:{str(r['level']):[r['min_kw'],r['max_kw']] for r in db.execute('SELECT level,min_kw,max_kw FROM curve WHERE screen_id=? ORDER BY level',(scr,))}}
            books=schedule.effective(db,scr,start,end)
            if not books:raise ValueError('no_effective_booking')
            basis['bookings']=[{'id':b['id'],'status':b['status'],'start_at':b['start_at'],'end_at':b['end_at']} for b in books]
            peak=_true_hi(_dim_peak(s,pt['max_kw']),s['delta'])
            basis['guaranteed_peak_kw']=peak
            if peak>limit+EPS:raise ValueError('dim_insufficient')
            db.execute('INSERT INTO control VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (d['id'],'dim',scr,level,start,end,decided,limit,None,0.0,'issued',json.dumps(basis,sort_keys=True),sid))
            return {'id':d['id'],'kind':'dim','status':'issued','guaranteed_peak_kw':peak}
        if kind=='discharge':
            declared=float(d['discharge_kw'])
            need=_required_discharge(s,limit)
            if need<=0:raise ValueError('discharge_unneeded')
            if declared<need-EPS:raise ValueError('discharge_insufficient')
            screens=sorted({sec['gap']['screen'] for sec in s['per_sec'] if sec['gap']})
            books=[b for scr in screens for b in schedule.effective(db,scr,start,end)]
            if not books:raise ValueError('no_effective_booking')
            basis['bookings']=[{'id':b['id'],'status':b['status'],'start_at':b['start_at'],'end_at':b['end_at']} for b in books]
            basis['curve']={scr:{str(r['level']):[r['min_kw'],r['max_kw']] for r in db.execute('SELECT level,min_kw,max_kw FROM curve WHERE screen_id=? ORDER BY level',(scr,))} for scr in screens}
            res_p=sum(r['discharge_kw'] for r in db.execute("SELECT discharge_kw FROM control WHERE kind='discharge' AND start_at<? AND end_at>?",(end,start)))
            res_e=sum(r['reserved_kwh'] for r in db.execute("SELECT reserved_kwh FROM control WHERE kind='discharge' AND start_at<? AND end_at>?",(end,start)))
            if res_p+declared>up['max_discharge_kw']+EPS:raise ValueError('inverter_overload')
            dc=declared*seconds(start,end)/3600.0/up['inverter_efficiency']
            if res_e+dc>up['usable_kwh']+EPS:raise ValueError('ups_energy_exceeded')
            guaranteed=_true_hi(_metered_peak(s),s['delta'])-declared
            basis.update({'required_discharge_kw':need,'reserved_kwh':dc,'guaranteed_peak_kw':guaranteed})
            db.execute('INSERT INTO control VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (d['id'],'discharge',None,None,start,end,decided,limit,declared,dc,'issued',json.dumps(basis,sort_keys=True),sid))
            return {'id':d['id'],'kind':'discharge','status':'issued','reserved_kwh':dc,'guaranteed_peak_kw':guaranteed}
        raise ValueError('invalid_control_kind')

def receipt(path,file):
    """屏控回执：广告主降档时段与补救权益的唯一来源。"""
    with connect(path) as db,db:
        sid,d,replay=source(db,'receipt',file)
        if already(db,'receipt',d['id'],sid):return {'id':d['id'],'replayed':True}
        c=db.execute('SELECT * FROM control WHERE id=?',(d['control_id'],)).fetchone()
        if not c:raise ValueError('unknown_control')
        if c['kind']!='dim':raise ValueError('receipt_for_non_dim')
        start,end=at(d['start_at']),at(d['end_at'])
        ok=(start<end and start>=c['start_at'] and end<=c['end_at']
            and d['screen_id']==c['screen_id'] and int(d['level'])==c['level'])
        if not ok:raise ValueError('receipt_mismatch')
        db.execute('INSERT INTO receipt VALUES(?,?,?,?,?,?,?)',(d['id'],c['id'],d['screen_id'],int(d['level']),start,end,sid))
        db.execute("UPDATE control SET status='acked' WHERE id=?",(c['id'],))
        return {'id':d['id'],'control_id':c['id'],'dimmed_seconds':seconds(start,end)}

# ---------------------------------------------------------------- 功率归属
def _segments(series):
    """[(t,kw)] 行程长度编码为 [{start_at,end_at,kw}]。"""
    segs=[]
    for t,kw in series:
        if segs and abs(segs[-1]['kw']-kw)<=EPS and segs[-1]['_next']==t:segs[-1]['_next']=t+1
        else:segs.append({'start_at':_iso(t),'_next':t+1,'kw':kw})
    for seg in segs:seg['end_at']=_iso(seg.pop('_next'))
    return segs

def _attribution(s):
    per_sec,delta=s['per_sec'],s['delta']
    worlds=[]
    for i,w in enumerate(s['worlds']):
        comp={'main':[(sec['t'],sec['main']) for sec in per_sec]}
        for m,scr in s['subs'].items():
            ser=[]
            for sec,a in zip(per_sec,w['alloc']):
                ser.append((sec['t'],a['gap_kw'] if a['screen']==scr else s['readings'][m][sec['iso']]))
            comp[scr or m]=ser
        comp['hvac']=[(sec['t'],a['hvac']) for sec,a in zip(per_sec,w['alloc'])]
        for l in s['loads']:
            comp[l['name']]=[(sec['t'],l['kw'] if l['start_at']<=sec['iso']<l['end_at'] else 0.0) for sec in per_sec]
        residual=max(abs(a['main']-sum(v[t_i][1] for k,v in comp.items() if k!='main'))
                     for t_i,a in enumerate(w['alloc']))
        worlds.append({'name':f'W{i+1}','hvac_on':w['hvac_on'],
                       'energy_kj':{k:sum(kw for _,kw in v) for k,v in comp.items()},
                       'flow':{k:_segments(v) for k,v in comp.items()},
                       'conservation':{'max_residual_kw':residual}})
    screens={}
    for m,scr in s['subs'].items():
        name=scr or m
        gap_secs=[sec for sec in per_sec if sec['gap'] and sec['gap']['screen']==scr]
        metered=sum(s['readings'][m][sec['iso']] for sec in per_sec if sec['iso'] in s['readings'].get(m,{}))
        if gap_secs:
            per_world=[metered+sum(a['gap_kw'] for a in w['alloc'] if a['screen']==scr) for w in s['worlds']]
            lo,hi=min(per_world),max(per_world)
        else:lo=hi=metered
        screens[name]={'gap_seconds':len(gap_secs),'energy_kj':[lo,hi],
                       'true_energy_kj':[_true_lo(lo,delta),_true_hi(hi,delta)]}
    venue=sum(sec['main'] for sec in per_sec)
    hvac_e=[sum(a['hvac'] for a in w['alloc']) for w in s['worlds']]
    return {'window':{'start_at':s['start'],'end_at':s['end'],'seconds':s['t1']-s['t0']},
            'calibration':{'id':s['calibration']['id'],'max_bias_pct':s['calibration']['max_bias_pct'],'shared':True},
            'clock':{'id':s['clock']['id'],'max_offset_ms':s['clock']['max_offset_ms']},
            'worlds':worlds,'screens':screens,
            'hvac':{'energy_kj':[min(hvac_e),max(hvac_e)]},
            'loads':{l['name']:{'energy_kj':[sum(l['kw'] for sec in per_sec if l['start_at']<=sec['iso']<l['end_at'])]*2} for l in s['loads']},
            'venue':{'metered_energy_kj':venue,'true_energy_kj':[_true_lo(venue,delta),_true_hi(venue,delta)],
                     'reactive_kvar_s':sum(sec['reactive'] for sec in per_sec)},
            'peak':{'metered_kw':_metered_peak(s),'guaranteed_true_kw':_true_hi(_metered_peak(s),delta)}}

def attribute(path,start,end):
    """分表断采期间各屏与场馆的可证能耗区间、完整功率流与守恒见证。"""
    with connect(path) as db:return _attribution(_worlds(db,at(start),at(end)))

def energy_report(path,start,end):
    """电网侧与广告主侧可验证的能耗/峰值/储能/补救报告。"""
    with connect(path) as db:
        start,end=at(start),at(end)
        base=_attribution(_worlds(db,start,end))
        ctrls=[dict(r) for r in db.execute('SELECT * FROM control WHERE start_at<? AND end_at>? ORDER BY id',(end,start))]
        commitment=None
        if ctrls:
            c=ctrls[-1];basis=json.loads(c['basis'])
            commitment={'route':c['kind'],'control_id':c['id'],'status':c['status'],'limit_kw':c['limit_kw'],
                        'guaranteed_peak_kw':basis['guaranteed_peak_kw'],
                        'holds':basis['guaranteed_peak_kw']<=c['limit_kw']+EPS,
                        'worlds_checked':[w['name'] for w in base['worlds']]}
        up=db.execute('SELECT * FROM ups ORDER BY ts DESC,id DESC LIMIT 1').fetchone()
        ups_sec=None
        if up:
            reserved=sum(r['reserved_kwh'] for r in db.execute("SELECT reserved_kwh FROM control WHERE kind='discharge' AND start_at<? AND end_at>?",(end,start)))
            ac=sum(r['active_kw'] for r in db.execute("SELECT r.active_kw FROM reading r JOIN meter m ON m.id=r.meter_id WHERE m.kind='ups' AND r.ts>=? AND r.ts<?",(start,end)))/3600.0
            dc=ac/up['inverter_efficiency']
            ups_sec={'usable_kwh':up['usable_kwh'],'inverter_efficiency':up['inverter_efficiency'],
                     'reserved_kwh':reserved,'discharged_kwh':dc,'remaining_kwh':up['usable_kwh']-dc,
                     'solvent':up['usable_kwh']-dc>=-EPS,'recharge_cost_kwh':dc}
        dimmed=[];remedy=0
        for r in db.execute('SELECT * FROM receipt ORDER BY id'):
            secs=0
            for b in db.execute("SELECT * FROM booking WHERE screen_id=? AND status IN ('confirmed','interrupted') AND start_at<? AND end_at>?",(r['screen_id'],r['end_at'],r['start_at'])):
                secs+=seconds(max(r['start_at'],b['start_at']),min(r['end_at'],b['end_at']))
            dimmed.append({'receipt_id':r['id'],'control_id':r['control_id'],'screen_id':r['screen_id'],
                           'level':r['level'],'start_at':r['start_at'],'end_at':r['end_at'],'remedy_seconds':secs})
            remedy+=secs
        base['commitment']=commitment
        base['ups']=ups_sec
        base['advertiser']={'dimmed_intervals':dimmed,'remedy_seconds':remedy,'source':'screen_control_receipts'}
        base['controls']=[{'id':c['id'],'kind':c['kind'],'status':c['status'],'limit_kw':c['limit_kw']} for c in ctrls]
        return base
