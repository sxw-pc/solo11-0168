"""Anonymous footfall capture and provable visitor bounds.

Two layers live here:

1. ``ingest`` keeps the original local token ledger used by the baseline tests:
   a confirmed booking is required before rotating-token samples are stored, and
   the reported number is only ``COUNT(DISTINCT token)``.
2. The bounded-attribution layer treats probe frames, one-way gate counts, shared
   clock calibrations and venue geometry as raw evidence.  It enumerates every
   spatiotemporally feasible witness assignment (a complete world mapping each
   real gate passage to an anonymous visitor) and proves the tight minimum and
   maximum number of distinct visitors compatible with all evidence at once.

Raw rotating tokens never leave the local database: reports and witnesses only
carry frame ids, gates and timestamps.
"""
import json
from datetime import datetime

from .store import connect, source, at


# --------------------------------------------------------------------------- #
# Baseline token import (unchanged behaviour, kept replay-compatible)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Small time / union-find / partition helpers
# --------------------------------------------------------------------------- #
def epoch(value):
    return int(datetime.fromisoformat(value[:-1] + '+00:00').timestamp())


class _Union:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _partitions(k):
    """All set partitions of k labelled components as restricted-growth tuples."""
    if k == 0:
        return
    labels = [0] * k
    ceiling = [1] * k  # 1 + max label used to the left
    yield tuple(labels)
    while True:
        i = k - 1
        while i > 0 and labels[i] == ceiling[i]:
            i -= 1
        if i == 0:
            return
        labels[i] += 1
        for j in range(i + 1, k):
            labels[j] = 0
            ceiling[j] = max(ceiling[i], labels[i] + 1)
        yield tuple(labels)


# --------------------------------------------------------------------------- #
# Raw-evidence importers
# --------------------------------------------------------------------------- #
def import_probe(path, file):
    """Paginated local probe frames. Tokens are stored locally, never exported."""
    with connect(path) as db, db:
        sid, doc, replay = source(db, 'probe', file)
        booking = db.execute('SELECT * FROM booking WHERE id=?', (doc['booking_id'],)).fetchone()
        if not booking or booking['status'] != 'confirmed':
            raise ValueError('booking_not_confirmed')
        if db.execute('SELECT 1 FROM probe_page WHERE source_id=?', (sid,)).fetchone():
            return {'booking_id': doc['booking_id'], 'replayed': True}
        total_pages = int(doc['page']['total_pages'])
        page_no = int(doc['page']['page_no'])
        if not 1 <= page_no <= total_pages:
            raise ValueError('invalid_probe_page')
        for frame in doc['frames']:
            observed = at(frame['observed_at'])
            if observed < booking['start_at'] or observed >= booking['end_at']:
                raise ValueError('sample_outside_booking')
            old = db.execute('SELECT source_id FROM probe_frame WHERE id=?', (frame['id'],)).fetchone()
            if old:
                if old['source_id'] != sid:
                    raise ValueError('frame_id_conflict')
                continue
            db.execute('INSERT INTO probe_frame VALUES(?,?,?,?,?,?)',
                       (frame['id'], doc['booking_id'], frame['sensor_id'],
                        observed, frame['token'], sid))
        db.execute('INSERT INTO probe_page VALUES(?,?,?,?,?,?)',
                   (sid, doc['booking_id'], doc['sensor_id'], page_no, total_pages,
                    1 if doc['page'].get('final') else 0))
        return {'booking_id': doc['booking_id'], 'page_no': page_no,
                'frames': len(doc['frames']), 'replayed': replay}


