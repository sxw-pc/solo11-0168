"""Immutable advertiser evidence-package publication protocol.

A package is a *snapshot*: one booking version, the obligation rows and proof
index visible at publication time, and a settlement summary.  Every file of a
package is written into a staging directory first, hashed, and only exposed
through an atomic rename plus a database commit.  Package directories are
immutable once visible; new content always gets a new package id and a new
sequence number.  Acknowledgements verify the bytes the client actually
downloaded, never only the digest stored in the database.

The legacy ``publish``/``acknowledge`` entry points keep their original
arguments and return shape; they are thin wrappers over this protocol and
still populate the original ``client_package``/``client_ack`` tables.

The ``naive_*`` functions are intentionally broken reference implementations
of the three "obvious" approaches; tests prove the wrong acceptance
conclusions they produce.  Do not use them outside those demonstrations.
"""
import hashlib
import json
import os
from pathlib import Path

from .report import report
from .store import connect, canonical, digest_of, now_utc, fsync_dir

PACKAGE_DIR = 'packages'
STAGING_DIR = '.staging'
PACKAGE_FILES = ('body.json', 'proof-index.json', 'settlement.json')
# Statuses that mean a package has been reachable by an external client.
VISIBLE_STATUSES = ('published', 'accepted', 'withdrawn', 'quarantined')


# --------------------------------------------------------------------------- #
# Pure snapshot / package construction (no filesystem, no database mutation)
# --------------------------------------------------------------------------- #
def _settlement(booking, proofs):
    from .playback import seconds
    target = seconds(booking['start_at'], booking['end_at'])
    valid = [p for p in proofs if p['status'] == 'valid']
    actual = sum(seconds(p['start_at'], p['end_at']) for p in valid)
    missing = max(0, target - actual)
    reason = ('maintenance_interrupted' if booking['status'] == 'interrupted'
              else 'underplayed' if missing else 'fulfilled')
    return {'booking_id': booking['id'], 'planned_seconds': target,
            'valid_seconds': actual, 'missing_seconds': missing, 'reason': reason}


def build_snapshot(db, package_id, seq, booking_id, recipient, route):
    booking = db.execute('SELECT * FROM booking WHERE id=?', (booking_id,)).fetchone()
    if not booking:
        raise ValueError('unknown_booking')
    proofs = [dict(r) for r in db.execute(
        'SELECT * FROM proof WHERE booking_id=? ORDER BY id', (booking_id,))]
    obligations = [dict(r) for r in db.execute(
        'SELECT * FROM obligation WHERE booking_id=? ORDER BY id', (booking_id,))]
    # Tenant isolation: every embedded row must belong to this booking, so a
    # package for advertiser A can never carry another advertiser's material.
    if any(p['booking_id'] != booking_id for p in proofs):
        raise ValueError('cross_tenant_proof')
    if any(o['booking_id'] != booking_id for o in obligations):
        raise ValueError('cross_tenant_obligation')
    settlement = _settlement(dict(booking), proofs)
    proof_index = [{'id': p['id'], 'screen_id': p['screen_id'],
                    'start_at': p['start_at'], 'end_at': p['end_at'],
                    'observer': p['observer'], 'status': p['status']} for p in proofs]
    snapshot = {
        'package_id': package_id, 'seq': seq, 'booking_id': booking_id,
        'recipient': recipient, 'route': route,
        'booking': dict(booking), 'proof': proofs, 'obligation': obligations,
        'settlement': settlement, 'proof_index': proof_index,
        'recipient_visible': [
            {'name': 'body.json', 'purpose': 'snapshot body: booking, proof and obligation versions'},
            {'name': 'proof-index.json', 'purpose': 'raw independent playback proof index'},
            {'name': 'settlement.json', 'purpose': 'settlement summary and obligation ledger'},
            {'name': 'manifest.json', 'purpose': 'recipient-visible file list and content digests'},
        ],
    }
    return snapshot


