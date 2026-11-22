"""Basic offline advertiser package publication and acknowledgement."""
import hashlib
import json
from pathlib import Path
from .report import report
from .store import connect


def publish(db_path, file, output_dir):
    request = json.loads(Path(file).read_text(encoding='utf8'))
    booking_id = request['booking_id']
    state = report(db_path)['state']
    booking = next((row for row in state['booking'] if row['id'] == booking_id), None)
    if not booking:
        raise ValueError('unknown_booking')
    data = {'package_id': request['id'], 'booking': booking,
            'proof': [row for row in state['proof'] if row['booking_id'] == booking_id],
            'obligation': [row for row in state['obligation'] if row['booking_id'] == booking_id]}
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf8')
    digest = hashlib.sha256(raw).hexdigest()
    target = Path(output_dir) / f"{request['id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as db, db:
        if db.execute('SELECT 1 FROM client_package WHERE id=?', (request['id'],)).fetchone():
            raise ValueError('package_id_conflict')
        target.write_bytes(raw)
        db.execute('INSERT INTO client_package VALUES(?,?,?,?,?,?)',
                   (request['id'], booking_id, request['recipient'], digest, str(target), 'published'))
    return {'id': request['id'], 'digest': digest, 'status': 'published'}


def acknowledge(db_path, file):
    receipt = json.loads(Path(file).read_text(encoding='utf8'))
    with connect(db_path) as db, db:
        package = db.execute('SELECT * FROM client_package WHERE id=?', (receipt['package_id'],)).fetchone()
        if not package:
            raise ValueError('unknown_package')
        status = ('accepted' if receipt['recipient'] == package['recipient']
                  and receipt['digest'] == package['digest'] else 'held')
        db.execute('INSERT INTO client_ack VALUES(?,?,?,?,?)',
                   (receipt['id'], receipt['package_id'], receipt['recipient'], receipt['digest'], status))
        if status == 'accepted':
            db.execute("UPDATE client_package SET status='accepted' WHERE id=?", (receipt['package_id'],))
    return {'id': receipt['id'], 'status': status}
