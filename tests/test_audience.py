from pathlib import Path
from tempfile import TemporaryDirectory

from adflight import audience, creative, schedule
from adflight.report import report, audit


FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'


def test_basic_device_reach():
    with TemporaryDirectory() as directory:
        db = Path(directory) / 'ad.sqlite'
        creative.register(db, FIXTURES / 'creative-ok.json')
        creative.license(db, FIXTURES / 'license-ok.json')
        schedule.screen(db, FIXTURES / 'screen-ok.json')
        assert schedule.book(db, FIXTURES / 'booking-ok.json')['status'] == 'confirmed'
        assert audience.ingest(db, FIXTURES / 'audience-ok.json')['device_reach'] == 3
        assert report(db)['audience'] == [{'booking_id': 'B1', 'device_reach': 3}]
        assert audit(db)['replay_ok']
