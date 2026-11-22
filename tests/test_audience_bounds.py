import json
from pathlib import Path

import pytest

from adflight import audience, creative, schedule
from adflight.report import report, audit

FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def write(tmp, name, data):
    p = tmp / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return p


def book_b1(db):
    creative.register(db, FIX / 'creative-ok.json')
    creative.license(db, FIX / 'license-ok.json')
    schedule.screen(db, FIX / 'screen-ok.json')
    assert schedule.book(db, FIX / 'booking-ok.json')['status'] == 'confirmed'


def import_evidence(db, *, gate_counts='gate-count-ok.json', calibration='calibration-ok.json',
                    east='probe-east-ok.json', west='probe-west-ok.json'):
    audience.import_geometry(db, FIX / 'geometry-ok.json')
    audience.import_calibration(db, FIX / calibration)
    audience.import_gate_counts(db, FIX / gate_counts)
    audience.import_probe(db, FIX / east)
    audience.import_probe(db, FIX / west)


def load_frames(db, booking_id='B1'):
    from adflight.store import connect
    with connect(db) as rdb:
        return [dict(r) for r in rdb.execute(
            'SELECT * FROM probe_frame WHERE booking_id=? ORDER BY observed_at,id', (booking_id,))]


# --------------------------------------------------------------------------- #
# Same capture bytes, two complete worlds W2/W3, tight bound [2,3]
# --------------------------------------------------------------------------- #
def test_two_feasible_worlds_and_exact_bounds(tmp_path):
    db = tmp_path / 'a.sqlite'
    book_b1(db)
    import_evidence(db)
    result = audience.bounds(db, 'B1')

    assert result['bound'] == [2, 3]
    assert result['min_persons'] == 2 and result['max_persons'] == 3
    assert result['feasible_world_sizes'] == [2, 3]
    assert result['gap'] is False
    assert result['gap_reasons'] == []

    min_world = [w for w in result['witnesses'] if w['kind'] == 'min']
    max_world = [w for w in result['witnesses'] if w['kind'] == 'max']
    # W2: rot-11(F1) and rot-13(F3) are one rotating-code re-passage at the east
    # entry gate; rot-12(F2) is a second visitor exiting the west gate.
    w2 = next(p for p in min_world if p['frame_ids'] == ['F1', 'F3'])
    assert w2['gate_path'] == ['gate-east', 'gate-east']
    assert next(p for p in min_world if p['frame_ids'] == ['F2'])['gate_path'] == ['gate-west']
    assert sum(len(p['frame_ids']) for p in min_world) == 3
    # W3: three distinct visitors, one passage each.
    assert sorted(p['frame_ids'] for p in max_world) == [['F1'], ['F2'], ['F3']]
    assert len(max_world) == 3

    # The one-person explanation violates the 3-minute cross-gate walk floor.
    assert result['feasible_world_sizes'][0] >= 2


def test_worlds_generated_from_identical_capture_bytes(tmp_path):
    db_a = tmp_path / 'a.sqlite'
    db_b = tmp_path / 'b.sqlite'
    for db in (db_a, db_b):
        book_b1(db)
        import_evidence(db)
    ra, rb = audience.bounds(db_a, 'B1'), audience.bounds(db_b, 'B1')
    raw_a = (FIX / 'probe-east-ok.json').read_bytes(), (FIX / 'probe-west-ok.json').read_bytes()
    raw_b = (FIX / 'probe-east-ok.json').read_bytes(), (FIX / 'probe-west-ok.json').read_bytes()
    assert raw_a == raw_b
    assert ra['witnesses'] == rb['witnesses']


