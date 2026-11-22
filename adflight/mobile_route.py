"""Offline vehicle positioning capture and municipal road-segment permit adjudication.

A vehicle screen is sold as a fixed city position, but municipal permits bind to
the road actually travelled: school no-broadcast zones forbid commercial display
while the vehicle is inside them.  This module keeps the raw evidence (GPS/INS
frames with device clock, the shared base-station calibration certificate,
versioned road topology, independent intersection camera passes) and adjudicates
every captured segment as one of: permitted / no_broadcast / ambiguous /
insufficient_evidence.

Candidate routes must satisfy *all* raw frames under one shared clock bias and
stay connected in the road network (speed limits, one-way direction, time
windows) - per-frame nearest-road snapping that splices disconnected roads is
never accepted as a driving fact.  When two complete physical worlds fit the
same positioning bytes, both are reported together with their common safe zone,
and no commercial permit is claimed for the ambiguous stretch.
"""
import json, math
from datetime import datetime, timedelta, timezone
from .store import connect, source, already, at

EARTH_R = 6371000.0
ZONES = ('permitted', 'school_no_broadcast')
BASE_VERDICTS = ('permitted', 'no_broadcast', 'ambiguous', 'insufficient_evidence')
CAMERA_BRANCH_TOLERANCE_S = 90  # independent camera pass must be observed at the branch, not later

def _parse(value):
    return datetime.fromisoformat(value[:-1] + '+00:00')

def _fmt(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def _quantize(dt):
    # Segment boundaries are quantized to whole seconds so stored timestamps
    # stay lexicographically comparable (no fractional-second strings).
    return datetime.fromtimestamp(round(dt.timestamp()), tz=timezone.utc)

# ---------------------------------------------------------------- geometry
def _xy(lat, lon, origin_lat, origin_lon):
    x = math.radians(lon - origin_lon) * EARTH_R * math.cos(math.radians(origin_lat))
    y = math.radians(lat - origin_lat) * EARTH_R
    return x, y

def _length(points):
    return sum(math.hypot(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in zip(points, points[1:]))

def _project(px, py, points):
    best, cum = None, 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        vx, vy = x2 - x1, y2 - y1
        vv = vx * vx + vy * vy
        t = 0.0 if vv == 0 else max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / vv))
        d = math.hypot(px - (x1 + t * vx), py - (y1 + t * vy))
        ln = math.sqrt(vv)
        if best is None or d < best[0]:
            best = (d, cum + t * ln)
        cum += ln
    return best  # (perpendicular distance, arc length along polyline)

# ---------------------------------------------------------------- loading
def _latest_map(db):
    row = db.execute('SELECT id FROM map_version ORDER BY published_at DESC, id DESC LIMIT 1').fetchone()
    return row['id'] if row else None

def _roads(db, map_id):
    roads = {}
    for r in db.execute('SELECT * FROM road WHERE map_version=?', (map_id,)):
        pts = [tuple(p) for p in json.loads(r['points'])]
        roads[r['id']] = {'id': r['id'], 'from': r['from_node'], 'to': r['to_node'], 'zone': r['zone'],
                          'limit': r['speed_limit_kmh'] / 3.6, 'points': pts, 'length': _length(pts)}
    return roads

def _frames(db, package_id):
    pkg = db.execute('SELECT * FROM gps_package WHERE id=?', (package_id,)).fetchone()
    if not pkg:
        raise ValueError('unknown_package')
    cal = db.execute('SELECT * FROM calibration WHERE device_id=? ORDER BY valid_to DESC LIMIT 1',
                     (pkg['device_id'],)).fetchone()
    if not cal:
        raise ValueError('calibration_missing')
    frames = []
    for f in db.execute('SELECT * FROM gps_frame WHERE package_id=? ORDER BY seq', (package_id,)):
        # one shared base-station bias corrects every frame of the package;
        # per-frame independent correction is exactly the splice we must reject
        t = _quantize(_parse(f['device_clock']) - timedelta(seconds=cal['bias_seconds']))
        if not (cal['valid_from'] <= _fmt(t) <= cal['valid_to']):
            raise ValueError('calibration_not_valid_for_window')
        frames.append({'seq': f['seq'], 'at': t, 'lat': f['lat'], 'lon': f['lon']})
    return pkg, cal, frames