def render_files(snapshot):
    """Return the immutable file mapping for a pinned snapshot."""
    files = {
        'body.json': canonical(snapshot),
        'proof-index.json': canonical(snapshot['proof_index']),
        'settlement.json': canonical({'settlement': snapshot['settlement'],
                                      'obligation': snapshot['obligation']}),
    }
    manifest = {
        'package_id': snapshot['package_id'], 'seq': snapshot['seq'],
        'booking_id': snapshot['booking_id'], 'recipient': snapshot['recipient'],
        'route': snapshot['route'], 'created_at': now_utc(),
        'recipient_visible': snapshot['recipient_visible'],
        'snapshot': snapshot,
        'files': [{'name': name, 'sha256': digest_of(raw), 'bytes': len(raw)}
                  for name, raw in sorted(files.items())],
    }
    files['manifest.json'] = canonical(manifest)
    return files, manifest


def content_digest(files):
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode('utf8'))
        h.update(hashlib.sha256(files[name]).digest())
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Filesystem + SQLite commit sequence with injectable crash points
# --------------------------------------------------------------------------- #
def _paths(output_dir, package_id):
    out = Path(output_dir)
    return out, out / STAGING_DIR / package_id, out / PACKAGE_DIR / package_id


def _write_durable(tmp_dir, files):
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for name, raw in files.items():
        target = tmp_dir / name
        with open(target, 'wb') as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
    fsync_dir(tmp_dir)


def _crash(control, stage):
    if not control:
        return
    wanted = control if isinstance(control, str) else control(stage)
    if wanted == stage:
        raise RuntimeError(f'injected_crash:{stage}')


def publish_package(db_path, request_file, output_dir, request=None, crash_at=None):
    """Publish a new immutable package.

    ``crash_at`` may be a stage name (``after_stage_write``,
    ``after_staging_commit`` or ``after_rename``) or a callable receiving the
    stage and returning the stage to crash at.
    """
    if request is None:
        request = json.loads(Path(request_file).read_text(encoding='utf8'))
    crash_at = request.get('crash_at', crash_at)
    package_id = request['id']
    booking_id = request['booking_id']
    recipient = request['recipient']
    route = request.get('route', 'closeout')
    if route not in ('closeout', 'rolling'):
        raise ValueError('unknown_route')
    out, tmp_dir, final_dir = _paths(output_dir, package_id)
    if final_dir.exists():
        raise ValueError('package_dir_exists')
    if tmp_dir.exists():
        raise ValueError('recover_pending')

    with connect(db_path) as db, db:
        if db.execute('SELECT 1 FROM package_version WHERE id=?', (package_id,)).fetchone():
            raise ValueError('package_id_conflict')
        fixed = db.execute('SELECT * FROM publication_route WHERE booking_id=?', (booking_id,)).fetchone()
        if fixed:
            if fixed['recipient'] != recipient:
                raise ValueError('recipient_conflict')
            route = fixed['route']
        seq_row = db.execute('SELECT COALESCE(MAX(seq),0)+1 AS seq FROM package_version WHERE booking_id=?',
                             (booking_id,)).fetchone()
        seq = seq_row['seq']
        current = db.execute('SELECT * FROM publication_pointer WHERE booking_id=?', (booking_id,)).fetchone()
        previous = db.execute('SELECT * FROM package_version WHERE id=?',
                              (current['current_package_id'],)).fetchone() if current else None
        if previous is not None:
            if route == 'closeout':
                raise ValueError('closeout_period_sealed')
            if previous['status'] == 'accepted':
                raise ValueError('accepted_version_immutable')
            if previous['status'] not in ('published',):
                raise ValueError('previous_version_not_replaceable')
        snapshot = build_snapshot(db, package_id, seq, booking_id, recipient, route)
        files, manifest = render_files(snapshot)
        manifest_digest = digest_of(files['manifest.json'])
        c_digest = content_digest({n: files[n] for n in PACKAGE_FILES})
        created = now_utc()
        if not fixed:
            db.execute('INSERT INTO publication_route VALUES(?,?,?,?)',
                       (booking_id, recipient, route, created))
        db.execute('INSERT INTO package_snapshot(id,booking_id,recipient,route,payload,created_at) VALUES(?,?,?,?,?,?)',
                   (package_id, booking_id, recipient, route, json.dumps(snapshot, sort_keys=True, ensure_ascii=False), created))
        db.execute('INSERT INTO package_version VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (package_id, booking_id, recipient, seq, package_id, str(final_dir), str(tmp_dir),
                    manifest_digest, c_digest, 'staging', created, created))
        for name, raw in files.items():
            db.execute('INSERT INTO package_file VALUES(?,?,?,?)',
                       (package_id, name, digest_of(raw), len(raw)))
        db.execute('INSERT INTO package_event(package_id,kind,at,detail) VALUES(?,?,?,?)',
                   (package_id, 'staged', created, json.dumps({'route': route, 'seq': seq})))

    # Stage 1: durable files, no external visibility, no staging commit yet.
    _write_durable(tmp_dir, files)
    _crash(crash_at, 'after_stage_write')

    with connect(db_path) as db, db:
        # Staging row already exists; this commit marks files verified on disk.
        db.execute("UPDATE package_version SET status='staging_committed',updated_at=? WHERE id=?",
                   (now_utc(), package_id))
        db.execute('INSERT INTO package_event(package_id,kind,at,detail) VALUES(?,?,?,?)',
                   (package_id, 'staging_committed', now_utc(), None))
    _crash(crash_at, 'after_staging_commit')

    # Stage 2: atomic rename makes the bytes externally reachable.
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    os.rename(tmp_dir, final_dir)
    fsync_dir(final_dir.parent)
    _crash(crash_at, 'after_rename')

    # Stage 3: database commit publishes the pointer. Pointer only ever points
    # at a directory that exists on disk and has just been verified.
    _commit_published(db_path, package_id, previous['id'] if previous is not None else None, recovered=False)
    return {'id': package_id, 'digest': manifest_digest, 'content_digest': c_digest,
            'status': 'published', 'seq': seq, 'route': route}