def import_gate_counts(path, file):
    """One-way gate mechanical passage counts, paginated."""
    with connect(path) as db, db:
        sid, doc, replay = source(db, 'gate_count', file)
        if db.execute('SELECT 1 FROM gate_count WHERE source_id=?', (sid,)).fetchone():
            return {'replayed': True}
        for page in doc['pages']:
            total_pages = int(page['total_pages'])
            page_no = int(page['page_no'])
            if not 1 <= page_no <= total_pages:
                raise ValueError('invalid_gate_count_page')
            db.execute('INSERT INTO gate_count VALUES(?,?,?,?,?,?,?,?,?)',
                       (doc['venue_id'], page['gate_id'], page_no, total_pages,
                        1 if page.get('final') else 0, at(page['window_start']), at(page['window_end']),
                        int(page['passages']), sid))
        return {'venue_id': doc['venue_id'], 'pages': len(doc['pages']), 'replayed': replay}


def import_calibration(path, file):
    """Sensor clock calibration: one bias interval per sensor, grouped so that
    probes sharing a physical clock share one bias variable."""
    with connect(path) as db, db:
        sid, doc, replay = source(db, 'calibration', file)
        if db.execute('SELECT 1 FROM sensor_calibration WHERE source_id=?', (sid,)).fetchone():
            return {'replayed': True}
        for sensor in doc['sensors']:
            lo, hi = int(sensor['bias_min_seconds']), int(sensor['bias_max_seconds'])
            if lo > hi:
                raise ValueError('invalid_calibration_interval')
            old = db.execute('SELECT source_id FROM sensor_calibration WHERE sensor_id=?',
                             (sensor['sensor_id'],)).fetchone()
            if old:
                if old['source_id'] != sid:
                    raise ValueError('sensor_calibration_conflict')
                continue
            db.execute('INSERT INTO sensor_calibration VALUES(?,?,?,?,?)',
                       (sensor['sensor_id'], sensor['group_id'], lo, hi, sid))
        for gap in doc.get('offline', []):
            db.execute('INSERT INTO sensor_offline VALUES(?,?,?,?)',
                       (gap['sensor_id'], at(gap['start_at']), at(gap['end_at']), sid))
        return {'sensors': len(doc['sensors']), 'replayed': replay}


def import_geometry(path, file):
    """Venue geometry: one-way gates, visibility area and minimum walk times."""
    with connect(path) as db, db:
        sid, doc, replay = source(db, 'geometry', file)
        if db.execute('SELECT 1 FROM venue_gate WHERE venue_id=?', (doc['venue_id'],)).fetchone():
            return {'replayed': True}
        for gate in doc['gates']:
            if gate['direction'] not in ('entry', 'exit', 'both'):
                raise ValueError('invalid_gate_direction')
            db.execute('INSERT INTO venue_gate VALUES(?,?,?,?)',
                       (doc['venue_id'], gate['gate_id'], gate['direction'],
                        1 if gate.get('in_view', True) else 0))
        for walk in doc['walk_times']:
            if int(walk['min_seconds']) < 0:
                raise ValueError('invalid_walk_time')
            db.execute('INSERT INTO walk_time VALUES(?,?,?,?)',
                       (doc['venue_id'], walk['from_gate'], walk['to_gate'], int(walk['min_seconds'])))
        return {'venue_id': doc['venue_id'], 'gates': len(doc['gates']), 'replayed': replay}


def import_contract(path, file):
    """Pricing route signed before capture; the tariff is fixed before evidence."""
    with connect(path) as db, db:
        sid, doc, replay = source(db, 'contract', file)
        if db.execute('SELECT 1 FROM contract WHERE id=?', (doc['id'],)).fetchone():
            return {'id': doc['id'], 'replayed': True}
        if doc['route'] not in ('guaranteed_reach', 'opportunity_reach'):
            raise ValueError('invalid_contract_route')
        booking = db.execute('SELECT * FROM booking WHERE id=?', (doc['booking_id'],)).fetchone()
        if not booking:
            raise ValueError('unknown_booking')
        earliest = db.execute('SELECT MIN(observed_at) AS m FROM probe_frame WHERE booking_id=?',
                              (doc['booking_id'],)).fetchone()['m']
        signed_at = at(doc['signed_at'])
        if earliest and signed_at > earliest:
            raise ValueError('contract_signed_after_capture')
        db.execute('INSERT INTO contract VALUES(?,?,?,?,?,?,?)',
                   (doc['id'], doc['booking_id'], doc['route'], int(doc['threshold']),
                    int(doc['bonus_cny']), signed_at, sid))
        return {'id': doc['id'], 'route': doc['route'], 'replayed': replay}