def test_conventional_pipelines_are_refuted_by_the_two_worlds(tmp_path):
    db = tmp_path / 'a.sqlite'
    book_b1(db)
    import_evidence(db)
    result = audience.bounds(db, 'B1')
    frames = load_frames(db)

    # Token dedup reports 3: it cannot express W2, so it over-counts that world.
    assert audience.naive_token_count(frames) == 3
    # Forced same-gate local merge reports 2: it cannot express W3, so it
    # under-counts that world (the task's 4-minute local merge window).
    assert audience.naive_same_gate_merge(frames) == 2
    # Neither single value survives: both sizes [2,3] are genuinely feasible.
    assert result['bound'] == [2, 3]

    from adflight.store import connect
    with connect(db) as rdb:
        calib = {r['sensor_id']: dict(r) for r in rdb.execute('SELECT * FROM sensor_calibration')}
        walk = {(r['from_gate'], r['to_gate']): r['min_seconds']
                for r in rdb.execute('SELECT * FROM walk_time')}
    # The per-pair if-rule with per-frame extreme picking also collapses to a
    # single number that no shared-bias assignment can certify here.
    assert audience.naive_pairwise_extremes(frames, calib, walk) in (1, 2, 3)


def test_pairwise_if_rule_cannot_replace_global_joint_solver(tmp_path):
    # Four passages where every *individual* cross-gate pair looks linkable when
    # each pair may pick its own clock extremes, but the two links together
    # demand contradictory shared biases; only the 3/4-person worlds survive.
    db = tmp_path / 'joint.sqlite'
    book_b1(db)
    calib = write(tmp_path, 'calib.json', {
        'sensors': [
            {'sensor_id': 'gate-east', 'group_id': 'clock-east',
             'bias_min_seconds': -60, 'bias_max_seconds': 60},
            {'sensor_id': 'gate-west', 'group_id': 'clock-west',
             'bias_min_seconds': -60, 'bias_max_seconds': 60}],
        'offline': []})
    geometry = write(tmp_path, 'geom.json', {
        'venue_id': 'V1',
        'gates': [{'gate_id': 'gate-east', 'direction': 'both', 'in_view': True},
                  {'gate_id': 'gate-west', 'direction': 'exit', 'in_view': True}],
        'walk_times': [{'from_gate': 'gate-east', 'to_gate': 'gate-west', 'min_seconds': 180},
                       {'from_gate': 'gate-west', 'to_gate': 'gate-east', 'min_seconds': 180}]})
    counts = write(tmp_path, 'counts.json', {
        'venue_id': 'V1',
        'pages': [
            {'gate_id': 'gate-east', 'page_no': 1, 'total_pages': 1, 'final': True,
             'window_start': '2026-06-10T10:00:00Z', 'window_end': '2026-06-10T10:10:00Z',
             'passages': 2},
            {'gate_id': 'gate-west', 'page_no': 1, 'total_pages': 1, 'final': True,
             'window_start': '2026-06-10T10:00:00Z', 'window_end': '2026-06-10T10:10:00Z',
             'passages': 2}]})
    east = write(tmp_path, 'east.json', {
        'booking_id': 'B1', 'sensor_id': 'gate-east',
        'page': {'page_no': 1, 'total_pages': 1, 'final': True},
        'frames': [{'id': 'F1', 'sensor_id': 'gate-east', 'observed_at': '2026-06-10T10:01:00Z', 'token': 't1'},
                   {'id': 'F3', 'sensor_id': 'gate-east', 'observed_at': '2026-06-10T10:03:30Z', 'token': 't3'}]})
    west = write(tmp_path, 'west.json', {
        'booking_id': 'B1', 'sensor_id': 'gate-west',
        'page': {'page_no': 1, 'total_pages': 1, 'final': True},
        'frames': [{'id': 'F2', 'sensor_id': 'gate-west', 'observed_at': '2026-06-10T10:02:30Z', 'token': 't2'},
                   {'id': 'F4', 'sensor_id': 'gate-west', 'observed_at': '2026-06-10T10:05:00Z', 'token': 't4'}]})
    audience.import_geometry(db, geometry)
    audience.import_calibration(db, calib)
    audience.import_gate_counts(db, counts)
    audience.import_probe(db, east)
    audience.import_probe(db, west)

    from adflight.store import connect
    with connect(db) as rdb:
        cal = {r['sensor_id']: dict(r) for r in rdb.execute('SELECT * FROM sensor_calibration')}
        wlk = {(r['from_gate'], r['to_gate']): r['min_seconds']
               for r in rdb.execute('SELECT * FROM walk_time')}
    frames = load_frames(db)
    # Greedy pairwise rule happily chains all four into fewer visitors.
    greedy = audience.naive_pairwise_extremes(frames, cal, wlk)
    result = audience.bounds(db, 'B1')
    # The joint solver keeps the lower bound above the greedy claim: one shared
    # bias per clock cannot satisfy both cross-gate links simultaneously.
    assert result['min_persons'] > greedy