def _commit_published(db_path, package_id, previous_id, recovered):
    with connect(db_path) as db, db:
        row = db.execute('SELECT * FROM package_version WHERE id=?', (package_id,)).fetchone()
        if not row:
            raise ValueError('unknown_package')
        ts = now_utc()
        db.execute("UPDATE package_version SET status='published',tmp_path=NULL,updated_at=? WHERE id=?",
                   (ts, package_id))
        if previous_id:
            db.execute("UPDATE package_version SET status='withdrawn',updated_at=? WHERE id=?",
                       (ts, previous_id))
            db.execute('INSERT INTO package_event(package_id,kind,at,detail) VALUES(?,?,?,?)',
                       (previous_id, 'withdrawn', ts, json.dumps({'replaced_by': package_id})))
        db.execute('INSERT INTO publication_pointer(booking_id,current_package_id,updated_at) VALUES(?,?,?) '
                   'ON CONFLICT(booking_id) DO UPDATE SET current_package_id=excluded.current_package_id,'
                   'updated_at=excluded.updated_at',
                   (row['booking_id'], package_id, ts))
        db.execute('INSERT INTO package_event(package_id,kind,at,detail) VALUES(?,?,?,?)',
                   (package_id, 'recovered' if recovered else 'published', ts, None))
        # Keep the legacy compatibility table in lockstep.
        db.execute('INSERT INTO client_package VALUES(?,?,?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET digest=excluded.digest,status=excluded.status',
                   (package_id, row['booking_id'], row['recipient'], row['manifest_digest'],
                    row['dir_path'], 'published'))


