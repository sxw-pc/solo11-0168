"""Offline maintenance evidence capture, joint fault hypotheses, constrained
dispatch, physical spare-parts ledger and dual independent recommission.

The same original blackout package is compatible with two complete physical
worlds (W-PSU / W-PANEL).  Per-screen local heuristics cannot tell them apart;
this module keeps both candidates until independent supplementary readings
close the diagnosis, and never lets one physical spare be promised twice.
"""
import json,hashlib
from .store import connect,source,at,parse,plus

COMPONENTS=('psu','panel')
# Canonical worlds of the two-screen blackout: exactly one psu fault and one
# panel fault; the world name is the faulty component of S1.
WORLDS={'W-PSU':{'S1':'psu','S2':'panel'},'W-PANEL':{'S1':'panel','S2':'psu'}}
ARTIFACT_TYPES={'waveform','panel_self_test','heartbeat','part_scan','reinspect_photo',
                'power_isolation','panel_lightup'}

def _sha256(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()

def _validate_artifact(a):
    for key in ('id','screen_id','type','device_clock_at','clock_offset_seconds','operator','origin_sha','payload'):
        if key not in a:raise ValueError('artifact_missing_'+key)
    at(a['device_clock_at'])
    if not isinstance(a['clock_offset_seconds'],(int,float)) or abs(a['clock_offset_seconds'])>300:
        raise ValueError('device_clock_not_calibrated')
    if not a['operator'] or a['operator']=='operator':raise ValueError('operator_identity_required')
    if a['type'] not in ARTIFACT_TYPES:raise ValueError('unknown_artifact_type')
    if a['origin_sha']!=_sha256(a['payload']):raise ValueError('artifact_origin_hash_mismatch')

def _conclusive(a):
    """Return (component, healthy) when an artifact independently settles a
    screen: healthy=True means the component tested healthy."""
    p=a['payload']
    if a['type']=='power_isolation':
        if 'bus_voltage' not in p:raise ValueError('power_isolation_requires_voltage')
        return ('psu',p['bus_voltage']>=p.get('threshold',48.0))
    if a['type']=='panel_lightup':
        if 'lit' not in p:raise ValueError('panel_lightup_requires_lit')
        return ('panel',bool(p['lit']))
    return None

def _screen_view(db,screen_id):
    hyps=db.execute('SELECT component,status FROM fault_hypothesis WHERE screen_id=? ORDER BY component',(screen_id,)).fetchall()
    candidates=[h['component'] for h in hyps if h['status']=='candidate']
    confirmed=next((h['component'] for h in hyps if h['status']=='confirmed'),None)
    coverage=sorted(r['type'] for r in db.execute('SELECT type FROM artifact WHERE screen_id=?',(screen_id,)))
    return {'screen_id':screen_id,'candidate_components':candidates,
            'confirmed_component':confirmed,'evidence_coverage':coverage}

def _open_worlds(db):
    ids=[r['id'] for r in db.execute('SELECT id FROM screen ORDER BY id')]
    views={s:_screen_view(db,s) for s in ids}
    worlds={}
    for name,assign in WORLDS.items():
        ok=True
        for screen,component in assign.items():
            view=views.get(screen)
            if not view:continue
            if view['confirmed_component'] is not None and view['confirmed_component']!=component:ok=False
            if view['confirmed_component'] is None and component not in view['candidate_components']:ok=False
        if ok:worlds[name]=dict(assign)
    return worlds,views

def _missing_checks(views):
    notes={}
    for screen,view in views.items():
        missing=[]
        if 'power_isolation' not in view['evidence_coverage']:missing.append('power_isolation')
        if 'panel_lightup' not in view['evidence_coverage']:missing.append('panel_lightup')
        if missing:notes[screen]=missing
    return notes

def _register_spare(db,serial,kind,package_id):
    row=db.execute('SELECT * FROM spare_part WHERE serial=?',(serial,)).fetchone()
    if row:
        if row['kind']!=kind:raise ValueError('part_kind_conflict')
        return
    db.execute('INSERT INTO spare_part VALUES(?,?,?,?,?,?)',(serial,kind,'in_stock',None,None,package_id))
    db.execute('INSERT INTO part_movement(serial,movement,screen_id,other_serial,at_,package_id,source_id) VALUES(?,?,?,?,?,?,?)',
               (serial,'scanned_into_stock',None,None,None,package_id,None))

def capture(path,file):
    """Import an offline maintenance capture package.

    First blackout capture opens outages and keeps both component candidates on
    every screen.  Later supplement captures carry independent readings that
    may eliminate one of the two physical worlds.
    """
    with connect(path) as db,db:
        sid,d,replay=source(db,'capture',file)
        if replay and db.execute('SELECT 1 FROM capture_package WHERE id=?',(d['id'],)).fetchone():
            worlds,views=_open_worlds(db)
            return {'id':d['id'],'replayed':True,'screens':[views[s] for s in sorted(views)],
                    'open_worlds':sorted(worlds),'missing_verification':_missing_checks(views)}
        at(d['captured_at'])
        if d['kind'] not in ('blackout','supplement'):raise ValueError('unknown_capture_kind')
        if db.execute('SELECT 1 FROM capture_package WHERE id=?',(d['id'],)).fetchone():
            raise ValueError('event_id_conflict')
        db.execute('INSERT INTO capture_package VALUES(?,?,?,?)',(d['id'],d['kind'],d['captured_at'],sid))
        artifacts=d.get('artifacts',[])
        for a in artifacts:_validate_artifact(a)
        for part in d.get('spares',[]):
            _register_spare(db,part['serial'],part['kind'],d['id'])
        # warehouse scan artifacts carry the same provenance chain and also
        # register the physical piece by its scanned serial
        for a in artifacts:
            if a['type']=='part_scan':
                p=a['payload']
                if not p.get('serial') or not p.get('kind'):raise ValueError('part_scan_requires_serial_and_kind')
                _register_spare(db,p['serial'],p['kind'],d['id'])
        for a in artifacts:
            if not db.execute('SELECT 1 FROM screen WHERE id=?',(a['screen_id'],)).fetchone():
                raise ValueError('unknown_screen')
            db.execute('INSERT INTO artifact VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (a['id'],d['id'],a['screen_id'],a['type'],a['device_clock_at'],
                        a['clock_offset_seconds'],a['operator'],a['origin_sha'],
                        json.dumps(a['payload'],ensure_ascii=False,sort_keys=True),sid))
        if d['kind']=='blackout':
            for screen in sorted({a['screen_id'] for a in artifacts}):
                if not db.execute('SELECT 1 FROM screen WHERE id=?',(screen,)).fetchone():
                    raise ValueError('unknown_screen')
                for component in COMPONENTS:
                    db.execute('INSERT INTO fault_hypothesis(screen_id,component,status,eliminated_by,package_id) VALUES(?,?,?,?,?)',
                               (screen,component,'candidate',None,d['id']))
                outage_id='OUT-'+d['id']+'-'+screen
                if not db.execute('SELECT 1 FROM screen_outage WHERE id=?',(outage_id,)).fetchone():
                    db.execute('INSERT INTO screen_outage(id,screen_id,package_id,opened_at,maintenance_id,status) VALUES(?,?,?,?,?,?)',
                               (outage_id,screen,d['id'],d['captured_at'],'M-'+d['id']+'-'+screen,'open'))
                    mid='M-'+d['id']+'-'+screen
                    if not db.execute('SELECT 1 FROM maintenance WHERE id=?',(mid,)).fetchone():
                        db.execute('INSERT INTO maintenance VALUES(?,?,?,?,?)',
                                   (mid,screen,d['captured_at'],None,sid))
                        affected=db.execute("SELECT id FROM booking WHERE screen_id=? AND start_at<? AND end_at>? AND status='confirmed'",
                                            (screen,d['captured_at'],d['captured_at'])).fetchall()
                        for row in affected:
                            db.execute("UPDATE booking SET status='interrupted' WHERE id=?",(row['id'],))
        # independent supplementary readings narrow the possible worlds
        for a in artifacts:
            verdict=_conclusive(a)
            if not verdict:continue
            component,healthy=verdict
            other=next(c for c in COMPONENTS if c!=component)
            target=component if not healthy else other
            if db.execute("SELECT 1 FROM fault_hypothesis WHERE screen_id=? AND component=? AND status='confirmed'",
                          (a['screen_id'],target)).fetchone():
                continue
            if healthy:
                db.execute("UPDATE fault_hypothesis SET status='eliminated',eliminated_by=? WHERE screen_id=? AND component=? AND status='candidate'",
                           (a['id'],a['screen_id'],component))
            else:
                db.execute("UPDATE fault_hypothesis SET status='eliminated',eliminated_by=? WHERE screen_id=? AND component=? AND status='candidate'",
                           (a['screen_id'],a['screen_id'],other))
            db.execute("INSERT INTO fault_hypothesis(screen_id,component,status,eliminated_by,package_id) VALUES(?,?,?,?,?)",
                       (a['screen_id'],target,'confirmed',a['id'],d['id']))
        worlds,views=_open_worlds(db)
        if d['kind']=='supplement' and not worlds:raise ValueError('hypotheses_mutually_exclusive')
        closed_world=_close_plans(db,d,worlds)
        return {'id':d['id'],'screens':[views[s] for s in sorted(views)],
                'open_worlds':sorted(worlds),'missing_verification':_missing_checks(views),
                'closed_world':closed_world}

