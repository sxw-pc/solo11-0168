import sqlite3,hashlib,json
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime

SCHEMA='''PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,kind TEXT,sha TEXT UNIQUE,raw BLOB);
CREATE TABLE IF NOT EXISTS creative(id TEXT,revision INTEGER,category TEXT,city TEXT,valid_from TEXT,valid_to TEXT,status TEXT,source_id INTEGER,PRIMARY KEY(id,revision));
CREATE TABLE IF NOT EXISTS license(id TEXT PRIMARY KEY,creative_id TEXT,category TEXT,city TEXT,valid_to TEXT,issuer TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS screen(id TEXT PRIMARY KEY,city TEXT,grade INTEGER,source_id INTEGER);
CREATE TABLE IF NOT EXISTS maintenance(id TEXT PRIMARY KEY,screen_id TEXT,start_at TEXT,end_at TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS booking(id TEXT PRIMARY KEY,creative_id TEXT,revision INTEGER,screen_id TEXT,start_at TEXT,end_at TEXT,priority INTEGER,status TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS proof(id TEXT PRIMARY KEY,booking_id TEXT,screen_id TEXT,start_at TEXT,end_at TEXT,observer TEXT,status TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS obligation(id INTEGER PRIMARY KEY,booking_id TEXT,missing_seconds INTEGER,status TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS makegood(id TEXT PRIMARY KEY,obligation_id INTEGER,booking_id TEXT,status TEXT,source_id INTEGER);
-- Offline repair intake.  Every physical observation keeps its original bytes,
-- original identity reference, operator and the device-clock calibration used.
CREATE TABLE IF NOT EXISTS repair_case(id TEXT PRIMARY KEY,package_id TEXT,status TEXT,fault_at TEXT,constraints TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS repair_case_screen(case_id TEXT,screen_id TEXT,site_id TEXT,PRIMARY KEY(case_id,screen_id));
CREATE TABLE IF NOT EXISTS repair_artifact(id TEXT PRIMARY KEY,case_id TEXT,kind TEXT,screen_id TEXT,origin TEXT,source_ref TEXT,sha TEXT,raw BLOB,operator TEXT,observed_at TEXT,device_time TEXT,clock_offset_seconds REAL,clock_uncertainty_seconds REAL,payload TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS fault_candidate(case_id TEXT,screen_id TEXT,component TEXT,status TEXT,support TEXT,missing TEXT,detail TEXT,PRIMARY KEY(case_id,screen_id,component));
CREATE TABLE IF NOT EXISTS possible_world(case_id TEXT,world_id TEXT,assignment TEXT,status TEXT,PRIMARY KEY(case_id,world_id));
CREATE TABLE IF NOT EXISTS screen_site(screen_id TEXT PRIMARY KEY,site_id TEXT);
CREATE TABLE IF NOT EXISTS site_travel(from_site TEXT,to_site TEXT,minutes INTEGER,PRIMARY KEY(from_site,to_site));
CREATE TABLE IF NOT EXISTS technician(id TEXT PRIMARY KEY,name TEXT,home_site TEXT,available_from TEXT,available_to TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS spare(item_id TEXT PRIMARY KEY,component_type TEXT,serial TEXT UNIQUE,site_id TEXT,status TEXT,available_at TEXT,scan_sha TEXT,operator TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS outage_permit(id TEXT PRIMARY KEY,site_id TEXT,screen_id TEXT,start_at TEXT,end_at TEXT,status TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS dispatch_plan(id TEXT PRIMARY KEY,case_id TEXT,route TEXT,objective TEXT,commitment TEXT,committed_at TEXT,technician_id TEXT,status TEXT,resolved_world TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS dispatch_task(plan_id TEXT,world_id TEXT,seq INTEGER,screen_id TEXT,site_id TEXT,action TEXT,component_type TEXT,spare_id TEXT,start_at TEXT,end_at TEXT,PRIMARY KEY(plan_id,world_id,seq));
CREATE TABLE IF NOT EXISTS spare_allocation(plan_id TEXT,world_id TEXT,item_id TEXT,screen_id TEXT,at TEXT,PRIMARY KEY(plan_id,world_id,item_id));
CREATE TABLE IF NOT EXISTS returned_part(id TEXT PRIMARY KEY,case_id TEXT,screen_id TEXT,component_type TEXT,serial TEXT,returned_at TEXT,plan_id TEXT,world_id TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS rts(id TEXT PRIMARY KEY,case_id TEXT,screen_id TEXT,plan_id TEXT,world_id TEXT,power_recheck TEXT,picture_recheck TEXT,closed_at TEXT,status TEXT,missing TEXT,source_id INTEGER);
'''
@contextmanager
def connect(path):
    dest=Path(path);dest.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(dest);db.row_factory=sqlite3.Row
    try:db.executescript(SCHEMA);yield db
    finally:db.close()

def at(value):
    if not isinstance(value,str) or not value.endswith('Z'):raise ValueError('utc_required')
    datetime.fromisoformat(value[:-1]+'+00:00');return value

def source(db,kind,file):
    return source_bytes(db,kind,Path(file).read_bytes())

def source_bytes(db,kind,raw):
    digest=hashlib.sha256(raw).hexdigest();doc=json.loads(raw)
    old=db.execute('SELECT id,kind FROM source WHERE sha=?',(digest,)).fetchone()
    if old:
        if old['kind']!=kind:raise ValueError('source_kind_conflict')
        return old['id'],doc,True
    return db.execute('INSERT INTO source(kind,sha,raw) VALUES(?,?,?)',(kind,digest,raw)).lastrowid,doc,False

def already(db,table,event_id,sid):
    old=db.execute(f'SELECT source_id FROM {table} WHERE id=?',(event_id,)).fetchone()
    if old:
        if old['source_id']!=sid:raise ValueError('event_id_conflict')
        return True
    return False

def state(db):
    tables=('creative','license','screen','maintenance','booking','proof','obligation','makegood',
            'repair_case','repair_case_screen','fault_candidate','possible_world',
            'screen_site','site_travel','technician','spare','spare_allocation','returned_part',
            'outage_permit','dispatch_plan','dispatch_task','rts')
    out={t:[dict(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1')] for t in tables}
    # Original artifact bytes are verified by sha256; do not dump binary into the report.
    out['repair_artifact']=[dict(r) for r in db.execute(
        'SELECT id,case_id,kind,screen_id,origin,source_ref,sha,operator,observed_at,device_time,'
        'clock_offset_seconds,clock_uncertainty_seconds,payload,source_id '
        'FROM repair_artifact ORDER BY id')]
    return out
