"""Generate the offline maintenance / spare-parts / dispatch fixtures.

Run once:  python tools/build_repair_fixtures.py
All cities, people and serials are synthetic.  Every artifact payload is
hashed into origin_sha so the ledger can verify original identity.
"""
import json,hashlib
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'fixtures'/'repair'
OUT.mkdir(parents=True,exist_ok=True)

def sha(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()

def art(id_,screen,type_,clock,operator,payload,offset=0):
    return {'id':id_,'screen_id':screen,'type':type_,'device_clock_at':clock,
            'clock_offset_seconds':offset,'operator':operator,'origin_sha':sha(payload),'payload':payload}

def write(name,doc):
    (OUT/name).write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding='utf-8')

# ---------------------------------------------------------------- blackout PKG1
T0='2026-06-10T10:04:00Z'
pkg={'id':'PKG1','kind':'blackout','captured_at':T0,
     'spares':[{'serial':'PSU-9','kind':'psu'},{'serial':'WU-1','kind':'whole_unit'},
               {'serial':'PSU-OLD-S1','kind':'psu'},{'serial':'PNL-OLD-S2','kind':'panel'}],
     'artifacts':[
        art('A-SCAN-PSU9','S1','part_scan',T0,'storekeeper-yu',
            {'serial':'PSU-9','kind':'psu','bin':'A-12','action':'scan_into_stock'}),
        art('A-SCAN-WU1','S1','part_scan',T0,'storekeeper-yu',
            {'serial':'WU-1','kind':'whole_unit','bin':'B-03','action':'scan_into_stock'}),
        art('A-S1-WF','S1','waveform',T0,'tech-lin',
            {'bus_voltage':31.2,'rated_voltage':48.0,'sample_hz':1000,'samples':4096},offset=2),
        art('A-S1-PST','S1','panel_self_test','2026-06-10T10:04:20Z','tech-lin',
            {'code':'0x-- incomplete','completed':False,'rows':3,'expected_rows':12},offset=2),
        art('A-S1-HB','S1','heartbeat','2026-06-10T10:04:30Z','tech-lin',
            {'alive':True,'note':'control board heartbeat persists during black screen'},offset=2),
        art('A-S2-WF','S2','waveform',T0,'tech-lin',
            {'bus_voltage':30.8,'rated_voltage':48.0,'sample_hz':1000,'samples':4096},offset=4),
        art('A-S2-HB','S2','heartbeat','2026-06-10T10:04:30Z','tech-lin',
            {'alive':True,'note':'single heartbeat point, screen black'},offset=4)]}
write('capture-blackout.json',pkg)

# --------------------------------------------- independent supplementary reads
def supplement(pkg_id,s1_power,s1_lit,s2_power,s2_lit):
    arts=[]
    if s1_power is not None:
        arts.append(art('A-S1-ISO','S1','power_isolation','2026-06-10T10:05:00Z','tech-lin',
                        {'bus_voltage':s1_power,'threshold':48.0,'isolated_rail':True},offset=1))
    if s1_lit is not None:
        arts.append(art('A-S1-LIT','S1','panel_lightup','2026-06-10T10:05:20Z','tech-lin',
                        {'lit':s1_lit,'luminance_cd_m2':1200 if s1_lit else 0},offset=1))
    if s2_power is not None:
        arts.append(art('A-S2-ISO','S2','power_isolation','2026-06-10T10:05:00Z','tech-lin',
                        {'bus_voltage':s2_power,'threshold':48.0,'isolated_rail':True},offset=1))
    if s2_lit is not None:
        arts.append(art('A-S2-LIT','S2','panel_lightup','2026-06-10T10:05:20Z','tech-lin',
                        {'lit':s2_lit,'luminance_cd_m2':1200 if s2_lit else 0},offset=1))
    return {'id':pkg_id,'kind':'supplement','captured_at':'2026-06-10T10:05:30Z','artifacts':arts}

# W-PSU: S1 psu faulty (low isolated voltage, panel healthy on external test
# supply), S2 panel faulty (healthy psu, panel will not light)
write('supplement-w-psu.json',supplement('PKG2-PSU',32.0,True,52.1,False))
# W-PANEL: S1 panel faulty (healthy isolated voltage, no light), S2 psu faulty
write('supplement-w-panel.json',supplement('PKG2-PNL',51.5,False,33.4,True))

# ------------------------------------------------------------- naive local plan
write('naive-dispatch.json',
      {'id':'NP1','package_id':'PKG1','locked_at':'2026-06-10T10:05:00Z','voltage_threshold':48.0,
       'resources':{'technician':'tech-lin','permit_id':'PMT-1',
                    'permit_from':'2026-06-10T10:00:00Z','permit_to':'2026-06-10T12:00:00Z',
                    'earliest_at':'2026-06-10T10:04:00Z','psu_serial':'PSU-9','unit_serial':'WU-1'}})