# ------------------------------------------------------- route enumeration
def _walk(roads, r1, s1, r2, s2):
    """All simple network paths from arc s1 of road r1 to arc s2 of road r2.
    Each leg is a list of (road_id, distance_on_road); connectivity is what
    stops entry-inner/exit-outer splices that per-frame snapping would allow."""
    legs = []
    if r1 == r2 and s1 <= s2 + 1e-9:
        legs.append([(r1, s2 - s1)])
    def dfs(node, used, acc):
        for rid, r in roads.items():
            if r['from'] != node or rid in used:
                continue
            if rid == r2:
                legs.append(acc + [(rid, s2)])
            elif len(acc) < len(roads):
                dfs(r['to'], used | {rid}, acc + [(rid, r['length'])])
    start = roads[r1]
    dfs(start['to'], {r1}, [(r1, start['length'] - s1)])
    return legs

def _trajectories(db, package_id, map_id=None):
    pkg, cal, frames = _frames(db, package_id)
    map_id = map_id or _latest_map(db)
    if not map_id:
        raise ValueError('map_missing')
    mrow = db.execute('SELECT * FROM map_version WHERE id=?', (map_id,)).fetchone()
    roads = _roads(db, map_id)
    tol = cal['tolerance_m']
    nodes = {}
    for r in roads.values():
        nodes.setdefault(r['from'], r['points'][0])
        nodes.setdefault(r['to'], r['points'][-1])
    # A frame inside the tolerance of an intersection is an intersection pass:
    # one node token replaces the arc hits on every incident road, so the same
    # physical route is not double-counted through rival arc assignments.
    token_sets = []
    for f in frames:
        px, py = _xy(f['lat'], f['lon'], mrow['origin_lat'], mrow['origin_lon'])
        near = sorted(n for n, (nx, ny) in nodes.items() if math.hypot(px - nx, py - ny) <= tol + 1e-9)
        tokens = [('node', n, math.hypot(px - nodes[n][0], py - nodes[n][1])) for n in near]
        for rid, r in roads.items():
            if r['from'] in near or r['to'] in near:
                continue
            d, s = _project(px, py, r['points'])
            if d <= tol + 1e-9:
                tokens.append(('arc', rid, s, d))
        if not tokens:
            raise ValueError('frame_off_network')
        token_sets.append(tokens)
    combos = [[]]
    for tokens in token_sets:
        combos = [c + [tok] for c in combos for tok in tokens]
    trajs, seen = [], set()
    for combo in combos:
        seg_options, ok = [], True
        for k in range(len(combo) - 1):
            u, v = combo[k], combo[k + 1]
            t0, t1 = frames[k]['at'], frames[k + 1]['at']
            dt = (t1 - t0).total_seconds()
            starts = [(rid, 0.0) for rid in roads if roads[rid]['from'] == u[1]] if u[0] == 'node' else [(u[1], u[2])]
            ends = [(rid, roads[rid]['length']) for rid in roads if roads[rid]['to'] == v[1]] if v[0] == 'node' else [(v[1], v[2])]
            good = []
            for r1, s1 in starts:
                for r2, s2 in ends:
                    for leg in _walk(roads, r1, s1, r2, s2):
                        dist = sum(d for _, d in leg)
                        limit = min(roads[r]['limit'] for r, _ in leg)
                        req = dist / dt if dt > 0 else float('inf')
                        if req <= limit + 1e-9:  # speed/time-window feasibility, whole leg
                            good.append((leg, req))
            if not good:
                ok = False
                break
            seg_options.append((t0, t1, good))
        if not ok:
            continue
        plans = [[]]
        for t0, t1, good in seg_options:
            plans = [p + [(t0, t1, leg, req)] for p in plans for leg, req in good]
        for plan in plans:
            roads_seq = [r for r, _ in plan[0][2]]
            for _, _, leg, _ in plan[1:]:
                for r, _ in leg:
                    if r != roads_seq[-1]:
                        roads_seq.append(r)
            key = tuple(roads_seq)
            if key in seen:
                continue
            seen.add(key)
            trajs.append({'roads': roads_seq, 'legs': plan,
                          'max_speed_ms': max(req for _, _, _, req in plan),
                          'max_frame_error_m': max(tok[-1] for tok in combo)})
    return pkg, cal, frames, roads, map_id, trajs

