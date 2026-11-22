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
CREATE TABLE IF NOT EXISTS audience_sample(id TEXT PRIMARY KEY,booking_id TEXT NOT NULL,
  sensor_id TEXT NOT NULL,observed_at TEXT NOT NULL,token TEXT NOT NULL,source_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS probe_frame(id TEXT PRIMARY KEY,booking_id TEXT NOT NULL,
  sensor_id TEXT NOT NULL,observed_at TEXT NOT NULL,token TEXT NOT NULL,source_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS probe_page(source_id INTEGER NOT NULL,booking_id TEXT NOT NULL,
  sensor_id TEXT NOT NULL,page_no INTEGER NOT NULL,total_pages INTEGER NOT NULL,final INTEGER NOT NULL,
  PRIMARY KEY(source_id,sensor_id,page_no));
CREATE TABLE IF NOT EXISTS gate_count(venue_id TEXT NOT NULL,gate_id TEXT NOT NULL,page_no INTEGER NOT NULL,
  total_pages INTEGER NOT NULL,final INTEGER NOT NULL,window_start TEXT NOT NULL,window_end TEXT NOT NULL,
  passages INTEGER NOT NULL,source_id INTEGER NOT NULL,PRIMARY KEY(source_id,gate_id,page_no));
CREATE TABLE IF NOT EXISTS sensor_calibration(sensor_id TEXT PRIMARY KEY,group_id TEXT NOT NULL,
  bias_min_seconds INTEGER NOT NULL,bias_max_seconds INTEGER NOT NULL,source_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sensor_offline(sensor_id TEXT NOT NULL,start_at TEXT NOT NULL,end_at TEXT NOT NULL,source_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS venue_gate(venue_id TEXT NOT NULL,gate_id TEXT NOT NULL,direction TEXT NOT NULL,
  in_view INTEGER NOT NULL,PRIMARY KEY(venue_id,gate_id));
CREATE TABLE IF NOT EXISTS walk_time(venue_id TEXT NOT NULL,from_gate TEXT NOT NULL,to_gate TEXT NOT NULL,
  min_seconds INTEGER NOT NULL,PRIMARY KEY(venue_id,from_gate,to_gate));
CREATE TABLE IF NOT EXISTS person_bridge(id TEXT NOT NULL,booking_id TEXT NOT NULL,frame_a TEXT NOT NULL,
  frame_b TEXT NOT NULL,relation TEXT NOT NULL,source_id INTEGER NOT NULL,PRIMARY KEY(id,frame_a,frame_b));
CREATE TABLE IF NOT EXISTS reach_bound(id INTEGER PRIMARY KEY AUTOINCREMENT,booking_id TEXT NOT NULL,version INTEGER NOT NULL,
  min_persons INTEGER NOT NULL,max_persons INTEGER,gap INTEGER NOT NULL,gap_reasons TEXT NOT NULL,world_sizes TEXT NOT NULL,
  unattributed_passages INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS witness(id INTEGER PRIMARY KEY AUTOINCREMENT,booking_id TEXT NOT NULL,version INTEGER NOT NULL,
  kind TEXT NOT NULL,person_no INTEGER NOT NULL,frame_ids TEXT NOT NULL,gate_path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contract(id TEXT PRIMARY KEY,booking_id TEXT NOT NULL,route TEXT NOT NULL,
  threshold INTEGER NOT NULL,bonus_cny INTEGER NOT NULL,signed_at TEXT NOT NULL,source_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS settlement(id INTEGER PRIMARY KEY AUTOINCREMENT,contract_id TEXT NOT NULL,booking_id TEXT NOT NULL,
  version INTEGER NOT NULL,bound_min INTEGER NOT NULL,bound_max INTEGER,paid_cny INTEGER NOT NULL,status TEXT NOT NULL,note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS correction(id INTEGER PRIMARY KEY AUTOINCREMENT,booking_id TEXT NOT NULL,contract_id TEXT NOT NULL,
  version INTEGER NOT NULL,delta_cny INTEGER NOT NULL,reason TEXT NOT NULL,prior_settlement_id INTEGER,new_settlement_id INTEGER);
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
    raw={t:[dict(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1')]
         for t in ('creative','license','screen','maintenance','booking','proof','obligation','makegood',
                   'probe_page','gate_count','sensor_calibration','sensor_offline','venue_gate','walk_time',
                   'person_bridge','contract')}
    # Raw rotating tokens stay local: the exported state never carries probe_frame / audience_sample tokens.
    raw['probe_frame']=[{'id':r['id'],'booking_id':r['booking_id'],'sensor_id':r['sensor_id'],
                         'observed_at':r['observed_at'],'source_id':r['source_id']}
                        for r in db.execute('SELECT id,booking_id,sensor_id,observed_at,source_id FROM probe_frame ORDER BY 1')]
    # Derived rows are deterministic functions of the raw evidence; their
    # surrogate autoincrement ids are not part of the auditable content.
    raw['reach_bound']=[{k:r[k] for k in ('booking_id','version','min_persons','max_persons','gap',
                                          'gap_reasons','world_sizes','unattributed_passages')}
                        for r in db.execute('SELECT * FROM reach_bound ORDER BY id')]
    raw['witness']=[{k:r[k] for k in ('booking_id','version','kind','person_no','frame_ids','gate_path')}
                    for r in db.execute('SELECT * FROM witness ORDER BY id')]
    raw['settlement']=[{k:r[k] for k in ('contract_id','booking_id','version','bound_min','bound_max',
                                         'paid_cny','status','note')}
                       for r in db.execute('SELECT * FROM settlement ORDER BY id')]
    raw['correction']=[{k:r[k] for k in ('booking_id','contract_id','version','delta_cny','reason')}
                       for r in db.execute('SELECT * FROM correction ORDER BY id')]
    return raw