def import_bridge(path, file):
    """Post-hoc verifiable opt-in person bridge. It never rewrites history:
    recomputed bounds and settlements get new versions plus correction rows."""
    with connect(path) as db, db:
        sid, doc, replay = source(db, 'bridge', file)
        if db.execute('SELECT 1 FROM person_bridge WHERE source_id=?', (sid,)).fetchone():
            return {'replayed': True}
        if not db.execute('SELECT 1 FROM booking WHERE id=?', (doc['booking_id'],)).fetchone():
            raise ValueError('unknown_booking')
        for link in doc['links']:
            if link['relation'] not in ('same', 'different'):
                raise ValueError('invalid_bridge_relation')
            for frame_id in (link['frame_a'], link['frame_b']):
                if not db.execute('SELECT 1 FROM probe_frame WHERE id=? AND booking_id=?',
                                  (frame_id, doc['booking_id'])).fetchone():
                    raise ValueError('bridge_unknown_frame')
            db.execute('INSERT INTO person_bridge VALUES(?,?,?,?,?,?)',
                       (doc['id'], doc['booking_id'], link['frame_a'], link['frame_b'],
                        link['relation'], sid))
        return {'id': doc['id'], 'links': len(doc['links']), 'replayed': replay}


# --------------------------------------------------------------------------- #
# Joint attribution: shared-bias difference constraints + world enumeration
# --------------------------------------------------------------------------- #
def _load_context(db, booking_id):
    booking = db.execute('SELECT * FROM booking WHERE id=?', (booking_id,)).fetchone()
    if not booking:
        raise ValueError('unknown_booking')
    frames = [dict(r) for r in db.execute(
        'SELECT * FROM probe_frame WHERE booking_id=? ORDER BY observed_at,id', (booking_id,))]
    if not frames:
        raise ValueError('no_probe_evidence')
    calib = {r['sensor_id']: dict(r) for r in db.execute('SELECT * FROM sensor_calibration')}
    for frame in frames:
        if frame['sensor_id'] not in calib:
            raise ValueError('sensor_calibration_missing')
    gates = {r['gate_id']: dict(r) for r in db.execute('SELECT * FROM venue_gate')}
    if not gates:
        raise ValueError('geometry_missing')
    walk = {(r['from_gate'], r['to_gate']): r['min_seconds']
            for r in db.execute('SELECT * FROM walk_time')}
    return booking, frames, calib, gates, walk