# --------------------------------------------------------------------------- #
# Shared calibration bias is one variable per clock group
# --------------------------------------------------------------------------- #
def test_shared_clock_bias_cannot_pick_per_frame_extremes(tmp_path):
    db = tmp_path / 'shared.sqlite'
    book_b1(db)
    import_evidence(db, calibration='calibration-shared-bias.json')
    result = audience.bounds(db, 'B1')
    # Both probes share one bias variable in [-30,30]; no value makes the
    # 2-minute cross-gate reading gaps reach the 3-minute walk floor.
    assert result['bound'] == [2, 3]
    assert result['min_persons'] == 2  # same-gate W2 merge needs no cross-gate link


# --------------------------------------------------------------------------- #
# Coverage watermark: unknown upper bound, never zero for unseen pedestrians
# --------------------------------------------------------------------------- #
def test_unclosed_probe_pagination_unknown_upper_bound(tmp_path):
    db = tmp_path / 'gap.sqlite'
    book_b1(db)
    import_evidence(db, east='probe-east-unclosed.json')
    result = audience.bounds(db, 'B1')
    assert result['max_persons'] is None
    assert result['min_persons'] == 2
    assert any(r.startswith('probe_pagination_unclosed:gate-east') for r in result['gap_reasons'])
    assert all(w['kind'] != 'max' for w in result['witnesses'])


def test_gate_count_missing_page_unknown_upper_bound(tmp_path):
    db = tmp_path / 'gap2.sqlite'
    book_b1(db)
    import_evidence(db, gate_counts='gate-count-missing-page.json')
    result = audience.bounds(db, 'B1')
    assert result['max_persons'] is None
    assert any(r.startswith('gate_count_missing_page:gate-west') for r in result['gap_reasons'])
    assert result['min_persons'] == 2


def test_device_offline_unknown_upper_bound(tmp_path):
    db = tmp_path / 'gap3.sqlite'
    book_b1(db)
    import_evidence(db, calibration='calibration-offline.json')
    result = audience.bounds(db, 'B1')
    assert result['max_persons'] is None
    assert any(r.startswith('device_offline:gate-west') for r in result['gap_reasons'])


def test_extra_gate_passages_widen_known_upper_bound(tmp_path):
    db = tmp_path / 'extra.sqlite'
    book_b1(db)
    counts = write(tmp_path, 'counts.json', {
        'venue_id': 'V1',
        'pages': [
            {'gate_id': 'gate-east', 'page_no': 1, 'total_pages': 1, 'final': True,
             'window_start': '2026-06-10T10:00:00Z', 'window_end': '2026-06-10T10:10:00Z',
             'passages': 4},
            {'gate_id': 'gate-west', 'page_no': 1, 'total_pages': 1, 'final': True,
             'window_start': '2026-06-10T10:00:00Z', 'window_end': '2026-06-10T10:10:00Z',
             'passages': 1}]})
    import_evidence(db, gate_counts=counts)
    result = audience.bounds(db, 'B1')
    # Two extra mechanical passages without device frames can be two more people.
    assert result['min_persons'] == 2
    assert result['max_persons'] == 5
    assert result['unattributed_gate_passages'] == 2