def verify_package(db_path, package_id):
    """Verify the actual on-disk package against manifest and database records."""
    with connect(db_path) as db:
        version = db.execute('SELECT * FROM package_version WHERE id=?', (package_id,)).fetchone()
        if not version:
            return {'status': 'unknown_package', 'package_id': package_id}
        file_rows = db.execute('SELECT * FROM package_file WHERE package_id=? ORDER BY name',
                               (package_id,)).fetchall()
        final_dir = Path(version['dir_path'])
        if not final_dir.is_dir():
            return {'status': 'package_dir_missing', 'package_id': package_id, 'detail': str(final_dir)}
        on_disk = {}
        for name in PACKAGE_FILES + ('manifest.json',):
            p = final_dir / name
            if not p.is_file():
                return {'status': 'file_missing', 'package_id': package_id, 'detail': name}
            on_disk[name] = p.read_bytes()
        for fr in file_rows:
            raw = on_disk.get(fr['name'])
            if raw is None or digest_of(raw) != fr['digest'] or len(raw) != fr['bytes_len']:
                return {'status': 'file_tampered', 'package_id': package_id, 'detail': fr['name']}
        if digest_of(on_disk['manifest.json']) != version['manifest_digest']:
            return {'status': 'manifest_tampered', 'package_id': package_id}
        manifest = json.loads(on_disk['manifest.json'].decode('utf8'))
        if content_digest({n: on_disk[n] for n in PACKAGE_FILES}) != version['content_digest']:
            return {'status': 'content_digest_mismatch', 'package_id': package_id}
        listed = {f['name']: f['sha256'] for f in manifest['files']}
        for name in PACKAGE_FILES:
            if listed.get(name) != digest_of(on_disk[name]):
                return {'status': 'manifest_listing_mismatch', 'package_id': package_id, 'detail': name}
        snap = manifest['snapshot']
        if (snap['package_id'], snap['recipient'], snap['booking_id']) != (
                package_id, version['recipient'], version['booking_id']):
            return {'status': 'snapshot_identity_mismatch', 'package_id': package_id}
        if any(p['booking_id'] != version['booking_id'] for p in snap['proof']):
            return {'status': 'cross_tenant_proof', 'package_id': package_id}
    return {'status': 'ok', 'package_id': package_id,
            'manifest_digest': version['manifest_digest'],
            'content_digest': version['content_digest'],
            'manifest_raw': on_disk['manifest.json'].decode('utf8')}


def claim(db_path, request_file, request=None):
    """Record that a recipient collected the *current* package, verifying bytes."""
    if request is None:
        request = json.loads(Path(request_file).read_text(encoding='utf8'))
    package_id = request['package_id']
    recipient = request['recipient']
    with connect(db_path) as db:
        version = db.execute('SELECT * FROM package_version WHERE id=?', (package_id,)).fetchone()
        if not version:
            raise ValueError('unknown_package')
        pointer = db.execute('SELECT * FROM publication_pointer WHERE booking_id=?',
                             (version['booking_id'],)).fetchone()
    if not pointer or pointer['current_package_id'] != package_id:
        raise ValueError('not_current_package')
    if version['recipient'] != recipient:
        raise ValueError('recipient_mismatch')
    if version['status'] != 'published':
        raise ValueError(f"package_not_claimable:{version['status']}")
    result = verify_package(db_path, package_id)
    if result['status'] != 'ok':
        raise ValueError(f"package_unverifiable:{result['status']}")
    ts = now_utc()
    with connect(db_path) as db, db:
        db.execute('INSERT INTO package_claim(package_id,recipient,claimed_at) VALUES(?,?,?)',
                   (package_id, recipient, ts))
        db.execute('INSERT INTO package_event(package_id,kind,at,detail) VALUES(?,?,?,?)',
                   (package_id, 'claimed', ts, json.dumps({'recipient': recipient})))
    return {'package_id': package_id, 'recipient': recipient, 'claimed_at': ts}


