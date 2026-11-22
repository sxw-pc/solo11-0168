"""Publication protocol: crash recovery, immutability, routes and receipts."""
import json
from pathlib import Path

import pytest

from adflight import creative, schedule, playback, publisher
from adflight.report import publication_report, publication_audit
from adflight.store import connect, canonical, digest_of

FIX = Path(__file__).resolve().parent.parent / 'fixtures'
IMPORT = [(creative.register, 'creative-ok.json'), (creative.license, 'license-ok.json'),
          (schedule.screen, 'screen-ok.json'), (schedule.book, 'booking-ok.json'),
          (playback.proof, 'proof-ok.json')]


def bootstrap(db):
    for func, name in IMPORT:
        func(db, FIX / name)


def write(tmp, name, payload):
    path = tmp / name
    path.write_text(json.dumps(payload), encoding='utf8')
    return path


def receipt(tmp, rid, package_id, digest, recipient='advertiser-A', key='manifest_digest'):
    return write(tmp, f'{rid}.json', {'id': rid, 'package_id': package_id,
                                      'recipient': recipient, key: digest})


def settle_120(db):
    result = playback.settle(db, 'B1')
    assert result['missing_seconds'] == 120
    return result


def package_dir(out, package_id):
    return out / 'packages' / package_id


# --------------------------------------------------------------------------- #
# Baseline behaviour preserved through the new protocol
# --------------------------------------------------------------------------- #
def test_legacy_publish_and_ack_through_protocol(tmp_path):
    db = tmp_path / 'campaign.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    result = publisher.publish(db, FIX / 'publish-ok.json', out)
    assert result['status'] == 'published'
    pdir = package_dir(out, 'PK1')
    assert {p.name for p in pdir.iterdir()} == {
        'body.json', 'proof-index.json', 'settlement.json', 'manifest.json'}
    manifest = json.loads((pdir / 'manifest.json').read_text())
    assert manifest['recipient'] == 'advertiser-A'
    assert all(item['sha256'] for item in manifest['files'])
    # The recipient-visible listing covers exactly the snapshot files.
    assert {x['name'] for x in manifest['recipient_visible']} == {
        'body.json', 'proof-index.json', 'settlement.json', 'manifest.json'}
    ack = receipt(tmp_path, 'ACK1', 'PK1', result['digest'])
    assert publisher.acknowledge(db, ack)['status'] == 'accepted'
    with connect(db) as conn:
        assert conn.execute('SELECT status FROM client_package WHERE id=?', ('PK1',)).fetchone()[0] == 'accepted'
        assert conn.execute('SELECT status FROM package_version WHERE id=?', ('PK1',)).fetchone()[0] == 'accepted'
    view = publication_report(db)
    assert view['client_accepted'] == ['PK1']
    assert view['current_pointers'] == {'B1': 'PK1'}


def test_two_database_chains_pk1_accepted_then_pk2_holds_stale_receipt(tmp_path):
    # Database 1: the documented 120-second chain, PK1 signed.
    db1 = tmp_path / 'a.sqlite'
    out1 = tmp_path / 'out1'
    bootstrap(db1)
    settle_120(db1)
    pk1 = publisher.publish(db1, FIX / 'publish-ok.json', out1)
    assert publisher.acknowledge(
        db1, receipt(tmp_path, 'ACK1', 'PK1', pk1['digest']))['status'] == 'accepted'

    # Database 2: same chain, then independent P2 fills the missing 120 seconds.
    db2 = tmp_path / 'b.sqlite'
    out2 = tmp_path / 'out2'
    bootstrap(db2)
    settle_120(db2)
    assert playback.proof(db2, FIX / 'proof-makegood-p2.json')['status'] == 'valid'
    settled = playback.settle(db2, 'B1')
    assert settled['missing_seconds'] == 0
    with connect(db2) as conn:
        statuses = [r['status'] for r in conn.execute('SELECT status FROM obligation ORDER BY id')]
    # The original due obligation is superseded; no new due row is needed at 0.
    assert statuses == ['superseded']

    pk1_first = publisher.publish(db2, write(
        tmp_path, 'pk1-rolling.json',
        {'id': 'PK1', 'booking_id': 'B1', 'recipient': 'advertiser-A', 'route': 'rolling'}), out2)
    pk2 = publisher.publish(db2, FIX / 'publish-pk2.json', out2)
    assert pk2['digest'] != pk1_first['digest']
    # Replaying PK1's old digest against PK2 must be held, not accepted.
    stale = publisher.acknowledge_receipt(
        db2, receipt(tmp_path, 'ACK2-stale', 'PK2', pk1_first['digest']))
    assert stale == {'id': 'ACK2-stale', 'status': 'held', 'reason': 'digest_mismatch'}
    # Only PK2's real digest accepts it.
    ok = publisher.acknowledge_receipt(
        db2, receipt(tmp_path, 'ACK2', 'PK2', pk2['digest']))
    assert ok['status'] == 'accepted'
    with connect(db2) as conn:
        assert conn.execute('SELECT status FROM package_version WHERE id=?', ('PK1',)).fetchone()[0] == 'withdrawn'
        assert conn.execute('SELECT status FROM package_version WHERE id=?', ('PK2',)).fetchone()[0] == 'accepted'
    # PK1 bytes are still on disk for the audit trail.
    assert (package_dir(out2, 'PK1') / 'body.json').is_file()