def _bias_system_feasible(blocks, frames_by_id, calib, walk, gate_dirs):
    """True time t_true = t_reading + b(group). One bias variable per calibration
    group; every link of every person adds a difference constraint into ONE
    shared system, so per-frame extreme picking is impossible. One-way gate
    direction rules out re-passage through a pure entry/exit gate."""
    for block in blocks:
        ordered = sorted(block, key=lambda fid: (epoch(frames_by_id[fid]['observed_at']), fid))
        for a, b in zip(ordered, ordered[1:]):
            ga = frames_by_id[a]['sensor_id']
            if gate_dirs.get(ga) in ('entry', 'exit'):
                return False  # a strictly one-way gate cannot be re-passed by one visitor
    groups = sorted({calib[frames_by_id[fid]['sensor_id']]['group_id']
                     for block in blocks for fid in block})
    index = {'REF': 0}
    for g in groups:
        index[g] = len(index)
    n = len(index)
    neg = float('-inf')
    d = [[neg] * n for _ in range(n)]
    for i in range(n):
        d[i][i] = 0
    for sensor, c in calib.items():
        g = index[c['group_id']]
        d[index['REF']][g] = max(d[index['REF']][g], c['bias_min_seconds'])      # b_g >= lo
        d[g][index['REF']] = max(d[g][index['REF']], -c['bias_max_seconds'])     # b_g <= hi
    for block in blocks:
        ordered = sorted(block, key=lambda fid: (epoch(frames_by_id[fid]['observed_at']), fid))
        for a, b in zip(ordered, ordered[1:]):
            fa, fb = frames_by_id[a], frames_by_id[b]
            ga, gb = fa['sensor_id'], fb['sensor_id']
            if ga == gb:
                continue  # re-passage at the same one-way gate needs no walk floor
            key = (ga, gb)
            if key not in walk:
                raise ValueError('walk_time_missing')
            reading_gap = epoch(fb['observed_at']) - epoch(fa['observed_at'])
            # reading_gap + (b_gb - b_ga) >= min_walk
            need = walk[key] - reading_gap
            u, v = index[calib[ga]['group_id']], index[calib[gb]['group_id']]
            d[u][v] = max(d[u][v], need)
    for k in range(n):
        for i in range(n):
            if d[i][k] == neg:
                continue
            for j in range(n):
                if d[k][j] == neg:
                    continue
                d[i][j] = max(d[i][j], d[i][k] + d[k][j])
    return all(d[i][i] <= 0 for i in range(n))


def _enumerate_worlds(frames, calib, walk, must_links, cannot_links, gate_dirs):
    """Every feasible complete witness assignment, with the person count each
    world implies. Must-link components (same rotating token / opt-in same) are
    atomic; cannot-link pairs can never share a person."""
    frames_by_id = {f['id']: f for f in frames}
    union = _Union([f['id'] for f in frames])
    for a, b in must_links:
        union.union(a, b)
    comp_of = {fid: union.find(fid) for fid in frames_by_id}
    components = sorted(set(comp_of.values()))
    comp_members = {c: sorted(fid for fid in frames_by_id if comp_of[fid] == c) for c in components}
    forbidden = set()
    for a, b in cannot_links:
        ca, cb = comp_of[a], comp_of[b]
        if ca == cb:
            return []  # directly contradictory evidence
        forbidden.add((min(ca, cb), max(ca, cb)))
    worlds = []
    for labels in _partitions(len(components)):
        blocks_map = {}
        for comp, label in zip(components, labels):
            blocks_map.setdefault(label, []).extend(comp_members[comp])
        pair_seen = set()
        for block in blocks_map.values():
            comps = sorted(set(comp_of[fid] for fid in block))
            for x in range(len(comps)):
                for y in range(x + 1, len(comps)):
                    pair = (min(comps[x], comps[y]), max(comps[x], comps[y]))
                    pair_seen.add(pair)
        if pair_seen & forbidden:
            continue
        blocks = [sorted(v) for v in blocks_map.values()]
        if _bias_system_feasible(blocks, frames_by_id, calib, walk, gate_dirs):
            worlds.append(blocks)
    worlds.sort(key=lambda blocks: (len(blocks), json.dumps(blocks)))
    return worlds