def test_frames_exceeding_gate_count_are_rejected(tmp_path):
    db = tmp_path / 'impossible.sqlite'
    book_b1(db)
    counts = write(tmp_path, 'counts.json', {
        'venue_id': 'V1',
        'pages': [
            {'gate_id': 'gate-east', 'page_no': 1, 'total_pages': 1, 'final': True,
             'window_start': '2026-06-10T10:00:00Z', 'window_end': '2026-06-10T10:10:00Z',
             'passages': 1},
            {'gate_id': 'gate-west', 'page_no': 1, 'total_pages': 1, 'final': True,
             'window_start': '2026-06-10T10:00:00Z', 'window_end': '2026-06-10T10:10:00Z',
             'passages': 1}]})
    import_evidence(db, gate_counts=counts)
    with pytest.raises(ValueError, match='frame_count_exceeds_gate_count'):
        audience.bounds(db, 'B1')


def test_duplicate_frames_never_add_people(tmp_path):
    db = tmp_path / 'dup.sqlite'
    book_b1(db)
    audience.import_geometry(db, FIX / 'geometry-ok.json')
    audience.import_calibration(db, FIX / 'calibration-ok.json')
    counts = write(tmp_path, 'counts.json', {
        'venue_id': 'V1',
        'pages': [
            {'gate_id': 'gate-east', 'page_no': 1, 'total_pages': 1, 'final': True,
             'window_start': '2026-06-10T10:00:00Z', 'window_end': '2026-06-10T10:10:00Z',
             'passages': 2},
            {'gate_id': 'gate-west', 'page_no': 1, 'total_pages': 1, 'final': True,
             'window_start': '2026-06-10T10:00:00Z', 'window_end': '2026-06-10T10:10:00Z',
             'passages': 1}]})
    audience.import_gate_counts(db, counts)
    east = write(tmp_path, 'east.json', {
        'booking_id': 'B1', 'sensor_id': 'gate-east',
        'page': {'page_no': 1, 'total_pages': 1, 'final': True},
        'frames': [
            {'id': 'F1', 'sensor_id': 'gate-east', 'observed_at': '2026-06-10T10:01:00Z', 'token': 'rot-11'},
            {'id': 'F1b', 'sensor_id': 'gate-east', 'observed_at': '2026-06-10T10:01:05Z', 'token': 'rot-11'},
            {'id': 'F3', 'sensor_id': 'gate-east', 'observed_at': '2026-06-10T10:05:00Z', 'token': 'rot-13'}]})
    west = write(tmp_path, 'west.json', {
        'booking_id': 'B1', 'sensor_id': 'gate-west',
        'page': {'page_no': 1, 'total_pages': 1, 'final': True},
        'frames': [{'id': 'F2', 'sensor_id': 'gate-west', 'observed_at': '2026-06-10T10:03:00Z', 'token': 'rot-12'}]})
    audience.import_probe(db, east)
    audience.import_probe(db, west)
    result = audience.bounds(db, 'B1')
    # Same rotating token twice is one device-carrying passage; bound unchanged.
    assert result['bound'] == [2, 3]
    min_world = [w for w in result['witnesses'] if w['kind'] == 'min']
    assert sorted(len(p['frame_ids']) for p in min_world) == [1, 3]


# --------------------------------------------------------------------------- #
# Two pre-signed pricing routes over the same uncertain evidence
# --------------------------------------------------------------------------- #
def _signed_contracts(db):
    audience.import_contract(db, FIX / 'contract-guaranteed-ok.json')
    audience.import_contract(db, FIX / 'contract-opportunity-ok.json')


def test_two_contract_routes_same_bounds_different_payments(tmp_path):
    db = tmp_path / 'c.sqlite'
    book_b1(db)
    import_evidence(db)
    _signed_contracts(db)
    result = audience.bounds(db, 'B1')
    by_route = {s['contract_id']: s for s in result['settlements']}

    guaranteed = by_route['C-G1']
    assert guaranteed['paid_cny'] == 0
    assert guaranteed['status'] == 'not_guaranteed'
    assert '2–3' in guaranteed['note'] and '未保证达量' in guaranteed['note']

    opportunity = by_route['C-O1']
    assert opportunity['paid_cny'] == 300
    assert opportunity['status'] == 'possible_only'
    assert '可能' in opportunity['note']
    # It must never masquerade as a confirmed three.
    assert '非确认3人' in opportunity['note']