# --------------------------------------------------------------------------- #
# Crash recovery at every crash point -> one of the four classifications
# --------------------------------------------------------------------------- #
def rolling_pk1(db, out, tmp_path, rid='ACK1'):
    pk1 = publisher.publish(db, write(
        tmp_path, 'pk1-route.json',
        {'id': 'PK1', 'booking_id': 'B1', 'recipient': 'advertiser-A', 'route': 'rolling'}), out)
    return pk1


def test_crash_after_stage_write_never_visible_and_resumable(tmp_path):
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    rolling_pk1(db, out, tmp_path)
    with pytest.raises(RuntimeError, match='after_stage_write'):
        publisher.publish_package(db, FIX / 'publish-crash-stage-write.json', out)
    # No final directory, no pointer movement, PK1 stays current.
    assert not package_dir(out, 'PK2').exists()
    with connect(db) as conn:
        assert conn.execute('SELECT status FROM package_version WHERE id=?', ('PK2',)).fetchone()[0] == 'staging'
        assert conn.execute('SELECT current_package_id FROM publication_pointer').fetchone()[0] == 'PK1'
    verdict = publisher.recover(db, out)
    # Staging bytes are intact so the same package is resumed and published.
    assert 'PK2' in verdict['resumed']
    assert 'PK2' in verdict['published_pending']
    assert package_dir(out, 'PK2').is_dir()
    with connect(db) as conn:
        assert conn.execute('SELECT status FROM package_version WHERE id=?', ('PK2',)).fetchone()[0] == 'published'
        assert conn.execute('SELECT current_package_id FROM publication_pointer').fetchone()[0] == 'PK2'
        assert conn.execute('SELECT status FROM package_version WHERE id=?', ('PK1',)).fetchone()[0] == 'withdrawn'
    assert publication_audit(db)['replay_ok']


def test_crash_after_staging_commit_resumes_with_same_digest(tmp_path):
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    rolling_pk1(db, out, tmp_path)
    with pytest.raises(RuntimeError, match='after_staging_commit'):
        publisher.publish_package(db, FIX / 'publish-crash-staging-commit.json', out)
    with connect(db) as conn:
        row = conn.execute('SELECT * FROM package_version WHERE id=?', ('PK2',)).fetchone()
        assert row['status'] == 'staging_committed'
        expected_digest = row['manifest_digest']
    verdict = publisher.recover(db, out)
    assert 'PK2' in verdict['resumed']
    assert (package_dir(out, 'PK2') / 'manifest.json').is_file()
    with connect(db) as conn:
        assert conn.execute('SELECT manifest_digest FROM package_version WHERE id=?',
                            ('PK2',)).fetchone()[0] == expected_digest
        assert conn.execute('SELECT current_package_id FROM publication_pointer').fetchone()[0] == 'PK2'


def test_crash_after_rename_publishes_existing_bytes(tmp_path):
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    rolling_pk1(db, out, tmp_path)
    with pytest.raises(RuntimeError, match='after_rename'):
        publisher.publish_package(db, FIX / 'publish-crash-rename.json', out)
    # Bytes are already externally reachable but the pointer still names PK1.
    assert package_dir(out, 'PK2').is_dir()
    with connect(db) as conn:
        assert conn.execute('SELECT current_package_id FROM publication_pointer').fetchone()[0] == 'PK1'
    verdict = publisher.recover(db, out)
    assert 'PK2' in verdict['resumed']
    with connect(db) as conn:
        assert conn.execute('SELECT current_package_id FROM publication_pointer').fetchone()[0] == 'PK2'
    # Re-running recovery is idempotent.
    again = publisher.recover(db, out)
    assert again['resumed'] == [] and again['manual_review'] == []