def acknowledge_receipt(db_path, receipt_file, receipt=None):
    """Accept a receipt only when the downloaded bytes actually match it."""
    if receipt is None:
        receipt = json.loads(Path(receipt_file).read_text(encoding='utf8'))
    receipt_digest = receipt.get('manifest_digest', receipt.get('digest'))
    with connect(db_path) as db:
        version = db.execute('SELECT * FROM package_version WHERE id=?', (receipt['package_id'],)).fetchone()
        if not version:
            raise ValueError('unknown_package')
        already = db.execute('SELECT 1 FROM package_receipt WHERE id=?', (receipt['id'],)).fetchone()
        prior_claim = db.execute('SELECT 1 FROM package_claim WHERE package_id=?',
                                 (receipt['package_id'],)).fetchone()
    if already:
        raise ValueError('receipt_id_conflict')
    verification = verify_package(db_path, receipt['package_id'])
    reason = None
    if verification['status'] != 'ok':
        reason = f"disk_{verification['status']}"
        # A package that was externally visible no longer verifies: quarantine,
        # never silently overwrite or delete it.
        if version['status'] in VISIBLE_STATUSES:
            with connect(db_path) as db, db:
                db.execute("UPDATE package_version SET status='quarantined',updated_at=? WHERE id=?",
                           (now_utc(), receipt['package_id']))
                db.execute('INSERT INTO package_event(package_id,kind,at,detail) VALUES(?,?,?,?)',
                           (receipt['package_id'], 'quarantined', now_utc(), reason))
    elif receipt['recipient'] != version['recipient']:
        reason = 'recipient_mismatch'
    elif receipt_digest != version['manifest_digest']:
        reason = 'digest_mismatch'
    elif receipt_digest != verification['manifest_digest']:
        reason = 'downloaded_digest_mismatch'
    elif version['status'] == 'withdrawn':
        reason = 'package_withdrawn'
    elif version['status'] not in ('published', 'accepted'):
        reason = f"package_status_{version['status']}"
    status = 'accepted' if reason is None else 'held'
    ts = now_utc()
    with connect(db_path) as db, db:
        db.execute('INSERT INTO package_receipt VALUES(?,?,?,?,?,?,?)',
                   (receipt['id'], receipt['package_id'], receipt['recipient'],
                    receipt_digest, status, reason, ts))
        db.execute('INSERT INTO package_event(package_id,kind,at,detail) VALUES(?,?,?,?)',
                   (receipt['package_id'], 'acknowledged', ts, json.dumps({'status': status, 'reason': reason})))
        if status == 'accepted':
            db.execute("UPDATE package_version SET status='accepted',updated_at=? WHERE id=?",
                       (ts, receipt['package_id']))
            if not prior_claim:
                # Legacy clients never call claim explicitly; preserve a
                # distinct claim timestamp at first acknowledgement.
                db.execute('INSERT INTO package_claim(package_id,recipient,claimed_at) VALUES(?,?,?)',
                           (receipt['package_id'], receipt['recipient'], ts))
                db.execute('INSERT INTO package_event(package_id,kind,at,detail) VALUES(?,?,?,?)',
                           (receipt['package_id'], 'claimed', ts, json.dumps({'implicit': True})))
            db.execute('INSERT INTO client_ack VALUES(?,?,?,?,?) '
                       'ON CONFLICT(id) DO UPDATE SET status=excluded.status',
                       (receipt['id'], receipt['package_id'], receipt['recipient'], receipt_digest, status))
            db.execute("UPDATE client_package SET status='accepted' WHERE id=?", (receipt['package_id'],))
        else:
            db.execute('INSERT INTO client_ack VALUES(?,?,?,?,?) '
                       'ON CONFLICT(id) DO UPDATE SET status=excluded.status',
                       (receipt['id'], receipt['package_id'], receipt['recipient'], receipt_digest, status))
    return {'id': receipt['id'], 'status': status, **({'reason': reason} if reason else {})}


