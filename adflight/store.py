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
CREATE TABLE IF NOT EXISTS auction(id TEXT PRIMARY KEY,objective TEXT,deadline TEXT,pages_total INTEGER,accepted_qualifications TEXT,status TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS auction_page(auction_id TEXT,page INTEGER,bid_count INTEGER,source_id INTEGER,PRIMARY KEY(auction_id,page));
CREATE TABLE IF NOT EXISTS bid(auction_id TEXT,id TEXT,version INTEGER,agent TEXT,submitted_at TEXT,withdrawn INTEGER,amount INTEGER,budget_cap INTEGER,qualification TEXT,public_welfare INTEGER,mode TEXT,source_id INTEGER,PRIMARY KEY(auction_id,id,version));
CREATE TABLE IF NOT EXISTS bid_slot(auction_id TEXT,bid_id TEXT,version INTEGER,idx INTEGER,screen_id TEXT,start_at TEXT,end_at TEXT,creative_id TEXT,revision INTEGER,priority INTEGER,source_id INTEGER,PRIMARY KEY(auction_id,bid_id,version,idx));
CREATE TABLE IF NOT EXISTS correction(id TEXT PRIMARY KEY,auction_id TEXT,bid_id TEXT,note TEXT,submitted_at TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS allocation(auction_id TEXT PRIMARY KEY,fingerprint TEXT,objective TEXT,total_amount INTEGER,winners TEXT,losers TEXT,source_id INTEGER);
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
    return {t:[dict(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1')]
            for t in ('creative','license','screen','maintenance','booking','proof','obligation','makegood',
                      'auction','auction_page','bid','bid_slot','correction','allocation')}