def test_corrupt_staging_is_aborted_never_externally_visible(tmp_path):
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    rolling_pk1(db, out, tmp_path)
    with pytest.raises(RuntimeError):
        publisher.publish_package(db, FIX / 'publish-crash-stage-write.json', out)
    # Corrupt the staged bytes before recovery.
    staged = out / '.staging' / 'PK2' / 'body.json'
    staged.write_bytes(b'{"tampered": true}')
    verdict = publisher.recover(db, out)
    assert verdict['never_externally_visible'][0]['package_id'] == 'PK2'
    assert not package_dir(out, 'PK2').exists()
    with connect(db) as conn:
        assert conn.execute('SELECT status FROM package_version WHERE id=?', ('PK2',)).fetchone()[0] == 'aborted'
        assert conn.execute('SELECT current_package_id FROM publication_pointer').fetchone()[0] == 'PK1'
    # The corrupt staging dir is preserved, not deleted.
    assert staged.is_file()


def test_tampered_published_package_is_held_and_quarantined(tmp_path):
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    pk1 = rolling_pk1(db, out, tmp_path)
    body = package_dir(out, 'PK1') / 'body.json'
    body.write_bytes(canonical({'tampered': True}))
    held = publisher.acknowledge_receipt(db, receipt(tmp_path, 'ACK1', 'PK1', pk1['digest']))
    assert held['status'] == 'held'
    assert held['reason'].startswith('disk_')
    with connect(db) as conn:
        assert conn.execute('SELECT status FROM package_version WHERE id=?', ('PK1',)).fetchone()[0] == 'quarantined'
    verdict = publisher.recover(db, out)
    assert verdict['manual_review'][0]['package_id'] == 'PK1'
    # The damaged file is left exactly as found for the human investigation.
    assert body.read_bytes() == canonical({'tampered': True})


# --------------------------------------------------------------------------- #
# The three naive plans demonstrably reach wrong acceptance conclusions
# --------------------------------------------------------------------------- #
def test_naive_txn_plan_leaves_unregistered_orphan_file(tmp_path):
    db = tmp_path / 'n.sqlite'
    out = tmp_path / 'n-out'
    bootstrap(db)
    settle_120(db)
    request = {'id': 'PK1', 'booking_id': 'B1', 'recipient': 'advertiser-A'}
    with pytest.raises(RuntimeError, match='naive_txn'):
        publisher.naive_txn_publish(db, request, out)
    orphan = out / 'PK1.json'
    assert orphan.is_file()  # disk kept the bytes...
    with connect(db) as conn:
        assert conn.execute('SELECT 1 FROM client_package WHERE id=?', ('PK1',)).fetchone() is None
    # ...but the database has no record at all: unreconcilable by schema alone.
    verdict = publisher.recover(db, out)
    # The real protocol never publishes through that path; the loose orphan
    # file is reported for manual review and never auto-deleted.
    assert any(item['reason'] == 'unregistered_loose_file' and item['package_id'] == 'PK1.json'
               for item in verdict['manual_review'])
    assert orphan.is_file()

    # The real protocol on a clean directory recovers cleanly instead.
    good = publisher.publish(db, FIX / 'publish-ok.json', out / 'protocol')
    assert good['status'] == 'published'
    assert publisher.recover(db, out / 'protocol')['manual_review'] == []


