import sqlite3,hashlib,json
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone

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
CREATE TABLE IF NOT EXISTS capture_package(id TEXT PRIMARY KEY,kind TEXT,captured_at TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS artifact(id TEXT PRIMARY KEY,package_id TEXT,screen_id TEXT,type TEXT,device_clock_at TEXT,clock_offset_seconds INTEGER,operator TEXT,origin_sha TEXT,payload TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS fault_hypothesis(id INTEGER PRIMARY KEY AUTOINCREMENT,screen_id TEXT,component TEXT,status TEXT,eliminated_by TEXT,package_id TEXT);
CREATE TABLE IF NOT EXISTS screen_outage(id TEXT PRIMARY KEY,screen_id TEXT,package_id TEXT,opened_at TEXT,maintenance_id TEXT,status TEXT,note TEXT);
CREATE TABLE IF NOT EXISTS spare_part(serial TEXT PRIMARY KEY,kind TEXT,status TEXT,installed_screen TEXT,installed_at TEXT,package_id TEXT);
CREATE TABLE IF NOT EXISTS part_movement(id INTEGER PRIMARY KEY AUTOINCREMENT,serial TEXT,movement TEXT,screen_id TEXT,other_serial TEXT,at_ TEXT,package_id TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS dispatch_plan(id TEXT PRIMARY KEY,route TEXT,locked_at TEXT,closed_at TEXT,status TEXT,package_id TEXT,commitments TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS plan_step(id TEXT PRIMARY KEY,plan_id TEXT,screen_id TEXT,action TEXT,part_serial TEXT,requires_world TEXT,technician TEXT,permit_id TEXT,scheduled_at TEXT,travel_from_screen TEXT,travel_minutes INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS part_reservation(part_serial TEXT,plan_id TEXT,screen_id TEXT,package_id TEXT,PRIMARY KEY(part_serial,plan_id));
CREATE TABLE IF NOT EXISTS repair_order(id TEXT PRIMARY KEY,plan_id TEXT,screen_id TEXT,executed_at TEXT,package_id TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS reverify(id INTEGER PRIMARY KEY AUTOINCREMENT,screen_id TEXT,check_kind TEXT,result TEXT,package_id TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS screen_recommission(screen_id TEXT PRIMARY KEY,outage_id TEXT,power_artifact TEXT,display_artifact TEXT,reopened_at TEXT,sell_from TEXT,status TEXT,note TEXT,package_id TEXT,source_id INTEGER);
'''
REPAIR_TABLES=('capture_package','artifact','fault_hypothesis','screen_outage','spare_part','part_movement',
               'dispatch_plan','plan_step','part_reservation','repair_order','reverify','screen_recommission')

@contextmanager
def connect(path):
    dest=Path(path);dest.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(dest);db.row_factory=sqlite3.Row
    try:db.executescript(SCHEMA);yield db
    finally:db.close()

def at(value):
    if value is None:return None
    if not isinstance(value,str) or not value.endswith('Z'):raise ValueError('utc_required')
    datetime.fromisoformat(value[:-1]+'+00:00');return value

def parse(value):
    return datetime.fromisoformat(value[:-1]+'+00:00')

def fmt(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def plus(value,seconds):
    return fmt(parse(value)+timedelta(seconds=seconds))

def source(db,kind,file):
    raw=Path(file).read_bytes();digest=hashlib.sha256(raw).hexdigest();doc=json.loads(raw)
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
    tables=('creative','license','screen','maintenance','booking','proof','obligation','makegood')+REPAIR_TABLES
    return {t:[dict(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1')]
            for t in tables}
