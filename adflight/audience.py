"""Simple offline count of locally observed device tokens at an ad screen."""
from .store import connect, source, at


def ingest(path, file):
    with connect(path) as db, db:
        sid, doc, replay = source(db, 'audience', file)
        booking = db.execute('SELECT * FROM booking WHERE id=?', (doc['booking_id'],)).fetchone()
        if not booking or booking['status'] != 'confirmed':
            raise ValueError('booking_not_confirmed')
        for sample in doc['samples']:
            observed = at(sample['observed_at'])
            if observed < booking['start_at'] or observed >= booking['end_at']:
                raise ValueError('sample_outside_booking')
            old = db.execute('SELECT source_id FROM audience_sample WHERE id=?', (sample['id'],)).fetchone()
            if old:
                if old['source_id'] != sid:
                    raise ValueError('sample_id_conflict')
                continue
            db.execute('INSERT INTO audience_sample VALUES(?,?,?,?,?,?)',
                       (sample['id'], doc['booking_id'], sample['sensor_id'],
                        observed, sample['token'], sid))
        estimate = db.execute('SELECT COUNT(DISTINCT token) FROM audience_sample WHERE booking_id=?',
                              (doc['booking_id'],)).fetchone()[0]
        return {'booking_id': doc['booking_id'], 'device_reach': estimate, 'replayed': replay}