def test_naive_rewrite_plan_breaks_signed_digest(tmp_path):
    # --- Naive in-place rewrite on one ledger ---
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    pk1 = rolling_pk1(db, out, tmp_path)
    body_path = package_dir(out, 'PK1') / 'body.json'
    first_bytes = body_path.read_bytes()
    playback.proof(db, FIX / 'proof-makegood-p2.json')
    playback.settle(db, 'B1')
    new_snapshot = {'package_id': 'PK1', 'booking_id': 'B1', 'note': 'rewritten in place'}
    info = publisher.naive_rewrite_package(body_path, new_snapshot)
    assert info['old_digest'] != info['new_digest']
    # The honest old receipt no longer matches the bytes the client holds...
    held = publisher.acknowledge_receipt(db, receipt(tmp_path, 'ACK1', 'PK1', pk1['digest']))
    assert held['status'] == 'held'
    # ...and the original immutable download has been destroyed in place.
    assert body_path.read_bytes() != first_bytes

    # --- The real protocol on a separate ledger: never rewrite, version it ---
    db2 = tmp_path / 'c2.sqlite'
    out2 = tmp_path / 'proper'
    bootstrap(db2)
    settle_120(db2)
    rolling_pk1(db2, out2, tmp_path)
    playback.proof(db2, FIX / 'proof-makegood-p2.json')
    playback.settle(db2, 'B1')
    pk2 = publisher.publish(db2, FIX / 'publish-pk2.json', out2)
    assert json.loads((package_dir(out2, 'PK1') / 'body.json').read_text())[
        'settlement']['missing_seconds'] == 120
    assert json.loads((package_dir(out2, 'PK2') / 'body.json').read_text())[
        'settlement']['missing_seconds'] == 0
    assert publisher.acknowledge_receipt(
        db2, receipt(tmp_path, 'ACKP2', 'PK2', pk2['digest']))['status'] == 'accepted'
    # PK1 directory and its signed digest remain byte-identical and verifiable.
    assert digest_of((package_dir(out2, 'PK1') / 'body.json').read_bytes()) == \
        {f['name']: f['sha256'] for f in json.loads(
            (package_dir(out2, 'PK1') / 'manifest.json').read_text())['files']}['body.json']
    assert publisher.verify_package(db2, 'PK1')['status'] == 'ok'


def test_naive_digest_ack_accepts_replaced_file(tmp_path):
    db = tmp_path / 'n.sqlite'
    out = tmp_path / 'n-out'
    bootstrap(db)
    settle_120(db)
    pk1 = publisher.publish(db, FIX / 'publish-ok.json', out)
    body = out / 'packages' / 'PK1' / 'body.json'
    body.write_bytes(canonical({'package_id': 'PK1', 'booking_id': 'B1', 'malicious': True}))
    # Naive plan: database digest still matches the replayed receipt -> accepted
    # even though the client's actual file is a different document.
    naive_receipt = write(tmp_path, 'naive-ack.json',
                          {'id': 'ACKN', 'package_id': 'PK1',
                           'recipient': 'advertiser-A', 'digest': pk1['digest']})
    assert publisher.naive_digest_ack(db, naive_receipt)['status'] == 'accepted'
    with connect(db) as conn:
        assert conn.execute('SELECT status FROM client_package WHERE id=?', ('PK1',)).fetchone()[0] == 'accepted'
    # The real protocol refuses the same receipt on the same damaged bytes.
    proper = publisher.acknowledge_receipt(
        db, receipt(tmp_path, 'ACKR', 'PK1', pk1['digest']))
    assert proper['status'] == 'held'


# --------------------------------------------------------------------------- #
# Routes: closeout seals the period; rolling replaces the current pointer
# --------------------------------------------------------------------------- #
def test_closeout_route_keeps_pk1_accepted_and_seals_period(tmp_path):
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    pk1 = publisher.publish(db, FIX / 'publish-ok.json', out)  # default closeout
    assert publisher.acknowledge_receipt(
        db, receipt(tmp_path, 'ACK1', 'PK1', pk1['digest']))['status'] == 'accepted'
    # Supplementary proof arrives afterwards; the settled 120-second package
    # remains the period's official delivery.
    playback.proof(db, FIX / 'proof-makegood-p2.json')
    playback.settle(db, 'B1')
    with pytest.raises(ValueError, match='closeout_period_sealed'):
        publisher.publish(db, FIX / 'publish-pk2.json', out)
    # A second closeout request must never overwrite the downloaded PK1 bytes.
    snapshot = json.loads((package_dir(out, 'PK1') / 'body.json').read_text())
    assert snapshot['settlement']['missing_seconds'] == 120
    with connect(db) as conn:
        assert conn.execute('SELECT route FROM publication_route').fetchone()[0] == 'closeout'