def _close_plans(db,d,worlds):
    """When supplementary evidence leaves exactly one physical world, every
    ratified plan is closed against it deterministically.  A plan whose
    pre-fixed psu target contradicts the confirmed world is rejected rather
    than rewritten after the fact."""
    if len(worlds)!=1:return None
    name=next(iter(worlds))
    for row in db.execute("SELECT * FROM dispatch_plan WHERE status='ratified' AND route!='naive'"):
        target=next(s for s,c in worlds[name].items() if c=='psu')
        for st in db.execute("SELECT * FROM plan_step WHERE plan_id=? AND action='install_psu'",(row['id'],)):
            if st['requires_world']==name and st['screen_id']!=target:
                raise ValueError('plan_contradicts_closed_diagnosis')
        if d['captured_at']<=row['locked_at']:raise ValueError('closure_before_lock')
        db.execute("UPDATE dispatch_plan SET status='closed',closed_at=? WHERE id=?",(d['captured_at'],row['id']))
    return name

# ---------------------------------------------------------------------------
# Naive per-screen solution: local threshold rules + first-file-wins spare grab.
# Demonstrated against both physical worlds with the SAME original package.
# ---------------------------------------------------------------------------
def naive_local_dispatch(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'naive_plan',file)
        plan_id=d['id']
        old=db.execute('SELECT * FROM dispatch_plan WHERE id=?',(plan_id,)).fetchone()
        if old:
            if old['source_id']!=sid:raise ValueError('event_id_conflict')
            return {'id':plan_id,'replayed':True}
        res=d['resources'];psu_serial=res['psu_serial'];unit_serial=res['unit_serial'];tech=res['technician']
        when=at(res['earliest_at'])
        if not (at(res['permit_from'])<=when<=at(res['permit_to'])):
            raise ValueError('outside_power_permit_window')
        screens=[r['screen_id'] for r in db.execute('SELECT DISTINCT screen_id AS screen_id FROM artifact ORDER BY screen_id')]
        verdicts={};double_promise=[];travel_conflicts=[];stranded=[];steps=[]
        occupied={}  # technician -> screen at `when`
        for screen in screens:
            dip=db.execute("SELECT 1 FROM artifact WHERE screen_id=? AND type='waveform' AND json_extract(payload,'$.bus_voltage')<?",
                           (screen,d.get('voltage_threshold',48.0))).fetchone()
            guess='psu' if dip else 'panel'
            verdicts[screen]=guess
            if guess=='psu':
                held=db.execute('SELECT * FROM part_reservation WHERE part_serial=?',(psu_serial,)).fetchone()
                if held:
                    double_promise.append({'serial':psu_serial,'first_screen':held['screen_id'],'second_screen':screen})
                    stranded.append(screen)
                    continue
                db.execute('INSERT OR IGNORE INTO part_reservation VALUES(?,?,?,?)',(psu_serial,plan_id,screen,d['package_id']))
                serial,action=psu_serial,'install_psu'
            else:
                serial,action=unit_serial,'install_unit'
            # naive planner ignores the >=4 minute cross-site travel window
            if tech in occupied and occupied[tech]!=screen:
                travel_conflicts.append({'technician':tech,'at':when,'screens':sorted([screen,occupied[tech]])})
            occupied[tech]=screen
            steps.append((plan_id+'-'+screen,screen,action,serial,tech,res.get('permit_id'),when))
        infeasible=bool(double_promise or travel_conflicts)
        db.execute('INSERT INTO dispatch_plan VALUES(?,?,?,?,?,?,?,?)',
                   (plan_id,'naive',d['locked_at'],None,'infeasible' if infeasible else 'ratified',
                    d['package_id'],json.dumps({'local_verdicts':verdicts},ensure_ascii=False,sort_keys=True),sid))
        for st in steps:
            db.execute('INSERT INTO plan_step VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (st[0],plan_id,st[1],st[2],st[3],None,st[4],st[5],st[6],None,0,'planned'))
        worlds,_=_open_worlds(db)
        wrong=[]
        for name,assign in worlds.items():
            for screen,component in assign.items():
                if verdicts.get(screen)!=component:
                    wrong.append({'world':name,'screen':screen,'guessed':verdicts.get(screen),'actual':component})
        return {'id':plan_id,'local_verdicts':verdicts,'double_promise':double_promise,
                'travel_conflicts':travel_conflicts,'stranded_screens':sorted(set(stranded)),
                'wrong_in_worlds':wrong,'open_worlds':sorted(worlds),
                'status':'infeasible' if infeasible else 'ratified'}

# ---------------------------------------------------------------------------
# Joint, contract-authorized dispatch routes.
# ---------------------------------------------------------------------------
def _reserve(db,serial,plan_id,screen,package_id):
    # reservations are plan-scoped commitments; the physical piece stays
    # in_stock until execute_repair actually installs it, so two pre-authorized
    # routes may both name the same serial without consuming it twice
    part=db.execute('SELECT * FROM spare_part WHERE serial=?',(serial,)).fetchone()
    if not part:raise ValueError('unknown_spare_part')
    if part['status']!='in_stock':raise ValueError('spare_not_in_stock')
    if not db.execute('SELECT 1 FROM part_reservation WHERE part_serial=? AND plan_id=?',(serial,plan_id)).fetchone():
        db.execute('INSERT INTO part_reservation VALUES(?,?,?,?)',(serial,plan_id,screen,package_id))

def ratify(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'dispatch_plan',file)
        plan_id=d['id']
        old=db.execute('SELECT * FROM dispatch_plan WHERE id=?',(plan_id,)).fetchone()
        if old:
            if old['source_id']!=sid:raise ValueError('event_id_conflict')
            return {'id':plan_id,'replayed':True,'status':old['status']}
        route=d['route']
        if route not in ('fast_core','min_downtime'):raise ValueError('unknown_route')
        worlds,views=_open_worlds(db)
        if not worlds:raise ValueError('no_open_world')
        # routes and customer commitments must be fixed BEFORE the diagnosis is
        # distinguished; locking afterwards is a retroactive, report-driven choice
        if len(worlds)==1:
            latest=db.execute('SELECT MAX(captured_at) AS t FROM capture_package WHERE kind=?',('supplement',)).fetchone()['t']
            if latest and at(d['locked_at'])>=latest:raise ValueError('closure_before_lock')
        res=d['resources'];tech=res['technician'];permit=res['permit_id']
        pfrom,pto=at(res['permit_from']),at(res['permit_to'])
        steps=[];tech_slots={}
        def add(screen,action,serial,when,requires_world=None,travel_from=None,travel_minutes=None):
            if not (pfrom<=when<=pto):raise ValueError('outside_power_permit_window')
            # contingent steps belong to mutually exclusive world branches; only
            # steps inside the same branch (or a branch-neutral step) can collide
            for (tech2,when2,branch2),screen2 in tech_slots.items():
                if tech2==tech and when2==when and (branch2==requires_world or branch2 is None or requires_world is None):
                    raise ValueError('technician_double_booking')
            if travel_minutes is not None and travel_minutes<4:raise ValueError('travel_window_ignored')
            tech_slots[(tech,when,requires_world)]=screen
            suffix='-'+requires_world if requires_world else ''
            steps.append((plan_id+'-'+screen+'-'+action+suffix,screen,action,serial,
                          requires_world,tech,permit,when,travel_from,travel_minutes))
        commitments={}
        if route=='fast_core':
            core=d['core_screen']
            if core not in views:raise ValueError('unknown_core_screen')
            other=next(s for s in views if s!=core)
            when_unit=max(at(res['unit_available_at']),at(res['earliest_at']))
            add(core,'install_unit',res['unit_serial'],when_unit)
            commitments[core]={'action':'whole_unit','sell_from':plus(when_unit,180),
                               'customer':'new_orders_accepted_after_recommission'}
            commitments[other]={'action':'hold_pending_isolation','sell_from':None,
                                'customer':'sales_suspended'}
        else:
            psu_when=max(at(res['psu_available_at']),at(res['earliest_at']))
            travel=d.get('travel_minutes',4)
            for name,assign in worlds.items():
                psu_screen=next(s for s,c in assign.items() if c=='psu')
                unit_screen=next(s for s,c in assign.items() if c=='panel')
                unit_when=plus(psu_when,travel*60)  # travel window between the two sites
                add(psu_screen,'install_psu',res['psu_serial'],psu_when,requires_world=name)
                add(unit_screen,'install_unit',res['unit_serial'],unit_when,requires_world=name,
                    travel_from=psu_screen,travel_minutes=travel)
                commitments[name]={'psu_screen':psu_screen,'unit_screen':unit_screen,
                                   'psu_at':psu_when,'unit_at':unit_when}
        # a serial may never be promised to two different screens inside one world branch
        branch={}
        for st in steps:
            key=(st[4],st[3])
            if key in branch and branch[key]!=st[1]:raise ValueError('spare_double_promised')
            branch[key]=st[1]
            _reserve(db,st[3],plan_id,st[1],d['package_id'])
        db.execute('INSERT INTO dispatch_plan VALUES(?,?,?,?,?,?,?,?)',
                   (plan_id,route,at(d['locked_at']),None,'ratified',d['package_id'],
                    json.dumps(commitments,ensure_ascii=False,sort_keys=True),sid))
        for st in steps:
            db.execute('INSERT INTO plan_step VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (st[0],plan_id,st[1],st[2],st[3],st[4],st[5],st[6],st[7],st[8],st[9],'planned'))
        return {'id':plan_id,'route':route,'status':'ratified','open_worlds':sorted(worlds),
                'commitments':commitments,'locked_at':d['locked_at']}

def close_diagnosis(path,file):
    """Import a supplementary capture that leaves exactly one physical world;
    ratified plans are closed (or rejected on contradiction) inside capture."""
    result=capture(path,file)
    if result.get('closed_world') is None:raise ValueError('diagnosis_still_ambiguous')
    return {'closed_world':result['closed_world'],'capture':result}

def execute_repair(path,file):
    with connect(path) as db,db:
        sid,d,replay=source(db,'repair',file)
        oid=d['id']
        if db.execute('SELECT 1 FROM repair_order WHERE id=?',(oid,)).fetchone():
            return {'id':oid,'replayed':True}
        plan=db.execute('SELECT * FROM dispatch_plan WHERE id=?',(d['plan_id'],)).fetchone()
        if not plan:raise ValueError('unknown_plan')
        if plan['route']!='naive' and plan['status']!='closed':raise ValueError('plan_not_closed')
        screen=d['screen_id'];when=at(d['executed_at'])
        name=None
        if plan['route']!='naive':
            worlds,_=_open_worlds(db)
            if len(worlds)!=1:raise ValueError('diagnosis_still_ambiguous')
            name=next(iter(worlds));actual=worlds[name][screen]
            step=db.execute("SELECT * FROM plan_step WHERE plan_id=? AND screen_id=? AND status='planned' AND (requires_world IS NULL OR requires_world=?) ORDER BY CASE WHEN requires_world IS NULL THEN 0 ELSE 1 END,scheduled_at,id LIMIT 1",
                            (d['plan_id'],screen,name)).fetchone()
            if not step:raise ValueError('no_planned_step')
            # a whole-unit swap serves either component fault; a psu swap only fits a psu fault
            if step['action']=='install_psu' and actual!='psu':
                raise ValueError('repair_contradicts_diagnosis')
            if when<step['scheduled_at']:raise ValueError('before_technician_arrival_window')
            prev=db.execute("SELECT * FROM plan_step WHERE plan_id=? AND technician=? AND (requires_world IS NULL OR requires_world=?) AND scheduled_at<? ORDER BY scheduled_at DESC LIMIT 1",
                            (d['plan_id'],step['technician'],name,when)).fetchone()
            if prev and prev['screen_id']!=screen and (parse(when)-parse(prev['scheduled_at'])).total_seconds()<240:
                raise ValueError('technician_travel_window_violated')
        else:
            step=db.execute("SELECT * FROM plan_step WHERE plan_id=? AND screen_id=? AND status='planned' ORDER BY scheduled_at,id LIMIT 1",
                            (d['plan_id'],screen)).fetchone()
            if not step:raise ValueError('no_planned_step')
        serial=step['part_serial']
        part=db.execute('SELECT * FROM spare_part WHERE serial=?',(serial,)).fetchone()
        if not part or part['status']!='in_stock':raise ValueError('spare_not_available')
        if not db.execute('SELECT 1 FROM part_reservation WHERE part_serial=? AND plan_id=?',(serial,d['plan_id'])).fetchone():
            raise ValueError('spare_not_reserved')
        old_serial=d.get('removed_serial')
        if old_serial:
            if old_serial==serial:raise ValueError('old_and_new_must_be_distinct_pieces')
            old=db.execute('SELECT * FROM spare_part WHERE serial=?',(old_serial,)).fetchone()
            if not old:raise ValueError('unknown_removed_part')
            db.execute("UPDATE spare_part SET status='returned_stock',installed_screen=NULL,installed_at=NULL WHERE serial=?",(old_serial,))
            db.execute('INSERT INTO part_movement(serial,movement,screen_id,other_serial,at_,package_id,source_id) VALUES(?,?,?,?,?,?,?)',
                       (old_serial,'return_to_stock',screen,serial,when,d['package_id'],sid))
        db.execute("UPDATE spare_part SET status='installed',installed_screen=?,installed_at=? WHERE serial=?",(screen,when,serial))
        db.execute('INSERT INTO part_movement(serial,movement,screen_id,other_serial,at_,package_id,source_id) VALUES(?,?,?,?,?,?,?)',
                   (serial,'installed_on_screen',screen,old_serial,when,d['package_id'],sid))
        db.execute("UPDATE plan_step SET status='done' WHERE id=?",(step['id'],))
        db.execute('INSERT INTO repair_order VALUES(?,?,?,?,?,?)',(oid,d['plan_id'],screen,when,d['package_id'],sid))
        return {'id':oid,'screen_id':screen,'installed_serial':serial,'removed_serial':old_serial,'at':when}

def recommission(path,file):
    """Close an outage only after two independent checks: isolated power and
    panel light-up.  A lone heartbeat never proves content can safely air."""
    with connect(path) as db,db:
        sid,d,replay=source(db,'recommission',file)
        screen=d['screen_id']
        prior=db.execute('SELECT * FROM screen_recommission WHERE screen_id=?',(screen,)).fetchone()
        if prior and prior['source_id']==sid:
            return {'screen_id':screen,'replayed':True,'status':prior['status'],'sell_from':prior['sell_from']}
        if prior and prior['status']=='recommissioned':raise ValueError('screen_already_recommissioned')
        when=at(d['reopened_at'])
        outages=db.execute("SELECT * FROM screen_outage WHERE screen_id=? AND status IN ('open','suspended') ORDER BY opened_at",(screen,)).fetchall()
        if not outages:raise ValueError('no_open_outage')
        kinds=[]
        for a in d.get('artifacts',[]):
            _validate_artifact(a)
            db.execute('INSERT INTO artifact VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (a['id'],d['package_id'],a['screen_id'],a['type'],a['device_clock_at'],
                        a['clock_offset_seconds'],a['operator'],a['origin_sha'],
                        json.dumps(a['payload'],ensure_ascii=False,sort_keys=True),sid))
            verdict=_conclusive(a)
            result='pass' if (verdict and verdict[1]) else 'fail' if verdict else 'observed'
            kinds.append((a['type'],result,a['id']))
            db.execute('INSERT INTO reverify(screen_id,check_kind,result,package_id,source_id) VALUES(?,?,?,?,?)',
                       (screen,a['type'],result,d['package_id'],sid))
        only_heartbeat=bool(kinds) and all(k[0]=='heartbeat' for k in kinds)
        power=next((k for k in kinds if k[0]=='power_isolation'),None)
        display=next((k for k in kinds if k[0]=='panel_lightup'),None)
        missing=[]
        if not power or power[1]!='pass':missing.append('power_isolation')
        if not display or display[1]!='pass':missing.append('panel_lightup')
        if only_heartbeat or missing:
            note='heartbeat_alone_not_sufficient' if only_heartbeat else 'missing_'+'_'.join(missing)
            db.execute("UPDATE screen_outage SET status='suspended',note=? WHERE screen_id=? AND status IN ('open','suspended')",(note,screen))
            db.execute('INSERT OR REPLACE INTO screen_recommission VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (screen,outages[-1]['id'],power[2] if power else None,display[2] if display else None,
                        None,None,'suspended',note,d['package_id'],sid))
            return {'screen_id':screen,'status':'suspended','sell_from':None,'note':note}
        rows=db.execute('SELECT * FROM maintenance WHERE screen_id=? AND end_at IS NULL ORDER BY start_at',(screen,)).fetchall()
        if not rows:raise ValueError('no_open_maintenance')
        for row in rows:
            if when<=row['start_at']:raise ValueError('reopen_before_outage')
            db.execute('UPDATE maintenance SET end_at=? WHERE id=?',(when,row['id']))
        db.execute("UPDATE screen_outage SET status='recommissioned',note=NULL WHERE screen_id=? AND status IN ('open','suspended')",(screen,))
        db.execute('INSERT OR REPLACE INTO screen_recommission VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (screen,outages[-1]['id'],power[2],display[2],when,when,'recommissioned',
                    'dual_checks_closed',d['package_id'],sid))
        # past interrupted bookings keep their adjudication and underplay obligations
        return {'screen_id':screen,'status':'recommissioned','sell_from':when,
                'power_artifact':power[2],'display_artifact':display[2]}