def _timeline(roads, traj):
    """Zone intervals of one trajectory, walking each leg at the constant
    speed the frames require; boundaries quantized to whole seconds."""
    intervals = []
    for t0, t1, leg, _ in traj['legs']:
        dist = sum(d for _, d in leg)
        span = (t1 - t0).total_seconds()
        cum = 0.0
        for rid, d in leg:
            a = _quantize(t0 + timedelta(seconds=span * cum / dist)) if dist else t0
            cum += d
            b = _quantize(t0 + timedelta(seconds=span * cum / dist)) if dist else t1
            if b > a:
                intervals.append({'start': a, 'end': b, 'road': rid, 'zone': roads[rid]['zone']})
    return intervals

def _verdict(zones):
    if not zones:
        return 'insufficient_evidence'
    if zones == {'permitted'}:
        return 'permitted'
    if zones == {'school_no_broadcast'}:
        return 'no_broadcast'
    return 'ambiguous'

def _zone_at(timeline, t):
    hit = next((i['zone'] for i in timeline if i['start'] <= t < i['end']), None)
    return hit

def _segments(timelines, start, end):
    bounds = sorted({start, end} | {i['start'] for t in timelines for i in t}
                    | {i['end'] for t in timelines for i in t})
    out = []
    for a, b in zip(bounds, bounds[1:]):
        if a >= b:
            continue
        mid = a + (b - a) / 2
        out.append({'start': a, 'end': b,
                    'verdict': _verdict({z for t in timelines for z in [_zone_at(t, mid)] if z})})
    return out

def _verdict_for_window(timelines, start, end):
    a, b = _parse(start), _parse(end)
    mid = a + (b - a) / 2
    return _verdict({z for t in timelines for z in [_zone_at(t, mid)] if z})

def _branch(db, package_id):
    """Where the feasible worlds diverge: first segment whose road sets differ.
    Decisions and camera evidence must predate this point, not the GPS guess."""
    pkg, cal, frames, roads, map_id, trajs = _trajectories(db, package_id)
    timelines = [_timeline(roads, t) for t in trajs]
    bounds = sorted({i['start'] for t in timelines for i in t} | {i['end'] for t in timelines for i in t})
    for a, b in zip(bounds, bounds[1:]):
        mid = a + (b - a) / 2
        sets = {tuple(sorted(i['road'] for i in t if i['start'] <= mid < i['end'])) for t in timelines}
        if len(sets) > 1:
            node = next(i['road'] for t in timelines for i in t if i['end'] == a)
            node = roads[node]['to']
            clean = [t for t in timelines if all(i['zone'] == 'permitted' for i in t)]
            permitted_roads = {i['road'] for t in clean for i in t if i['start'] == a}
            return a, node, permitted_roads
    return None, None, set()

def _nearest(db, package_id):
    """Baseline the task rejects: snap each frame to its nearest road, stitch by
    shortest feasible path.  Reported only to exhibit its unsound releases."""
    pkg, cal, frames, roads, map_id, trajs = _trajectories(db, package_id)
    mrow = db.execute('SELECT * FROM map_version WHERE id=?', (map_id,)).fetchone()
    snaps = []
    for f in frames:
        px, py = _xy(f['lat'], f['lon'], mrow['origin_lat'], mrow['origin_lon'])
        d, s, rid = min((proj[0], proj[1], rid) for rid, r in roads.items()
                        for proj in [_project(px, py, r['points'])])
        snaps.append((rid, s))
    legs = []
    pairs = list(zip(frames, snaps))
    for (f1, h1), (f2, h2) in zip(pairs, pairs[1:]):
        dt = (f2['at'] - f1['at']).total_seconds()
        options = []
        for leg in _walk(roads, h1[0], h1[1], h2[0], h2[1]):
            dist = sum(d for _, d in leg)
            req = dist / dt if dt > 0 else float('inf')
            if req <= min(roads[r]['limit'] for r, _ in leg) + 1e-9:
                options.append((dist, leg, req))
        if not options:
            raise ValueError('no_feasible_trajectory')
        dist, leg, req = min(options, key=lambda o: o[0])
        legs.append((f1['at'], f2['at'], leg, req))
    roads_seq = [r for r, _ in legs[0][2]]
    for _, _, leg, _ in legs[1:]:
        for r, _ in leg:
            if r != roads_seq[-1]:
                roads_seq.append(r)
    return roads_seq, _timeline(roads, {'legs': legs})