def test_opportunity_route_with_unknown_upper_bound_does_not_bill(tmp_path):
    db = tmp_path / 'c2.sqlite'
    book_b1(db)
    import_evidence(db, east='probe-east-unclosed.json')
    _signed_contracts(db)
    result = audience.bounds(db, 'B1')
    by_route = {s['contract_id']: s for s in result['settlements']}
    assert by_route['C-O1']['paid_cny'] == 0
    assert by_route['C-O1']['status'] == 'upper_bound_unknown'


def test_contract_cannot_be_signed_after_capture(tmp_path):
    db = tmp_path / 'late.sqlite'
    book_b1(db)
    import_evidence(db)
    late = write(tmp_path, 'late.json', {
        'id': 'C-LATE', 'booking_id': 'B1', 'route': 'guaranteed_reach',
        'threshold': 3, 'bonus_cny': 300, 'signed_at': '2026-06-10T10:02:00Z'})
    with pytest.raises(ValueError, match='contract_signed_after_capture'):
        audience.import_contract(db, late)


# --------------------------------------------------------------------------- #
# Opt-in bridge: new version, corrections, immutable history
# --------------------------------------------------------------------------- #
def test_bridge_tightens_bounds_and_corrects_but_keeps_history(tmp_path):
    db = tmp_path / 'br.sqlite'
    book_b1(db)
    import_evidence(db)
    _signed_contracts(db)
    first = audience.bounds(db, 'B1')
    assert first['version'] == 1
    v1_settlements = {s['contract_id']: dict(s) for s in first['settlements']}

    audience.import_bridge(db, FIX / 'bridge-ok.json')  # F1 == F3, excludes W3
    second = audience.bounds(db, 'B1')
    assert second['version'] == 2
    assert second['bound'] == [2, 2]

    from adflight.store import connect
    with connect(db) as rdb:
        versions = [dict(r) for r in rdb.execute(
            'SELECT version,min_persons,max_persons FROM reach_bound ORDER BY version')]
        settlements = [dict(r) for r in rdb.execute(
            'SELECT * FROM settlement ORDER BY id')]
        corrections = [dict(r) for r in rdb.execute('SELECT * FROM correction ORDER BY id')]
    assert versions == [
        {'version': 1, 'min_persons': 2, 'max_persons': 3},
        {'version': 2, 'min_persons': 2, 'max_persons': 2}]
    # Historical v1 rows remain on the ledger, untouched.
    v1 = [s for s in settlements if s['version'] == 1]
    assert v1_settlements['C-G1']['paid_cny'] == next(
        s['paid_cny'] for s in v1 if s['contract_id'] == 'C-G1')
    assert v1_settlements['C-O1']['paid_cny'] == next(
        s['paid_cny'] for s in v1 if s['contract_id'] == 'C-O1') == 300
    # The opportunity bonus already paid out is retained under the old basis.
    assert corrections and all('historical_payment_retained' in c['reason'] for c in corrections)


def test_bridge_excluding_w2_pushes_to_three(tmp_path):
    db = tmp_path / 'br2.sqlite'
    book_b1(db)
    import_evidence(db)
    bridge = write(tmp_path, 'bridge-different.json', {
        'id': 'BR2', 'booking_id': 'B1',
        'links': [{'frame_a': 'F1', 'frame_b': 'F3', 'relation': 'different'}]})
    audience.import_bridge(db, bridge)
    result = audience.bounds(db, 'B1')
    assert result['bound'] == [3, 3]
    min_world = [w for w in result['witnesses'] if w['kind'] == 'min']
    assert len(min_world) == 3