def withdraw(db_path, request_file, request=None):
    """Withdraw the current pointer (rolling route only, before acceptance)."""
    if request is None:
        request = json.loads(Path(request_file).read_text(encoding='utf8'))
    package_id = request['package_id']
    with connect(db_path) as db:
        version = db.execute('SELECT * FROM package_version WHERE id=?', (package_id,)).fetchone()
        if not version:
            raise ValueError('unknown_package')
        route = db.execute('SELECT * FROM publication_route WHERE booking_id=?',
                           (version['booking_id'],)).fetchone()
        pointer = db.execute('SELECT * FROM publication_pointer WHERE booking_id=?',
                             (version['booking_id'],)).fetchone()
    if not route or route['route'] != 'rolling':
        raise ValueError('withdraw_requires_rolling_route')
    if not pointer or pointer['current_package_id'] != package_id:
        raise ValueError('not_current_package')
    if version['status'] == 'accepted':
        raise ValueError('accepted_version_immutable')
    if verify_package(db_path, package_id)['status'] != 'ok':
        raise ValueError('package_unverifiable')
    ts = now_utc()
    with connect(db_path) as db, db:
        db.execute("UPDATE package_version SET status='withdrawn',updated_at=? WHERE id=?", (ts, package_id))
        db.execute('DELETE FROM publication_pointer WHERE booking_id=?', (version['booking_id'],))
        db.execute('INSERT INTO package_event(package_id,kind,at,detail) VALUES(?,?,?,?)',
                   (package_id, 'withdrawn', ts, json.dumps({'manual': True})))
        db.execute("UPDATE client_package SET status='withdrawn' WHERE id=?", (package_id,))
    return {'package_id': package_id, 'status': 'withdrawn', 'withdrawn_at': ts}