# --------------------------------------------------------------------------- #
# Coverage watermark
# --------------------------------------------------------------------------- #
def _coverage_gaps(db, booking, frames, gates):
    reasons = []
    # 1. probe pagination must be closed and contiguous for every gate sensor
    frame_sensors = sorted({f['sensor_id'] for f in frames})
    for sensor in sorted(set(gates) | set(frame_sensors)):
        pages = db.execute(
            'SELECT * FROM probe_page WHERE booking_id=? AND sensor_id=? ORDER BY page_no',
            (booking['id'], sensor)).fetchall()
        if not pages:
            reasons.append(f'probe_pagination_unclosed:{sensor}')
            continue
        total = pages[0]['total_pages']
        if any(p['total_pages'] != total for p in pages):
            reasons.append(f'probe_pagination_unclosed:{sensor}')
            continue
        if [p['page_no'] for p in pages] != list(range(1, total + 1)):
            reasons.append(f'probe_pagination_unclosed:{sensor}')
            continue
        if not pages[-1]['final']:
            reasons.append(f'probe_pagination_unclosed:{sensor}')
    # 2. device offline intervals overlapping the booking window
    for row in db.execute('SELECT * FROM sensor_offline ORDER BY sensor_id,start_at'):
        if row['start_at'] < booking['end_at'] and row['end_at'] > booking['start_at']:
            reasons.append(f'device_offline:{row["sensor_id"]}')
    # 3. gate count pages must be closed and contiguous for every one-way gate
    totals = {}
    for gate in sorted(gates):
        pages = db.execute(
            'SELECT * FROM gate_count WHERE gate_id=? ORDER BY page_no', (gate,)).fetchall()
        if not pages:
            reasons.append(f'gate_count_missing_page:{gate}')
            continue
        if any(p['window_start'] != booking['start_at'] or p['window_end'] != booking['end_at']
               for p in pages):
            reasons.append(f'gate_count_window_mismatch:{gate}')
            continue
        total = pages[0]['total_pages']
        if any(p['total_pages'] != total for p in pages) \
                or [p['page_no'] for p in pages] != list(range(1, total + 1)) \
                or not pages[-1]['final']:
            reasons.append(f'gate_count_missing_page:{gate}')
            continue
        totals[gate] = sum(p['passages'] for p in pages)
    return reasons, totals


def _bounds_for_evidence(db, booking_id, bridge_source_limit=0):
    """Recompute bounds using bridge rows whose source_id <= limit.
    Limit 0 = raw evidence only (version 1)."""
    booking, frames, calib, gates, walk = _load_context(db, booking_id)
    gate_dirs = {gate: meta['direction'] for gate, meta in gates.items()}
    must = [(f['id'], n['id']) for f in frames for n in frames
            if f['id'] < n['id'] and f['token'] == n['token']]
    cannot = []
    bridge_rows = db.execute('SELECT * FROM person_bridge WHERE booking_id=? ORDER BY id',
                             (booking_id,)).fetchall()
    for row in bridge_rows:
        if row['source_id'] > bridge_source_limit:
            continue
        (must if row['relation'] == 'same' else cannot).append((row['frame_a'], row['frame_b']))
    worlds = _enumerate_worlds(frames, calib, walk, must, cannot, gate_dirs)
    if not worlds:
        raise ValueError('no_feasible_world')
    min_world = min(worlds, key=len)
    max_world = max(worlds, key=len)
    feasible_sizes = sorted({len(w) for w in worlds})
    gaps, count_totals = _coverage_gaps(db, booking, frames, gates)
    # distinct device-carrying passages per gate (repeated frames never add people)
    distinct_by_gate = {}
    for f in frames:
        distinct_by_gate.setdefault(f['sensor_id'], set()).add(f['token'])
    extra = 0
    for gate, total in count_totals.items():
        seen = len(distinct_by_gate.get(gate, set()))
        if seen > total:
            raise ValueError(f'frame_count_exceeds_gate_count:{gate}')
        extra += max(0, total - seen)
    min_persons = len(min_world)
    max_persons = None if gaps else len(max_world) + extra
    return {
        'booking_id': booking_id,
        'version': None,
        'min_persons': min_persons,
        'max_persons': max_persons,
        'gap': bool(gaps),
        'gap_reasons': gaps,
        'feasible_world_sizes': feasible_sizes,
        'min_world': _witness_view(min_world, frames),
        'max_world': None if gaps else _witness_view(max_world, frames),
        'unattributed_gate_passages': extra,
    }


def _witness_view(blocks, frames):
    by_id = {f['id']: f for f in frames}
    view = []
    for person_no, block in enumerate(sorted(blocks, key=lambda b: (epoch(by_id[b[0]]['observed_at']), b[0])), 1):
        ordered = sorted(block, key=lambda fid: (epoch(by_id[fid]['observed_at']), fid))
        view.append({
            'person': person_no,
            'frame_ids': ordered,
            'gate_path': [by_id[fid]['sensor_id'] for fid in ordered],
        })
    return view