# ------------------------------------------------------------------ events
def gps_import(path, file):
    with connect(path) as db, db:
        sid, d, replay = source(db, 'gps', file)
        if already(db, 'gps_package', d['id'], sid):
            return {'id': d['id'], 'replayed': True}
        if not d.get('frames'):
            raise ValueError('gps_frames_missing')
        seqs = [f['seq'] for f in d['frames']]
        clocks = [at(f['device_clock']) for f in d['frames']]
        if len(set(seqs)) != len(seqs) or seqs != sorted(seqs) or clocks != sorted(clocks):
            raise ValueError('gps_sequence_error')  # dropped frames leave seq gaps; reordering is rejected
        db.execute('INSERT INTO gps_package VALUES(?,?,?,?,?)',
                   (d['id'], d['device_id'], d['vehicle_id'], d['screen_id'], sid))
        for f in d['frames']:
            db.execute('INSERT INTO gps_frame VALUES(?,?,?,?,?)',
                       (d['id'], f['seq'], f['device_clock'], f['lat'], f['lon']))
        return {'id': d['id'], 'frames': len(d['frames'])}

def calibration_import(path, file):
    with connect(path) as db, db:
        sid, d, replay = source(db, 'calibration', file)
        if already(db, 'calibration', d['id'], sid):
            return {'id': d['id'], 'replayed': True}
        if not d['issuer'] or d['issuer'] == 'operator':
            raise ValueError('independent_calibration_required')
        if at(d['valid_from']) >= at(d['valid_to']):
            raise ValueError('invalid_calibration_window')
        if float(d['tolerance_m']) <= 0:
            raise ValueError('invalid_tolerance')
        db.execute('INSERT INTO calibration VALUES(?,?,?,?,?,?,?,?,?)',
                   (d['id'], d['device_id'], d['base_station'], float(d['bias_seconds']),
                    float(d['tolerance_m']), d['valid_from'], d['valid_to'], d['issuer'], sid))
        return {'id': d['id'], 'status': 'calibrated'}

def map_import(path, file):
    with connect(path) as db, db:
        sid, d, replay = source(db, 'map', file)
        if already(db, 'map_version', d['id'], sid):
            return {'id': d['id'], 'replayed': True}
        at(d['published_at'])
        db.execute('INSERT INTO map_version VALUES(?,?,?,?,?)',
                   (d['id'], d['published_at'], d['origin']['lat'], d['origin']['lon'], sid))
        for road in d['roads']:
            if road['zone'] not in ZONES:
                raise ValueError('unknown_zone')
            if len(road['points']) < 2:
                raise ValueError('road_geometry_too_short')
            db.execute('INSERT INTO road VALUES(?,?,?,?,?,?,?,?)',
                       (road['id'], d['id'], road['from'], road['to'], road['zone'],
                        int(road.get('one_way', 1)), float(road['speed_limit_kmh']),
                        json.dumps(road['points'])))
        _readjudicate(db, d['id'])
        return {'id': d['id'], 'roads': len(d['roads'])}

def _readjudicate(db, new_map):
    """Map revision re-adjudicates only the affected windows.  Rulings already
    finalized by enforcement-grade evidence (released/stopped) keep their
    original map version; only base verdicts are reconsidered."""
    rows = db.execute('SELECT * FROM ruling WHERE current=1').fetchall()
    for pkg_id in {r['package_id'] for r in rows}:
        try:
            _, _, _, roads, _, trajs = _trajectories(db, pkg_id, map_id=new_map)
        except ValueError:
            continue
        timelines = [_timeline(roads, t) for t in trajs]
        for r in rows:
            if r['package_id'] != pkg_id or r['verdict'] not in BASE_VERDICTS:
                continue
            new = _verdict_for_window(timelines, r['start_at'], r['end_at'])
            if new != r['verdict']:
                _supersede(db, r, new, f'revision:{new_map}', map_version=new_map)

def camera_import(path, file):
    with connect(path) as db, db:
        sid, d, replay = source(db, 'camera', file)
        if already(db, 'camera_pass', d['id'], sid):
            return {'id': d['id'], 'replayed': True}
        if not d['issuer'] or d['issuer'] == 'operator':
            raise ValueError('non_independent_camera')
        at(d['observed_at'])
        map_id = _latest_map(db)
        if not map_id:
            raise ValueError('map_missing')
        road = db.execute('SELECT * FROM road WHERE id=? AND map_version=?', (d['road_id'], map_id)).fetchone()
        if not road:
            raise ValueError('unknown_camera_road')
        if d['node'] not in (road['from_node'], road['to_node']):
            raise ValueError('camera_node_mismatch')
        db.execute('INSERT INTO camera_pass VALUES(?,?,?,?,?,?,?,?)',
                   (d['id'], d['camera_id'], d['node'], d['road_id'], d['vehicle_id'],
                    d['observed_at'], d['issuer'], sid))
        _finalize_avoid(db)
        return {'id': d['id'], 'status': 'recorded'}