# naive plan for a case where both screens present healthy bus voltage: the
# local rule then guesses panel on both and schedules one technician at one minute
write('naive-dispatch-travel.json',
      {'id':'NP2','package_id':'PKG1','locked_at':'2026-06-10T10:05:00Z','voltage_threshold':10.0,
       'resources':{'technician':'tech-lin','permit_id':'PMT-1',
                    'permit_from':'2026-06-10T10:00:00Z','permit_to':'2026-06-10T12:00:00Z',
                    'earliest_at':'2026-06-10T10:04:00Z','psu_serial':'PSU-9','unit_serial':'WU-1'}})

# ---------------------------------------------------------------- dispatch plans
common_res={'technician':'tech-lin','permit_id':'PMT-1',
            'permit_from':'2026-06-10T10:00:00Z','permit_to':'2026-06-10T12:00:00Z',
            'earliest_at':'2026-06-10T10:04:00Z',
            'psu_available_at':'2026-06-10T10:06:00Z','unit_available_at':'2026-06-10T10:08:00Z',
            'psu_serial':'PSU-9','unit_serial':'WU-1'}
write('plan-fast-core.json',
      {'id':'PLAN-A','package_id':'PKG1','route':'fast_core','core_screen':'S1',
       'locked_at':'2026-06-10T10:05:00Z','resources':common_res})
write('plan-min-downtime.json',
      {'id':'PLAN-B','package_id':'PKG1','route':'min_downtime',
       'locked_at':'2026-06-10T10:05:00Z','resources':common_res})
# invalid plan claiming only 2 minutes of cross-site travel for the single technician
bad_travel=json.loads((OUT/'plan-min-downtime.json').read_text())
bad_travel['id']='PLAN-BAD-TRAVEL';bad_travel['travel_minutes']=2
write('plan-bad-travel.json',bad_travel)

# ----------------------------------------------------------------- repair orders
def repair(oid,plan,screen,when,removed):
    write(oid.lower()+'.json',
          {'id':oid,'plan_id':plan,'screen_id':screen,'package_id':'PKG1',
           'executed_at':when,'removed_serial':removed})

# Route A: whole unit onto S1 at 10:08
repair('RO-A1','PLAN-A','S1','2026-06-10T10:08:00Z','WU-OLD-S1')

# Route B timings: psu at 10:06 on the confirmed psu screen, unit at 10:10 on the other
repair('RO-B1','PLAN-B','S1','2026-06-10T10:06:00Z','PSU-OLD-S1')
repair('RO-B2','PLAN-B','S2','2026-06-10T10:10:00Z','PNL-OLD-S2')
# W-PANEL ordering: S2 psu at 10:06, S1 unit at 10:10
repair('RO-B1P','PLAN-B','S2','2026-06-10T10:06:00Z','PSU-OLD-S2')
repair('RO-B2P','PLAN-B','S1','2026-06-10T10:10:00Z','PNL-OLD-S1')
# spares referenced by removed serials must exist in stock for the ledger
extra=[{'serial':'WU-OLD-S1','kind':'whole_unit'},{'serial':'PSU-OLD-S2','kind':'psu'},{'serial':'PNL-OLD-S1','kind':'panel'}]
p=json.loads((OUT/'capture-blackout.json').read_text())
have={x['serial'] for x in p['spares']}
for e in extra:
    if e['serial'] not in have:p['spares'].append(e)
write('capture-blackout.json',p)

# ------------------------------------------------------------- recommissions
def reopen(doc_id,screen,when,power_v,lit,heartbeat_only=False):
    if heartbeat_only:
        arts=[art('A-'+screen+'-HB2',screen,'heartbeat',when,'tech-lin',{'alive':True},offset=0)]
    else:
        arts=[art('A-'+screen+'-ISO2',screen,'power_isolation',when,'tech-lin',
                  {'bus_voltage':power_v,'threshold':48.0,'isolated_rail':True},offset=0),
              art('A-'+screen+'-LIT2',screen,'panel_lightup',when,'tech-lin',
                  {'lit':lit,'luminance_cd_m2':1200},offset=0),
              art('A-'+screen+'-PHO',screen,'reinspect_photo',when,'tech-lin',
                  {'photo_sha':sha({'scene':screen+' final visual reinspection'})},offset=0)]
    write(doc_id+'.json',{'id':doc_id,'package_id':'PKG3','screen_id':screen,
                          'reopened_at':when,'artifacts':arts})

reopen('reopen-s1','S1','2026-06-10T10:11:00Z',52.0,True)
reopen('reopen-s2','S2','2026-06-10T10:13:00Z',52.0,True)
reopen('reopen-s2-heartbeat','S2','2026-06-10T10:13:00Z',None,None,heartbeat_only=True)
# only one independent check: conclusion must remain unreliable
write('reopen-s2-power-only.json',
      {'id':'RE-S2-PO','package_id':'PKG3','screen_id':'S2','reopened_at':'2026-06-10T10:13:00Z',
       'artifacts':[art('A-S2-ISO3','S2','power_isolation','2026-06-10T10:13:00Z','tech-lin',
                        {'bus_voltage':52.0,'threshold':48.0,'isolated_rail':True},offset=0)]})

# S2 screen definition used alongside screen-ok.json's S1
(OUT/'screen-s2.json').write_text(json.dumps({'id':'S2','city':'C1','grade':3}),encoding='utf-8')
print('wrote',len(list(OUT.glob('*.json'))),'fixtures to',OUT)
