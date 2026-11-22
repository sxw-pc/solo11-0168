import json
from pathlib import Path

from adflight import creative, schedule, playback, publisher
from adflight.store import connect


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def test_publish_and_acknowledge(tmp_path):
    db = tmp_path / 'campaign.sqlite'
    creative.register(db, FIX / 'creative-ok.json')
    creative.license(db, FIX / 'license-ok.json')
    schedule.screen(db, FIX / 'screen-ok.json')
    schedule.book(db, FIX / 'booking-ok.json')
    playback.proof(db, FIX / 'proof-ok.json')
    result = publisher.publish(db, FIX / 'publish-ok.json', tmp_path / 'out')
    assert result['status'] == 'published'
    receipt = tmp_path / 'receipt.json'
    receipt.write_text(json.dumps({'id': 'ACK1', 'package_id': 'PK1',
                                   'recipient': 'advertiser-A', 'digest': result['digest']}))
    assert publisher.acknowledge(db, receipt)['status'] == 'accepted'
    with connect(db) as connection:
        assert connection.execute('SELECT status FROM client_package WHERE id=?', ('PK1',)).fetchone()[0] == 'accepted'