def adjudicate(path, file):
    with connect(path) as db, db:
        sid, d, replay = source(db, 'adjudication', file)
        if already(db, 'adjudication', d['id'], sid):
            return {'id': d['id'], 'replayed': True}
        pkg, cal, frames, roads, map_id, trajs = _trajectories(db, d['package_id'])
        if not trajs:
            raise ValueError('no_feasible_trajectory')
        timelines = [_timeline(roads, t) for t in trajs]
        segs = _segments(timelines, frames[0]['at'], frames[-1]['at'])
        db.execute("UPDATE ruling SET current=0 WHERE package_id=? AND current=1 AND verdict IN ('permitted','no_broadcast','ambiguous','insufficient_evidence')",
                   (d['package_id'],))
        finalized = {(r['start_at'], r['end_at']) for r in db.execute(
            'SELECT start_at,end_at FROM ruling WHERE package_id=? AND current=1', (d['package_id'],))}
        for s in segs:
            if (_fmt(s['start']), _fmt(s['end'])) in finalized:
                continue  # evidence-finalized windows keep their ruling
            db.execute('INSERT INTO ruling(package_id,start_at,end_at,verdict,map_version,decided_by,current) VALUES(?,?,?,?,?,?,1)',
                       (d['package_id'], _fmt(s['start']), _fmt(s['end']), s['verdict'], map_id,
                        'adjudication:' + d['id']))
        db.execute('INSERT INTO adjudication VALUES(?,?,?)', (d['id'], d['package_id'], sid))
        return {'id': d['id'], 'package_id': d['package_id'], 'map_version': map_id,
                'candidates': [t['roads'] for t in trajs],
                'segments': [{'start': _fmt(s['start']), 'end': _fmt(s['end']), 'verdict': s['verdict']}
                             for s in segs]}

def decide(path, file):
    with connect(path) as db, db:
        sid, d, replay = source(db, 'decision', file)
        if already(db, 'route_decision', d['id'], sid):
            return {'id': d['id'], 'replayed': True}
        if d['mode'] not in ('avoid_outer', 'pause_verify'):
            raise ValueError('unknown_route_mode')
        decided = at(d['decided_at'])
        if not db.execute('SELECT 1 FROM ruling WHERE package_id=? AND current=1', (d['package_id'],)).fetchone():
            raise ValueError('adjudication_required')
        branch_at, node, permitted_roads = _branch(db, d['package_id'])
        if branch_at is None or _parse(decided) >= branch_at:
            raise ValueError('decision_after_branch')  # no retroactive permit from GPS guessing
        stop_start = stop_end = None
        if d['mode'] == 'pause_verify':
            stop_start, stop_end = at(d['stop_start']), at(d['stop_end'])
            if stop_start >= stop_end:
                raise ValueError('invalid_stop_window')
            amb = db.execute("SELECT MIN(start_at) AS a, MAX(end_at) AS b FROM ruling WHERE package_id=? AND current=1 AND verdict='ambiguous'",
                             (d['package_id'],)).fetchone()
            if amb['a'] and (stop_start > amb['a'] or stop_end < amb['b']):
                raise ValueError('stop_window_incomplete')
        db.execute('INSERT INTO route_decision VALUES(?,?,?,?,?,?,?,?)',
                   (d['id'], d['package_id'], d['mode'], decided, stop_start, stop_end, 'pending', sid))
        if d['mode'] == 'avoid_outer':
            _finalize_avoid(db, d['package_id'])
        status = db.execute('SELECT status FROM route_decision WHERE id=?', (d['id'],)).fetchone()['status']
        return {'id': d['id'], 'mode': d['mode'], 'status': status, 'branch_at': _fmt(branch_at)}