# --------------------------------------------------------------------------- #
# Deterministic recomputation of every derived row (audit replay support)
# --------------------------------------------------------------------------- #
def sync_derived(db, booking_id=None):
    booking_ids = [r['booking_id'] for r in db.execute(
        'SELECT DISTINCT booking_id FROM probe_frame ORDER BY booking_id')]
    if booking_id is not None:
        booking_ids = [b for b in booking_ids if b == booking_id]
    for bid in booking_ids:
        for table in ('reach_bound', 'witness', 'settlement', 'correction'):
            db.execute(f'DELETE FROM {table} WHERE booking_id=?', (bid,))
        bridge_sids = [r['source_id'] for r in db.execute(
            'SELECT DISTINCT source_id FROM person_bridge WHERE booking_id=? ORDER BY source_id', (bid,))]
        versions = [0] + bridge_sids  # v1 limit 0 = raw evidence only
        contracts = [dict(r) for r in db.execute(
            'SELECT * FROM contract WHERE booking_id=? ORDER BY id', (bid,))]
        prior_settlement = {}
        for version_no, limit in enumerate(versions, 1):
            result = _bounds_for_evidence(db, bid, bridge_source_limit=limit)
            db.execute(
                'INSERT INTO reach_bound(booking_id,version,min_persons,max_persons,gap,gap_reasons,'
                'world_sizes,unattributed_passages) VALUES(?,?,?,?,?,?,?,?)',
                (bid, version_no, result['min_persons'], result['max_persons'],
                 1 if result['gap'] else 0, json.dumps(result['gap_reasons']),
                 json.dumps(result['feasible_world_sizes']), result['unattributed_gate_passages']))
            for kind, world in (('min', result['min_world']),
                                ('max', result['max_world'] or [])):
                for person in world:
                    db.execute('INSERT INTO witness(booking_id,version,kind,person_no,frame_ids,gate_path) '
                               'VALUES(?,?,?,?,?,?)',
                               (bid, version_no, kind, person['person'],
                                json.dumps(person['frame_ids']), json.dumps(person['gate_path'])))
            for contract in contracts:
                paid, status, note = _settle(contract, result)
                settlement_id = db.execute(
                    'INSERT INTO settlement(contract_id,booking_id,version,bound_min,bound_max,'
                    'paid_cny,status,note) VALUES(?,?,?,?,?,?,?,?)',
                    (contract['id'], bid, version_no, result['min_persons'], result['max_persons'],
                     paid, status, note)).lastrowid
                if contract['id'] in prior_settlement:
                    old_id, old_paid = prior_settlement[contract['id']]
                    if old_paid != paid:
                        reason = (f'bridge_evidence_version_{version_no}:paid_{old_paid}->'
                                  f'{paid};historical_payment_retained')
                        db.execute('INSERT INTO correction(booking_id,contract_id,version,delta_cny,'
                                   'reason,prior_settlement_id,new_settlement_id) VALUES(?,?,?,?,?,?,?)',
                                   (bid, contract['id'], version_no, paid - old_paid, reason,
                                    old_id, settlement_id))
                prior_settlement[contract['id']] = (settlement_id, paid)


def _bound_text(result):
    if result['max_persons'] is None:
        return f"{result['min_persons']}–未知"
    return f"{result['min_persons']}–{result['max_persons']}"