def test_rolling_route_pk2_pending_ack_rejects_old_receipt(tmp_path):
    db = tmp_path / 'r.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    pk1 = rolling_pk1(db, out, tmp_path)
    # A downloads PK1, but has not acknowledged it.
    publisher.claim(db, FIX / 'claim-pk1.json')
    playback.proof(db, FIX / 'proof-makegood-p2.json')
    playback.settle(db, 'B1')
    publisher.publish(db, FIX / 'publish-pk2.json', out)
    with connect(db) as conn:
        assert conn.execute('SELECT status FROM package_version WHERE id=?', ('PK1',)).fetchone()[0] == 'withdrawn'
        assert conn.execute('SELECT status FROM package_version WHERE id=?', ('PK2',)).fetchone()[0] == 'published'
        assert conn.execute('SELECT current_package_id FROM publication_pointer').fetchone()[0] == 'PK2'
    # PK1's receipt cannot certify the new period.
    stale = publisher.acknowledge_receipt(db, receipt(tmp_path, 'ACK-old', 'PK2', pk1['digest']))
    assert stale['status'] == 'held'
    view = publication_report(db)
    assert 'PK2' in view['platform_published'] and view['client_accepted'] == []
    # Explicit withdrawal before acknowledgement leaves no current version.
    pk2b = publisher.publish(db, write(
        tmp_path, 'pk3.json',
        {'id': 'PK3', 'booking_id': 'B1', 'recipient': 'advertiser-A', 'route': 'rolling'}), out)
    assert publisher.withdraw(db, write(
        tmp_path, 'withdraw-pk3.json', {'package_id': 'PK3'}))['status'] == 'withdrawn'
    with connect(db) as conn:
        assert conn.execute('SELECT 1 FROM publication_pointer').fetchone() is None
    assert publisher.acknowledge_receipt(
        db, receipt(tmp_path, 'ACK3', 'PK3', pk2b['digest']))['reason'] == 'package_withdrawn'


def test_route_is_fixed_before_first_publication(tmp_path):
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    rolling_pk1(db, out, tmp_path)
    # A closeout-flavoured request for the same booking cannot switch routes.
    with pytest.raises(ValueError):
        publisher.publish(db, FIX / 'publish-ok.json', out)


# --------------------------------------------------------------------------- #
# Snapshot isolation, tenant isolation, audit
# --------------------------------------------------------------------------- #
def test_snapshot_pins_versions_against_concurrent_report_change(tmp_path, monkeypatch):
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    import adflight.publisher as mod
    original = mod.build_snapshot

    def noisy(db_conn, *args, **kwargs):
        snap = original(db_conn, *args, **kwargs)
        # Simulate a report change arriving while the package is being written:
        # a new proof row lands after the snapshot was pinned but before files
        # hit disk. The package must not mix old and new content.
        db_conn.execute(
            "INSERT INTO proof VALUES('P9','B1','S1','2026-06-10T10:09:00Z','2026-06-10T10:10:00Z','auditor-9','valid',NULL)")
        return snap

    monkeypatch.setattr(mod, 'build_snapshot', noisy)
    publisher.publish(db, FIX / 'publish-ok.json', out)
    monkeypatch.undo()
    body = json.loads((package_dir(out, 'PK1') / 'body.json').read_text())
    assert {p['id'] for p in body['proof']} == {'P1'}  # pinned, no half-new mix
    assert body['settlement']['missing_seconds'] == 120
    with connect(db) as conn:
        assert conn.execute('SELECT 1 FROM proof WHERE id=?', ('P9',)).fetchone() is not None
    # The published bytes verify despite the concurrent insert.
    assert publisher.verify_package(db, 'PK1')['status'] == 'ok'


def test_advertiser_a_package_never_carries_advertiser_b_material(tmp_path):
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    # Build an independent booking B2 for advertiser B.
    schedule.screen(db, write(tmp_path, 's2.json', {'id': 'S2', 'city': 'C1', 'grade': 3}))
    schedule.book(db, write(tmp_path, 'b2.json', {
        'id': 'B2', 'creative_id': 'AD1', 'revision': 1, 'screen_id': 'S2',
        'start_at': '2026-06-11T10:00:00Z', 'end_at': '2026-06-11T10:10:00Z', 'priority': 1}))
    publisher.publish(db, FIX / 'publish-ok.json', out)
    body = json.loads((package_dir(out, 'PK1') / 'body.json').read_text())
    assert all(p['booking_id'] == 'B1' for p in body['proof'])
    assert body['booking']['id'] == 'B1'
    # Publishing B's package is fully isolated.
    pkb = publisher.publish(db, FIX / 'publish-pkb.json', out)
    bbody = json.loads((package_dir(out, 'PK-B') / 'body.json').read_text())
    assert bbody['recipient'] == 'advertiser-B'
    assert bbody['booking']['id'] == 'B2'
    assert bbody['proof'] == []
    # Cross-recipient receipt is held.
    cross = publisher.acknowledge_receipt(db, receipt(
        tmp_path, 'ACKX', 'PK-B', pkb['digest'], recipient='advertiser-A'))
    assert cross['status'] == 'held' and cross['reason'] == 'recipient_mismatch'