def stop_import(path, file):
    with connect(path) as db, db:
        sid, d, replay = source(db, 'stop', file)
        if already(db, 'stop_receipt', d['id'], sid):
            return {'id': d['id'], 'replayed': True}
        start, end = at(d['start_at']), at(d['end_at'])
        if start >= end:
            raise ValueError('invalid_stop_window')
        if d.get('attestation') != 'hardware':
            raise ValueError('hardware_receipt_required')
        dec = db.execute('SELECT * FROM route_decision WHERE id=?', (d['decision_id'],)).fetchone()
        if not dec:
            raise ValueError('unknown_decision')
        if dec['mode'] != 'pause_verify':
            raise ValueError('decision_mode_mismatch')
        pkg = db.execute('SELECT * FROM gps_package WHERE id=?', (dec['package_id'],)).fetchone()
        if d['device_id'] != pkg['device_id']:
            raise ValueError('stop_device_mismatch')
        if start > dec['stop_start'] or end < dec['stop_end']:
            raise ValueError('stop_receipt_incomplete')
        db.execute('INSERT INTO stop_receipt VALUES(?,?,?,?,?,?,?)',
                   (d['id'], d['decision_id'], d['device_id'], start, end, d['attestation'], sid))
        db.execute("UPDATE route_decision SET status='finalized' WHERE id=?", (dec['id'],))
        for r in db.execute("SELECT * FROM ruling WHERE package_id=? AND current=1 AND verdict='ambiguous' AND start_at>=? AND end_at<=?",
                            (dec['package_id'], dec['stop_start'], dec['stop_end'])).fetchall():
            _supersede(db, r, 'stopped', 'decision:' + dec['id'])
        # customer make-good credit for the verifiably dark window
        for b in db.execute("SELECT * FROM booking WHERE screen_id=? AND status='confirmed' AND start_at<? AND end_at>?",
                            (pkg['screen_id'], dec['stop_end'], dec['stop_start'])).fetchall():
            a, z = max(b['start_at'], dec['stop_start']), min(b['end_at'], dec['stop_end'])
            missing = int((_parse(z) - _parse(a)).total_seconds())
            dup = db.execute("SELECT 1 FROM obligation WHERE booking_id=? AND status='due' AND reason='no_broadcast_pause' AND missing_seconds=?",
                             (b['id'], missing)).fetchone()
            if missing > 0 and not dup:
                db.execute("INSERT INTO obligation(booking_id,missing_seconds,status,reason) VALUES(?,?,'due','no_broadcast_pause')",
                           (b['id'], missing))
        return {'id': d['id'], 'status': 'stopped'}

def _supersede(db, ruling, verdict, decided_by, map_version=None):
    db.execute('UPDATE ruling SET current=0 WHERE id=?', (ruling['id'],))
    db.execute('INSERT INTO ruling(package_id,start_at,end_at,verdict,map_version,decided_by,current) VALUES(?,?,?,?,?,?,1)',
               (ruling['package_id'], ruling['start_at'], ruling['end_at'], verdict,
                map_version or ruling['map_version'], decided_by))

def _finalize_avoid(db, package_id=None):
    q = "SELECT * FROM route_decision WHERE mode='avoid_outer' AND status='pending'"
    for dec in db.execute(q).fetchall():
        if package_id and dec['package_id'] != package_id:
            continue
        pkg = db.execute('SELECT * FROM gps_package WHERE id=?', (dec['package_id'],)).fetchone()
        branch_at, node, permitted_roads = _branch(db, dec['package_id'])
        proof = None
        for cam in db.execute('SELECT * FROM camera_pass WHERE vehicle_id=?', (pkg['vehicle_id'],)).fetchall():
            if (cam['node'] == node and cam['road_id'] in permitted_roads and branch_at
                    and _parse(cam['observed_at']) <= branch_at + timedelta(seconds=CAMERA_BRANCH_TOLERANCE_S)):
                proof = cam
                break
        if not proof:
            continue
        for r in db.execute("SELECT * FROM ruling WHERE package_id=? AND current=1 AND verdict='ambiguous'",
                            (dec['package_id'],)).fetchall():
            _supersede(db, r, 'released', 'decision:' + dec['id'])
        db.execute("UPDATE route_decision SET status='finalized' WHERE id=?", (dec['id'],))