# --------------------------------------------------------------------------- #
# Upstream booking eligibility still gates the downstream capture
# --------------------------------------------------------------------------- #
def test_one_way_gate_direction_rules_out_repassage(tmp_path):
    db = tmp_path / 'oneway.sqlite'
    book_b1(db)
    geometry = write(tmp_path, 'geom-oneway.json', {
        'venue_id': 'V1',
        'gates': [{'gate_id': 'gate-east', 'direction': 'entry', 'in_view': True},
                  {'gate_id': 'gate-west', 'direction': 'exit', 'in_view': True}],
        'walk_times': [{'from_gate': 'gate-east', 'to_gate': 'gate-west', 'min_seconds': 180},
                       {'from_gate': 'gate-west', 'to_gate': 'gate-east', 'min_seconds': 180}]})
    audience.import_geometry(db, geometry)
    audience.import_calibration(db, FIX / 'calibration-ok.json')
    audience.import_gate_counts(db, FIX / 'gate-count-ok.json')
    # A strictly one-way entry gate cannot be re-passed by one visitor, so the
    # W2 rotating-code re-passage world disappears and the bound collapses to 3.
    audience.import_probe(db, FIX / 'probe-east-ok.json')
    audience.import_probe(db, FIX / 'probe-west-ok.json')
    result = audience.bounds(db, 'B1')
    assert result['bound'] == [3, 3]
    assert result['feasible_world_sizes'] == [3]


def test_cancelled_booking_blocks_probe_capture(tmp_path):
    db_a = tmp_path / 'A.sqlite'
    book_b1(db_a)
    import_evidence(db_a)
    assert audience.bounds(db_a, 'B1')['bound'] == [2, 3]

    db_b = tmp_path / 'B.sqlite'
    book_b1(db_b)
    schedule.cancel(db_b, write(tmp_path, 'cancel.json', {'booking_id': 'B1'}))
    with pytest.raises(ValueError, match='booking_not_confirmed'):
        audience.import_probe(db_b, FIX / 'probe-east-ok.json')
    from adflight.store import connect
    with connect(db_b) as rdb:
        assert rdb.execute('SELECT COUNT(*) AS c FROM probe_frame').fetchone()['c'] == 0


def test_original_audience_import_still_guarded_by_booking(tmp_path):
    db = tmp_path / 'orig.sqlite'
    book_b1(db)
    assert audience.ingest(db, FIX / 'audience-ok.json')['device_reach'] == 3
    schedule.cancel(db, write(tmp_path, 'cancel.json', {'booking_id': 'B1'}))
    db2 = tmp_path / 'orig2.sqlite'
    book_b1(db2)
    schedule.cancel(db2, write(tmp_path, 'cancel2.json', {'booking_id': 'B1'}))
    with pytest.raises(ValueError, match='booking_not_confirmed'):
        audience.ingest(db2, FIX / 'audience-ok.json')


# --------------------------------------------------------------------------- #
# Replay, idempotency and privacy
# --------------------------------------------------------------------------- #
def test_full_ledger_audit_replays(tmp_path):
    db = tmp_path / 'audit.sqlite'
    book_b1(db)
    import_evidence(db)
    _signed_contracts(db)
    audience.bounds(db, 'B1')
    audience.import_bridge(db, FIX / 'bridge-ok.json')
    audience.bounds(db, 'B1')
    outcome = audit(db)
    assert outcome['replay_ok']
    # Re-importing the same evidence is idempotent.
    import_evidence(db)
    assert audit(db)['replay_ok']


def test_raw_tokens_never_leave_local_ledger(tmp_path):
    db = tmp_path / 'priv.sqlite'
    book_b1(db)
    import_evidence(db)
    audience.bounds(db, 'B1')
    blob = json.dumps(report(db), ensure_ascii=False)
    assert 'rot-11' not in blob and 'rot-12' not in blob and 'rot-13' not in blob
    result = audience.bounds(db, 'B1')
    assert json.dumps(result, ensure_ascii=False).count('rot-') == 0
    # Tokens still exist locally for replay and attribution.
    from adflight.store import connect
    with connect(db) as rdb:
        assert rdb.execute('SELECT COUNT(DISTINCT token) AS c FROM probe_frame').fetchone()['c'] == 3
