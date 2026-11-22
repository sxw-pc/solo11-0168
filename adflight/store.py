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
CREATE TABLE IF NOT EXISTS meter(id TEXT PRIMARY KEY,kind TEXT,screen_id TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS reading(meter_id TEXT,ts TEXT,active_kw REAL,reactive_kvar REAL,source_id INTEGER,PRIMARY KEY(meter_id,ts));
CREATE TABLE IF NOT EXISTS calibration(id TEXT PRIMARY KEY,max_bias_pct REAL,valid_from TEXT,valid_to TEXT,issuer TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS curve(screen_id TEXT,level INTEGER,min_kw REAL,max_kw REAL,source_id INTEGER,PRIMARY KEY(screen_id,level));
CREATE TABLE IF NOT EXISTS brightness(id TEXT PRIMARY KEY,screen_id TEXT,level INTEGER,start_at TEXT,end_at TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS hvac(id TEXT PRIMARY KEY,rated_kw REAL,start_at TEXT,end_at TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS declared_load(id TEXT PRIMARY KEY,name TEXT,kw REAL,start_at TEXT,end_at TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS clock(id TEXT PRIMARY KEY,max_offset_ms INTEGER,issuer TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS ups(id TEXT PRIMARY KEY,ts TEXT,usable_kwh REAL,inverter_efficiency REAL,max_discharge_kw REAL,source_id INTEGER);
CREATE TABLE IF NOT EXISTS control(id TEXT PRIMARY KEY,kind TEXT,screen_id TEXT,level INTEGER,start_at TEXT,end_at TEXT,decided_at TEXT,limit_kw REAL,discharge_kw REAL,reserved_kwh REAL,status TEXT,basis TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS receipt(id TEXT PRIMARY KEY,control_id TEXT,screen_id TEXT,level INTEGER,start_at TEXT,end_at TEXT,source_id INTEGER);
'''
TABLES=('creative','license','screen','maintenance','booking','proof','obligation','makegood',
        'meter','reading','calibration','curve','brightness','hvac','declared_load','clock','ups','control','receipt')
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
    return {t:[dict(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1')] for t in TABLES}