# ------------------------------------------------------------------ report
def route_report(db, package_id):
    pkg, cal, frames, roads, map_id, trajs = _trajectories(db, package_id)
    timelines = [_timeline(roads, t) for t in trajs]
    start, end = frames[0]['at'], frames[-1]['at']
    live = _segments(timelines, start, end)
    current = db.execute('SELECT * FROM ruling WHERE package_id=? AND current=1 ORDER BY start_at,end_at',
                         (package_id,)).fetchall()
    history = db.execute('SELECT * FROM ruling WHERE package_id=? AND current=0 ORDER BY id',
                         (package_id,)).fetchall()
    rulings = current or [dict(package_id=package_id, start_at=_fmt(s['start']), end_at=_fmt(s['end']),
                               verdict=s['verdict'], map_version=map_id, decided_by='live', current=1)
                          for s in live]
    safe = [{'start': r['start_at'], 'end': r['end_at']} for r in rulings if r['verdict'] == 'permitted']
    safe = _merge(safe)
    nearest_roads, nearest_timeline = _nearest(db, package_id)
    unsound = [{'start': _fmt(s['start']), 'end': _fmt(s['end'])} for s in live
               if s['verdict'] in ('ambiguous', 'no_broadcast')
               and _zone_at(nearest_timeline, s['start'] + (s['end'] - s['start']) / 2) == 'permitted']
    would_release = _merge([{'start': _fmt(i['start']), 'end': _fmt(i['end'])}
                            for i in nearest_timeline if i['zone'] == 'permitted'])
    decision = db.execute('SELECT * FROM route_decision WHERE package_id=? ORDER BY decided_at DESC, id DESC LIMIT 1',
                          (package_id,)).fetchone()
    stop = db.execute('SELECT s.* FROM stop_receipt s JOIN route_decision d ON d.id=s.decision_id WHERE d.package_id=?',
                      (package_id,)).fetchone()
    return {
        'package': package_id, 'device_id': pkg['device_id'], 'vehicle_id': pkg['vehicle_id'],
        'screen_id': pkg['screen_id'], 'map_version': map_id,
        'correction': {'base_station': cal['base_station'], 'bias_seconds': cal['bias_seconds'],
                       'tolerance_m': cal['tolerance_m'], 'issuer': cal['issuer']},
        'frames': [{'seq': f['seq'], 'at': _fmt(f['at']), 'lat': f['lat'], 'lon': f['lon']} for f in frames],
        'candidates': [{'roads': t['roads'], 'max_speed_ms': round(t['max_speed_ms'], 3),
                        'within_limit': t['max_speed_ms'] <= min(roads[r]['limit'] for r in t['roads']) + 1e-9,
                        'max_frame_error_m': round(t['max_frame_error_m'], 2),
                        'zones': [{'start': _fmt(i['start']), 'end': _fmt(i['end']), 'road': i['road'], 'zone': i['zone']}
                                  for i in tl]}
                       for t, tl in zip(trajs, timelines)],
        'segments': [{'start': r['start_at'], 'end': r['end_at'], 'verdict': r['verdict'],
                      'map_version': r['map_version']} for r in rulings],
        'safe_zone': safe,
        'nearest_route': {'roads': nearest_roads, 'would_release': would_release,
                          'unsound_release': unsound},
        'decision': dict(decision) if decision else None,
        'camera': [dict(c) for c in db.execute('SELECT * FROM camera_pass WHERE vehicle_id=?', (pkg['vehicle_id'],))],
        'stop': dict(stop) if stop else None,
        'rulings': {'current': [dict(r) for r in current], 'history': [dict(r) for r in history]},
        'permit_claims': _permit_claims(db, pkg, rulings, start, end),
    }

def _merge(windows):
    out = []
    for w in sorted(windows, key=lambda w: w['start']):
        if out and out[-1]['end'] == w['start']:
            out[-1]['end'] = w['end']
        else:
            out.append(dict(w))
    return out

def _permit_claims(db, pkg, rulings, start, end):
    # never claim a municipal zone permit for no-broadcast or unproven stretches
    claims = []
    for b in db.execute("SELECT * FROM booking WHERE screen_id=? AND status IN ('confirmed','waitlisted','interrupted') AND start_at<? AND end_at>?",
                        (pkg['screen_id'], _fmt(end), _fmt(start))).fetchall():
        segs = []
        for r in rulings:
            a, z = max(r['start_at'], b['start_at']), min(r['end_at'], b['end_at'])
            if a < z:
                segs.append({'start': a, 'end': z, 'verdict': r['verdict'],
                             'claimable': r['verdict'] in ('permitted', 'released')})
        if b['end_at'] > _fmt(end):
            segs.append({'start': _fmt(end), 'end': b['end_at'],
                         'verdict': 'insufficient_evidence', 'claimable': False})
        claims.append({'booking_id': b['id'], 'segments': segs})
    return claims