def test_publication_audit_distinguishes_platform_and_client_views(tmp_path):
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    pk1 = rolling_pk1(db, out, tmp_path)
    before = publication_audit(db)
    assert before['replay_ok']
    assert [v['package_id'] for v in before['verified']] == ['PK1']
    view = publication_report(db)
    assert view['platform_published'] == ['PK1']
    assert view['client_accepted'] == []  # platform published, client not yet
    publisher.acknowledge_receipt(db, receipt(tmp_path, 'ACK1', 'PK1', pk1['digest']))
    view = publication_report(db)
    assert view['client_accepted'] == ['PK1']
    # Timed lifecycle events exist for every stage.
    kinds = {e['kind'] for e in view['events']}
    assert {'staged', 'staging_committed', 'published', 'claimed', 'acknowledged'} <= kinds
    assert publication_audit(db)['replay_ok']


def test_recovery_covers_all_four_classifications(tmp_path):
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    rolling_pk1(db, out, tmp_path)
    # 1) never externally visible: crashed staging with corrupt bytes -> aborted
    with pytest.raises(RuntimeError):
        publisher.publish_package(db, FIX / 'publish-crash-stage-write.json', out)
    (out / '.staging' / 'PK2' / 'body.json').write_bytes(b'broken')
    verdict = publisher.recover(db, out)
    never = {item['package_id']: item['reason'] for item in verdict['never_externally_visible']}
    assert never.get('PK2') == 'file_tampered'
    # 2) published pending: PK1 is still the current, unacknowledged package
    assert 'PK1' in verdict['published_pending']
    # 3) client accepted old version
    pk3 = publisher.publish(db, write(
        tmp_path, 'pk3.json',
        {'id': 'PK3', 'booking_id': 'B1', 'recipient': 'advertiser-A', 'route': 'rolling'}), out)
    publisher.acknowledge_receipt(db, receipt(tmp_path, 'ACK3', 'PK3', pk3['digest']))
    # 4) manual review: damage the accepted package on disk
    (package_dir(out, 'PK3') / 'settlement.json').write_bytes(b'{}')
    verdict = publisher.recover(db, out)
    assert any(x['package_id'] == 'PK3' for x in verdict['client_accepted_old']) is False
    assert any(x['package_id'] == 'PK3' and 'tampered' in x['reason']
               for x in verdict['manual_review'])


def test_republish_while_recover_pending_is_refused(tmp_path):
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    rolling_pk1(db, out, tmp_path)
    with pytest.raises(RuntimeError):
        publisher.publish_package(db, FIX / 'publish-crash-stage-write.json', out)
    with pytest.raises(ValueError, match='recover_pending'):
        publisher.publish_package(db, FIX / 'publish-crash-stage-write.json', out)
    publisher.recover(db, out)
    # After recovery the same package id is already published; the immutable
    # directory blocks re-creation before any database check.
    with pytest.raises(ValueError, match='package_dir_exists'):
        publisher.publish_package(db, FIX / 'publish-crash-stage-write.json', out)


def test_recovery_classifies_accepted_old_version(tmp_path):
    db = tmp_path / 'c.sqlite'
    out = tmp_path / 'out'
    bootstrap(db)
    settle_120(db)
    pk1 = publisher.publish(db, FIX / 'publish-ok.json', out)
    publisher.acknowledge_receipt(db, receipt(tmp_path, 'ACK1', 'PK1', pk1['digest']))
    verdict = publisher.recover(db, out)
    assert verdict['client_accepted_old'][0]['package_id'] == 'PK1'
    assert verdict['manual_review'] == []
    # PK1 bytes remain byte-identical to the signed snapshot.
    manifest = json.loads((package_dir(out, 'PK1') / 'manifest.json').read_text())
    for item in manifest['files']:
        raw = (package_dir(out, 'PK1') / item['name']).read_bytes()
        assert digest_of(raw) == item['sha256']