def recover(db_path, output_dir):
    """Restart reconciliation. Classifies every package into one of:

    never_externally_visible / published_pending / client_accepted_old /
    manual_review.  No final package file is ever deleted or overwritten.
    """
    out = Path(output_dir)
    result = {'never_externally_visible': [], 'published_pending': [],
              'client_accepted_old': [], 'manual_review': [], 'resumed': []}
    with connect(db_path) as db:
        versions = [dict(v) for v in db.execute('SELECT * FROM package_version ORDER BY id')]
        pointer_rows = {p['booking_id']: dict(p)
                        for p in db.execute('SELECT * FROM publication_pointer')}
    final_root = out / PACKAGE_DIR
    staging_root = out / STAGING_DIR

    for v in versions:
        pid = v['id']
        final_dir = Path(v['dir_path'])
        tmp_dir = Path(v['tmp_path']) if v['tmp_path'] else None
        if v['status'] in ('staging', 'staging_committed'):
            # Never reached the publish commit: not externally visible unless
            # the rename itself had already happened.
            if final_dir.is_dir():
                check = _verify_dir(final_dir, pid, db_path)
                if check['status'] == 'ok':
                    # Crash after rename, before commit: bytes are already
                    # reachable; finish the pointer move against those bytes.
                    with connect(db_path) as db:
                        previous = db.execute(
                            "SELECT id FROM package_version WHERE booking_id=? AND id<>? AND status='published'",
                            (v['booking_id'], pid)).fetchone()
                    _commit_published(db_path, pid, previous['id'] if previous else None, recovered=True)
                    result['resumed'].append(pid)
                    result['published_pending'].append(pid)
                    continue
                with connect(db_path) as db, db:
                    db.execute("UPDATE package_version SET status='quarantined',updated_at=? WHERE id=?",
                               (now_utc(), pid))
                    db.execute('INSERT INTO package_event(package_id,kind,at,detail) VALUES(?,?,?,?)',
                               (pid, 'quarantined', now_utc(), check['status']))
                result['manual_review'].append(
                    {'package_id': pid, 'reason': check['status'], 'detail': check.get('detail')})
                continue
            check = _verify_dir(tmp_dir, pid, db_path) if tmp_dir and tmp_dir.is_dir() else {'status': 'staging_missing'}
            if check['status'] == 'ok':
                # Crash after staging commit (or write) but before rename:
                # safe to finish the exact same bytes.
                final_dir.parent.mkdir(parents=True, exist_ok=True)
                os.rename(tmp_dir, final_dir)
                fsync_dir(final_dir.parent)
                with connect(db_path) as db:
                    previous = db.execute(
                        "SELECT id FROM package_version WHERE booking_id=? AND id<>? AND status='published'",
                        (v['booking_id'], pid)).fetchone()
                _commit_published(db_path, pid, previous['id'] if previous else None, recovered=True)
                result['resumed'].append(pid)
                result['published_pending'].append(pid)
            else:
                with connect(db_path) as db, db:
                    db.execute("UPDATE package_version SET status='aborted',updated_at=? WHERE id=?",
                               (now_utc(), pid))
                    db.execute('INSERT INTO package_event(package_id,kind,at,detail) VALUES(?,?,?,?)',
                               (pid, 'aborted', now_utc(), check['status']))
                result['never_externally_visible'].append(
                    {'package_id': pid, 'reason': check['status'], 'staging_dir': str(tmp_dir)})
            continue
        verification = verify_package(db_path, pid)
        if verification['status'] != 'ok':
            # Was visible (or accepted) and bytes no longer match: human needed.
            with connect(db_path) as db, db:
                db.execute("UPDATE package_version SET status='quarantined',updated_at=? WHERE id=?",
                           (now_utc(), pid))
                db.execute('INSERT INTO package_event(package_id,kind,at,detail) VALUES(?,?,?,?)',
                           (pid, 'quarantined', now_utc(), verification['status']))
            result['manual_review'].append(
                {'package_id': pid, 'reason': verification['status'], 'detail': verification.get('detail')})
            continue
        if v['status'] == 'accepted':
            result['client_accepted_old'].append(
                {'package_id': pid, 'digest': v['manifest_digest'], 'dir': str(final_dir)})
        elif v['status'] in ('published', 'withdrawn'):
            result['published_pending'].append(pid)
        else:
            result['manual_review'].append({'package_id': pid, 'reason': f"status_{v['status']}"})

    # Pointer must only reference an intact, visible package.
    for booking_id, pointer in pointer_rows.items():
        target = next((v for v in versions if v['id'] == pointer['current_package_id']), None)
        if not target or verify_package(db_path, pointer['current_package_id'])['status'] != 'ok':
            result['manual_review'].append(
                {'package_id': pointer['current_package_id'], 'reason': 'dangling_current_pointer'})

    # Orphan directories/files: never delete. Unregistered final packages
    # might be real client downloads; unregistered staging dirs were never
    # visible; loose single-file outputs (legacy layout) are unregistered too.
    known_files = set()
    if final_root.is_dir():
        known = {Path(v['dir_path']).name for v in versions}
        for child in sorted(final_root.iterdir()):
            if child.name not in known:
                result['manual_review'].append(
                    {'package_id': child.name, 'reason': 'unregistered_final_package', 'dir': str(child)})
            elif child.is_dir():
                known_files.update(str(p) for p in child.iterdir())
    if staging_root.is_dir():
        known_tmp = {Path(v['tmp_path']).name for v in versions if v['tmp_path']}
        for child in sorted(staging_root.iterdir()):
            if child.name not in known_tmp:
                result['never_externally_visible'].append(
                    {'package_id': child.name, 'reason': 'unregistered_staging_dir', 'staging_dir': str(child)})
    for child in sorted(out.iterdir()) if out.is_dir() else []:
        if child.is_file() and str(child) not in known_files:
            result['manual_review'].append(
                {'package_id': child.name, 'reason': 'unregistered_loose_file', 'dir': str(child)})
    return result


def _verify_dir(directory, package_id, db_path):
    with connect(db_path) as db:
        rows = db.execute('SELECT * FROM package_file WHERE package_id=? ORDER BY name',
                          (package_id,)).fetchall()
        version = db.execute('SELECT * FROM package_version WHERE id=?', (package_id,)).fetchone()
    if not rows or not version:
        return {'status': 'unknown_package'}
    directory = Path(directory)
    on_disk = {}
    for name in PACKAGE_FILES + ('manifest.json',):
        p = directory / name
        if not p.is_file():
            return {'status': 'file_missing', 'detail': name}
        on_disk[name] = p.read_bytes()
    for fr in rows:
        if digest_of(on_disk[fr['name']]) != fr['digest']:
            return {'status': 'file_tampered', 'detail': fr['name']}
    if digest_of(on_disk['manifest.json']) != version['manifest_digest']:
        return {'status': 'manifest_tampered'}
    return {'status': 'ok'}