def _settle(contract, result):
    lo, hi, threshold, bonus = (result['min_persons'], result['max_persons'],
                                contract['threshold'], contract['bonus_cny'])
    bound_text = _bound_text(result)
    if contract['route'] == 'guaranteed_reach':
        if lo >= threshold:
            return bonus, 'guaranteed_paid', f'{bound_text}，已保证达量{threshold}人，支付{bonus}元'
        return 0, 'not_guaranteed', f'{bound_text}，未保证达量（门槛{threshold}人），不付{bonus}元达量奖金'
    # opportunity route settles on the feasible upper bound only
    if hi is None:
        return 0, 'upper_bound_unknown', f'{bound_text}，上界未知，不得按未见行人计费，暂不支付'
    if hi >= threshold:
        return bonus, 'possible_only', (f'{bound_text}，仅可能触达达量（门槛{threshold}人），'
                                        f'按可行上界支付{bonus}元，非确认{threshold}人')
    return 0, 'below_potential', f'{bound_text}，可行上界未达门槛{threshold}人，不支付'


def bounds(path, booking_id):
    """Public provable bounds for one booking window, with both extreme worlds."""
    with connect(path) as db, db:
        sync_derived(db, booking_id)
        row = db.execute('SELECT * FROM reach_bound WHERE booking_id=? ORDER BY version DESC LIMIT 1',
                         (booking_id,)).fetchone()
        witnesses = [dict(r) for r in db.execute(
            'SELECT * FROM witness WHERE booking_id=? AND version=? ORDER BY kind,person_no',
            (booking_id, row['version']))]
        contracts = [dict(r) for r in db.execute(
            'SELECT * FROM settlement WHERE booking_id=? AND version=? ORDER BY contract_id',
            (booking_id, row['version']))]
        return {
            'booking_id': booking_id,
            'version': row['version'],
            'min_persons': row['min_persons'],
            'max_persons': row['max_persons'],
            'bound': [row['min_persons'], row['max_persons']],
            'gap': bool(row['gap']),
            'gap_reasons': json.loads(row['gap_reasons']),
            'feasible_world_sizes': json.loads(row['world_sizes']),
            'unattributed_gate_passages': row['unattributed_passages'],
            'witnesses': [{k: (json.loads(r[k]) if k in ('frame_ids', 'gate_path') else r[k])
                           for k in ('kind', 'person_no', 'frame_ids', 'gate_path')} for r in witnesses],
            'settlements': contracts,
        }


# --------------------------------------------------------------------------- #
# Conventional per-token baselines, retained only to demonstrate why they fail
# --------------------------------------------------------------------------- #
def naive_token_count(frames):
    """Strongest conventional pipeline up to token dedup: distinct tokens."""
    return len({f['token'] for f in frames})


def naive_same_gate_merge(frames, within_seconds=240):
    """Conventional local merge: collapse same-gate records inside a window."""
    remaining = sorted(frames, key=lambda f: (f['sensor_id'], epoch(f['observed_at']), f['id']))
    groups = 0
    while remaining:
        groups += 1
        anchor = remaining.pop(0)
        rest = []
        for f in remaining:
            if f['sensor_id'] == anchor['sensor_id'] and \
                    0 <= epoch(f['observed_at']) - epoch(anchor['observed_at']) <= within_seconds:
                continue
            rest.append(f)
        remaining = rest
    return groups


def naive_pairwise_extremes(frames, calib, walk):
    """Add an if-rule per adjacent pair while still choosing a fresh clock
    extreme for every pair. Returns the persons a local greedy merge accepts,
    which no single shared bias assignment may support."""
    ordered = sorted(frames, key=lambda f: (epoch(f['observed_at']), f['id']))
    persons = len(ordered)
    for a, b in zip(ordered, ordered[1:]):
        ga, gb = a['sensor_id'], b['sensor_id']
        if ga == gb:
            persons -= 1  # local rule always merges same-gate neighbours
            continue
        gap = epoch(b['observed_at']) - epoch(a['observed_at'])
        ca, cb = calib[ga], calib[gb]
        # per-pair independent extreme: best case reading gap + (hi_b - lo_a)
        if gap + cb['bias_max_seconds'] - ca['bias_min_seconds'] >= walk[(ga, gb)]:
            persons -= 1  # rule says "same person possible" and greedy merges
    return persons