# --------------------------------------------------------------------------- #
# Legacy entry points (original arguments and return shape)
# --------------------------------------------------------------------------- #
def publish(db_path, file, output_dir):
    result = publish_package(db_path, file, output_dir)
    return {'id': result['id'], 'digest': result['digest'], 'status': 'published'}


def acknowledge(db_path, file):
    out = acknowledge_receipt(db_path, file)
    return {'id': out['id'], 'status': out['status']}


# --------------------------------------------------------------------------- #
# Deliberately naive reference implementations — for failure demonstrations.
# Each produces a wrong client-facing acceptance conclusion.
# --------------------------------------------------------------------------- #
def naive_txn_publish(db_path, request, output_dir, crash_before_commit=True):
    """Naive plan 1: file write + INSERT in one SQLite transaction.

    Wrong conclusion demonstrated: after a crash between the write and the
    commit, the transaction rolls back but ``PK1.json`` stays on disk with no
    database registration — the ledger cannot tell "published" from "never
    visible", and the only "fix" would be deleting an unknown file that may
    already have been downloaded.
    """
    booking_id = request['booking_id']
    state = report(db_path)['state']
    booking = next(row for row in state['booking'] if row['id'] == booking_id)
    data = {'package_id': request['id'], 'booking': booking,
            'proof': [r for r in state['proof'] if r['booking_id'] == booking_id],
            'obligation': [r for r in state['obligation'] if r['booking_id'] == booking_id]}
    raw = canonical(data)
    digest = digest_of(raw)
    target = Path(output_dir) / f"{request['id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    if crash_before_commit:
        raise RuntimeError('injected_crash:naive_txn_before_commit')
    with connect(db_path) as db, db:
        db.execute('INSERT INTO client_package VALUES(?,?,?,?,?,?)',
                   (request['id'], booking_id, request['recipient'], digest, str(target), 'published'))
    return {'id': request['id'], 'digest': digest, 'status': 'published'}


def naive_rewrite_package(package_path, new_snapshot):
    """Naive plan 2: rewrite PK1.json in place when new proof arrives.

    Wrong conclusion demonstrated: the digest the client already signed now
    names different bytes, so their honest receipt verifies nothing; the
    original immutable download is destroyed and audit of what was actually
    delivered becomes impossible.
    """
    target = Path(package_path)
    old = target.read_bytes()
    target.write_bytes(canonical(new_snapshot))
    return {'path': str(target), 'old_digest': digest_of(old),
            'new_digest': digest_of(target.read_bytes())}


def naive_digest_ack(db_path, receipt_file):
    """Naive plan 3: compare receipt against database digest only.

    Wrong conclusion demonstrated: it never opens the downloaded file, so a
    package whose bytes were replaced (or that is missing from disk) is still
    marked ``accepted`` whenever the old database digest is replayed.
    """
    receipt = json.loads(Path(receipt_file).read_text(encoding='utf8'))
    with connect(db_path) as db, db:
        package = db.execute('SELECT * FROM client_package WHERE id=?',
                             (receipt['package_id'],)).fetchone()
        if not package:
            raise ValueError('unknown_package')
        status = ('accepted' if receipt['recipient'] == package['recipient']
                  and receipt['digest'] == package['digest'] else 'held')
        db.execute('INSERT OR REPLACE INTO client_ack VALUES(?,?,?,?,?)',
                   (receipt['id'], receipt['package_id'], receipt['recipient'], receipt['digest'], status))
        if status == 'accepted':
            db.execute("UPDATE client_package SET status='accepted' WHERE id=?", (receipt['package_id'],))
    return {'id': receipt['id'], 'status': status}
